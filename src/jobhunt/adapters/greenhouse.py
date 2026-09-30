"""Greenhouse job board adapter.

API: GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true

The list endpoint has no structured pay field. Boards that enable Greenhouse's pay
transparency feature render the range into ``content`` as a ``<div class="pay-range">``
block, so pay is parsed from there when present. ``content=true`` is always requested
for that reason, even when descriptions are not wanted.
"""

from __future__ import annotations

import html
import re
from datetime import datetime

import httpx

from jobhunt.adapters.http import PoliteClient
from jobhunt.models import ATS, BoardNotFound, BoardRef, Job

API_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"

# Keys dropped from ``Job.raw`` when descriptions are not requested.
_DESCRIPTION_KEYS = ("content",)

_PAY_BLOCK_RE = re.compile(
    r'(?:<div class="title">(?P<title>[^<]*)</div>\s*)?<div class="pay-range">(?P<range>.*?)</div>',
    re.DOTALL,
)
_SPAN_RE = re.compile(r"<span[^>]*>(.*?)</span>", re.DOTALL)
_AMOUNT_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_CURRENCY_RE = re.compile(r"\b([A-Z]{3})\b")


class GreenhouseAdapter:
    ats: ATS = "greenhouse"

    def __init__(self, client: PoliteClient | None = None) -> None:
        self._client = client or PoliteClient()

    def fetch(self, board: BoardRef, *, with_descriptions: bool = False) -> list[Job]:
        url = API_URL.format(slug=board.slug)
        try:
            data = self._client.get_json(url, params={"content": "true"})
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise BoardNotFound(board.key()) from e
            raise
        return [_to_job(board, raw, with_descriptions) for raw in data.get("jobs", [])]


def _to_job(board: BoardRef, raw: dict, with_descriptions: bool) -> Job:
    content = html.unescape(raw.get("content") or "")
    pay_min, pay_max, currency, period = _parse_pay(content)
    location = (raw.get("location") or {}).get("name")
    departments = raw.get("departments") or []
    return Job(
        board=board,
        external_id=str(raw["id"]),
        title=raw["title"].strip(),
        company=raw.get("company_name") or board.company_name or board.slug,
        url=raw["absolute_url"],
        locations=[location] if location else [],
        remote=_remote(raw, location),
        description_html=(content or None) if with_descriptions else None,
        posted_at=_parse_dt(raw.get("first_published") or raw.get("updated_at")),
        pay_min=pay_min,
        pay_max=pay_max,
        pay_currency=currency,
        pay_period=period,
        department=departments[0]["name"] if departments else None,
        raw=raw if with_descriptions else _without(raw, _DESCRIPTION_KEYS),
    )


def _remote(raw: dict, location: str | None) -> bool | None:
    """Prefer a board's explicit location-type metadata; fall back to the location text."""
    for field in raw.get("metadata") or []:
        name = (field.get("name") or "").lower()
        value = field.get("value")
        if ("location type" in name or "workplace" in name) and isinstance(value, str):
            return "remote" in value.lower()
    if location and "remote" in location.lower():
        return True
    return None


def _parse_pay(
    content: str,
) -> tuple[float | None, float | None, str | None, str | None]:
    """Extract the widest pay range from Greenhouse's pay-transparency blocks.

    A posting can list one block per location or level. They are merged only when they
    share a currency and period; otherwise the first block wins.
    """
    ranges = []
    for match in _PAY_BLOCK_RE.finditer(content):
        spans = [re.sub(r"<[^>]+>", "", s).strip() for s in _SPAN_RE.findall(match["range"])]
        amounts = [float(a.replace(",", "")) for s in spans for a in _AMOUNT_RE.findall(s)]
        if not amounts:
            continue
        currency = next((c for s in spans for c in _CURRENCY_RE.findall(s)), None)
        ranges.append((min(amounts), max(amounts), currency, _period(match["title"] or "")))
    if not ranges:
        return None, None, None, None
    first = ranges[0]
    same = [r for r in ranges if r[2:] == first[2:]]
    return min(r[0] for r in same), max(r[1] for r in same), first[2], first[3]


def _period(title: str) -> str | None:
    title = title.lower()
    if "hour" in title:
        return "hour"
    if "annual" in title or "year" in title or "salary" in title:
        return "year"
    return None


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _without(raw: dict, keys: tuple[str, ...]) -> dict:
    return {k: v for k, v in raw.items() if k not in keys}
