"""Gem adapter. EXPERIMENTAL.

Gem has no documented public job-board API. This adapter calls the same unofficial GraphQL
batch endpoint that ``jobs.gem.com/{board}`` pages call from the browser. The operations
and field names were copied from Gem's front-end bundles (``ExternalJobBoardList`` and
``jobBoards`` chunks on static.gem.com) and confirmed live against ``jobs.gem.com/gem``.
Gem can change or lock this down at any time; if it breaks, re-read those bundles.

Request: ``POST https://jobs.gem.com/api/public/graphql/batch`` with a JSON *array* of
``{"operationName", "variables", "query"}`` objects. The response is an array of
``{"data": ...}`` objects in the same order, so several job details fit in one request.

- ``JobBoardList(boardId)`` lists postings; ``jobBoardExternal`` is ``null`` when the board
  doesn't exist (the endpoint still answers 200).
- ``ExternalJobPosting(boardId, extId)`` returns one posting with ``descriptionHtml``;
  ``oatsExternalJobPosting`` is ``null`` for an unknown ``extId``.

The queries below are trimmed to the fields this adapter uses.
"""

from __future__ import annotations

from datetime import UTC, datetime

from jobhunt.adapters.http import PoliteClient
from jobhunt.models import BoardNotFound, BoardRef, Job

GRAPHQL_URL = "https://jobs.gem.com/api/public/graphql/batch"
BOARD_URL = "https://jobs.gem.com/{board}/{ext_id}"
DETAIL_BATCH_SIZE = 10  # detail operations per batch request

LIST_QUERY = """
query JobBoardList($boardId: String!) {
  oatsExternalJobPostings(boardId: $boardId) {
    jobPostings {
      id
      extId
      title
      locations { id name city isoCountry isRemote extId }
      job {
        id
        department { id name extId }
        locationType
        employmentType
      }
    }
  }
  jobBoardExternal(vanityUrlPath: $boardId) {
    id
    teamDisplayName
    pageTitle
  }
}
"""

DETAIL_QUERY = """
query ExternalJobPosting($boardId: String!, $extId: String!) {
  oatsExternalJobPosting(boardId: $boardId, extId: $extId) {
    id
    title
    descriptionHtml
    extId
    startDateTs
    firstPublishedTsSec
    locations { id extId name city isoCountry isRemote }
    job {
      id
      locationType
      employmentType
      requisitionId
      teamDisplayName
      department { id extId name }
    }
    jobPostSectionHtml { introHtml outroHtml }
    compensationHtml
  }
}
"""

# Gem's job.locationType values seen live: REMOTE, HYBRID, IN_OFFICE.
_REMOTE_BY_LOCATION_TYPE = {"REMOTE": True, "HYBRID": False, "IN_OFFICE": False}


class GemError(RuntimeError):
    """The GraphQL endpoint answered with errors instead of data."""


class GemAdapter:
    """EXPERIMENTAL: built on Gem's unofficial public GraphQL endpoint (see module docstring)."""

    ats = "gem"

    def __init__(self, client: PoliteClient | None = None) -> None:
        self._client = client or PoliteClient()

    def fetch(self, board: BoardRef, *, with_descriptions: bool = False) -> list[Job]:
        data = self._run([_operation("JobBoardList", LIST_QUERY, boardId=board.slug)])[0]
        meta = data.get("jobBoardExternal")
        if meta is None:
            raise BoardNotFound(f"gem:{board.slug}")
        company = board.company_name or meta.get("teamDisplayName") or board.slug
        postings = (data.get("oatsExternalJobPostings") or {}).get("jobPostings") or []
        jobs = [_to_job(board, company, posting) for posting in postings]
        if with_descriptions:
            self._add_details(board, jobs)
        return jobs

    def _add_details(self, board: BoardRef, jobs: list[Job]) -> None:
        for start in range(0, len(jobs), DETAIL_BATCH_SIZE):
            chunk = jobs[start : start + DETAIL_BATCH_SIZE]
            operations = [
                _operation(
                    "ExternalJobPosting", DETAIL_QUERY, boardId=board.slug, extId=job.external_id
                )
                for job in chunk
            ]
            for job, data in zip(chunk, self._run(operations), strict=True):
                detail = data.get("oatsExternalJobPosting")
                if detail:  # None if the posting closed since the listing call
                    _apply_detail(job, detail)

    def _run(self, operations: list[dict]) -> list[dict]:
        """POST a batch and return each operation's ``data``, raising on GraphQL errors."""
        results = self._client.post_json(GRAPHQL_URL, operations)
        if not isinstance(results, list) or len(results) != len(operations):
            raise GemError(f"unexpected batch response shape: {str(results)[:200]}")
        out = []
        for result in results:
            if result.get("errors") and not result.get("data"):
                raise GemError(str(result["errors"])[:500])
            out.append(result.get("data") or {})
        return out


def _operation(name: str, query: str, **variables: str) -> dict:
    return {"operationName": name, "variables": variables, "query": query}


def _to_job(board: BoardRef, company: str, posting: dict) -> Job:
    job_info = posting.get("job") or {}
    locations = posting.get("locations") or []
    return Job(
        board=board,
        external_id=posting["extId"],
        title=(posting.get("title") or "").strip(),
        company=company,
        url=BOARD_URL.format(board=board.slug, ext_id=posting["extId"]),
        locations=[loc["name"] for loc in locations if loc.get("name")],
        remote=_remote(job_info.get("locationType"), locations),
        department=((job_info.get("department") or {}).get("name")) or None,
        raw=dict(posting),
    )


def _apply_detail(job: Job, detail: dict) -> None:
    job.raw["detail"] = detail
    sections = detail.get("jobPostSectionHtml") or {}
    parts = [
        sections.get("introHtml"),
        detail.get("descriptionHtml"),
        detail.get("compensationHtml"),
        sections.get("outroHtml"),
    ]
    job.description_html = "\n".join(p for p in parts if p) or None
    if published := detail.get("firstPublishedTsSec"):
        job.posted_at = datetime.fromtimestamp(published, tz=UTC)
    department = ((detail.get("job") or {}).get("department") or {}).get("name")
    if department:
        job.department = department


def _remote(location_type: str | None, locations: list[dict]) -> bool | None:
    if any(loc.get("isRemote") for loc in locations):
        return True
    return _REMOTE_BY_LOCATION_TYPE.get(location_type or "")
