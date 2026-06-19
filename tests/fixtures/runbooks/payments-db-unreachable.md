# Runbook: payments API cannot reach its database

## Symptoms
- payments `api` pods in CrashLoopBackOff
- crash logs show `connection timed out` / `could not connect to server` to the DB host
- no auth error (a password failure would say "authentication failed")

## Root cause
The database the API depends on is unreachable: either the `db` Service/endpoints do
not exist in the namespace, a NetworkPolicy blocks egress to port 5432, or the configured
DB host is wrong. A connection timeout is a reachability problem, not a credentials one.

## Resolution
1. Confirm the `db` Service and its endpoints exist in the `payments` namespace.
2. If missing, deploy the database (or point the app's DB host at the real endpoint).
3. Check NetworkPolicies allow egress to the DB port.
4. Restart the deployment once the dependency is reachable.
