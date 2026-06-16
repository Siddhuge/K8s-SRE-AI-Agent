"""Unit tests for the outward-facing notification guards (no real Slack/Teams)."""
from k8s_sre_agent.config import Settings


def _tools(settings):
    captured = {}

    class M:
        def tool(self):
            def d(fn):
                captured[fn.__name__] = fn
                return fn
            return d

    import k8s_sre_agent.tools.incidents as inc
    # patch get_settings so the tools see our test settings
    inc.get_settings = lambda: settings  # type: ignore
    inc.register(M())
    return captured


def test_slack_post_refused_when_notifications_disabled():
    t = _tools(Settings(allow_notifications=False, _env_file=None))
    out = t["slack_post"](channel="#sre", text="hi")
    assert out["ok"] is False and "disabled" in out["error"]


def test_slack_post_refused_when_channel_not_allowlisted():
    s = Settings(allow_notifications=True, slack_allowed_channels="#sre-incidents", _env_file=None)
    out = _tools(s)["slack_post"](channel="#random", text="hi")
    assert out["ok"] is False and "allow-listed" in out["error"]


def test_teams_post_refused_when_disabled():
    out = _tools(Settings(allow_notifications=False, _env_file=None))["teams_post"](text="hi")
    assert out["ok"] is False
