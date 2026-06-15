"""Correlation helpers: build a timeline and find the change that precedes failure.

The single most useful RCA question is "what changed right before this broke?".
`find_recent_change` ranks candidate changes (ArgoCD syncs, CI deploys, secret/
configmap rotations) by how closely they precede the first failure event.
"""
from __future__ import annotations

from datetime import datetime, timezone


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def first_failure_time(events: list[dict]) -> datetime | None:
    """Earliest Warning event — approximates when the incident began."""
    warnings = [e for e in events if e.get("type") == "Warning" and e.get("timestamp")]
    times = sorted(t for e in warnings if (t := _parse(e.get("timestamp"))))
    return times[0] if times else None


def find_recent_change(context: dict) -> dict | None:
    """Pick the change that most plausibly caused the failure.

    Inputs (any may be absent):
      context["argocd_history"]   list of {revision, deployed_at}
      context["deployments"]      list of {sha, created_at}
      context["secret_changes"]   list of {name, at}  (derived from resource_version deltas)
    Returns a normalized dict: {kind, name?, revision?, at, rollback_target?}.
    """
    failure_t = context.get("first_failure_time") or datetime.now(timezone.utc)
    candidates: list[dict] = []

    for h in context.get("argocd_history", []):
        t = _parse(h.get("deployed_at"))
        if t and t <= failure_t:
            candidates.append({"kind": "sync", "revision": h.get("revision"), "at": h.get("deployed_at"), "t": t})

    for d in context.get("deployments", []):
        t = _parse(d.get("created_at"))
        if t and t <= failure_t:
            candidates.append({"kind": "deploy", "revision": d.get("sha"), "at": d.get("created_at"), "t": t})

    for s in context.get("secret_changes", []):
        t = _parse(s.get("at"))
        if t and t <= failure_t:
            candidates.append({"kind": "secret", "name": s.get("name"), "at": s.get("at"), "t": t})

    if not candidates:
        return None
    # Closest preceding change wins.
    best = max(candidates, key=lambda c: c["t"])
    # Provide a rollback target = the change just before the offending one.
    same_kind = sorted((c for c in candidates if c["kind"] == best["kind"]), key=lambda c: c["t"])
    if len(same_kind) >= 2:
        best["rollback_target"] = same_kind[-2].get("revision", "previous revision")
    best.pop("t", None)
    return best


def build_timeline(context: dict) -> list[str]:
    """Human-readable, time-ordered list of notable signals for the report."""
    entries: list[tuple[datetime, str]] = []
    for e in context.get("events", []):
        if t := _parse(e.get("timestamp")):
            entries.append((t, f"{e.get('reason')}: {e.get('message','')[:120]}"))
    if change := context.get("recent_change"):
        if t := _parse(change.get("at")):
            label = change.get("revision") or change.get("name") or "change"
            entries.append((t, f"CHANGE [{change['kind']}] {label}"))
    entries.sort(key=lambda x: x[0])
    return [f"{t.isoformat()}  {msg}" for t, msg in entries]
