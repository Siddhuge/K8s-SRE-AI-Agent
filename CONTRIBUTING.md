# Contributing

## Dev setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,rag,azure,aws]" -c constraints.txt
pre-commit install        # runs ruff + mypy + secret-detect on commit
```

## Quality gates (must pass — CI enforces them)

```bash
make lint        # ruff
make type        # mypy (strict — no `|| true`)
make test-unit   # pytest, no cluster needed
```

## Working against a live cluster

The integration tests + the whole demo run on a local **kind** cluster:

```bash
make stack-up            # kind + all failure scenarios + Prometheus/Loki/Grafana + Istio + ArgoCD
make demo                # run an RCA against the crashloop workload
make test-integration    # live tests (auto-skip when a backend isn't reachable)
make stack-down          # tear it all down
```

## How detectors are built (the core workflow)

The RCA engine pairs **deterministic detectors** (rule-based, explainable) with the
LLM's judgement. To add a failure class, follow the loop that's found a real bug nearly
every time:

1. **Deploy a real scenario** on kind (add to `tests/fixtures/scenariosN.yaml`).
2. **Diagnose it** (`diagnose(cluster, ns, subject)`) and watch where the engine is
   wrong — real cluster data diverges from assumptions (bytes-vs-str, event scoping,
   timeouts vs auth, init containers, …).
3. **Write/fix the detector** in `rca/detectors.py` — a pure `ctx -> Hypothesis | None`
   with weighted, explainable `Evidence`. Add any new signal to `rca/engine.collect_context`.
4. **Test both**: a synthetic unit test in `tests/test_rca.py` and a live entry in
   `tests/test_integration_kind.py::SCENARIOS`.
5. Keep it read-only — detectors *recommend*, never mutate.

## Security boundary

The agent is read-only by RBAC (`get/list/watch` only; Secret values unreadable) and
that is asserted at the API server in `tests/test_integration_rbac.py`. Don't add a tool
that mutates cluster state. The only outward-facing tools (`slack_post`/`teams_post`)
must stay behind the `ALLOW_NOTIFICATIONS` + allow-list guards.

## Commits

Conventional, imperative subject; explain *why* in the body. End with the
`Co-Authored-By` trailer if pairing with an assistant.
