# Runbook: container OOMKilled

## Symptoms
- Container's last terminated reason is `OOMKilled`
- Working-set memory climbs to the limit before each restart

## Root cause
The container exceeded its memory limit and was killed by the kernel OOM killer —
either a memory leak, an under-sized limit, or too much concurrency.

## Resolution
1. Confirm the memory trend hit the limit (metric_memory).
2. Raise the memory limit, fix the leak, or reduce workload concurrency.
3. For a sidecar (istio-proxy) OOM, raise the proxy memory limit.
