"""Persistence layer: a `Store` protocol with SQLite and Postgres implementations.

Both backends share every SQL statement through `_SqlStore`. Queries stick to SQL that
SQLite (>= 3.35) and Postgres (>= 9.5) both accept: ``INSERT ... ON CONFLICT`` and
``RETURNING``, never SQLite-only verbs like ``INSERT OR REPLACE``. The dialect differences
each live in one small place:

* **placeholder**: SQL is written with ``?``; `PostgresStore._sql` rewrites it to ``%s``.
* **identity column / float type**: `_SqlStore._TYPES`, substituted into the migrations.
* **row access**: both connections return rows addressable by column name
  (`sqlite3.Row` / psycopg's `dict_row`), set up in each ``__init__``.
* **transactions and the migration lock**: `_transaction` and `_lock_schema`.

Schema changes are migrations (`_MIGRATIONS`), applied in order on open and recorded in
``schema_version``. Migration 1 is the original Phase 1 schema written with
``IF NOT EXISTS``, so a ``jobhunt.db`` created before migrations existed upgrades in place.

Design decisions not spelled out in the briefs (flagged in the PRs):

* ``record_relevant_hits(board_key, count, *, when=None)`` is an addition to the Phase 1
  method list: ``prune_boards`` needs something to set ``last_relevant_hit``.
* ``upsert_board`` both registers a board *and* records that it was just checked
  (``last_checked = now``). ``boards_to_poll`` relies on that to find stale boards.
* ``upsert_jobs`` auto-registers any board it hasn't seen yet, leaving ``last_checked``
  alone.
* A reappearing closed job is **reopened**, not reset: ``closed_at`` is cleared and
  ``status`` only flips back to ``"new"`` if it was ``"closed"``.
* ``mark_closed`` sets ``status = "closed"`` only when the status is still ``"new"``.
* ``prune_boards`` removes boards with no relevant hit within the cutoff (boards that never
  had one get a grace period from ``first_seen``), deleting their jobs first. Digest items
  pointing at those jobs go with them (``ON DELETE CASCADE``), so ``digest_uid`` returns
  ``None`` for a pruned job.
* ``set_status``, ``record_relevant_hits`` and ``save_score`` raise ``KeyError`` for a
  missing row; ``record_digest`` raises ``KeyError`` for an unknown uid and ``ValueError``
  for a repeated one, before writing anything.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from json import dumps as _json_dumps
from json import loads as _json_loads
from pathlib import Path
from typing import Any, ClassVar, Protocol, Self

import psycopg
from psycopg.rows import dict_row

from jobhunt.models import BoardRef, Job, Score, ScoredJob

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

# Each migration is a list of statements, run in one transaction together with the
# schema_version row that records it. `{float}` and `{identity_pk}` are filled per dialect.
# Never edit a migration that has shipped; append a new one.
_MIGRATIONS: list[list[str]] = [
    # 1: the Phase 1 schema (idempotent, so pre-migration databases adopt it as-is).
    [
        """
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
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS jobs (
            uid TEXT PRIMARY KEY,
            board_key TEXT NOT NULL REFERENCES companies (board_key),
            title TEXT NOT NULL,
            company TEXT NOT NULL,
            url TEXT NOT NULL,
            locations_json TEXT NOT NULL DEFAULT '[]',
            remote INTEGER,
            pay_min {{float}},
            pay_max {{float}},
            pay_currency TEXT,
            pay_period TEXT,
            posted_at TEXT,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            closed_at TEXT,
            status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ({_STATUS_CHECK_SQL})),
            score {{float}},
            notes TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_jobs_board_key ON jobs (board_key)",
    ],
    # 2: Phase 3 — descriptions, scores and numbered digests.
    [
        "ALTER TABLE jobs ADD COLUMN description_html TEXT",
        "ALTER TABLE jobs ADD COLUMN score_variant TEXT CHECK (score_variant IN ('se', 'fde'))",
        "ALTER TABLE jobs ADD COLUMN score_reason TEXT",
        "ALTER TABLE jobs ADD COLUMN pay_suspect INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE jobs ADD COLUMN scored_at TEXT",
        """
        CREATE TABLE digests (
            id {identity_pk},
            sent_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE digest_items (
            digest_id INTEGER NOT NULL REFERENCES digests (id) ON DELETE CASCADE,
            n INTEGER NOT NULL,
            uid TEXT NOT NULL REFERENCES jobs (uid) ON DELETE CASCADE,
            PRIMARY KEY (digest_id, n)
        )
        """,
        "CREATE INDEX idx_digest_items_uid ON digest_items (uid)",
    ],
]

SCHEMA_VERSION = len(_MIGRATIONS)

_UPSERT_BOARD_SQL = """
INSERT INTO companies (
    board_key, ats, slug, host, site, company_name, first_seen, last_checked, relevant_hits
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
ON CONFLICT (board_key) DO UPDATE SET
    ats = excluded.ats,
    slug = excluded.slug,
    host = excluded.host,
    site = excluded.site,
    company_name = excluded.company_name,
    last_checked = excluded.last_checked
"""

_ENSURE_BOARD_SQL = """
INSERT INTO companies (
    board_key, ats, slug, host, site, company_name, first_seen, relevant_hits
) VALUES (?, ?, ?, ?, ?, ?, ?, 0)
ON CONFLICT (board_key) DO NOTHING
"""

_UPSERT_JOB_SQL = """
INSERT INTO jobs (
    uid, board_key, title, company, url, locations_json, remote,
    pay_min, pay_max, pay_currency, pay_period, posted_at, description_html,
    first_seen, last_seen, closed_at, status
) VALUES (
    ?, ?, ?, ?, ?, ?, ?,
    ?, ?, ?, ?, ?, ?,
    ?, ?, NULL, 'new'
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
    description_html = COALESCE(excluded.description_html, jobs.description_html),
    last_seen = excluded.last_seen,
    closed_at = NULL,
    status = CASE WHEN jobs.status = 'closed' THEN 'new' ELSE jobs.status END
"""

# Columns needed to rebuild a full `Job` (plus its score, when present).
_JOB_COLUMNS_SQL = """
    jobs.uid, jobs.board_key, jobs.title, jobs.company, jobs.url, jobs.locations_json,
    jobs.remote, jobs.pay_min, jobs.pay_max, jobs.pay_currency, jobs.pay_period,
    jobs.posted_at, jobs.description_html,
    jobs.score, jobs.score_variant, jobs.score_reason, jobs.pay_suspect,
    companies.ats, companies.slug, companies.host, companies.site, companies.company_name
"""
_JOB_FROM_SQL = """
FROM jobs
JOIN companies ON companies.board_key = jobs.board_key
"""
_JOB_SELECT_SQL = "SELECT" + _JOB_COLUMNS_SQL + _JOB_FROM_SQL

# `_JOB_SELECT_SQL` plus the lifecycle columns a `JobRecord` carries.
_RECORD_SELECT_SQL = (
    "SELECT"
    + _JOB_COLUMNS_SQL.rstrip()
    + ",\n    jobs.status, jobs.first_seen, jobs.closed_at"
    + _JOB_FROM_SQL
)


def _to_utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def _resolve_now(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(UTC)


# How long a job with no description is held back from scoring (see `jobs_to_score`).
DESCRIPTION_GRACE = timedelta(days=2)


def _placeholders(n: int) -> str:
    return ", ".join(["?"] * n)


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


def _row_to_board(row: Any) -> BoardRef:
    return BoardRef(
        ats=row["ats"],
        slug=row["slug"],
        host=row["host"],
        site=row["site"],
        company_name=row["company_name"],
    )


def _row_to_job(row: Any) -> Job:
    board = _row_to_board(row)
    uid: str = row["uid"]
    prefix = f"{row['board_key']}:"
    if not uid.startswith(prefix):  # uid is always f"{board.key()}:{external_id}"
        raise ValueError(f"job uid {uid!r} does not start with its board key {prefix!r}")
    return Job(
        board=board,
        external_id=uid[len(prefix) :],
        title=row["title"],
        company=row["company"],
        url=row["url"],
        locations=_json_loads(row["locations_json"]),
        remote=None if row["remote"] is None else bool(row["remote"]),
        description_html=row["description_html"],
        posted_at=None if row["posted_at"] is None else datetime.fromisoformat(row["posted_at"]),
        pay_min=row["pay_min"],
        pay_max=row["pay_max"],
        pay_currency=row["pay_currency"],
        pay_period=row["pay_period"],
    )


def _row_to_score(row: Any) -> Score:
    return Score(
        value=int(row["score"]),
        variant=row["score_variant"],
        reason=row["score_reason"] or "",
        pay_suspect=bool(row["pay_suspect"]),
    )


def _row_to_optional_score(row: Any) -> Score | None:
    return None if row["score"] is None else _row_to_score(row)


def _parse_ts(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _like_pattern(text: str) -> str:
    """A ``LIKE`` pattern matching `text` literally as a substring (escape char ``!``)."""
    escaped = text.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    return f"%{escaped}%"


@dataclass(frozen=True)
class JobRecord:
    """A stored job with its score (``None`` until scored) and lifecycle fields."""

    job: Job
    score: Score | None
    status: str
    first_seen: datetime
    closed_at: datetime | None


@dataclass(frozen=True)
class BoardRecord:
    """A registered board with its polling bookkeeping."""

    board: BoardRef
    first_seen: datetime
    last_checked: datetime | None
    last_relevant_hit: datetime | None


def _row_to_record(row: Any) -> JobRecord:
    return JobRecord(
        job=_row_to_job(row),
        score=_row_to_optional_score(row),
        status=row["status"],
        first_seen=datetime.fromisoformat(row["first_seen"]),
        closed_at=_parse_ts(row["closed_at"]),
    )


class Store(Protocol):
    """Storage interface implemented by `SqliteStore` and `PostgresStore`."""

    def close(self) -> None: ...

    def __enter__(self) -> Self: ...

    def __exit__(self, *exc_info: object) -> None: ...

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

    def uids_missing_description(self, uids: Collection[str]) -> set[str]: ...

    def jobs_to_score(
        self,
        limit: int,
        *,
        now: datetime | None = None,
        description_grace: timedelta = DESCRIPTION_GRACE,
    ) -> list[Job]: ...

    def save_score(self, uid: str, score: Score, *, now: datetime | None = None) -> None: ...

    def jobs_for_digest(self) -> list[ScoredJob]: ...

    def record_digest(self, uids: list[str], *, now: datetime | None = None) -> int: ...

    def digest_uid(self, digest_id: int, n: int) -> str | None: ...

    def latest_digest_id(self) -> int | None: ...

    def get_job(self, uid: str) -> JobRecord | None: ...

    def search_jobs(
        self,
        *,
        query: str | None = None,
        statuses: Collection[str] | None = None,
        min_score: int | None = None,
        variant: str | None = None,
        remote: bool | None = None,
        include_closed: bool = False,
        limit: int = 50,
    ) -> list[JobRecord]: ...

    def list_boards(self) -> list[BoardRecord]: ...

    def digest_items(self, digest_id: int) -> list[tuple[int, str]]: ...


class _SqlStore:
    """Everything shared by the two backends. Subclasses supply the dialect hooks."""

    _TYPES: ClassVar[dict[str, str]]  # {float}, {identity_pk} for the migrations

    _conn: Any

    # -- dialect hooks -------------------------------------------------------

    def _sql(self, sql: str) -> str:
        """Translate the shared ``?``-placeholder SQL to this backend's style."""
        return sql

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        raise NotImplementedError

    def _lock_schema(self) -> None:
        """Serialize concurrent migrations; called first inside the migration transaction."""

    # -- helpers ---------------------------------------------------------------

    def _execute(self, sql: str, params: Sequence[object] = ()) -> Any:
        return self._conn.execute(self._sql(sql), params)

    def _fetchall(self, sql: str, params: Sequence[object] = ()) -> list[Any]:
        return self._execute(sql, params).fetchall()

    def _fetchone(self, sql: str, params: Sequence[object] = ()) -> Any:
        return self._execute(sql, params).fetchone()

    def _migrate(self) -> None:
        with self._transaction():
            self._lock_schema()
            self._execute(
                "CREATE TABLE IF NOT EXISTS schema_version "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            row = self._fetchone("SELECT MAX(version) AS version FROM schema_version")
            current = row["version"] or 0
            if current > SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema is version {current}, newer than this code "
                    f"({SCHEMA_VERSION}); upgrade jobhunt"
                )
            ts = _to_utc_iso(_resolve_now(None))
            for version in range(current + 1, SCHEMA_VERSION + 1):
                for statement in _MIGRATIONS[version - 1]:
                    self._execute(statement.format(**self._TYPES))
                self._execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (version, ts),
                )

    def schema_version(self) -> int:
        row = self._fetchone("SELECT MAX(version) AS version FROM schema_version")
        return row["version"] or 0

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- boards ------------------------------------------------------------

    def upsert_board(self, board: BoardRef, *, now: datetime | None = None) -> None:
        ts = _to_utc_iso(_resolve_now(now))
        with self._transaction():
            self._execute(_UPSERT_BOARD_SQL, _board_row(board, ts, ts))

    def boards_to_poll(
        self, *, older_than: timedelta | None = None, now: datetime | None = None
    ) -> list[BoardRef]:
        if older_than is None:
            rows = self._fetchall(
                "SELECT * FROM companies ORDER BY last_checked IS NOT NULL, last_checked"
            )
        else:
            cutoff = _to_utc_iso(_resolve_now(now) - older_than)
            rows = self._fetchall(
                """
                SELECT * FROM companies
                WHERE last_checked IS NULL OR last_checked <= ?
                ORDER BY last_checked IS NOT NULL, last_checked
                """,
                (cutoff,),
            )
        return [_row_to_board(row) for row in rows]

    # -- jobs ----------------------------------------------------------------

    def upsert_jobs(self, jobs: list[Job], *, now: datetime | None = None) -> list[str]:
        if not jobs:
            return []
        ts = _to_utc_iso(_resolve_now(now))

        uids = [job.uid for job in jobs]
        existing = {
            row["uid"]
            for row in self._fetchall(
                f"SELECT uid FROM jobs WHERE uid IN ({_placeholders(len(uids))})", uids
            )
        }

        boards_seen: dict[str, BoardRef] = {}
        for job in jobs:
            boards_seen.setdefault(job.board.key(), job.board)

        with self._transaction():
            for board in boards_seen.values():
                self._execute(_ENSURE_BOARD_SQL, _board_row(board, ts, ts)[:7])

            for job in jobs:
                self._execute(
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
                        job.description_html,
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
            exclude_clause = f"AND uid NOT IN ({_placeholders(len(seen_uids))})"
            params.extend(seen_uids)

        with self._transaction():
            rows = self._fetchall(
                f"""
                UPDATE jobs
                SET closed_at = ?,
                    status = CASE WHEN status = 'new' THEN 'closed' ELSE status END
                WHERE board_key = ? AND closed_at IS NULL {exclude_clause}
                RETURNING uid
                """,
                params,
            )
        return [row["uid"] for row in rows]

    def set_status(self, uid: str, status: str) -> None:
        if status not in STATUSES:
            raise ValueError(f"invalid status {status!r}; must be one of {STATUSES}")
        with self._transaction():
            cur = self._execute("UPDATE jobs SET status = ? WHERE uid = ?", (status, uid))
        if cur.rowcount == 0:
            raise KeyError(uid)

    # -- companies / pruning -------------------------------------------------

    def record_relevant_hits(
        self, board_key: str, count: int, *, when: datetime | None = None
    ) -> None:
        ts = _to_utc_iso(_resolve_now(when))
        with self._transaction():
            cur = self._execute(
                """
                UPDATE companies
                SET last_relevant_hit = ?,
                    relevant_hits = relevant_hits + ?
                WHERE board_key = ?
                """,
                (ts, count, board_key),
            )
        if cur.rowcount == 0:
            raise KeyError(board_key)

    def prune_boards(self, weeks: int, *, now: datetime | None = None) -> list[str]:
        cutoff = _to_utc_iso(_resolve_now(now) - timedelta(weeks=weeks))
        candidates = [
            row["board_key"]
            for row in self._fetchall(
                """
                SELECT board_key FROM companies
                WHERE (last_relevant_hit IS NOT NULL AND last_relevant_hit <= ?)
                   OR (last_relevant_hit IS NULL AND first_seen <= ?)
                """,
                (cutoff, cutoff),
            )
        ]
        if not candidates:
            return []

        placeholders = _placeholders(len(candidates))
        with self._transaction():
            self._execute(f"DELETE FROM jobs WHERE board_key IN ({placeholders})", candidates)
            rows = self._fetchall(
                f"DELETE FROM companies WHERE board_key IN ({placeholders}) RETURNING board_key",
                candidates,
            )
        return [row["board_key"] for row in rows]

    # -- scoring -------------------------------------------------------------

    def uids_missing_description(self, uids: Collection[str]) -> set[str]:
        """Which of `uids` are open jobs with no stored description (their details are due)."""
        uids = list(uids)
        if not uids:
            return set()
        rows = self._fetchall(
            f"""
            SELECT uid FROM jobs
            WHERE uid IN ({_placeholders(len(uids))})
              AND description_html IS NULL AND closed_at IS NULL
            """,
            uids,
        )
        return {row["uid"] for row in rows}

    def jobs_to_score(
        self,
        limit: int,
        *,
        now: datetime | None = None,
        description_grace: timedelta = DESCRIPTION_GRACE,
    ) -> list[Job]:
        """Open, unscored `new` jobs, oldest first.

        A job with no description waits until it is `description_grace` old, so a detail
        fetch that failed gets a few more runs to succeed before the job is scored blind.
        """
        cutoff = _to_utc_iso(_resolve_now(now) - description_grace)
        rows = self._fetchall(
            _JOB_SELECT_SQL
            + """
            WHERE jobs.closed_at IS NULL AND jobs.status = 'new' AND jobs.score IS NULL
              AND (jobs.description_html IS NOT NULL OR jobs.first_seen <= ?)
            ORDER BY jobs.first_seen, jobs.uid
            LIMIT ?
            """,
            (cutoff, limit),
        )
        return [_row_to_job(row) for row in rows]

    def save_score(self, uid: str, score: Score, *, now: datetime | None = None) -> None:
        ts = _to_utc_iso(_resolve_now(now))
        with self._transaction():
            cur = self._execute(
                """
                UPDATE jobs
                SET score = ?, score_variant = ?, score_reason = ?, pay_suspect = ?,
                    scored_at = ?
                WHERE uid = ?
                """,
                (score.value, score.variant, score.reason, int(score.pay_suspect), ts, uid),
            )
        if cur.rowcount == 0:
            raise KeyError(uid)

    # -- digests -------------------------------------------------------------

    def jobs_for_digest(self) -> list[ScoredJob]:
        rows = self._fetchall(
            _JOB_SELECT_SQL
            + """
            WHERE jobs.closed_at IS NULL AND jobs.status = 'new' AND jobs.score IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM digest_items WHERE digest_items.uid = jobs.uid)
            ORDER BY jobs.score DESC, jobs.first_seen, jobs.uid
            """
        )
        return [ScoredJob(job=_row_to_job(row), score=_row_to_score(row)) for row in rows]

    def record_digest(self, uids: list[str], *, now: datetime | None = None) -> int:
        if len(set(uids)) != len(uids):
            raise ValueError("record_digest: uids must be unique")
        ts = _to_utc_iso(_resolve_now(now))
        with self._transaction():
            if uids:
                found = {
                    row["uid"]
                    for row in self._fetchall(
                        f"SELECT uid FROM jobs WHERE uid IN ({_placeholders(len(uids))})", uids
                    )
                }
                missing = [uid for uid in uids if uid not in found]
                if missing:
                    raise KeyError(missing[0])
            digest_id = self._fetchone(
                "INSERT INTO digests (sent_at) VALUES (?) RETURNING id", (ts,)
            )["id"]
            for n, uid in enumerate(uids, start=1):
                self._execute(
                    "INSERT INTO digest_items (digest_id, n, uid) VALUES (?, ?, ?)",
                    (digest_id, n, uid),
                )
        return digest_id

    def digest_uid(self, digest_id: int, n: int) -> str | None:
        row = self._fetchone(
            "SELECT uid FROM digest_items WHERE digest_id = ? AND n = ?", (digest_id, n)
        )
        return None if row is None else row["uid"]

    def latest_digest_id(self) -> int | None:
        return self._fetchone("SELECT MAX(id) AS id FROM digests")["id"]

    # -- read queries (MCP server) -----------------------------------------------

    def get_job(self, uid: str) -> JobRecord | None:
        row = self._fetchone(_RECORD_SELECT_SQL + "WHERE jobs.uid = ?", (uid,))
        return None if row is None else _row_to_record(row)

    def search_jobs(
        self,
        *,
        query: str | None = None,
        statuses: Collection[str] | None = None,
        min_score: int | None = None,
        variant: str | None = None,
        remote: bool | None = None,
        include_closed: bool = False,
        limit: int = 50,
    ) -> list[JobRecord]:
        conditions: list[str] = []
        params: list[object] = []
        if statuses is not None:
            wanted = list(dict.fromkeys(statuses))
            unknown = [s for s in wanted if s not in STATUSES]
            if unknown:
                raise ValueError(f"unknown status: {unknown[0]!r}")
            if not wanted:
                return []
            conditions.append(f"jobs.status IN ({_placeholders(len(wanted))})")
            params.extend(wanted)
        if query:
            pattern = _like_pattern(query.lower())
            conditions.append(
                "(LOWER(jobs.title) LIKE ? ESCAPE '!' OR LOWER(jobs.company) LIKE ? ESCAPE '!')"
            )
            params.extend([pattern, pattern])
        if min_score is not None:
            conditions.append("jobs.score >= ?")
            params.append(min_score)
        if variant is not None:
            conditions.append("jobs.score_variant = ?")
            params.append(variant)
        if remote is not None:
            conditions.append("jobs.remote = ?")
            params.append(int(remote))
        if not include_closed:
            conditions.append("jobs.closed_at IS NULL")
        where = f"WHERE {' AND '.join(conditions)} " if conditions else ""
        rows = self._fetchall(
            _RECORD_SELECT_SQL
            + where
            + "ORDER BY jobs.score IS NULL, jobs.score DESC, jobs.first_seen, jobs.uid LIMIT ?",
            (*params, limit),
        )
        return [_row_to_record(row) for row in rows]

    def list_boards(self) -> list[BoardRecord]:
        rows = self._fetchall("SELECT * FROM companies ORDER BY board_key")
        return [
            BoardRecord(
                board=_row_to_board(row),
                first_seen=datetime.fromisoformat(row["first_seen"]),
                last_checked=_parse_ts(row["last_checked"]),
                last_relevant_hit=_parse_ts(row["last_relevant_hit"]),
            )
            for row in rows
        ]

    def digest_items(self, digest_id: int) -> list[tuple[int, str]]:
        rows = self._fetchall(
            "SELECT n, uid FROM digest_items WHERE digest_id = ? ORDER BY n", (digest_id,)
        )
        return [(row["n"], row["uid"]) for row in rows]


class SqliteStore(_SqlStore):
    """SQLite-backed `Store`, using the stdlib `sqlite3` module."""

    _TYPES: ClassVar[dict[str, str]] = {
        "float": "REAL",  # 8-byte in SQLite
        "identity_pk": "INTEGER PRIMARY KEY AUTOINCREMENT",
    }

    def __init__(self, path: str | Path = ":memory:") -> None:
        # autocommit=True: no implicit transactions; `_transaction` issues BEGIN/COMMIT
        # itself, which also makes the migrations' DDL transactional.
        self._conn = sqlite3.connect(str(path), autocommit=True)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        try:
            self._migrate()
        except BaseException:
            self._conn.close()
            raise

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        # IMMEDIATE takes the write lock up front: a second process waits instead of
        # failing on a read-to-write lock upgrade. That also serializes migrations.
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")


class PostgresStore(_SqlStore):
    """Postgres-backed `Store`, using psycopg 3."""

    _TYPES: ClassVar[dict[str, str]] = {
        "float": "DOUBLE PRECISION",  # Postgres REAL is 4-byte
        "identity_pk": "INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY",
    }

    def __init__(self, url: str) -> None:
        # autocommit=True so reads don't leave a transaction open and `conn.transaction()`
        # below is a real BEGIN/COMMIT rather than a savepoint.
        self._conn = psycopg.connect(url, autocommit=True, row_factory=dict_row)
        try:
            self._migrate()
        except BaseException:
            self._conn.close()
            raise

    def _sql(self, sql: str) -> str:
        # The shared SQL contains no literal "?" or "%", so this swap is exact.
        return sql.replace("?", "%s")

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._conn.transaction():
            yield

    def _lock_schema(self) -> None:
        # Arbitrary app-wide key; held until the migration transaction ends.
        self._execute("SELECT pg_advisory_xact_lock(7415381)")


def open_store(target: str | Path) -> Store:
    """Open a store: ``postgres://`` / ``postgresql://`` URLs use Postgres, anything else
    is a SQLite path (``":memory:"`` included)."""
    if isinstance(target, str) and target.startswith(("postgres://", "postgresql://")):
        return PostgresStore(target)
    return SqliteStore(target)
