# Incident Scenarios — Diagnosis Playbook

How the agent diagnoses each failure class: the signals it reads, the tools it calls, and
the detector that fires. Detectors are in
[rca/detectors.py](../src/k8s_sre_agent/rca/detectors.py); the orchestration is
`rca_diagnose`.

## Pod issues

| Scenario | Primary signals | Tools | Detector → typical root cause |
|----------|-----------------|-------|-------------------------------|
| **CrashLoopBackOff** | waiting reason; **previous-container** logs; restart rate | `k8s_describe_pod`, `logs_pod previous=true`, `metric_restarts`, `argocd_history` | `crashloop_db_connection` → app exits on startup; DB connection error + recent secret rotation/release |
| **ImagePullBackOff / ErrImagePull** | `Failed`/`BackOff` events | `k8s_get_events`, `k8s_describe_pod` | `image_pull` → bad tag (`not found`) vs registry auth (`unauthorized`) |
| **CreateContainerError / CreateContainerConfigError** | event message | `k8s_get_events`, `k8s_get_configmaps`, `k8s_get_secrets_metadata` | `container_config_error` → missing ConfigMap/Secret reference |
| **Pending Pods** | `FailedScheduling` event | `k8s_get_events`, `k8s_get_nodes` | `pending_unschedulable` → insufficient cpu/mem, taint, affinity, or PVC binding |
| **OOMKilled** | last-terminated reason; memory trend | `k8s_describe_pod`, `metric_memory` | `oom_killed` → exceeded memory limit (leak or under-sized limit) |
| **Container restart loop** | restart counts over time | `metric_restarts`, `logs_pod previous=true` | feeds CrashLoop / probe detectors |
| **Init container failure** | `Init:CrashLoopBackOff`/`Init:Error`; init-container logs | `rca_diagnose` (`init_container_failure`) | failing init step (migration/config fetch) — pod never starts app containers |
| **Pod evicted (ephemeral storage)** | pod `Failed` / `Evicted` + message | `rca_diagnose` (`storage_eviction`) | container/emptyDir disk over the ephemeral-storage limit |
| **Liveness probe failure** | `Unhealthy` events (liveness) | `k8s_get_events`, `k8s_describe_pod` | `probe_failure` → failing liveness → restarts; check path/port/initialDelay |
| **Readiness probe failure** | `Unhealthy` events (readiness) | `k8s_get_events` | `probe_failure` → never Ready → removed from Service endpoints |

## Node issues

| Scenario | Signals | Tools | Detector → root cause |
|----------|---------|-------|-----------------------|
| **NodeNotReady** | node `Ready != True` | `k8s_get_nodes`, `logs_node` | `node_pressure` → kubelet/runtime/network problem; cordon+replace |
| **DiskPressure** | node DiskPressure condition | `k8s_get_nodes`, `metric_disk` | `node_pressure` → free disk / prune images & logs |
| **MemoryPressure** | node MemoryPressure | `k8s_get_nodes`, `metric_memory` | `node_pressure` → evictions imminent; add capacity |
| **PIDPressure** | node PIDPressure | `k8s_get_nodes` | `node_pressure` → raise PID limits / fewer procs |
| **Network failures** | NetworkUnavailable; CNI daemonset | `k8s_get_nodes`, `k8s_get_daemonsets`, `logs_node` | node ready + CNI pod health |

## Application issues

| Scenario | Signals | Tools | Detector / approach |
|----------|---------|-------|---------------------|
| **Database connection failure** | connection refused/timeout in logs; secret age | `logs_pod`, `k8s_get_secrets_metadata`, `argocd_history` | `crashloop_db_connection`; correlate with secret rotation |
| **Secret issue** | secret rotated near failure; CreateContainerConfigError | `k8s_get_secrets_metadata`, `k8s_get_events` | metadata-only correlation (never reads values) |
| **ConfigMap issue** | "configmap … not found"; bad config in logs | `k8s_get_configmaps`, `k8s_get_events` | `container_config_error` |
| **TLS certificate issue** | x509/expired/verify-failed in logs; cert-manager objects | `logs_pod`, (cert-manager read via RBAC) | `tls_certificate` |
| **DNS issue** | "no such host"/resolution errors | `logs_pod`, `loki_query` (CoreDNS) | `dns_failure` → CoreDNS / NetworkPolicy :53 / bad FQDN |
| **API connectivity** | upstream 5xx/timeout in logs; endpoints empty | `logs_pod`, `k8s_get_services`, `loki_query` | endpoint/Service correlation |

## Platform issues

| Scenario | Signals | Tools | Approach |
|----------|---------|-------|----------|
| **Ingress failure** | no address; backend service has no endpoints; 404/502 | `k8s_get_ingress`, `k8s_get_services`, `loki_query` (ingress ctlr) | ingress→service→endpoints chain |
| **Service mesh / Istio — routing** | VirtualService routes to an undefined subset | `istio_mesh_analyze` | dangling-subset → 503 "no healthy upstream" (invisible in app logs) |
| **Service mesh / Istio — mTLS** | PeerAuthentication STRICT vs DestinationRule `DISABLE`/`SIMPLE` | `istio_mesh_analyze`, `istio_get_peerauthentications` | mTLS mode conflict → 503 UC / connection reset |
| **Service mesh / Istio — sidecar** | `istio-proxy` not Ready (proxy OOM, image pull, istiod unreachable) | `rca_diagnose` (`istio_sidecar_not_ready` detector) | pod can't serve mesh traffic even if the app is healthy |
| **Service mesh / Istio — ingress Gateway** | Gateway with no VirtualService bound, or VS binding to a non-existent Gateway | `istio_mesh_analyze` | edge route never programmed → 404 at the ingress gateway |
| **Load balancer** | LB ingress empty; cloud LB events | `k8s_get_services`, `k8s_get_events` | service type=LoadBalancer status |
| **Storage / PVC** | Pending (PVC); volume node-affinity conflict | `k8s_get_events`, `k8s_get_nodes` | `pending_unschedulable` (PVC branch); also StatefulSet subjects |
| **HPA can't scale** | HPA `ScalingActive=False` (FailedGetResourceMetric) | `rca_diagnose` (`hpa_cannot_scale`) | metrics-server missing or target has no resource requests |
| **PDB blocking** | PDB `disruptionsAllowed=0` | `rca_diagnose` (`pdb_blocking`) | drains/upgrades/rollouts hang — too-tight minAvailable vs replicas |

## The change-correlation thread that runs through all of them

For most regressions, the decisive question is *"what changed right before this broke?"*.
`correlate.find_recent_change` ranks ArgoCD syncs, CI deploys and secret/configmap
rotations by how closely they precede the first failure event, and the engine threads that
into the report:

```
…CHANGE [sync] abc1234 (v1.3) deployed 2026-06-13T09:58Z
 2026-06-13T10:00Z  BackOff: Back-off restarting failed container
 2026-06-13T10:00Z  Unhealthy: Liveness probe failed: connection refused
```

That timeline is what lets the agent say *"failure started after release v1.3 → rollback
candidate is the prior revision"* with a defensible confidence score — and, where the cause
is a config/secret rather than code, recommend a fix instead of a rollback.
