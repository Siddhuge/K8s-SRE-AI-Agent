"""Shared HTTP client with sane timeouts + retry for upstream observability APIs."""
from __future__ import annotations

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


@retry(
    retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
    wait=wait_exponential(multiplier=0.5, max=4),
    stop=stop_after_attempt(3),
    reraise=True,
)
def get_json(url: str, *, params: dict | None = None, headers: dict | None = None) -> dict:
    with httpx.Client(timeout=_TIMEOUT) as client:
        resp = client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        return resp.json()


@retry(
    retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
    wait=wait_exponential(multiplier=0.5, max=4),
    stop=stop_after_attempt(3),
    reraise=True,
)
def post_json(url: str, *, json: dict, headers: dict | None = None) -> dict:
    with httpx.Client(timeout=_TIMEOUT) as client:
        resp = client.post(url, json=json, headers=headers)
        resp.raise_for_status()
        return resp.json() if resp.content else {}
