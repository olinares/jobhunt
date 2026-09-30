"""One run of the pipeline: seeds + discovery -> fetch every board -> filter -> store.

Matched jobs are stored with their descriptions, which the scorer needs. Greenhouse, Lever
and Ashby include them in the listing payload, so they cost nothing extra. Workday and Gem
need one extra request per posting (Gem: per batch), so those are fetched only for relevant
jobs the store holds no description for. A failed detail fetch doesn't fail the board; the
job stays without a description and is retried on the next run.

Everything with side effects (store, adapters, search client, clock) is passed in, so a
whole run can be exercised in tests against recorded fixtures.
"""

from __future__ import annotations

import queue
import time
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
import yaml

from jobhunt.adapters.gem import GemAdapter
from jobhunt.adapters.workday import WorkdayAdapter
from jobhunt.config import ConfigError, RolesConfig
from jobhunt.discovery.queries import build_queries
from jobhunt.discovery.search import SearchClient, SearchRunReport, run_capped_search
from jobhunt.discovery.slugs import board_from_url
from jobhunt.filter import matches
from jobhunt.models import ATS, Adapter, BoardNotFound, BoardRef, Job
from jobhunt.store import Store

# Boards fetched at once. Each worker takes one host's boards in turn (see `_fetch_all`).
DEFAULT_WORKERS = 8


@dataclass
class BoardResult:
    """What happened to one board during a run."""

    board: BoardRef
    fetched: int = 0
    matched: int = 0
    new: int = 0
    closed: int = 0
    error: str | None = None
    detail_error: str | None = None  # descriptions couldn't be fetched; retried next run
    seconds: float = 0.0  # wall time spent on this board


@dataclass
class RunReport:
    boards: list[BoardResult] = field(default_factory=list)
    matched_jobs: list[Job] = field(default_factory=list)
    new_jobs: list[Job] = field(default_factory=list)
    discovered: list[BoardRef] = field(default_factory=list)
    search: SearchRunReport | None = None
    search_error: str | None = None
    pruned: list[str] = field(default_factory=list)
    deferred: list[BoardRef] = field(default_factory=list)  # not started before the deadline

    @property
    def failed(self) -> list[BoardResult]:
        return [b for b in self.boards if b.error]

    @property
    def all_failed(self) -> bool:
        return bool(self.boards) and len(self.failed) == len(self.boards)

    @property
    def detail_failures(self) -> list[BoardResult]:
        return [b for b in self.boards if b.detail_error]


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
    store: Store,
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


@dataclass
class Fetched:
    """One board's listing, filtered: the network half of polling a board."""

    board: BoardRef
    jobs: list[Job]
    matched: list[Job]
    seconds: float = 0.0


def fetch_board(board: BoardRef, adapter: Adapter, cfg: RolesConfig) -> Fetched:
    """Fetch one board and keep the relevant jobs. Network only; safe to run in a thread."""
    started = time.monotonic()
    # Descriptions are free in greenhouse/lever/ashby listings; workday/gem get them later.
    detailed = isinstance(adapter, WorkdayAdapter | GemAdapter)
    jobs = adapter.fetch(board, with_descriptions=not detailed)
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
    return Fetched(board, jobs, matched, seconds=time.monotonic() - started)


def record_board(
    fetched: Fetched, adapter: Adapter, store: Store, *, now: datetime
) -> tuple[BoardResult, list[Job], list[Job]]:
    """Save one fetched board. Uses the store, so it runs on the caller's thread.

    Returns the board's result, its matched jobs, and the subset that is new.
    """
    started = time.monotonic()
    board, jobs, matched = fetched.board, fetched.jobs, fetched.matched
    new_uids = set(store.upsert_jobs(matched, now=now))
    detail_error = None
    if isinstance(adapter, WorkdayAdapter | GemAdapter):
        # Ask the store rather than using new_uids, so a job whose details failed on an
        # earlier run is retried. Jobs detailed for their locations already have a
        # description and cost no second request.
        due = store.uids_missing_description([job.uid for job in matched])
        missing = [job for job in matched if job.uid in due and not job.description_html]
        if missing:
            try:
                adapter.add_details(board, missing)
            except Exception as exc:  # noqa: BLE001 -- the listing itself succeeded
                detail_error = f"{type(exc).__name__}: {exc}"
            # Save whatever did arrive; upsert_jobs keeps NULL for the rest.
            store.upsert_jobs([job for job in missing if job.description_html], now=now)
    closed = store.mark_closed(board.key(), {job.uid for job in jobs}, now=now)
    store.upsert_board(board, now=now)
    if matched:
        # Only real hits: record_relevant_hits also stamps last_relevant_hit, which pruning reads.
        store.record_relevant_hits(board.key(), len(matched), when=now)

    new_jobs = [job for job in matched if job.uid in new_uids]
    result = BoardResult(
        board,
        fetched=len(jobs),
        matched=len(matched),
        new=len(new_jobs),
        closed=len(closed),
        detail_error=detail_error,
        seconds=fetched.seconds + time.monotonic() - started,
    )
    return result, matched, new_jobs


def poll_board(
    board: BoardRef,
    adapter: Adapter,
    cfg: RolesConfig,
    store: Store,
    *,
    now: datetime,
) -> tuple[BoardResult, list[Job], list[Job]]:
    """Fetch one board, keep the relevant jobs, and record everything in the store."""
    return record_board(fetch_board(board, adapter, cfg), adapter, store, now=now)


def run(
    store: Store,
    adapters: dict[ATS, Adapter],
    cfg: RolesConfig,
    *,
    seeds: Sequence[BoardRef] = (),
    search_client: SearchClient | None = None,
    region_terms: Sequence[str] = (),
    max_queries: int = 20,
    prune_weeks: int | None = None,
    now: datetime | None = None,
    workers: int = DEFAULT_WORKERS,
    deadline: float | None = None,
) -> RunReport:
    """Register seeds, optionally discover more boards, then poll every board in the store.

    Boards are fetched in parallel, one lane per host (see `_fetch_all`); everything that
    touches the store stays on this thread. `deadline` is a `time.monotonic()` value: boards
    not started by then are left for the next run, which polls them first.
    """
    now = now or datetime.now(UTC)
    report = RunReport()

    # Register new seeds (or a changed company name) only: upsert_board stamps last_checked,
    # which would push an unpolled seed to the back of the queue.
    known = {board.key(): board for board in store.boards_to_poll()}
    for seed in seeds:
        if known.get(seed.key()) != seed:
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

    # Seeds first (the hand-picked boards), then the rest least recently checked first.
    seed_keys = {seed.key() for seed in seeds}
    boards = store.boards_to_poll()
    boards = [b for b in boards if b.key() in seed_keys] + [
        b for b in boards if b.key() not in seed_keys
    ]
    done: dict[int, tuple[BoardResult, list[Job], list[Job]]] = {}
    deferred: dict[int, BoardRef] = {}
    for i, board, outcome in _fetch_all(boards, adapters, cfg, workers=workers, deadline=deadline):
        if outcome is None:
            deferred[i] = board
        elif isinstance(outcome, BoardNotFound):
            done[i] = (BoardResult(board, error="board not found"), [], [])
        elif isinstance(outcome, Exception):
            done[i] = (BoardResult(board, error=f"{type(outcome).__name__}: {outcome}"), [], [])
        else:
            try:
                done[i] = record_board(outcome, adapters[board.ats], store, now=now)
            except Exception as exc:  # noqa: BLE001 -- one bad board must never stop the run
                done[i] = (BoardResult(board, error=f"{type(exc).__name__}: {exc}"), [], [])

    # Report in poll order, whatever order the lanes finished in.
    for i in sorted(done):
        result, matched, new_jobs = done[i]
        report.boards.append(result)
        report.matched_jobs.extend(matched)
        report.new_jobs.extend(new_jobs)
    report.deferred = [deferred[i] for i in sorted(deferred)]

    if prune_weeks is not None:
        report.pruned = store.prune_boards(prune_weeks, now=now)
    return report


# The one host behind every board of these ATSs. Workday is one host per tenant.
_SHARED_HOST_ATS = {"greenhouse", "lever", "ashby", "gem"}

_Outcome = Fetched | Exception | None  # None: deferred, not started before the deadline


def _lane(board: BoardRef) -> str:
    """Boards on the same host share a lane, so each host sees one request at a time."""
    if board.ats in _SHARED_HOST_ATS or not board.host:
        return board.ats
    return board.host


def _fetch_all(
    boards: Sequence[BoardRef],
    adapters: dict[ATS, Adapter],
    cfg: RolesConfig,
    *,
    workers: int,
    deadline: float | None,
) -> Iterator[tuple[int, BoardRef, _Outcome]]:
    """Fetch boards in parallel lanes, one per host; yield (index, board, outcome) as ready.

    Each lane walks its boards in poll order on one worker thread. PoliteClient's per-host
    lock backs this up, so politeness never depends on the lane grouping alone.
    """
    lanes: dict[str, list[tuple[int, BoardRef]]] = {}
    for i, board in enumerate(boards):
        lanes.setdefault(_lane(board), []).append((i, board))

    ready: queue.Queue[tuple[int, BoardRef, _Outcome]] = queue.Queue()

    def work(lane: list[tuple[int, BoardRef]]) -> None:
        for i, board in lane:
            if deadline is not None and time.monotonic() >= deadline:
                ready.put((i, board, None))
                continue
            try:
                ready.put((i, board, fetch_board(board, adapters[board.ats], cfg)))
            except Exception as exc:  # noqa: BLE001 -- reported per board by run()
                ready.put((i, board, exc))

    with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="poll") as pool:
        futures = [pool.submit(work, lane) for lane in lanes.values()]
        for _ in boards:
            yield ready.get()
        for future in futures:
            future.result()
