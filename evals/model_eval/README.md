# Model-in-the-Loop RCA Eval

Complements [`evals/run_eval.py`](../run_eval.py). That one scores the **deterministic
detector** layer (free, CI-gated). This one scores the **LLM's arbitrated verdict** on the
same labeled incidents: given the evidence, does the model pick the right issue class — and
crucially, does it **correct the detectors when they're wrong** (the behavior we saw live,
when the model overrode a 94% secret-rotation verdict to find a missing DB service)?

```bash
# preview the exact prompts — no API call, no cost:
PYTHONPATH=src python3 evals/model_eval/run_model_eval.py --dry-run

# run it for real (costs API tokens):
pip install anthropic
ANTHROPIC_API_KEY=sk-... python3 evals/model_eval/run_model_eval.py \
    --model claude-haiku-4-5-20251001
```

## Why it's separate (and opt-in)

- **It costs money** (one API call per case) and is **non-deterministic**, so it must not
  run silently in CI. With no `ANTHROPIC_API_KEY` (or no `anthropic` SDK) it **skips
  cleanly** (exit 0).
- The detector harness is the regression fence; this is a **periodic quality probe** of the
  reasoning layer — run it when you change prompts/models, or to compare models (point
  `--model` at Haiku vs Sonnet vs Opus and watch accuracy/cost trade off).

## How it scores

Each case's evidence is rendered as a triaging SRE would see it (pods, describe, logs,
events…). The model must return JSON choosing **one issue class from a fixed list** (the
dataset's labels + `none`), which makes scoring exact. Healthy/negative cases require the
model to answer `none` — a false alarm there is as bad as a miss.

## Not yet covered (honest scope)

This evaluates the model's diagnosis **from pre-collected evidence**. It does **not** drive
the full agentic tool-calling loop (model decides which tools to call, in what order) —
that needs a live cluster + MCP session and is the natural next harness once you run it
against staging. The unit tests (`tests/test_model_eval.py`) cover prompt/parse/score
without any API.
