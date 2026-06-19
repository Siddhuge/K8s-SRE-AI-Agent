# Runbook: Istio 503 from mTLS mode conflict

## Symptoms
- Service-to-service calls return 503 UC / connection reset inside the mesh
- App logs show no error — the failure is in the mesh config

## Root cause
A PeerAuthentication requires STRICT mTLS for the namespace, but a DestinationRule
DISABLEs (or SIMPLEs) TLS for the host — clients send plaintext to an mTLS-only server.
The inverse (DR forces ISTIO_MUTUAL while PeerAuthentication DISABLEs) also breaks.

## Resolution
1. `istio_mesh_analyze` flags the conflicting host.
2. Align the DestinationRule tls.mode with the PeerAuthentication (ISTIO_MUTUAL under STRICT).
3. Also check VirtualService routes don't point at undefined subsets.
