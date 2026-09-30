# Agent J — Store read queries (for the MCP server)

## Branch / PR
`phase4/j-store-queries` → one PR into `main`. Runs in parallel with K.

## Agent model
`sonnet`: read-only SQL added to a well-patterned file that already handles both dialects.

## Owns
src/jobhunt/store.py, tests/test_store.py

## Build
Add to the `Store` protocol and implement once in `_SqlStore` (shared SQL, both backends).
No migration: every column already exists. Do not change models.py.

- Two frozen dataclasses in store.py:
  - `JobRecord(job: Job, score: Score | None, status: str, first_seen: datetime,
    closed_at: datetime | None)` — `score` is `None` when the job has no score yet
    (`_row_to_score` would crash on a NULL; guard it).
  - `BoardRecord(board: BoardRef, first_seen: datetime, last_checked: datetime | None,
    last_relevant_hit: datetime | None)` — the full `BoardRef`, so Workday `host`/`site` survive
    and the MCP server can poll the board again.
- `get_job(uid: str) -> JobRecord | None` — `None` for an unknown uid (not `KeyError`).
- `search_jobs(*, query: str | None = None, statuses: Collection[str] | None = None,
  min_score: int | None = None, variant: str | None = None, remote: bool | None = None,
  include_closed: bool = False, limit: int = 50) -> list[JobRecord]`
  - `query` matches title or company, case-insensitive.
  - `statuses=None` means any status; an empty collection returns `[]` without running SQL
    (`IN ()` is a syntax error). An unknown status raises `ValueError` (same as `set_status`).
  - Pipeline view is `search_jobs(statuses=[...], include_closed=True)`: a closed posting keeps
    its `applied` status and must still show.
- `list_boards() -> list[BoardRecord]` — every registered board, ordered by key.
- `digest_items(digest_id: int) -> list[tuple[int, str]]` — `(n, uid)` in order; `[]` for an
  unknown or empty digest.

## Traps (both dialects)
- `PostgresStore._sql` rewrites every `?` to `%s`, and psycopg treats any `%` in the SQL text as a
  placeholder. Never write a literal `%` in SQL. Put wildcards in the parameter:
  `LOWER(jobs.title) LIKE ? ESCAPE '!'` with `f"%{escaped}%"`, escaping the user's `%`, `_` and
  `!` with `!`. No backslash escapes.
- NULL ordering differs (SQLite: NULLs first on ASC, last on DESC; Postgres: the reverse). Use
  `ORDER BY jobs.score IS NULL, jobs.score DESC, jobs.first_seen, jobs.uid`, the same pattern
  `boards_to_poll` uses.
- `remote` is stored as INTEGER: bind `int(remote)`.
- Timestamps come back as ISO strings; parse with `datetime.fromisoformat` like the existing
  row helpers.

## Tests
Extend tests/test_store.py; the existing parametrized fixture runs every test on SQLite and on
Postgres when `TEST_DATABASE_URL` is set. Cover: unscored job → `score=None`; `query` containing
`%` and `_` matches literally; case-insensitive match; each filter alone and combined;
`include_closed`; empty and invalid `statuses`; score ordering with NULLs last on both backends;
Workday board round-trips through `list_boards` with host/site; `digest_items` order and empty.
No network other than the local Postgres service.

## Done when
Both backends pass in CI. PR open with the template filled in, CI green.
