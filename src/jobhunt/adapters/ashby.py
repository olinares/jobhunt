"""Ashby job board adapter.

API: GET https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true

With ``includeCompensation=true`` each job carries a ``compensation`` object. Pay comes
from its ``summaryComponents`` (already merged across tiers by Ashby); when that has no
salary, the ``compensationTiers`` are merged instead.
"""

from __future__ import annotations

from datetime import datetime

import httpx

from jobhunt.adapters.http import PoliteClient
from jobhunt.models import ATS, BoardNotFound, BoardRef, Job

API_URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}"

_DESCRIPTION_KEYS = ("descriptionHtml", "descriptionPlain")


class AshbyAdapter:
    ats: ATS = "ashby"

    def __init__(self, client: PoliteClient | None = None) -> None:
        self._client = client or PoliteClient()

    def fetch(self, board: BoardRef, *, with_descriptions: bool = False) -> list[Job]:
        url = API_URL.format(slug=board.slug)
        try:
            data = self._client.get_json(url, params={"includeCompensation": "true"})
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise BoardNotFound(board.key()) from e
            raise
        return [
            _to_job(board, raw, with_descriptions)
            for raw in data.get("jobs", [])
            if raw.get("isListed", True)
        ]


def _to_job(board: BoardRef, raw: dict, with_descriptions: bool) -> Job:
    locations = [raw["location"]] if raw.get("location") else []
    locations += [s["location"] for s in raw.get("secondaryLocations") or [] if s.get("location")]
    pay_min, pay_max, currency, period = _pay(raw.get("compensation") or {})
    return Job(
        board=board,
        external_id=raw["id"],
        title=raw["title"].strip(),
        company=board.company_name or board.slug,
        url=raw["jobUrl"],
        locations=locations,
        remote=_remote(raw),
        description_html=(raw.get("descriptionHtml") or None) if with_descriptions else None,
        posted_at=_parse_dt(raw.get("publishedAt")),
        pay_min=pay_min,
        pay_max=pay_max,
        pay_currency=currency,
        pay_period=period,
        department=raw.get("department") or raw.get("team"),
        raw=raw if with_descriptions else _without(raw, _DESCRIPTION_KEYS),
    )


def _remote(raw: dict) -> bool | None:
    """``workplaceType`` is the precise field; ``isRemote`` is set on hybrid roles too."""
    workplace = raw.get("workplaceType")
    if workplace == "Remote":
        return True
    if workplace in ("OnSite", "Hybrid"):
        return False
    is_remote = raw.get("isRemote")
    return is_remote if isinstance(is_remote, bool) else None


def _pay(compensation: dict) -> tuple[float | None, float | None, str | None, str | None]:
    salaries = _salaries(compensation.get("summaryComponents") or [])
    if not salaries:
        tiers = compensation.get("compensationTiers") or []
        salaries = _salaries([c for tier in tiers for c in tier.get("components") or []])
    if not salaries:
        return None, None, None, None
    first = salaries[0]
    key = (first.get("currencyCode"), first.get("interval"))
    same = [c for c in salaries if (c.get("currencyCode"), c.get("interval")) == key]
    mins = [float(c["minValue"]) for c in same if c.get("minValue") is not None]
    maxs = [float(c["maxValue"]) for c in same if c.get("maxValue") is not None]
    return (
        min(mins) if mins else None,
        max(maxs) if maxs else None,
        key[0],
        _period(key[1]),
    )


def _salaries(components: list[dict]) -> list[dict]:
    return [
        c
        for c in components
        if c.get("compensationType") == "Salary"
        and (c.get("minValue") is not None or c.get("maxValue") is not None)
    ]


def _period(interval: str | None) -> str | None:
    """Ashby intervals look like "1 YEAR" or "1 HOUR"."""
    if not interval:
        return None
    count, _, unit = interval.partition(" ")
    return unit.lower() if count == "1" and unit else interval.lower()


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _without(raw: dict, keys: tuple[str, ...]) -> dict:
    return {k: v for k, v in raw.items() if k not in keys}
