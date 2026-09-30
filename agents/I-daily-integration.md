# Agent I — `jobhunt daily` + cron (integration, after F, G, H merge)

## Branch / PR
`phase3/i-daily` → one PR into `main`. Starts only after F, G and H are merged.

## Agent model
`opus`: wires everything together; needs the whole codebase in view.

## Owns
src/jobhunt/pipeline.py, src/jobhunt/cli.py, src/jobhunt/adapters/gem.py (make `add_details`
public, mirroring Workday), tests/test_pipeline.py, tests/test_cli.py, new tests/test_daily.py,
.github/workflows/daily.yml, README.md (Phase 3 section)

## Build
- `pipeline.py`: type against the `Store` protocol, not `SqliteStore`. Keep descriptions:
  `fetch(board, with_descriptions=True)` for greenhouse/lever/ashby (same payload, no extra
  requests). For workday and gem, call `add_details` only for new matched jobs before saving.
- `jobhunt daily` subcommand:
  1. Run the pipeline (same options as `run`).
  2. `store.jobs_to_score(limit=--max-score, default 100)` → `score_many` → `save_score`.
  3. `store.jobs_for_digest()` → `render_digest` → `send_email` → **then** `record_digest`,
     so numbers are saved only if the email went out.
  - `--dry-run`: print the text digest to stdout; don't send or record anything.
  - `--db`: path or URL, default `$DATABASE_URL`, else `jobhunt.db`; use `open_store`.
  - Exit non-zero if every board failed, the email failed, or scoring failed for every job.
    A partial failure is reported in the digest footer, not as a crash.
- `.github/workflows/daily.yml`: `schedule: cron "30 13 * * *"` (06:30 PDT / 05:30 PST) and
  `workflow_dispatch`, `concurrency: daily` (no overlap), Python 3.12, `pip install .`,
  `jobhunt daily`. Secrets passed as env: `DATABASE_URL`, `ANTHROPIC_API_KEY`, `SEARCH_API_KEY`,
  `RESUME_SE`, `RESUME_FDE`, `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `DIGEST_TO`. A step timeout.
- README: what the daily job does, the secrets to set (`gh secret set NAME`), how to dry-run
  locally, and the note that GitHub pauses scheduled workflows after 60 days with no repo
  activity.

## Tests
End-to-end on recorded fixtures: adapters → SQLite store → fake scorer client → fake SMTP.
Check that numbers are recorded only after a successful send, that a second run doesn't resend
the same jobs, and that `--dry-run` records nothing. No network.

## Done when
PR open with the template filled in, CI green. Then Oz runs the workflow once by hand
(`gh workflow run daily.yml`) before relying on the schedule.
