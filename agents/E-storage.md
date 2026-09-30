# Agent E — Storage

## Branch / PR
`wave1/e-storage` → one PR into `main`.

## Owns
src/jobhunt/store.py, tests/test_store.py

## Build
- A `Store` interface plus a SQLite implementation (Postgres comes in Phase 3, so keep SQL portable).
- Tables:
  companies(board_key PK, ats, slug, host, site, company_name, first_seen, last_checked,
            last_relevant_hit, relevant_hits)
  jobs(uid PK, board_key FK, title, company, url, locations_json, remote, pay_min, pay_max,
       pay_currency, pay_period, posted_at, first_seen, last_seen, closed_at,
       status DEFAULT 'new', score, notes)
- Methods: upsert_board, boards_to_poll, upsert_jobs (returns only NEW uids), mark_closed (jobs no
  longer on the board), set_status, prune_boards(no relevant hit in N weeks).
- Status values: new, approved, skipped, applied, interviewing, offer, rejected, closed.

## Done when
Tests cover dedupe (the same job twice → one new), closing, and pruning.
PR is open with the template filled in and CI green.
