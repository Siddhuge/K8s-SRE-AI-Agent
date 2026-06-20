#!/usr/bin/env python3
"""RCA evaluation harness.

Scores the DETERMINISTIC detector engine (`evaluate(ctx)`) — the explainable layer the
LLM reasons over — against a labeled dataset. This is the reproducible, CI-gateable
signal: "do the diagnoses match ground truth, and is the confidence trustworthy?" It
does NOT call the model (no API key, no cost, no flakiness); an LLM-loop eval is a
separate, cost-incurring harness.

Run:  PYTHONPATH=src python3 evals/run_eval.py
Exit code is non-zero if any gate fails (see THRESHOLDS), so CI can block on it.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from k8s_sre_agent.rca.detectors import evaluate

CASES_DIR = Path(__file__).parent / "cases"

# CI gates. The dangerous quadrants (confident-but-wrong, false positives on healthy
# pods) are zero-tolerance; top-1 accuracy has a floor.
THRESHOLDS = {
    "min_top1_accuracy": 0.90,
    "max_confident_wrong": 0,      # wrong top-1 while >= confident_threshold
    "max_false_positives": 0,      # a finding on a NEGATIVE (healthy) case
    "confident_threshold": 80,
}


@dataclass
class CaseResult:
    name: str
    passed: bool
    predicted: str | None
    confidence: int | None
    expected: str | None
    reasons: list[str] = field(default_factory=list)


def _contains_all(haystack: str, needles: list[str]) -> list[str]:
    low = haystack.lower()
    return [n for n in needles if n.lower() not in low]


def _evidence_text(hyp) -> str:
    return " || ".join(e.summary for e in getattr(hyp, "evidence", []) or [])


def score_case(case: dict) -> CaseResult:
    ctx = case["ctx"]
    exp = case["expected"]
    hyps = evaluate(ctx)
    top = hyps[0] if hyps else None
    predicted = top.issue if top else None
    confidence = top.confidence if top else None
    reasons: list[str] = []

    # forbid_issue: a class that must not appear anywhere in the hypotheses.
    forbid = exp.get("forbid_issue")
    if forbid and any(h.issue == forbid for h in hyps):
        reasons.append(f"forbidden issue {forbid!r} present")

    want = exp.get("issue", "__unset__")
    if want is None:
        # NEGATIVE case: expect no finding (or at most a low-confidence one).
        if top is not None and (confidence or 0) >= 50:
            reasons.append(f"false positive: {predicted!r} @ {confidence}% (expected none)")
    elif want != "__unset__":
        issues_ranked = [h.issue for h in hyps]
        if exp.get("match") == "top3":
            if want not in issues_ranked[:3]:
                reasons.append(f"{want!r} not in top-3 {issues_ranked[:3]}")
        elif predicted != want:
            reasons.append(f"top-1 {predicted!r} != expected {want!r}")

        if top is not None:
            if "min_confidence" in exp and (confidence or 0) < exp["min_confidence"]:
                reasons.append(f"confidence {confidence} < min {exp['min_confidence']}")
            if "max_confidence" in exp and (confidence or 0) > exp["max_confidence"]:
                reasons.append(f"confidence {confidence} > max {exp['max_confidence']}")
            if "rollback_required" in exp and top.rollback_required != exp["rollback_required"]:
                reasons.append(f"rollback_required {top.rollback_required} != {exp['rollback_required']}")
            if (miss := _contains_all(top.root_cause, exp.get("root_cause_contains", []))):
                reasons.append(f"root_cause missing {miss}")
            ev = _evidence_text(top)
            if (miss := _contains_all(ev, exp.get("evidence_contains", []))):
                reasons.append(f"evidence missing {miss}")
            for bad in exp.get("evidence_excludes", []):
                if bad.lower() in ev.lower():
                    reasons.append(f"evidence wrongly cites {bad!r}")

    return CaseResult(case["name"], not reasons, predicted, confidence, exp.get("issue", None), reasons)


def load_cases() -> list[dict]:
    cases: list[dict] = []
    for f in sorted(CASES_DIR.glob("*.json")):
        cases.extend(json.loads(f.read_text()))
    return cases


def evaluate_dataset() -> dict:
    cases = load_cases()
    results = [score_case(c) for c in cases]
    by_name = {c["name"]: c for c in cases}

    # top-1 accuracy over cases that assert a concrete top-1 issue
    top1_cases = [r for r in results
                  if by_name[r.name]["expected"].get("issue") not in (None,)
                  and by_name[r.name]["expected"].get("match") != "top3"
                  and "issue" in by_name[r.name]["expected"]]
    top1_correct = sum(1 for r in top1_cases if r.predicted == r.expected)
    top1_acc = top1_correct / len(top1_cases) if top1_cases else 1.0

    negatives = [r for r in results if by_name[r.name]["expected"].get("issue") is None
                 and "forbid_issue" not in by_name[r.name]["expected"]]
    false_positives = [r for r in negatives if r.predicted is not None and (r.confidence or 0) >= 50]

    confident_wrong = [r for r in top1_cases
                       if r.predicted != r.expected and (r.confidence or 0) >= THRESHOLDS["confident_threshold"]]

    # Confidence calibration: bucket every positive prediction, measure correctness.
    buckets = {"50-69": [0, 0], "70-84": [0, 0], "85-94": [0, 0], "95-100": [0, 0]}
    for r in top1_cases:
        c = r.confidence or 0
        key = "95-100" if c >= 95 else "85-94" if c >= 85 else "70-84" if c >= 70 else "50-69" if c >= 50 else None
        if key:
            buckets[key][1] += 1
            if r.predicted == r.expected:
                buckets[key][0] += 1

    return {
        "results": results,
        "total": len(results),
        "passed": sum(1 for r in results if r.passed),
        "top1_accuracy": top1_acc,
        "top1_n": len(top1_cases),
        "false_positives": false_positives,
        "confident_wrong": confident_wrong,
        "calibration": buckets,
    }


def main() -> int:
    rep = evaluate_dataset()
    print("\n=== RCA Evaluation Harness ===\n")
    for r in rep["results"]:
        mark = "PASS" if r.passed else "FAIL"
        conf = f"{r.confidence}%" if r.confidence is not None else "  - "
        print(f"  [{mark}] {r.name:<42} pred={str(r.predicted):<34} {conf}")
        for why in r.reasons:
            print(f"         ↳ {why}")

    print("\n--- Confidence calibration (correct / total per bucket) ---")
    for b, (ok, n) in rep["calibration"].items():
        rate = f"{ok}/{n} ({100*ok//n}%)" if n else "0/0"
        print(f"  {b:>7} : {rate}")

    print("\n--- Summary ---")
    print(f"  cases passed       : {rep['passed']}/{rep['total']}")
    print(f"  top-1 accuracy     : {rep['top1_accuracy']:.0%}  (over {rep['top1_n']} labeled cases)")
    print(f"  confident-but-wrong: {len(rep['confident_wrong'])}  (>= {THRESHOLDS['confident_threshold']}% and wrong)")
    print(f"  false positives    : {len(rep['false_positives'])}  (finding on a healthy pod)")

    gates = [
        ("top-1 accuracy", rep["top1_accuracy"] >= THRESHOLDS["min_top1_accuracy"],
         f"{rep['top1_accuracy']:.0%} >= {THRESHOLDS['min_top1_accuracy']:.0%}"),
        ("confident-but-wrong", len(rep["confident_wrong"]) <= THRESHOLDS["max_confident_wrong"],
         f"{len(rep['confident_wrong'])} <= {THRESHOLDS['max_confident_wrong']}"),
        ("false positives", len(rep["false_positives"]) <= THRESHOLDS["max_false_positives"],
         f"{len(rep['false_positives'])} <= {THRESHOLDS['max_false_positives']}"),
        ("all cases pass", rep["passed"] == rep["total"], f"{rep['passed']}/{rep['total']}"),
    ]
    print("\n--- Gates ---")
    ok = True
    for name, passed, detail in gates:
        print(f"  [{'OK' if passed else 'XX'}] {name}: {detail}")
        ok = ok and passed
    print()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
