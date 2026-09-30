"""Pluggable search client for discovery queries.

Provider: Serper (https://serper.dev). ``SearchClient`` is a Protocol so other
providers can be swapped in later without touching callers. ``run_capped_search``
enforces a per-run query cap so a config mistake (or a huge title/region matrix)
can't blow through an API budget.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, Self

import httpx

USER_AGENT = "jobhunt/0.1 (+https://github.com/olinares/jobhunt)"
SERPER_URL = "https://google.serper.dev/search"


class SearchClient(Protocol):
    def search(self, query: str, *, count: int = 10) -> list[str]:
        """Run one search query and return result URLs (best-effort order preserved)."""
        ...


class SearchConfigError(Exception):
    """Raised when the search client can't be configured, e.g. a missing API key."""


class SerperClient:
    """SearchClient backed by Serper's /search endpoint (X-API-KEY header)."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        if api_key is None:
            import os

            api_key = os.environ.get("SEARCH_API_KEY")
        if not api_key:
            raise SearchConfigError(
                "SEARCH_API_KEY is not set. Export it or pass api_key= explicitly."
            )
        self._api_key = api_key
        self._client = httpx.Client(
            transport=transport,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
        )

    def search(self, query: str, *, count: int = 10) -> list[str]:
        response = self._client.post(
            SERPER_URL,
            json={"q": query, "num": count},
            headers={"X-API-KEY": self._api_key, "Content-Type": "application/json"},
        )
        response.raise_for_status()
        data = response.json()
        results = data.get("organic", [])
        urls: list[str] = []
        for result in results:
            link = result.get("link")
            if link:
                urls.append(link)
        return urls[:count]

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


@dataclass
class SearchRunReport:
    """Summary of a capped, multi-query search run."""

    queries_run: int = 0
    queries_skipped: int = 0
    urls: list[str] = field(default_factory=list)

    @property
    def capped(self) -> bool:
        return self.queries_skipped > 0


def run_capped_search(
    client: SearchClient,
    queries: list[str],
    *,
    max_queries: int,
    count: int = 10,
) -> SearchRunReport:
    """Run ``queries`` against ``client``, stopping after ``max_queries`` and reporting the cap."""
    report = SearchRunReport()
    for i, query in enumerate(queries):
        if i >= max_queries:
            report.queries_skipped = len(queries) - i
            break
        report.urls.extend(client.search(query, count=count))
        report.queries_run += 1
    return report
