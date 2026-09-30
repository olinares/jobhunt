# Agent F — Postgres store + Phase 3 schema

## Branch / PR
`phase3/f-postgres-store` → one PR into `main`.

## Agent model
`opus`: a refactor across two SQL dialects plus migrations; subtle bugs are costly.

## Owns
src/jobhunt/store.py, tests/test_store.py, pyproject.toml (dependency lines only),
.github/workflows/ci.yml (Postgres service + `TEST_DATABASE_URL` only)

## Build
- Keep the `Store` protocol and `SqliteStore` behaviour; every existing test must still pass.
- Add `PostgresStore` (psycopg 3, `psycopg[binary]`) sharing the same SQL. Factor the common code
  into a base class; the dialect differences (placeholder, identity column, row access) live in
  one small place each.
- `open_store(target: str | Path) -> Store`: `postgres://` / `postgresql://` → `PostgresStore`,
  anything else → `SqliteStore` path.
- Schema migrations: a `schema_version` table and an ordered list of migrations, applied on open.
  Migration 1 is today's schema (so existing `jobhunt.db` files upgrade in place); migration 2 adds:
  - jobs: `description_html TEXT`, `score_variant TEXT` (`se`|`fde`), `score_reason TEXT`,
    `pay_suspect` (boolean/int, default false), `scored_at TEXT`. `score` already exists.
  - `digests(id identity PK, sent_at TEXT NOT NULL)`
  - `digest_items(digest_id FK, n INTEGER, uid FK, PRIMARY KEY (digest_id, n))`
- `upsert_jobs` stores `Job.description_html`; on update it keeps the stored description when the
  incoming one is `None` (`COALESCE(excluded.description_html, jobs.description_html)`).
- New methods (add to the protocol too):
  - `jobs_to_score(limit: int) -> list[Job]` — open, status `new`, `score IS NULL`, oldest first,
    rebuilt as full `Job`s (board joined from `companies`, description included).
  - `save_score(uid: str, score: Score, *, now=None) -> None` — `KeyError` if no such job.
  - `jobs_for_digest() -> list[ScoredJob]` — scored, open, status `new`, never in any digest;
    highest score first.
  - `record_digest(uids: list[str], *, now=None) -> int` — one `digests` row plus items numbered
    1..N in the given order; returns the digest id.
  - `digest_uid(digest_id: int, n: int) -> str | None` — for Phase 4 ("approve 3 and 7").
  - `latest_digest_id() -> int | None`.
- `Score` and `ScoredJob` come from `jobhunt.models`. Do not change models.py.

## Tests
- The whole store suite runs against SQLite always, and also against Postgres when
  `TEST_DATABASE_URL` is set (parametrized fixture; each test gets a clean schema).
- CI: add a `postgres:16` service to `ci.yml` and set `TEST_DATABASE_URL` for the pytest step.
- Cover: migrating a database created with the old schema, description kept on re-upsert,
  scoring round-trip, digest numbering, a job already sent never reappears in `jobs_for_digest`.
- No network other than the local Postgres service.

## Done when
Both backends pass the same suite in CI. PR open with the template filled in, CI green.
