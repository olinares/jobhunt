"""One run of the pipeline: seeds + discovery -> fetch every board -> filter -> store.

Everything with side effects (store, adapters, search client, clock) is passed in, so a
whole run can be exercised in tests against recorded fixtures.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
import yaml

from jobhunt.adapters.workday import WorkdayAdapter
from jobhunt.config import ConfigError, RolesConfig
from jobhunt.discovery.queries import build_queries
from jobhunt.discovery.search import SearchClient, SearchRunReport, run_capped_search
from jobhunt.discovery.slugs import board_from_url
from jobhunt.filter import matches
from jobhunt.models import ATS, Adapter, BoardNotFound, BoardRef, Job
from jobhunt.store import SqliteStore


@dataclass
class BoardResult:
    """What happened to one board during a run."""

    board: BoardRef
    fetched: int = 0
    matched: int = 0
    new: int = 0
    closed: int = 0
    error: str | None = None


@dataclass
class RunReport:
    boards: list[BoardResult] = field(default_factory=list)
    matched_jobs: list[Job] = field(default_factory=list)
    new_jobs: list[Job] = field(default_factory=list)
    discovered: list[BoardRef] = field(default_factory=list)
    search: SearchRunReport | None = None
    search_error: str | None = None
    pruned: list[str] = field(default_factory=list)

    @property
    def failed(self) -> list[BoardResult]:
        return [b for b in self.boards if b.error]

    @property
    def all_failed(self) -> bool:
        return bool(self.boards) and len(self.failed) == len(self.boards)


def load_seeds(path: str | Path) -> list[BoardRef]:
    """Read seed boards: a `boards:` list of `{url, company}` entries."""
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc

    seeds: list[BoardRef] = []
    for i, entry in enumerate(data.get("boards") or []):
        url = entry.get("url") if isinstance(entry, dict) else None
        board = board_from_url(url) if isinstance(url, str) else None
        if board is None:
            raise ConfigError(f"{path}: boards[{i}] is not a recognised job-board URL: {url!r}")
        seeds.append(replace(board, company_name=entry.get("company") or None))
    return seeds


def discover(
    store: SqliteStore,
    client: SearchClient,
    titles: list[str],
    region_terms: list[str],
    *,
    max_queries: int,
    now: datetime,
) -> tuple[list[BoardRef], SearchRunReport]:
    """Search for boards and register the ones the store doesn't know yet.

    The query list is rotated by date, so a daily cap of `max_queries` walks through every
    domain x title x region combination over successive days instead of repeating the first N.
    """
    queries = build_queries(titles, region_terms)
    if not queries:
        return [], SearchRunReport()
    start = now.date().toordinal() * max_queries % len(queries)
    report = run_capped_search(client, queries[start:] + queries[:start], max_queries=max_queries)

    # Known boards are skipped: upsert_board would overwrite a seed's company name with None.
    known = {board.key() for board in store.boards_to_poll()}
    found: list[BoardRef] = []
    for url in report.urls:
        board = board_from_url(url)
        if board is None or board.key() in known:
            continue
        known.add(board.key())
        store.upsert_board(board, now=now)
        found.append(board)
    return found, report


def poll_board(
    board: BoardRef,
    adapter: Adapter,
    cfg: RolesConfig,
    store: SqliteStore,
    *,
    now: datetime,
) -> tuple[BoardResult, list[Job], list[Job]]:
    """Fetch one board, keep the relevant jobs, and record everything in the store.

    Returns the board's result, its matched jobs, and the subset that is new.
    """
    jobs = adapter.fetch(board)
    results = {job.uid: matches(job, cfg) for job in jobs}

    # Workday lists multi-location jobs as "6 Locations", which leaves no location to match.
    # Fetch details for just the jobs whose title matched, then filter them again.
    if isinstance(adapter, WorkdayAdapter):
        unknown = [
            job
            for job in jobs
            if results[job.uid].title_rule and not job.locations and job.remote is None
        ]
        if unknown:
            adapter.add_details(board, unknown)
            results.update({job.uid: matches(job, cfg) for job in unknown})

    matched = [job for job in jobs if results[job.uid].matched]
    new_uids = set(store.upsert_jobs(matched, now=now))
    closed = store.mark_closed(board.key(), {job.uid for job in jobs}, now=now)
    store.upsert_board(board, now=now)
    if matched:
        # Only real hits: record_relevant_hits also stamps last_relevant_hit, which pruning reads.
        store.record_relevant_hits(board.key(), len(matched), when=now)

    new_jobs = [job for job in matched if job.uid in new_uids]
    result = BoardResult(
        board, fetched=len(jobs), matched=len(matched), new=len(new_jobs), closed=len(closed)
    )
    return result, matched, new_jobs


def run(
    store: SqliteStore,
    adapters: dict[ATS, Adapter],
    cfg: RolesConfig,
    *,
    seeds: Sequence[BoardRef] = (),
    search_client: SearchClient | None = None,
    region_terms: Sequence[str] = (),
    max_queries: int = 20,
    prune_weeks: int | None = None,
    now: datetime | None = None,
) -> RunReport:
    """Register seeds, optionally discover more boards, then poll every board in the store."""
    now = now or datetime.now(UTC)
    report = RunReport()

    for seed in seeds:
        store.upsert_board(seed, now=now)

    if search_client is not None:
        try:
            report.discovered, report.search = discover(
                store,
                search_client,
                list(cfg.titles),
                list(region_terms),
                max_queries=max_queries,
                now=now,
            )
        except httpx.HTTPError as exc:
            report.search_error = f"{type(exc).__name__}: {exc}"

    for board in store.boards_to_poll():
        try:
            result, matched, new_jobs = poll_board(board, adapters[board.ats], cfg, store, now=now)
        except BoardNotFound:
            report.boards.append(BoardResult(board, error="board not found"))
            continue
        except Exception as exc:  # noqa: BLE001 -- one bad board must never stop the run
            report.boards.append(BoardResult(board, error=f"{type(exc).__name__}: {exc}"))
            continue
        report.boards.append(result)
        report.matched_jobs.extend(matched)
        report.new_jobs.extend(new_jobs)

    if prune_weeks is not None:
        report.pruned = store.prune_boards(prune_weeks, now=now)
    return report
