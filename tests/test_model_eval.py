"""Validate the model-in-the-loop eval scaffold WITHOUT calling any API: prompt building,
answer parsing, scoring (incl. the negative/healthy case), and that it skips cleanly with
no API key. The actual model run is opt-in + cost-incurring (see evals/model_eval/)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals" / "model_eval"))

import run_model_eval as m  # noqa: E402


def test_issue_classes_include_dataset_labels_and_none():
    classes = m.issue_classes(m.load_cases())
    assert "OOMKilled" in classes and "DNS Resolution Failure" in classes
    assert classes[-1] == "none"


def test_build_prompt_embeds_evidence_and_classes():
    case = {"name": "x", "ctx": {"pods": [{"reason": "CrashLoopBackOff"}],
                                 "previous_logs": ["FATAL: timeout"]}, "expected": {"issue": "CrashLoopBackOff"}}
    system, user = m.build_prompt(case, ["CrashLoopBackOff", "none"])
    assert "ONLY JSON" in system
    assert "CrashLoopBackOff" in user and "FATAL: timeout" in user


def test_parse_answer_tolerates_surrounding_prose():
    assert m.parse_answer('Here is my verdict: {"issue": "OOMKilled", "confidence": 95}')["issue"] == "OOMKilled"
    assert m.parse_answer("no json here")["issue"] is None


def test_score_exact_and_healthy_cases():
    assert m.score({"issue": "OOMKilled"}, {"issue": "OOMKilled"}) is True
    assert m.score({"issue": "Pending Pods"}, {"issue": "OOMKilled"}) is False
    # healthy (expected None) → model must say "none"
    assert m.score({"issue": "none"}, {"issue": None}) is True
    assert m.score({"issue": "OOMKilled"}, {"issue": None}) is False


def test_main_skips_without_api_key(monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["run_model_eval.py"])
    assert m.main() == 0
    assert "SKIP" in capsys.readouterr().out


def test_dry_run_builds_prompts_without_api(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["run_model_eval.py", "--dry-run"])
    assert m.main() == 0
    assert "dry-run" in capsys.readouterr().out
