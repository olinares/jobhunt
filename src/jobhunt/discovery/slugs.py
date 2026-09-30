"""Extract a BoardRef from a job-board or job-posting URL.

Supported ATS hosts: boards.greenhouse.io, job-boards.greenhouse.io, jobs.lever.co,
jobs.ashbyhq.com, *.wdN.myworkdayjobs.com (Workday), jobs.gem.com.

Notes on casing (fixed decision #4):
- Greenhouse, Lever, Ashby and Gem board slugs are taken verbatim from the URL path.
  These ATSes route case-sensitively in practice (a wrong-case slug 404s), so we do not
  normalize case here — we keep whatever the discovered URL contained.
- Workday tenants and site names are also kept verbatim. The *host* portion (which
  includes the tenant) is lowercased implicitly because URL authorities are already
  emitted lowercase by every real Workday URL we've seen; we do not force-lowercase it
  ourselves so an unusual mixed-case tenant would still round-trip.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from jobhunt.models import ATS, BoardRef

_GREENHOUSE_HOSTS = {"boards.greenhouse.io", "job-boards.greenhouse.io"}

# {tenant}.wd{N}.myworkdayjobs.com
_WORKDAY_HOST_RE = re.compile(r"^(?P<tenant>[^.]+)\.wd\d+\.myworkdayjobs\.com$")

# Workday locale segments look like en-US, fr-FR, pt-BR, etc.
_LOCALE_RE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")


def _strip_slashes(path: str) -> list[str]:
    return [p for p in path.split("/") if p]


def board_from_url(url: str) -> BoardRef | None:
    """Return the BoardRef a job/board URL points at, or None if it isn't one we recognize."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None

    host = (parsed.hostname or "").lower()
    if not host:
        return None
    parts = _strip_slashes(parsed.path)

    if host in _GREENHOUSE_HOSTS:
        return _greenhouse(host, parts, parsed.query)

    if host == "jobs.lever.co":
        return _lever(parts)

    if host == "jobs.ashbyhq.com":
        return _ashby(parts)

    if host == "jobs.gem.com":
        return _gem(parts)

    workday_match = _WORKDAY_HOST_RE.match(host)
    if workday_match:
        return _workday(host, workday_match.group("tenant"), parts)

    return None


def _make(ats: ATS, slug: str, *, host: str | None = None, site: str | None = None) -> BoardRef:
    return BoardRef(ats=ats, slug=slug, host=host, site=site)


def _greenhouse(host: str, parts: list[str], query: str) -> BoardRef | None:
    # Standard board root or job page: /{slug} or /{slug}/jobs/{id}
    if parts:
        first = parts[0]
        if first == "embed":
            # /embed/job_board?for=slug  or  /embed/job_app?for=slug&token=...
            if len(parts) >= 2 and parts[1] in {"job_board", "job_app"}:
                slug = _query_param(query, "for")
                if slug:
                    return _make("greenhouse", slug)
            return None
        return _make("greenhouse", first)
    return None


def _query_param(query: str, name: str) -> str | None:
    for pair in query.split("&"):
        if not pair:
            continue
        key, _, value = pair.partition("=")
        if key == name and value:
            return value
    return None


def _lever(parts: list[str]) -> BoardRef | None:
    # /{slug}  /{slug}/{postingId}  /{slug}/{postingId}/apply
    if parts:
        return _make("lever", parts[0])
    return None


def _ashby(parts: list[str]) -> BoardRef | None:
    # /{slug}  /{slug}/{jobUUID}
    if parts:
        return _make("ashby", parts[0])
    return None


def _gem(parts: list[str]) -> BoardRef | None:
    # /{slug}  /{slug}/{jobId}
    if parts:
        return _make("gem", parts[0])
    return None


def _workday(host: str, tenant: str, parts: list[str]) -> BoardRef | None:
    # [locale/]{site}/job/...
    if not parts:
        return None
    idx = 0
    if _LOCALE_RE.match(parts[0]):
        idx = 1
    if len(parts) <= idx:
        return None
    site = parts[idx]
    # Must actually be a job board path, e.g. .../job/... — otherwise it's not a board URL.
    if len(parts) <= idx + 1 or parts[idx + 1] != "job":
        # Still treat the bare site root (no /job/... suffix) as a valid board reference,
        # since that's the board itself (e.g. https://acme.wd5.myworkdayjobs.com/External).
        if len(parts) == idx + 1:
            return _make("workday", tenant, host=host, site=site)
        return None
    return _make("workday", tenant, host=host, site=site)
