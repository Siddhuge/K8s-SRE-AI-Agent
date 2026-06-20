"""Tests for the incident tools. The Slack/Teams POST tools are the agent's ONLY
outward-facing capability, so the guards matter for security: when a post is refused
(notifications disabled / channel not allow-listed / no webhook) NOTHING must be sent.
Also covers the Jira/ServiceNow read tools' auth + result mapping (mocked HTTP)."""
import k8s_sre_agent.tools.incidents as it
from k8s_sre_agent.config import Settings


def _tools(monkeypatch, settings, *, post_json=None, get_json=None):
    captured: dict = {}

    class FakeMCP:
        def tool(self, *a, **k):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    monkeypatch.setattr(it, "get_settings", lambda: settings)
    if post_json is not None:
        monkeypatch.setattr(it, "post_json", post_json)
    if get_json is not None:
        monkeypatch.setattr(it, "get_json", get_json)
    it.register(FakeMCP())
    return captured


def _no_send():
    def boom(*a, **k):
        raise AssertionError("post_json must NOT be called when a post is refused")
    return boom


def test_slack_refused_when_notifications_disabled_sends_nothing(monkeypatch):
    s = Settings(allow_notifications=False, slack_allowed_channels="#sre", _env_file=None)
    tools = _tools(monkeypatch, s, post_json=_no_send())
    r = tools["slack_post"]("#sre", "hi")
    assert r["ok"] is False and "disabled" in r["error"]


def test_slack_refused_when_channel_not_allowlisted_sends_nothing(monkeypatch):
    s = Settings(allow_notifications=True, slack_allowed_channels="#sre-incidents", _env_file=None)
    tools = _tools(monkeypatch, s, post_json=_no_send())
    r = tools["slack_post"]("#random", "hi")     # not allow-listed
    assert r["ok"] is False and "not allow-listed" in r["error"]


def test_slack_posts_when_enabled_and_allowlisted(monkeypatch):
    seen = {}

    def fake_post(url, json=None, headers=None):
        seen.update(url=url, json=json, headers=headers)
        return {"ok": True}

    s = Settings(allow_notifications=True, slack_allowed_channels="#sre",
                 slack_bot_token="xoxb-test", _env_file=None)
    tools = _tools(monkeypatch, s, post_json=fake_post)
    r = tools["slack_post"]("#sre", "RCA summary")
    assert r["ok"] is True
    assert seen["url"].endswith("/chat.postMessage")
    assert seen["json"]["channel"] == "#sre" and seen["headers"]["Authorization"] == "Bearer xoxb-test"


def test_teams_refused_paths_send_nothing(monkeypatch):
    disabled = Settings(allow_notifications=False, _env_file=None)
    tools = _tools(monkeypatch, disabled, post_json=_no_send())
    assert tools["teams_post"]("hi")["ok"] is False

    no_hook = Settings(allow_notifications=True, teams_webhook_url="", _env_file=None)
    tools = _tools(monkeypatch, no_hook, post_json=_no_send())
    r = tools["teams_post"]("hi")
    assert r["ok"] is False and "TEAMS_WEBHOOK_URL" in r["error"]


def test_teams_posts_to_webhook_when_enabled(monkeypatch):
    seen = {}
    s = Settings(allow_notifications=True, teams_webhook_url="https://hook.example/x", _env_file=None)
    tools = _tools(monkeypatch, s, post_json=lambda url, json=None, headers=None: seen.update(url=url, json=json))
    assert tools["teams_post"]("RCA")["ok"] is True
    assert seen["url"] == "https://hook.example/x" and seen["json"]["text"] == "RCA"


def test_jira_search_uses_basic_auth_and_maps_results(monkeypatch):
    seen = {}

    def fake_get(url, params=None, headers=None):
        seen.update(url=url, headers=headers)
        return {"issues": [{"key": "OPS-1", "fields": {"summary": "payments crashloop",
                                                       "status": {"name": "Open"}, "priority": {"name": "High"}}}]}

    s = Settings(jira_url="https://jira.example", jira_email="a@b.c", jira_token="tok", _env_file=None)
    tools = _tools(monkeypatch, s, get_json=fake_get)
    out = tools["jira_search"]("project = OPS")
    assert seen["headers"]["Authorization"].startswith("Basic ")
    assert out[0] == {"key": "OPS-1", "summary": "payments crashloop", "status": "Open", "priority": "High"}


def test_servicenow_search_maps_results(monkeypatch):
    s = Settings(servicenow_url="https://snow.example", servicenow_user="u", servicenow_password="p", _env_file=None)
    tools = _tools(monkeypatch, s,
                   get_json=lambda url, params=None, headers=None: {"result": [
                       {"number": "INC1", "short_description": "db down", "state": "2", "priority": "1"}]})
    out = tools["servicenow_search"]("active=true")
    assert out[0]["number"] == "INC1" and out[0]["short_description"] == "db down"
