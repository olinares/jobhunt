# jobhunt — project context for Claude Code

## What this is
A job-discovery pipeline for Oz's search for Solutions Engineer / Forward Deployed Engineer roles.
It finds company job boards on Greenhouse, Lever, Ashby, Workday and Gem, pulls every posting,
filters by title and region, scores fit, emails a daily digest, and (later) exposes it all as an MCP server.
The repo doubles as a public portfolio piece, so code quality and the README matter.

## Working rules
- Always start in plan mode. Present the plan, wait for approval, then build.
- `src/jobhunt/models.py` is the shared contract. Do not change it without asking first;
  parallel agents depend on it.
- Each agent touches only the files listed in its brief under `agents/`.
- Tests never hit the network. Record real API responses once into `tests/fixtures/` and test against those.
- Be polite to the ATS endpoints: one request at a time per host, ~1s delay, descriptive User-Agent,
  retries with backoff on 429/5xx.
- No secrets in the repo. API keys come from environment variables (`.env` is gitignored).
- Tailoring (Phase 4+) may only use facts from `facts/verified.md`. Never invent or estimate metrics.

## Git workflow (applies to every change, every phase)
- Never commit to `main`. The only exception was the initial scaffold commit.
- One branch and one PR per brief or task. Branch names: `wave1/a-json-adapters`, `phase3/digest`, `fix/<short-name>`.
- Before opening a PR: rebase on the latest `main`, run `ruff check .`, `ruff format --check .` and `pytest -q`.
- Open PRs with `gh pr create`, filling in `.github/pull_request_template.md`.
- Never merge a PR. Oz reviews and merges. CI must be green first.
- If a PR depends on another, say so in the PR and wait for that one to merge, then rebase.
- Review feedback gets new commits on the same branch, not a new PR.

## Stack
Python 3.12, httpx, pydantic-free dataclasses, pyyaml, pytest, ruff. SQLite now, Postgres in Phase 3.
MCP server in Phase 4 uses FastMCP from the official Python SDK.

## Commands
- `pip install -e ".[dev]"`
- `pytest -q`
- `ruff check . && ruff format --check .`
