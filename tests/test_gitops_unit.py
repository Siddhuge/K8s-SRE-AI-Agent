"""Unit tests for the ArgoCD tools — parsing logic, no live server (get_json mocked)."""
import k8s_sre_agent.tools.gitops as g


def _register():
    captured = {}

    class M:
        def tool(self):
            def d(fn):
                captured[fn.__name__] = fn
                return fn
            return d

    g.register(M())
    return captured


def test_argocd_app_surfaces_conditions(monkeypatch):
    payload = {
        "spec": {"source": {"targetRevision": "HEAD"}},
        "status": {
            "health": {"status": "Healthy"},
            "sync": {"status": "Unknown", "revision": "HEAD"},
            "conditions": [{"type": "ComparisonError", "message": "path does not exist"}],
            "resources": [],
        },
    }
    monkeypatch.setattr(g, "get_json", lambda url, headers=None: payload)
    monkeypatch.setattr(g, "_argocd", lambda c: ("http://argo", {}))
    out = _register()["argocd_app"](app="broken-app", cluster="c")
    assert out["sync"] == "Unknown"
    assert out["conditions"] and out["conditions"][0]["type"] == "ComparisonError"


def test_argocd_rollback_info_picks_previous_revision(monkeypatch):
    payload = {
        "status": {
            "sync": {"revision": "rev3"},
            "history": [
                {"revision": "rev1", "deployedAt": "t1"},
                {"revision": "rev2", "deployedAt": "t2"},
                {"revision": "rev3", "deployedAt": "t3"},
            ],
        }
    }
    monkeypatch.setattr(g, "get_json", lambda url, headers=None: payload)
    monkeypatch.setattr(g, "_argocd", lambda c: ("http://argo", {}))
    out = _register()["argocd_rollback_info"](app="app", cluster="c")
    assert out["current_revision"] == "rev3"
    assert out["rollback_candidate"] == "rev2"  # the one before current
