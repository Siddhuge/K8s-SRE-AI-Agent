"""Gate the RCA evaluation harness in CI: the detector engine must clear the accuracy /
calibration thresholds on the labeled dataset, and (importantly) never be confidently
wrong or fire on a healthy pod. See evals/run_eval.py."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals"))

import run_eval  # noqa: E402


def test_rca_eval_dataset_meets_gates():
    rep = run_eval.evaluate_dataset()
    t = run_eval.THRESHOLDS

    assert rep["passed"] == rep["total"], (
        f"{rep['total'] - rep['passed']} eval case(s) failed: "
        + "; ".join(f"{r.name}: {r.reasons}" for r in rep["results"] if not r.passed)
    )
    assert rep["top1_accuracy"] >= t["min_top1_accuracy"], rep["top1_accuracy"]
    # Zero-tolerance quadrants: a confident wrong answer or a finding on a healthy pod
    # is worse than a miss — those erode trust fastest.
    assert len(rep["confident_wrong"]) <= t["max_confident_wrong"], [
        r.name for r in rep["confident_wrong"]
    ]
    assert len(rep["false_positives"]) <= t["max_false_positives"], [
        r.name for r in rep["false_positives"]
    ]
