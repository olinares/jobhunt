# Agent O — Scorer eval set

## Branch / PR
- **O1** `phase5/o-evals` (this agent): the tooling. Runs in parallel with M, N and Q.
- **O2** `phase5/o-eval-results` (done by the lead after Oz labels): the labels plus results for
  both models.

## Agent model
`sonnet`: straightforward tooling over the existing scorer.

## Owns
src/jobhunt/evals.py (new), evals/ (new: README.md, labels.template.yaml, results/.gitkeep),
tests/test_evals.py (new)

## Background
`jobhunt.scoring` scores a job with `build_request` → `client.messages.create` →
`parse_score`. `score_job` hides token usage, so the eval makes those three calls itself to
record tokens and latency. The resumes are private (`load_resumes()` reads env or
`private/resumes/`), so the eval runs locally and never in CI. **Everything under `evals/` is
public.**

## Build
`python -m jobhunt.evals export -n 50 [--db URL]`
- Picks up to 50 scored jobs from the store: stratified by score (≥70, 45–69, <45, roughly a
  third each) and mixing SE and FDE variants, with a fixed seed.
- Writes `evals/jobs.jsonl`: posting fields only (uid, title, company, url, locations,
  remote, pay, description text). **Never the stored score, reason or variant**: the labels
  must not be anchored to the model, and reasons were written with the resume in view.
- Writes `evals/labels.yaml` from the template: `- uid, title, company, verdict: "",
  variant: "", note: ""`. The header says: "This file is public. verdict: strong | maybe | no;
  variant: se | fde; keep notes short and non-personal."

`python -m jobhunt.evals run --model MODEL`
- Loads jobs.jsonl and labels.yaml; skips unlabeled rows (and says how many).
- Scores each job one at a time and records score, variant, input and output tokens, and
  latency.
- Score → verdict buckets are **fixed in code before any run**: strong ≥ 70, maybe 45–69,
  no < 45.
- Metrics: verdict agreement (plus a confusion matrix), Spearman ρ between score and verdict
  rank (computed by hand, no scipy), variant accuracy, total and per-job cost from a small
  price table in the module (clearly labeled as approximate, with the date), and p50/p95
  latency.
- Writes `evals/results/<model>.md`: the metrics, then a per-job table of uid | label | score
  | predicted verdict | variant ✓/✗. **No model reasons, raw outputs or error text.** Failed
  jobs are counted, not quoted.
- The model comes from `--model`, else `JOBHUNT_SCORER_MODEL`, else the scorer default.

`evals/README.md`: how to export, label, run, and read the results.

## Tests
A fake Anthropic client and a SQLite store in `tmp_path`, no network. Cover: stratified export
is deterministic and has no score or reason fields; unlabeled rows skipped; bucket mapping at
the boundaries; Spearman against a hand-computed case; results markdown has no reason text
(use a fake reason string and assert it's absent).

## Done when
PR open with the template filled in, CI green. Don't run the real eval; that's O2.
