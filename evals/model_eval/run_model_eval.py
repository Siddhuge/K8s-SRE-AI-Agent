#!/usr/bin/env python3
"""Model-in-the-loop RCA eval (complements evals/run_eval.py).

run_eval.py scores the deterministic DETECTOR layer. THIS scores the LLM's arbitrated
verdict: given the same incident evidence, does the model pick the right issue class — and
does it CORRECT the detectors when they're wrong (as it did live, overriding a 94% secret
verdict to find a missing DB service)?

  # see what would be sent, no API call, no cost:
  PYTHONPATH=src python3 evals/model_eval/run_model_eval.py --dry-run
  # actually run it (costs API tokens):
  ANTHROPIC_API_KEY=... python3 evals/model_eval/run_model_eval.py --model claude-haiku-4-5-20251001

Gated: with no ANTHROPIC_API_KEY (or no `anthropic` SDK) it SKIPS cleanly (exit 0) — it is
opt-in and cost-incurring, never a silent CI cost.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

CASES_DIR = Path(__file__).resolve().parent.parent / "cases"


def load_cases() -> list[dict]:
    cases: list[dict] = []
    for f in sorted(CASES_DIR.glob("*.json")):
        cases.extend(json.loads(f.read_text()))
    return cases


def issue_classes(cases: list[dict]) -> list[str]:
    """The fixed label set the model must choose from (exact scoring), plus 'none'."""
    classes = {c["expected"]["issue"] for c in cases if c["expected"].get("issue")}
    return sorted(classes) + ["none"]


def _evidence(ctx: dict) -> str:
    """Render the incident context the way a triaging SRE would see it."""
    parts = []
    if ctx.get("pods"):
        parts.append("PODS: " + json.dumps(ctx["pods"]))
    if ctx.get("describe"):
        parts.append("DESCRIBE: " + json.dumps(ctx["describe"]))
    for key in ("logs", "previous_logs", "init_logs"):
        if ctx.get(key):
            parts.append(f"{key.upper()}: " + json.dumps(ctx[key]))
    for key in ("events", "services", "hpas", "pdbs", "job"):
        if ctx.get(key):
            parts.append(f"{key.upper()}: " + json.dumps(ctx[key]))
    return "\n".join(parts)


def build_prompt(case: dict, classes: list[str]) -> tuple[str, str]:
    system = (
        "You are a Kubernetes SRE doing root-cause analysis. From the evidence, choose the "
        "SINGLE best issue class from the allowed list (or 'none' if the workload looks "
        "healthy). Trust the evidence over any prior assumption. Respond with ONLY JSON: "
        '{"issue": "<one of the allowed classes>", "confidence": <0-100>, "root_cause": "<short>"}'
    )
    user = (
        f"Allowed issue classes: {classes}\n\n"
        f"Incident evidence for `{case['name']}`:\n{_evidence(case['ctx'])}"
    )
    return system, user


def parse_answer(text: str) -> dict:
    """Extract the JSON verdict from the model's reply (tolerant of surrounding prose)."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return {"issue": None, "confidence": 0, "root_cause": text[:120]}
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {"issue": None, "confidence": 0, "root_cause": text[:120]}


def score(answer: dict, expected: dict) -> bool:
    want = expected.get("issue")             # None ⇒ healthy ⇒ model should say "none"
    got = (answer.get("issue") or "").strip()
    if want is None:
        return got.lower() in ("none", "", "healthy")
    if expected.get("match") == "top3":      # model picks one; accept the labelled class
        return got == want
    return got == want


def _ask_model(client, model: str, system: str, user: str) -> str:
    msg = client.messages.create(
        model=model, max_tokens=300, system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(block.text for block in msg.content if getattr(block, "type", "") == "text")


def main() -> int:
    ap = argparse.ArgumentParser(description="Model-in-the-loop RCA eval")
    ap.add_argument("--model", default=os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"))
    ap.add_argument("--dry-run", action="store_true", help="print prompts, do not call the API")
    args = ap.parse_args()

    cases = load_cases()
    classes = issue_classes(cases)

    if args.dry_run:
        for c in cases[:3]:
            system, user = build_prompt(c, classes)
            print(f"\n===== {c['name']} =====\n[system] {system}\n[user] {user}")
        print(f"\n(dry-run: {len(cases)} cases, {len(classes)} classes — no API call)")
        return 0

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("SKIP: set ANTHROPIC_API_KEY to run the model-in-the-loop eval (costs tokens).")
        return 0
    try:
        import anthropic
    except ImportError:
        print("SKIP: `pip install anthropic` to run the model-in-the-loop eval.")
        return 0

    client = anthropic.Anthropic()
    correct = 0
    for c in cases:
        system, user = build_prompt(c, classes)
        answer = parse_answer(_ask_model(client, args.model, system, user))
        ok = score(answer, c["expected"])
        correct += ok
        print(f"  [{'OK' if ok else 'XX'}] {c['name']:<42} model={answer.get('issue')!r}")
    acc = correct / len(cases) if cases else 1.0
    print(f"\nmodel-in-the-loop accuracy: {acc:.0%}  ({correct}/{len(cases)}) on {args.model}")
    return 0 if acc >= 0.85 else 1


if __name__ == "__main__":
    sys.exit(main())
