# Agent R — Portfolio README

## Branch / PR
`phase5/r-readme` → one PR into `main`. Last in Phase 5: after P is deployed and O2's results
are merged.

## Agent model
`sonnet`: writing, from code and results that already exist.

## Owns
README.md, docs/architecture.svg (new) or a Mermaid block in README.md

## Build
The repo is a public portfolio piece for Solutions Engineer / FDE roles. A hiring manager should
understand what it does, how it works and what's interesting about it within a minute, and
be able to run it.
- **Top:** one paragraph on what it is, then the architecture diagram: ATS boards → discovery
  (Serper) → poll/filter → Neon → Haiku scoring → Gmail digest → one-tap links / remote MCP
  (Cloud Run, GitHub OAuth) → Claude in Chrome fills the form → Oz submits.
- **How a job flows:** from the email tap to "applied", with the exact status transitions.
- **Scorer evaluation:** the table from `evals/results/*.md` (agreement, Spearman, variant
  accuracy, cost per job, latency), and which model the daily run uses and why.
- **Engineering choices, briefly:**
  - Polite HTTP (one request at a time per host, backoff).
  - Idempotent digests.
  - Deterministic packet with ticked facts only.
  - Scanner-safe signed links.
  - OAuth allowlisted to one GitHub id.
  - Tests never hit the network.
- **Setup:** local (SQLite, `jobhunt run`), cloud digest (the existing secrets section), remote
  MCP (link to docs/deploy.md), Claude Code stdio (the existing Phase 4 section).
- Keep the existing accurate sections; tighten rather than duplicate. No personal facts,
  resume text or real job data beyond public posting titles.

## Done when
PR open with the template filled in, CI green. The rendered README checked on GitHub.
