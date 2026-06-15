"""Incident-system tools: Jira / ServiceNow (read), Slack / Teams (post).

Read tools (search) are always available — finding the matching past incident or
the active ticket is core to RCA. The POST tools are the ONLY outward-facing,
side-effecting capability in the agent and are disabled unless explicitly enabled
AND the target channel is allow-listed.
"""
from __future__ import annotations

import base64

from ..config import get_settings
from ._http import get_json, post_json


def register(mcp) -> None:
    @mcp.tool()
    def jira_search(jql: str, limit: int = 10) -> list[dict]:
        """Search Jira issues with JQL — e.g. find the open incident or past tickets
        for the same service: 'project = OPS AND text ~ "payments CrashLoop"'."""
        settings = get_settings()
        auth = base64.b64encode(f"{settings.jira_email}:{settings.jira_token}".encode()).decode()
        data = get_json(
            f"{settings.jira_url.rstrip('/')}/rest/api/3/search",
            params={"jql": jql, "maxResults": str(limit)},
            headers={"Authorization": f"Basic {auth}"},
        )
        return [
            {"key": i["key"], "summary": i["fields"]["summary"], "status": i["fields"]["status"]["name"],
             "priority": (i["fields"].get("priority") or {}).get("name")}
            for i in data.get("issues", [])
        ]

    @mcp.tool()
    def servicenow_search(query: str, table: str = "incident", limit: int = 10) -> list[dict]:
        """Search a ServiceNow table (default `incident`) with an encoded query."""
        settings = get_settings()
        auth = base64.b64encode(f"{settings.servicenow_user}:{settings.servicenow_password}".encode()).decode()
        data = get_json(
            f"{settings.servicenow_url.rstrip('/')}/api/now/table/{table}",
            params={"sysparm_query": query, "sysparm_limit": str(limit)},
            headers={"Authorization": f"Basic {auth}", "Accept": "application/json"},
        )
        return [
            {"number": r.get("number"), "short_description": r.get("short_description"),
             "state": r.get("state"), "priority": r.get("priority")}
            for r in data.get("result", [])
        ]

    @mcp.tool()
    def slack_post(channel: str, text: str) -> dict:
        """Post an RCA summary to a Slack channel. OUTWARD-FACING: requires
        ALLOW_NOTIFICATIONS=true and the channel to be allow-listed. Refuses otherwise."""
        settings = get_settings()
        if not settings.allow_notifications:
            return {"ok": False, "error": "notifications disabled (set ALLOW_NOTIFICATIONS=true)"}
        if channel not in settings.allowed_slack_channels():
            return {"ok": False, "error": f"channel {channel!r} not allow-listed"}
        return post_json(
            "https://slack.com/api/chat.postMessage",
            json={"channel": channel, "text": text},
            headers={"Authorization": f"Bearer {settings.slack_bot_token}"},
        )

    @mcp.tool()
    def teams_post(text: str) -> dict:
        """Post an RCA summary to the configured Teams channel (incoming webhook).
        OUTWARD-FACING: requires ALLOW_NOTIFICATIONS=true."""
        settings = get_settings()
        if not settings.allow_notifications:
            return {"ok": False, "error": "notifications disabled (set ALLOW_NOTIFICATIONS=true)"}
        if not settings.teams_webhook_url:
            return {"ok": False, "error": "TEAMS_WEBHOOK_URL not configured"}
        post_json(settings.teams_webhook_url, json={"text": text})
        return {"ok": True}
