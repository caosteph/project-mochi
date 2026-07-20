"""Web-search provider seam (Phase 8). Given an ALREADY-SCRUBBED query, return a small
list of `SearchResult`s. The scrubbing, approval, audit, and untrusted-framing all live in
the tool (app/agent/tools/web_tools.py) — this module only talks to a provider.

Providers are pluggable via `settings.web_search_provider`, so switching is a config change:
  - "tavily"     — best for agents; needs a free API key (httpx POST).
  - "duckduckgo" — no API key, no signup ($0); thinner, can rate-limit (via the `ddgs` pkg).

`client` (an httpx.Client) is injectable so the Tavily path runs offline against a fake in
tests. Results carry only public web text (title/url/snippet) — never anything personal.
"""

import httpx
from pydantic import BaseModel

from app.config import settings

_TAVILY_URL = "https://api.tavily.com/search"
_SNIPPET_CAP = 500
_TITLE_CAP = 200
_TIMEOUT = 15.0


class SearchResult(BaseModel):
    title: str
    url: str
    snippet: str


def _cap(s: str | None, n: int) -> str:
    return (s or "")[:n]


def _tavily(query: str, api_key: str | None, max_results: int, client: httpx.Client | None) -> list[SearchResult]:
    if not api_key:
        raise ValueError("tavily provider requires web_search_api_key")
    payload = {"api_key": api_key, "query": query, "max_results": max_results, "search_depth": "basic"}
    owns = client is None
    client = client or httpx.Client(timeout=_TIMEOUT)
    try:
        resp = client.post(_TAVILY_URL, json=payload)
        resp.raise_for_status()
        data = resp.json()
    finally:
        if owns:
            client.close()
    return [
        SearchResult(title=_cap(r.get("title"), _TITLE_CAP), url=r.get("url") or "", snippet=_cap(r.get("content"), _SNIPPET_CAP))
        for r in (data.get("results") or [])
    ][:max_results]


def _duckduckgo(query: str, max_results: int) -> list[SearchResult]:
    from ddgs import DDGS  # lazy import — the dep is only needed if this provider is used

    out: list[SearchResult] = []
    with DDGS() as d:
        for r in d.text(query, max_results=max_results):
            out.append(
                SearchResult(
                    title=_cap(r.get("title"), _TITLE_CAP),
                    url=r.get("href") or r.get("link") or r.get("url") or "",
                    snippet=_cap(r.get("body") or r.get("snippet"), _SNIPPET_CAP),
                )
            )
    return out


def search(
    query: str,
    *,
    provider: str | None = None,
    api_key: str | None = None,
    max_results: int | None = None,
    client: httpx.Client | None = None,
) -> list[SearchResult]:
    """Run one search with the configured (or overridden) provider. `query` must already
    be scrubbed by the caller — this function does not touch PII."""
    provider = (provider or settings.web_search_provider).lower()
    api_key = api_key if api_key is not None else settings.web_search_api_key
    max_results = max_results or settings.web_search_max_results
    if provider == "duckduckgo":
        return _duckduckgo(query, max_results)
    if provider == "tavily":
        return _tavily(query, api_key, max_results, client)
    raise ValueError(f"unknown web_search provider: {provider!r}")
