# Runbook: node drain / upgrade hangs (PodDisruptionBudget)

## Symptoms
- `kubectl drain` or a node upgrade / rollout hangs indefinitely
- A PodDisruptionBudget shows `ALLOWED DISRUPTIONS = 0`

## Root cause
A PDB whose minAvailable equals the replica count (e.g. minAvailable 1 with 1 replica)
allows zero voluntary disruptions, so the eviction API refuses to evict the pod and the
drain blocks.

## Resolution
1. Add replicas so the PDB allows at least one disruption, or
2. Relax the PDB (lower minAvailable / raise maxUnavailable).
3. Re-run the drain/upgrade.
