"""Lever postings adapter.

API: GET https://api.lever.co/v0/postings/{slug}?mode=json

Returns a bare JSON list of postings. Pay comes from the optional ``salaryRange`` object
(``{"currency", "interval", "min", "max"}``), which many boards leave unset.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import httpx

from jobhunt.adapters.http import PoliteClient
from jobhunt.models import ATS, BoardNotFound, BoardRef, Job

API_URL = "https://api.lever.co/v0/postings/{slug}"

_DESCRIPTION_KEYS = (
    "description",
    "descriptionPlain",
    "descriptionBody",
    "descriptionBodyPlain",
    "opening",
    "openingPlain",
    "lists",
    "additional",
    "additionalPlain",
    "salaryDescription",
    "salaryDescriptionPlain",
)

# Lever intervals look like "per-year-salary" or "per-hour-wage".
_INTERVAL_RE = re.compile(r"^per-([a-z]+)-")


class LeverAdapter:
    ats: ATS = "lever"

    def __init__(self, client: PoliteClient | None = None) -> None:
        self._client = client or PoliteClient()

    def fetch(self, board: BoardRef, *, with_descriptions: bool = False) -> list[Job]:
        url = API_URL.format(slug=board.slug)
        try:
            data = self._client.get_json(url, params={"mode": "json"})
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise BoardNotFound(board.key()) from e
            raise
        return [_to_job(board, raw, with_descriptions) for raw in data]


def _to_job(board: BoardRef, raw: dict, with_descriptions: bool) -> Job:
    categories = raw.get("categories") or {}
    locations = categories.get("allLocations") or (
        [categories["location"]] if categories.get("location") else []
    )
    salary = raw.get("salaryRange") or {}
    return Job(
        board=board,
        external_id=raw["id"],
        title=raw["text"].strip(),
        company=board.company_name or board.slug,
        url=raw["hostedUrl"],
        locations=list(locations),
        remote=_remote(raw.get("workplaceType")),
        description_html=_description(raw) if with_descriptions else None,
        posted_at=_parse_epoch_ms(raw.get("createdAt")),
        pay_min=_float(salary.get("min")),
        pay_max=_float(salary.get("max")),
        pay_currency=salary.get("currency"),
        pay_period=_period(salary.get("interval")),
        department=categories.get("department") or categories.get("team"),
        raw=raw if with_descriptions else _without(raw, _DESCRIPTION_KEYS),
    )


def _remote(workplace_type: str | None) -> bool | None:
    if workplace_type == "remote":
        return True
    if workplace_type in ("onsite", "hybrid"):
        return False
    return None  # "unspecified" or missing


def _description(raw: dict) -> str | None:
    """Rebuild the full posting body the way Lever's hosted page lays it out."""
    parts = [raw.get("description") or ""]
    for section in raw.get("lists") or []:
        parts.append(f"<h3>{section.get('text', '')}</h3><ul>{section.get('content', '')}</ul>")
    parts.append(raw.get("salaryDescription") or "")
    parts.append(raw.get("additional") or "")
    body = "\n".join(p for p in parts if p)
    return body or None


def _period(interval: str | None) -> str | None:
    if not interval:
        return None
    match = _INTERVAL_RE.match(interval)
    return match[1] if match else interval


def _float(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def _parse_epoch_ms(value: object) -> datetime | None:
    if not isinstance(value, int | float):
        return None
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def _without(raw: dict, keys: tuple[str, ...]) -> dict:
    return {k: v for k, v in raw.items() if k not in keys}
