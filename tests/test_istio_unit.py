"""Pure unit tests for the Istio mesh analysis (no cluster needed)."""
from k8s_sre_agent.tools.istio import analyze_mesh


def _vs(name, host, subset):
    return {"metadata": {"name": name},
            "spec": {"http": [{"route": [{"destination": {"host": host, "subset": subset}}]}]}}


def _dr(host, subsets):
    return {"spec": {"host": host, "subsets": [{"name": s} for s in subsets]}}


def test_flags_dangling_subset():
    findings = analyze_mesh([_vs("reviews", "reviews", "v2")], [_dr("reviews", ["v1"])])
    assert len(findings) == 1
    f = findings[0]
    assert f["issue"] == "subset_not_defined"
    assert f["subset"] == "v2" and f["host"] == "reviews"


def test_healthy_when_subset_defined():
    findings = analyze_mesh([_vs("reviews", "reviews", "v1")], [_dr("reviews", ["v1", "v2"])])
    assert findings == []


def test_route_without_subset_is_fine():
    findings = analyze_mesh([_vs("reviews", "reviews", None)], [])
    assert findings == []


def test_missing_destinationrule_entirely_flags():
    findings = analyze_mesh([_vs("ratings", "ratings", "v3")], [])
    assert findings and findings[0]["subset"] == "v3"


def _pa(mode, selector=None):
    spec = {"mtls": {"mode": mode}}
    if selector:
        spec["selector"] = {"matchLabels": selector}
    return {"metadata": {"name": "default"}, "spec": spec}


def _dr_tls(host, mode):
    return {"spec": {"host": host, "trafficPolicy": {"tls": {"mode": mode}}}}


def test_mtls_strict_vs_disable_conflict():
    findings = analyze_mesh([], [_dr_tls("ratings", "DISABLE")], [_pa("STRICT")])
    assert len(findings) == 1
    assert findings[0]["issue"] == "mtls_mode_conflict"
    assert findings[0]["host"] == "ratings"


def test_mtls_disable_vs_istio_mutual_conflict():
    findings = analyze_mesh([], [_dr_tls("ratings", "ISTIO_MUTUAL")], [_pa("DISABLE")])
    assert findings and findings[0]["issue"] == "mtls_mode_conflict"


def test_mtls_aligned_no_conflict():
    # STRICT server + ISTIO_MUTUAL client is the correct pairing → no finding.
    assert analyze_mesh([], [_dr_tls("ratings", "ISTIO_MUTUAL")], [_pa("STRICT")]) == []


def test_mtls_workload_scoped_peerauth_ignored_for_namespace_rule():
    # A selector-scoped PeerAuthentication is not the namespace default → not used here.
    assert analyze_mesh([], [_dr_tls("ratings", "DISABLE")], [_pa("STRICT", {"app": "x"})]) == []


def _gw(name, hosts):
    return {"metadata": {"name": name}, "spec": {"servers": [{"hosts": hosts}]}}


def _vs_gw(name, gateways):
    return {"metadata": {"name": name},
            "spec": {"gateways": gateways, "http": [{"route": [{"destination": {"host": "x"}}]}]}}


def test_gateway_without_virtualservice_flags_404():
    findings = analyze_mesh([], [], [], gateways=[_gw("edge", ["app.example.com"])])
    assert len(findings) == 1
    assert findings[0]["issue"] == "gateway_no_virtualservice"
    assert findings[0]["gateway"] == "edge"


def test_virtualservice_unknown_gateway_flags_404():
    findings = analyze_mesh([_vs_gw("route", ["missing-gw"])], [], [], gateways=[])
    assert any(f["issue"] == "virtualservice_unknown_gateway" and f["gateway"] == "missing-gw"
               for f in findings)


def test_gateway_bound_by_virtualservice_is_healthy():
    # A Gateway referenced by a VS (and the VS's gateway exists) → no gateway finding.
    findings = analyze_mesh([_vs_gw("route", ["edge"])], [], [], gateways=[_gw("edge", ["app.example.com"])])
    assert not [f for f in findings if "gateway" in f["issue"]]


def test_namespace_prefixed_gateway_reference_resolves():
    # "mesh/edge" should resolve to the "edge" Gateway in the namespace.
    findings = analyze_mesh([_vs_gw("route", ["mesh/edge"])], [], [], gateways=[_gw("edge", ["a"])])
    assert not [f for f in findings if "gateway" in f["issue"]]
