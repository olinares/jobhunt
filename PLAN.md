# Plan

Every phase ships as pull requests (see Git workflow in CLAUDE.md). Oz merges.

## Phase 0 — Inputs (Oz)
- Confirm titles, exclusions and regions in `config/roles.yaml` (regions default: Bay Area + Remote-US).
- Finish the resume verify checklist and fill `facts/verified.md`.

## Phase 1 — Fetcher  ← Wave 1 (parallel)
Adapters for Greenhouse, Lever, Ashby, Workday, Gem; normalize to `Job`; title + region filter;
location normalization with tests; SQLite store.
Done when: running on ~10 seed companies prints only relevant jobs.

## Phase 2 — Discovery  ← Wave 1 (parallel)
Search queries (ATS domain x title x region) → extract board refs from result URLs → company registry.
Done when: the registry grows from titles + regions alone.

## Wave 2 — Integrate (single agent, after Wave 1 merges)
`jobhunt run` CLI wiring discovery → fetch → filter → store. End-to-end smoke test.

## Phase 3 — Scoring + daily digest
Haiku scores each new job against the resumes and picks SE vs FDE version; hosted Postgres;
GitHub Actions cron; numbered email digest.
Done when: the digest arrives every morning untouched.

## Phase 4 — MCP server (local)
Tools: search_jobs, get_job, discover_companies, refresh_boards, update_status, build_packet, list_pipeline.
Resources: roles.yaml, facts/verified.md, company registry. Prompts: morning triage, prep application.
Approvals by chat ("approve 3 and 7"). Claude in Chrome fills forms; Oz submits.
Done when: one real application goes from digest to submitted.

## Phase 5 — Remote + portfolio
Deploy (Streamable HTTP), add as a custom connector, one-tap Approve/Skip links in the email,
eval set of ~50 labeled jobs for the scorer, README.
