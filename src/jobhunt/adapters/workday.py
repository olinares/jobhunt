"""Workday adapter.

Workday career sites are backed by an undocumented JSON API (the "CXS" API) that the
site's own front end calls. Shapes below were confirmed live against
``nvidia.wd5.myworkdayjobs.com`` (tenant ``nvidia``, site ``NVIDIAExternalCareerSite``).

Listing::

    POST https://{host}/wday/cxs/{tenant}/{site}/jobs
    {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}
    -> {"total": 23, "jobPostings": [{"title", "externalPath", "locationsText",
                                      "postedOn", "bulletFields"}, ...], "facets": [...]}

Detail::

    GET https://{host}/wday/cxs/{tenant}/{site}{externalPath}
    -> {"jobPostingInfo": {"jobDescription", "location", "additionalLocations",
                           "startDate", "jobReqId", "externalUrl", ...}, ...}

Quirks this adapter handles:

- ``limit`` above 20 is rejected with HTTP 400.
- ``total`` is only populated on the first page; later pages report ``total: 0``.
- An ``offset`` past the end does not return an empty page: Workday serves page one
  again. Paging therefore stops on ``total``, on a short page, and on any repeated
  ``externalPath``.
- An unknown site returns 404 and an unknown tenant returns 422. Both mean
  ``BoardNotFound``.
- ``locationsText`` is either a single location or a count like ``"6 Locations"``.
  The full list is only available from the detail endpoint.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

import httpx

from jobhunt.adapters.http import PoliteClient
from jobhunt.models import BoardNotFound, BoardRef, Job

MAX_PAGE_SIZE = 20  # Workday answers 400 to anything larger

_NOT_FOUND_STATUSES = {404, 422}  # 404: unknown site, 422: unknown tenant
_LOCATION_COUNT = re.compile(r"^\d+ Locations?$", re.IGNORECASE)
_DAYS_AGO = re.compile(r"^Posted (\d+) Days? Ago$", re.IGNORECASE)


class WorkdayAdapter:
    ats = "workday"

    def __init__(
        self,
        client: PoliteClient | None = None,
        *,
        page_size: int = MAX_PAGE_SIZE,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ValueError(f"page_size must be between 1 and {MAX_PAGE_SIZE}")
        self._client = client or PoliteClient()
        self._page_size = page_size
        self._clock = clock or (lambda: datetime.now(UTC))

    def fetch(self, board: BoardRef, *, with_descriptions: bool = False) -> list[Job]:
        if not board.host or not board.site:
            raise ValueError(f"Workday board {board.slug!r} needs both host and site")
        base = f"https://{board.host}/wday/cxs/{board.slug}/{board.site}"
        today = self._clock().date()

        jobs = [self._to_job(board, posting, today) for posting in self._list_postings(base)]
        if with_descriptions:
            for job in jobs:
                self._add_details(base, job)
        return jobs

    def add_details(self, board: BoardRef, jobs: list[Job]) -> None:
        """Fill descriptions and full locations for `jobs` from `board`, one request each.

        Lets callers fetch details only for the jobs that need them, e.g. listings that
        say "6 Locations" instead of naming them.
        """
        base = f"https://{board.host}/wday/cxs/{board.slug}/{board.site}"
        for job in jobs:
            self._add_details(base, job)

    def _list_postings(self, base: str) -> list[dict]:
        postings: list[dict] = []
        seen: set[str] = set()
        offset = 0
        total: int | None = None
        while True:
            payload = {
                "appliedFacets": {},
                "limit": self._page_size,
                "offset": offset,
                "searchText": "",
            }
            try:
                data = self._client.post_json(f"{base}/jobs", payload)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in _NOT_FOUND_STATUSES:
                    raise BoardNotFound(base) from exc
                raise
            page = data.get("jobPostings") or []
            if total is None:
                total = int(data.get("total") or 0)

            new = [p for p in page if p.get("externalPath") not in seen]
            seen.update(p.get("externalPath") for p in new)
            postings.extend(new)
            offset += len(page)

            # A repeated posting means we paged past the end and Workday wrapped around.
            if not page or len(new) < len(page):
                break
            if len(page) < self._page_size or offset >= total:
                break
        return postings

    def _to_job(self, board: BoardRef, posting: dict, today: date) -> Job:
        path = posting["externalPath"]
        locations_text = (posting.get("locationsText") or "").strip()
        has_single_location = bool(locations_text) and not _LOCATION_COUNT.match(locations_text)
        return Job(
            board=board,
            external_id=_external_id(path),
            title=posting.get("title", "").strip(),
            company=board.company_name or board.slug,
            url=f"https://{board.host}/{board.site}{path}",
            locations=[locations_text] if has_single_location else [],
            remote=True if "remote" in locations_text.lower() else None,
            posted_at=parse_posted_on(posting.get("postedOn"), today),
            raw=dict(posting),
        )

    def _add_details(self, base: str, job: Job) -> None:
        path = job.raw["externalPath"]
        try:
            data = self._client.get_json(f"{base}{path}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:  # closed between listing and detail
                return
            raise
        info = data.get("jobPostingInfo") or {}
        job.raw["detail"] = data
        job.description_html = info.get("jobDescription") or None
        if info.get("externalUrl"):
            job.url = info["externalUrl"]
        locations = [info["location"]] if info.get("location") else []
        locations += [loc for loc in info.get("additionalLocations") or [] if loc]
        if locations:
            job.locations = locations
        if any("remote" in loc.lower() for loc in locations):
            job.remote = True
        if info.get("startDate"):
            try:
                job.posted_at = datetime.fromisoformat(info["startDate"]).replace(tzinfo=UTC)
            except ValueError:
                pass


def _external_id(external_path: str) -> str:
    """``/job/US-CA-Santa-Clara/Some-Title_JR2021239`` -> ``JR2021239``.

    The requisition id after the last underscore is stable across title edits, unlike the
    slug. Fall back to the whole path if the shape is unexpected.
    """
    segment = external_path.rstrip("/").rsplit("/", 1)[-1]
    if "_" in segment:
        return segment.rsplit("_", 1)[-1]
    return external_path


def parse_posted_on(text: str | None, today: date) -> datetime | None:
    """Turn Workday's relative ``postedOn`` text into a UTC midnight datetime.

    ``"Posted Today"``, ``"Posted Yesterday"`` and ``"Posted N Days Ago"`` are exact;
    ``"Posted 30+ Days Ago"`` and anything unrecognised return ``None``.
    """
    if not text:
        return None
    text = text.strip()
    lowered = text.lower()
    if lowered == "posted today":
        days = 0
    elif lowered == "posted yesterday":
        days = 1
    elif match := _DAYS_AGO.match(text):
        days = int(match.group(1))
    else:
        return None
    day = today - timedelta(days=days)
    return datetime(day.year, day.month, day.day, tzinfo=UTC)
