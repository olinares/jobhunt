"""Persistence layer: a `Store` protocol plus a SQLite implementation.

Postgres arrives in Phase 3, so every query here sticks to standard SQL:
``INSERT ... ON CONFLICT (...) DO UPDATE`` / ``DO NOTHING`` and ``RETURNING``
(both supported by SQLite >= 3.35 and Postgres >= 9.5), never SQLite-only
verbs like ``INSERT OR REPLACE``. The single parameter placeholder style is
centralized in `PLACEHOLDER` below; porting to psycopg is meant to be:
swap that constant to ``"%s"`` and swap `sqlite3.connect` for a psycopg
connection (the SQL strings themselves don't need to change).

Design decisions not spelled out in the brief (flagged again in the PR):

* ``record_relevant_hits(board_key, count, *, when=None)`` is an addition to
  the brief's method list. ``prune_boards`` needs something to set
  ``last_relevant_hit`` / bump ``relevant_hits``, and the brief has no
  setter for either column.
* ``upsert_board`` both registers a board *and* records that it was just
  checked: it sets ``last_checked = now`` on every call, insert or update.
  ``boards_to_poll`` relies on that to find stale boards.
* ``upsert_jobs`` receives full `Job` objects (which carry a `BoardRef`), so
  it auto-registers any board it hasn't seen yet (``INSERT ... DO NOTHING``,
  leaving ``last_checked`` alone) rather than requiring a prior
  ``upsert_board`` call. Calling ``upsert_board`` yourself first still works
  and is how you'd set ``last_checked``/discover a board with no jobs yet.
* A reappearing closed job is **reopened**, not reset to "new": ``closed_at``
  is cleared, and ``status`` only flips back to ``"new"`` if it was
  ``"closed"``. Any further-along status (``applied``, ``rejected``, ...) is
  left untouched, since the posting reappearing shouldn't erase pipeline
  progress.
* ``mark_closed`` mirrors that: it sets ``status = "closed"`` only when the
  current status is still ``"new"``; anything the user has already acted on
  keeps its status, and only ``closed_at`` is stamped.
* ``prune_boards`` treats a board as eligible when it has never had a
  relevant hit and is itself older than the cutoff (grace period since
  discovery), or its last relevant hit is older than the cutoff. Pruning
  deletes the board's jobs and then the board row, returning the deleted
  ``board_key``s.
* ``set_status`` and ``record_relevant_hits`` raise ``KeyError`` if the
  target row doesn't exist; ``set_status`` raises ``ValueError`` for an
  unrecognized status.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from json import dumps as _json_dumps
from pathlib import Path
from typing import Protocol, Self

from jobhunt.models import BoardRef, Job

# Phase 3: change this to "%s" (and swap the sqlite3 connection for a
# psycopg one) to port to Postgres. Every query below is built with this
# constant so there is exactly one place to change.
PLACEHOLDER = "?"
_P = PLACEHOLDER

STATUSES: tuple[str, ...] = (
    "new",
    "approved",
    "skipped",
    "applied",
    "interviewing",
    "offer",
    "rejected",
    "closed",
)

_STATUS_CHECK_SQL = ", ".join(f"'{s}'" for s in STATUSES)

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS companies (
    board_key TEXT PRIMARY KEY,
    ats TEXT NOT NULL,
    slug TEXT NOT NULL,
    host TEXT,
    site TEXT,
    company_name TEXT,
    first_seen TEXT NOT NULL,
    last_checked TEXT,
    last_relevant_hit TEXT,
    relevant_hits INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS jobs (
    uid TEXT PRIMARY KEY,
    board_key TEXT NOT NULL REFERENCES companies (board_key),
    title TEXT NOT NULL,
    company TEXT NOT NULL,
    url TEXT NOT NULL,
    locations_json TEXT NOT NULL DEFAULT '[]',
    remote INTEGER,
    pay_min REAL,
    pay_max REAL,
    pay_currency TEXT,
    pay_period TEXT,
    posted_at TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    closed_at TEXT,
    status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ({_STATUS_CHECK_SQL})),
    score REAL,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_board_key ON jobs (board_key);
"""

_UPSERT_BOARD_SQL = f"""
INSERT INTO companies (
    board_key, ats, slug, host, site, company_name, first_seen, last_checked, relevant_hits
) VALUES ({_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P}, 0)
ON CONFLICT (board_key) DO UPDATE SET
    ats = excluded.ats,
    slug = excluded.slug,
    host = excluded.host,
    site = excluded.site,
    company_name = excluded.company_name,
    last_checked = excluded.last_checked
"""

_ENSURE_BOARD_SQL = f"""
INSERT INTO companies (
    board_key, ats, slug, host, site, company_name, first_seen, relevant_hits
) VALUES ({_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P}, 0)
ON CONFLICT (board_key) DO NOTHING
"""

_UPSERT_JOB_SQL = f"""
INSERT INTO jobs (
    uid, board_key, title, company, url, locations_json, remote,
    pay_min, pay_max, pay_currency, pay_period, posted_at,
    first_seen, last_seen, closed_at, status
) VALUES (
    {_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P},
    {_P}, {_P}, {_P}, {_P}, {_P},
    {_P}, {_P}, NULL, 'new'
)
ON CONFLICT (uid) DO UPDATE SET
    title = excluded.title,
    company = excluded.company,
    url = excluded.url,
    locations_json = excluded.locations_json,
    remote = excluded.remote,
    pay_min = excluded.pay_min,
    pay_max = excluded.pay_max,
    pay_currency = excluded.pay_currency,
    pay_period = excluded.pay_period,
    posted_at = excluded.posted_at,
    last_seen = excluded.last_seen,
    closed_at = NULL,
    status = CASE WHEN jobs.status = 'closed' THEN 'new' ELSE jobs.status END
"""


def _to_utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def _resolve_now(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(UTC)


def _board_row(board: BoardRef, first_seen: str, last_checked: str) -> tuple:
    return (
        board.key(),
        board.ats,
        board.slug,
        board.host,
        board.site,
        board.company_name,
        first_seen,
        last_checked,
    )


def _row_to_board(row: sqlite3.Row) -> BoardRef:
    return BoardRef(
        ats=row["ats"],
        slug=row["slug"],
        host=row["host"],
        site=row["site"],
        company_name=row["company_name"],
    )


class Store(Protocol):
    """Storage interface implemented by `SqliteStore` (and, later, a Postgres store)."""

    def upsert_board(self, board: BoardRef, *, now: datetime | None = None) -> None: ...

    def boards_to_poll(
        self, *, older_than: timedelta | None = None, now: datetime | None = None
    ) -> list[BoardRef]: ...

    def upsert_jobs(self, jobs: list[Job], *, now: datetime | None = None) -> list[str]: ...

    def mark_closed(
        self, board_key: str, seen_uids: set[str], *, now: datetime | None = None
    ) -> list[str]: ...

    def set_status(self, uid: str, status: str) -> None: ...

    def record_relevant_hits(
        self, board_key: str, count: int, *, when: datetime | None = None
    ) -> None: ...

    def prune_boards(self, weeks: int, *, now: datetime | None = None) -> list[str]: ...


class SqliteStore:
    """SQLite-backed `Store`. Uses the stdlib `sqlite3` module; no new dependencies."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        with self._conn:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- boards ------------------------------------------------------------

    def upsert_board(self, board: BoardRef, *, now: datetime | None = None) -> None:
        ts = _to_utc_iso(_resolve_now(now))
        with self._conn:
            self._conn.execute(_UPSERT_BOARD_SQL, _board_row(board, ts, ts))

    def boards_to_poll(
        self, *, older_than: timedelta | None = None, now: datetime | None = None
    ) -> list[BoardRef]:
        if older_than is None:
            rows = self._conn.execute(
                "SELECT * FROM companies ORDER BY last_checked IS NOT NULL, last_checked"
            ).fetchall()
        else:
            cutoff = _to_utc_iso(_resolve_now(now) - older_than)
            rows = self._conn.execute(
                f"""
                SELECT * FROM companies
                WHERE last_checked IS NULL OR last_checked <= {_P}
                ORDER BY last_checked IS NOT NULL, last_checked
                """,
                (cutoff,),
            ).fetchall()
        return [_row_to_board(row) for row in rows]

    # -- jobs ----------------------------------------------------------------

    def upsert_jobs(self, jobs: list[Job], *, now: datetime | None = None) -> list[str]:
        if not jobs:
            return []
        ts = _to_utc_iso(_resolve_now(now))

        uids = [job.uid for job in jobs]
        placeholders = ", ".join([_P] * len(uids))
        existing = {
            row["uid"]
            for row in self._conn.execute(
                f"SELECT uid FROM jobs WHERE uid IN ({placeholders})", uids
            ).fetchall()
        }

        boards_seen: dict[str, BoardRef] = {}
        for job in jobs:
            boards_seen.setdefault(job.board.key(), job.board)

        with self._conn:
            for board in boards_seen.values():
                self._conn.execute(_ENSURE_BOARD_SQL, _board_row(board, ts, ts)[:7])

            for job in jobs:
                self._conn.execute(
                    _UPSERT_JOB_SQL,
                    (
                        job.uid,
                        job.board.key(),
                        job.title,
                        job.company,
                        job.url,
                        _json_dumps(job.locations),
                        None if job.remote is None else int(job.remote),
                        job.pay_min,
                        job.pay_max,
                        job.pay_currency,
                        job.pay_period,
                        None if job.posted_at is None else _to_utc_iso(job.posted_at),
                        ts,
                        ts,
                    ),
                )

        seen_order = dict.fromkeys(uids)
        return [uid for uid in seen_order if uid not in existing]

    def mark_closed(
        self, board_key: str, seen_uids: set[str], *, now: datetime | None = None
    ) -> list[str]:
        ts = _to_utc_iso(_resolve_now(now))
        params: list[object] = [ts, board_key]
        exclude_clause = ""
        if seen_uids:
            placeholders = ", ".join([_P] * len(seen_uids))
            exclude_clause = f"AND uid NOT IN ({placeholders})"
            params.extend(seen_uids)

        with self._conn:
            rows = self._conn.execute(
                f"""
                UPDATE jobs
                SET closed_at = {_P},
                    status = CASE WHEN status = 'new' THEN 'closed' ELSE status END
                WHERE board_key = {_P} AND closed_at IS NULL {exclude_clause}
                RETURNING uid
                """,
                params,
            ).fetchall()
        return [row["uid"] for row in rows]

    def set_status(self, uid: str, status: str) -> None:
        if status not in STATUSES:
            raise ValueError(f"invalid status {status!r}; must be one of {STATUSES}")
        with self._conn:
            cur = self._conn.execute(
                f"UPDATE jobs SET status = {_P} WHERE uid = {_P}", (status, uid)
            )
        if cur.rowcount == 0:
            raise KeyError(uid)

    # -- companies / pruning -------------------------------------------------

    def record_relevant_hits(
        self, board_key: str, count: int, *, when: datetime | None = None
    ) -> None:
        ts = _to_utc_iso(_resolve_now(when))
        with self._conn:
            cur = self._conn.execute(
                f"""
                UPDATE companies
                SET last_relevant_hit = {_P},
                    relevant_hits = relevant_hits + {_P}
                WHERE board_key = {_P}
                """,
                (ts, count, board_key),
            )
        if cur.rowcount == 0:
            raise KeyError(board_key)

    def prune_boards(self, weeks: int, *, now: datetime | None = None) -> list[str]:
        cutoff = _to_utc_iso(_resolve_now(now) - timedelta(weeks=weeks))
        candidates = [
            row["board_key"]
            for row in self._conn.execute(
                f"""
                SELECT board_key FROM companies
                WHERE (last_relevant_hit IS NOT NULL AND last_relevant_hit <= {_P})
                   OR (last_relevant_hit IS NULL AND first_seen <= {_P})
                """,
                (cutoff, cutoff),
            ).fetchall()
        ]
        if not candidates:
            return []

        placeholders = ", ".join([_P] * len(candidates))
        with self._conn:
            self._conn.execute(f"DELETE FROM jobs WHERE board_key IN ({placeholders})", candidates)
            rows = self._conn.execute(
                f"DELETE FROM companies WHERE board_key IN ({placeholders}) RETURNING board_key",
                candidates,
            ).fetchall()
        return [row["board_key"] for row in rows]
