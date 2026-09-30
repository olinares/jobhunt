# Agent G — Scorer (Haiku)

## Branch / PR
`phase3/g-scorer` → one PR into `main`.

## Agent model
`opus`: prompt and rubric design and SDK usage; this is the product's judgment.

## Owns
src/jobhunt/scoring.py, tests/test_scoring.py, tests/fixtures/scoring/,
pyproject.toml (dependency lines only)

## Build
- Dependency: `anthropic` (official Python SDK). Read the claude-api skill's Python README and
  structured-outputs section before writing the call; don't write SDK code from memory.
- `Resumes(se: str, fde: str)` and `load_resumes() -> Resumes`: env `RESUME_SE` / `RESUME_FDE`,
  falling back to `private/resumes/se.md` / `fde.md` (gitignored). Clear error if neither exists.
  Resume text never goes into the repo, fixtures, logs or test output.
- `score_job(client, job: Job, resumes: Resumes) -> Score`
  - Model `claude-haiku-4-5` (named in PLAN.md; a short classification call made many times a day).
    Keep it in one constant, overridable by env `JOBHUNT_SCORER_MODEL`, so the Phase 5 eval can
    compare it with `claude-sonnet-5-5` without code changes.
  - Structured output (JSON schema via `output_config.format`; Haiku 4.5 supports it):
    `{value: int 0-100, variant: "se"|"fde", reason: str}`. Also check `stop_reason` before
    parsing (`max_tokens`, `refusal`) and treat those as a failed score. `max_tokens` ~512.
  - Haiku 4.5 specifics: don't send `output_config.effort` (Haiku 4.5 rejects it) and don't
    enable thinking. Haiku 4.5 only caches prefixes of 4096+ tokens, so the resume block may
    fall below that and not cache. That's fine at this volume; report `cache_read_input_tokens`
    in the PR and don't pad the prompt to reach the minimum.
  - System prompt: short rubric plus both resumes, in a block with `cache_control` so a run's
    later calls reuse it. Keep that block byte-identical across calls (no dates, no job data).
  - User message: title, company, locations, remote, pay, and the description as plain text
    (stdlib `html.parser`; no new dependency). Don't truncate silently; if a description is huge,
    cap it and say so in the prompt.
  - Rubric: fit of responsibilities to the resume, seniority match, customer-facing technical
    work. `variant` = the resume that fits better. `reason` = at most two sentences, no invented
    facts about the candidate.
- `pay_suspect(job) -> bool`, computed in code and set on the returned `Score`: yearly max below
  $20k, min of 0 with a max, or a max/min ratio above 4. (Placeholder pay like $1–$2 or
  $0–$500K showed up in live runs.)
- Errors: let `anthropic` typed errors propagate from `score_job`; add
  `score_many(client, jobs, resumes, *, limit) -> tuple[dict[str, Score], list[tuple[str, str]]]`
  that scores one at a time, records `(uid, error)` for failures and keeps going. A failed job is
  simply left unscored for the next run.

## Tests
- Record 4–5 real responses once (varied: strong SE fit, strong FDE fit, poor fit, suspect pay)
  into `tests/fixtures/scoring/` with the job inputs **but not the resumes** (use placeholder
  resume text for the recording, or strip it from the saved request). Tests replay them through a
  fake client; no network.
- Cover: schema parsing, variant choice, `pay_suspect` edge cases, HTML-to-text, failure isolation
  in `score_many`, `load_resumes` env vs file precedence.

## Done when
Tests pass without network or API key. PR open with the template filled in, CI green.
Flag the per-job token counts you observed (from `usage`) in the PR.
