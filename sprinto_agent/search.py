"""Tavily search API client. Results are LEADS ONLY: the underlying URL must be fetched and validated
before any claim is used. Search engine result pages are never scraped."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .util import get_logger

log = get_logger(__name__)
TAVILY_URL = "https://api.tavily.com/search"


class SearchUnavailable(Exception):
    """kind: quota | outage | credentials | error"""

    def __init__(self, message: str = "search unavailable", kind: str = "outage"):
        super().__init__(message)
        self.kind = kind


def _api_kind(status: int) -> str:
    if status in (401, 403):
        return "credentials"
    if status in (402, 429, 432, 433):
        return "quota"
    return "outage" if status >= 500 else "error"


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str
    published_date: Optional[str] = None


class TavilySearch:
    def __init__(self, api_key: str, timeout: float = 20.0):
        import httpx
        self._key = api_key
        self._client = httpx.Client(timeout=timeout)
        self.calls = 0

    def search(self, query: str, max_results: int = 5, include_domains: Optional[List[str]] = None,
               topic: str = "general", days: Optional[int] = None) -> List[SearchHit]:
        body: Dict[str, Any] = {"query": query, "max_results": max_results, "topic": topic,
                                "search_depth": "basic", "include_answer": False}
        if include_domains:
            body["include_domains"] = include_domains
        if days:
            body["time_range"] = "week" if days <= 7 else "month" if days <= 31 else "year"
        self.calls += 1
        try:
            r = self._client.post(TAVILY_URL, json=body, headers={"Authorization": "Bearer " + self._key})
        except Exception as e:  # network failure / timeout
            raise SearchUnavailable("Tavily unreachable (%s)" % type(e).__name__, "outage")
        if r.status_code != 200:
            raise SearchUnavailable("Tavily HTTP %d" % r.status_code, _api_kind(r.status_code))
        out = []
        for it in r.json().get("results", []) or []:
            if it.get("url"):
                out.append(SearchHit(title=it.get("title", ""), url=it["url"], snippet=(it.get("content") or "")[:500],
                                     published_date=it.get("published_date")))
        return out


class NoSearch:
    """Used when TAVILY_API_KEY is missing: search steps are skipped and the reason is recorded."""
    calls = 0

    def search(self, *a: Any, **k: Any) -> List[SearchHit]:
        raise SearchUnavailable("TAVILY_API_KEY not configured", "credentials")


class MockSearch:
    def __init__(self, hits: Dict[str, List[SearchHit]]):
        self.hits = hits
        self.calls = 0

    def search(self, query: str, **k: Any) -> List[SearchHit]:
        self.calls += 1
        for key, hits in self.hits.items():
            if key.lower() in query.lower():
                return hits
        return []
