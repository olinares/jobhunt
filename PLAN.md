# Plan

Every phase ships as pull requests (see Git workflow in CLAUDE.md). Oz merges.

## Phase 0 — Inputs (Oz)
- Confirm titles, exclusions and regions in `config/roles.yaml` (regions default: Bay Area + Remote-US).
- Finish the resume verify checklist: tick what you'd defend in gitignored `private/verified.md`
  (format: `facts/verified.example.md`). The repo is public, so real facts never get committed.

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

Runs in the cloud: GitHub Actions cron → Neon Postgres → Gmail SMTP. Resume text stays private
(gitignored `private/resumes/`, GitHub secrets in CI).
- Wave 3a (parallel): F Postgres store (agents/F-postgres-store.md), G scorer (agents/G-scorer.md),
  H digest (agents/H-digest.md), plus `fix/http-retry-timeouts` (PoliteClient retries timeouts and
  connection errors). F and G both add a dependency to pyproject.toml; the second to merge rebases.
- Wave 3b (after 3a merges): I `jobhunt daily` + daily.yml (agents/I-daily-integration.md).
- Follow-ups: #15 retries missing Workday/Gem descriptions and shows discovery status in the
  digest footer; #20 polls hosts in parallel (one request at a time per host) under a 30-minute
  time budget, and skips Workday placeholder postings.
- Optional: group near-duplicate postings. Exact duplicates are already handled (one uid per
  posting, never emailed twice), but one role posted per location, or on two ATSs, shows up as
  several digest entries, each scored. Group by company + normalized title: one entry with
  "also in NYC, Remote", scored once.

## Phase 4 — MCP server (local)
Tools: search_jobs, get_job, discover_companies, refresh_boards, update_status, build_packet, list_pipeline.
Resources: roles.yaml, verified facts (ticked items only), company registry. Prompts: morning triage, prep application.
Approvals by chat ("approve 3 and 7"), resolved against the latest digest's numbers. Claude in Chrome fills forms; Oz submits.
Done when: one real application goes from digest to submitted.

Local stdio server (FastMCP) over the same Neon database the daily digest writes. Verified facts live in
gitignored `private/verified.md`; only `[x]` items are loaded, and the Conflicts section never is.
`build_packet` is deterministic (no LLM call): job, description, resume variant, ticked facts, rules.
The chat model drafts from it.
- Wave 4a (parallel): J store read queries (agents/J-store-queries.md), K verified facts + packet
  (agents/K-facts-packet.md). No shared files.
- Wave 4b (after 4a merges): L MCP server (agents/L-mcp-server.md).

## Phase 5 — Remote + portfolio
Deploy (Streamable HTTP), add as a custom connector, one-tap Approve/Skip links in the email,
eval set of ~50 labeled jobs for the scorer, README.
Done when: the claude.ai connector works through GitHub login, an email tap approves a job,
the eval results are published, and the README tells the story.

Starts after the Phase 4 end-to-end application; bugs from it ship first as `fix/` PRs.
Decided (2026-09-30): Google Cloud Run (scales to zero, reaches Neon); the server is its own MCP
OAuth authorization server with GitHub login allowlisted to one numeric GitHub id; Approve/Skip
links are HMAC-signed and expire, GET only shows a confirmation page and POST changes the status
(`new` only), so a mail scanner prefetching links can't approve anything; verified facts reach
the host as a `VERIFIED_FACTS` secret; eval labels are public in `evals/` (no model reasons or
resume text there); daily score cap raised from 100 to 300.
- Wave 5a (parallel): M remote transport + OAuth (agents/M-remote-oauth.md), N email links
  (agents/N-email-links.md), O1 eval tooling (agents/O-evals.md), Q facts from a secret
  (agents/Q-facts-secret.md), plus `chore/score-cap-300`. Each appends to `.env.example`; merge
  M → N → Q and rebase.
- Wave 5b (after M, N, Q): P deploy (agents/P-deploy.md). Oz follows docs/deploy.md, sets
  `DEPLOY_ENABLED`, adds the connector.
- Wave 5c: O2 labels + results for claude-haiku-4-5 vs a larger model, then R README
  (agents/R-readme.md).
