# Scorer eval

A small labeled set for checking how well the scorer (`jobhunt.scoring`) agrees with a human
reading of the same postings. Everything in this directory is public: labels are short,
non-personal notes, and result files hold numbers only, never model reasons, raw output or
resume text.

## 1. Export

```bash
python -m jobhunt.evals export -n 50            # uses $DATABASE_URL, else jobhunt.db
python -m jobhunt.evals export -n 50 --db postgres://...
```

Picks up to 50 scored jobs with a fixed seed: about a third each from scores >= 70, 45-69 and
< 45, mixing SE and FDE. Writes `jobs.jsonl` (posting fields only) and `labels.yaml`. The stored
score, reason and variant are deliberately left out so your labels are not anchored to the model.

## 2. Label

Edit `labels.yaml` (see `labels.template.yaml` for the shape). For each job set:

- `verdict`: `strong`, `maybe` or `no`: would you apply, consider it, or pass?
- `variant`: `se` or `fde`: which resume you would send.
- `note`: optional, short, non-personal.

Rows with an empty `verdict` are skipped.

## 3. Run

```bash
python -m jobhunt.evals run --model claude-haiku-4-5
```

The model is `--model`, else `JOBHUNT_SCORER_MODEL`, else the scorer default. This needs the
private resumes (`RESUME_SE` / `RESUME_FDE` or `private/resumes/`) and `ANTHROPIC_API_KEY`, so
it runs locally and never in CI. Jobs are scored one at a time. Writes `results/<model>.md`.

## 4. Read the results

Scores map to verdicts with fixed buckets: strong >= 70, maybe 45-69, no < 45.

- **Verdict agreement**: share of jobs where the bucketed score equals your verdict; the
  confusion matrix shows which way it misses (rows are your labels).
- **Spearman rho**: rank correlation between the raw score and your verdict order
  (no < maybe < strong). Closer to 1 is better; it ignores where the bucket edges fall.
- **Variant accuracy**: share of jobs where the model picked the resume variant you did.
- **Cost and latency**: token totals, an approximate cost from list prices in
  `jobhunt/evals.py` (dated there; cache discounts ignored), and p50/p95 latency.
- **Per job**: uid, your label, the score, the predicted verdict and whether the variant
  matched. Failed jobs are counted, not quoted.
