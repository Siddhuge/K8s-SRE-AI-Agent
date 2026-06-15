"""Istio service-mesh read tools + mesh-config analysis.

Read-only access to VirtualServices, DestinationRules and Gateways (granted by the
RBAC rule for networking.istio.io), plus `istio_mesh_analyze`, which catches the most
common silent mesh misconfiguration: a VirtualService routing to a subset that no
DestinationRule defines — Envoy then has no endpoints for that subset and returns
503 (no healthy upstream / UC). The bug lives purely in the config, so the agent can
spot it without any traffic.
"""
from __future__ import annotations

from ..clusters import manager

_NET = "networking.istio.io"
_SEC = "security.istio.io"
_VERSION = "v1beta1"


def _list(cluster: str | None, namespace: str, plural: str, group: str = _NET) -> list[dict]:
    mgr = manager()
    mgr.guard_namespace(cluster, namespace)
    resp = mgr.clients(cluster).custom.list_namespaced_custom_object(
        group=group, version=_VERSION, namespace=namespace, plural=plural
    )
    return resp.get("items", [])


def _namespace_mtls_mode(peerauthentications: list[dict]) -> str | None:
    """The namespace-wide mTLS mode (a PeerAuthentication with no workload selector)."""
    for pa in peerauthentications:
        spec = pa.get("spec", {})
        if not spec.get("selector"):  # namespace/mesh-wide policy
            return spec.get("mtls", {}).get("mode")  # STRICT | PERMISSIVE | DISABLE
    return None


def analyze_mesh(
    virtualservices: list[dict],
    destinationrules: list[dict],
    peerauthentications: list[dict] | None = None,
    gateways: list[dict] | None = None,
) -> list[dict]:
    """Pure analysis (unit-testable) for classic, log-invisible mesh failures:

    1. A VirtualService route whose (host, subset) is not defined by any DestinationRule
       (503 no healthy upstream).
    2. An mTLS mode conflict between the namespace PeerAuthentication and a
       DestinationRule's TLS mode (503 UC / connection reset).
    3. Ingress Gateway binding errors: a Gateway with no VirtualService bound to it
       (404 at the edge), or a VirtualService referencing a Gateway that doesn't exist.
    """
    findings: list[dict] = []

    # ── (1) dangling subset references ──────────────────────────────────────
    defined: dict[str, set[str]] = {}
    for dr in destinationrules:
        host = dr.get("spec", {}).get("host", "")
        subsets = {s.get("name") for s in dr.get("spec", {}).get("subsets", []) or []}
        defined.setdefault(host, set()).update(subsets)

    for vs in virtualservices:
        name = vs.get("metadata", {}).get("name", "?")
        for http in vs.get("spec", {}).get("http", []) or []:
            for route in http.get("route", []) or []:
                dest = route.get("destination", {})
                host, subset = dest.get("host", ""), dest.get("subset")
                if subset and subset not in defined.get(host, set()):
                    findings.append({
                        "issue": "subset_not_defined",
                        "virtualservice": name, "host": host, "subset": subset,
                        "detail": (
                            f"VirtualService '{name}' routes to subset '{subset}' on host "
                            f"'{host}', but no DestinationRule defines that subset "
                            f"(defined: {sorted(defined.get(host, set())) or 'none'}). "
                            f"Envoy will have no endpoints → 503 no healthy upstream."
                        ),
                    })

    # ── (2) mTLS mode conflicts ─────────────────────────────────────────────
    ns_mode = _namespace_mtls_mode(peerauthentications or [])
    if ns_mode:
        for dr in destinationrules:
            host = dr.get("spec", {}).get("host", "")
            dr_mode = dr.get("spec", {}).get("trafficPolicy", {}).get("tls", {}).get("mode")
            if dr_mode is None:
                continue
            conflict = None
            if ns_mode == "STRICT" and dr_mode in ("DISABLE", "SIMPLE"):
                conflict = (
                    f"PeerAuthentication requires STRICT mTLS for the namespace, but the "
                    f"DestinationRule for host '{host}' sets tls.mode={dr_mode} (plaintext). "
                    f"Clients send plaintext to an mTLS-only server → 503 UC / connection reset."
                )
            elif ns_mode == "DISABLE" and dr_mode in ("ISTIO_MUTUAL", "MUTUAL"):
                conflict = (
                    f"DestinationRule for host '{host}' forces mTLS (tls.mode={dr_mode}) but "
                    f"PeerAuthentication DISABLEs mTLS server-side → handshake fails → 503."
                )
            if conflict:
                findings.append({
                    "issue": "mtls_mode_conflict",
                    "host": host, "peer_auth_mode": ns_mode, "destinationrule_mode": dr_mode,
                    "detail": conflict,
                })

    # ── (3) ingress Gateway binding errors ──────────────────────────────────
    gws = gateways or []
    gw_names = {g.get("metadata", {}).get("name", "") for g in gws}
    referenced: set[str] = set()
    for vs in virtualservices:
        for g in vs.get("spec", {}).get("gateways", []) or []:
            if g != "mesh":  # "mesh" = sidecar traffic, not an ingress Gateway
                referenced.add(g.split("/")[-1])  # strip optional "namespace/" prefix

    # 3a. A Gateway that no VirtualService binds to → nothing routes its hosts → 404.
    for g in gws:
        name = g.get("metadata", {}).get("name", "")
        hosts = [h for srv in g.get("spec", {}).get("servers", []) or [] for h in srv.get("hosts", []) or []]
        if name not in referenced:
            findings.append({
                "issue": "gateway_no_virtualservice",
                "gateway": name, "hosts": hosts,
                "detail": (
                    f"Gateway '{name}' (hosts {hosts or '[]'}) has no VirtualService bound to it "
                    f"(no VirtualService lists it in spec.gateways). Requests reaching the "
                    f"ingress gateway for these hosts get 404 — nothing routes them."
                ),
            })

    # 3b. A VirtualService referencing a Gateway that doesn't exist.
    for vs in virtualservices:
        name = vs.get("metadata", {}).get("name", "?")
        for g in vs.get("spec", {}).get("gateways", []) or []:
            if g == "mesh":
                continue
            short = g.split("/")[-1]
            if short not in gw_names:
                findings.append({
                    "issue": "virtualservice_unknown_gateway",
                    "virtualservice": name, "gateway": short,
                    "detail": (
                        f"VirtualService '{name}' binds to Gateway '{g}', which does not exist "
                        f"in the namespace. The route is never programmed at the edge → 404."
                    ),
                })
    return findings


def register(mcp) -> None:
    @mcp.tool()
    def istio_get_virtualservices(namespace: str, cluster: str | None = None) -> list[dict]:
        """List Istio VirtualServices (hosts + route destinations) in a namespace."""
        out = []
        for vs in _list(cluster, namespace, "virtualservices"):
            spec = vs.get("spec", {})
            routes = [
                {"host": r.get("destination", {}).get("host"),
                 "subset": r.get("destination", {}).get("subset"),
                 "weight": r.get("weight")}
                for http in spec.get("http", []) or [] for r in http.get("route", []) or []
            ]
            out.append({"name": vs["metadata"]["name"], "hosts": spec.get("hosts"), "routes": routes})
        return out

    @mcp.tool()
    def istio_get_destinationrules(namespace: str, cluster: str | None = None) -> list[dict]:
        """List Istio DestinationRules (host + defined subsets + tls mode) in a namespace."""
        out = []
        for dr in _list(cluster, namespace, "destinationrules"):
            spec = dr.get("spec", {})
            out.append({
                "name": dr["metadata"]["name"],
                "host": spec.get("host"),
                "subsets": [s.get("name") for s in spec.get("subsets", []) or []],
                "tls_mode": spec.get("trafficPolicy", {}).get("tls", {}).get("mode"),
            })
        return out

    @mcp.tool()
    def istio_get_gateways(namespace: str, cluster: str | None = None) -> list[dict]:
        """List Istio Gateways (selector + server hosts/ports) in a namespace."""
        out = []
        for gw in _list(cluster, namespace, "gateways"):
            spec = gw.get("spec", {})
            out.append({
                "name": gw["metadata"]["name"],
                "selector": spec.get("selector"),
                "servers": [
                    {"port": s.get("port", {}).get("number"), "protocol": s.get("port", {}).get("protocol"),
                     "hosts": s.get("hosts")}
                    for s in spec.get("servers", []) or []
                ],
            })
        return out

    @mcp.tool()
    def istio_get_peerauthentications(namespace: str, cluster: str | None = None) -> list[dict]:
        """List Istio PeerAuthentications (mTLS mode + selector) in a namespace."""
        out = []
        for pa in _list(cluster, namespace, "peerauthentications", group=_SEC):
            spec = pa.get("spec", {})
            out.append({
                "name": pa["metadata"]["name"],
                "mtls_mode": spec.get("mtls", {}).get("mode"),
                "selector": spec.get("selector", {}).get("matchLabels") if spec.get("selector") else None,
                "scope": "namespace" if not spec.get("selector") else "workload",
            })
        return out

    @mcp.tool()
    def istio_mesh_analyze(namespace: str, cluster: str | None = None) -> dict:
        """Analyze the mesh config in a namespace for log-invisible 503 causes:
        (1) VirtualService routes to a subset no DestinationRule defines, and
        (2) mTLS mode conflicts between PeerAuthentication and DestinationRule TLS.
        Returns findings with the offending objects."""
        vs = _list(cluster, namespace, "virtualservices")
        dr = _list(cluster, namespace, "destinationrules")
        pa = _list(cluster, namespace, "peerauthentications", group=_SEC)
        gw = _list(cluster, namespace, "gateways")
        findings = analyze_mesh(vs, dr, pa, gw)
        return {
            "namespace": namespace,
            "virtualservices": len(vs),
            "destinationrules": len(dr),
            "peerauthentications": len(pa),
            "gateways": len(gw),
            "findings": findings,
            "healthy": not findings,
        }
