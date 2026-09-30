"""Scorer eval: export a labeling set, then measure a model's scores against the labels.

    python -m jobhunt.evals export -n 50 [--db URL]
    python -m jobhunt.evals run --model MODEL

`export` picks scored jobs from the store and writes `evals/jobs.jsonl` (posting fields
only) plus an empty `evals/labels.yaml` to fill in by hand. `run` scores the labeled jobs
with a model and writes `evals/results/<model>.md`.

Everything under `evals/` is public. The stored score, reason and variant are never
exported (labels must not be anchored to the model), and result files hold numbers and
uids only: never a model's reasons, raw output, error text or resume text.

The resumes are private, so this runs locally and never in CI.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import random
import re
import sys
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anthropic
import yaml

from jobhunt.models import BoardRef, Job
from jobhunt.scoring import (
    MODEL_ENV,
    Resumes,
    ScoringError,
    build_request,
    html_to_text,
    load_resumes,
    parse_score,
    scorer_model,
)
from jobhunt.store import open_store

EVALS_DIR = Path("evals")
SEED = 20260930

# Score -> verdict buckets. Fixed here before any run; do not tune them against results.
STRONG_MIN = 70
MAYBE_MIN = 45
VERDICTS = ("strong", "maybe", "no")  # best first
_VERDICT_RANK = {"no": 0, "maybe": 1, "strong": 2}

LABELS_HEADER = (
    "# This file is public. verdict: strong | maybe | no; variant: se | fde; "
    "keep notes short and non-personal.\n"
)

# Approximate USD per million tokens (input, output), first-party API list prices as of
# 2026-09-25. Cache discounts and premiums are ignored, so cost is a rough estimate. Unknown
# models get no cost.
PRICES_AS_OF = "2026-09-25"
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-opus-5-5": (4.0, 20.0),
}

# Extra request fields per model, so every model scores the way Haiku does: without thinking.
# Claude Sonnet 5.5 thinks by default, and thinking tokens count against the scorer's small
# max_tokens; `between_tools` turns it off (`disabled` is a 400 on that model). Claude Opus 5.5
# can't turn thinking off at all, so it is not a like-for-like comparison and has no entry.
REQUEST_EXTRAS: dict[str, dict[str, Any]] = {
    "claude-sonnet-5-5": {"thinking": {"type": "between_tools"}},
}


def verdict_for(score: int) -> str:
    """Fixed bucket mapping: strong >= 70, maybe 45-69, no < 45."""
    if score >= STRONG_MIN:
        return "strong"
    if score >= MAYBE_MIN:
        return "maybe"
    return "no"


# --------------------------------------------------------------------------- metrics


def _ranks(values: Sequence[float]) -> list[float]:
    """1-based ranks, ties sharing their average rank."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Spearman rank correlation (Pearson on average ranks). None when undefined."""
    if len(xs) != len(ys):
        raise ValueError("length mismatch")
    if len(xs) < 2:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if vx == 0 or vy == 0:
        return None
    return cov / math.sqrt(vx * vy)


def percentile(values: Sequence[float], p: float) -> float | None:
    """Nearest-rank percentile (p in 0-100)."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


def _for_model(table: Mapping[str, Any], model: str) -> Any:
    for name, value in table.items():
        if model == name or model.startswith(name + "-"):
            return value
    return None


def price_for(model: str) -> tuple[float, float] | None:
    return _for_model(PRICES_PER_MTOK, model)


def eval_request(job: Job, resumes: Resumes, model: str) -> dict[str, Any]:
    """The daily scorer's request for `model`, plus any per-model extras (REQUEST_EXTRAS)."""
    return {**build_request(job, resumes, model=model), **(_for_model(REQUEST_EXTRAS, model) or {})}


# --------------------------------------------------------------------------- export


def pick_sample(
    candidates: Sequence[tuple[str, int, str]], n: int, *, seed: int = SEED
) -> list[str]:
    """Pick up to `n` uids from (uid, score, variant): a third from each score bucket,
    mixing variants within a bucket, deterministic for a given seed and candidate set.
    Leftover quota from a small bucket goes to the others."""
    rng = random.Random(seed)
    buckets: dict[str, dict[str, list[str]]] = {v: {"se": [], "fde": []} for v in VERDICTS}
    for uid, score, variant in sorted(candidates):
        buckets[verdict_for(score)].setdefault(variant, []).append(uid)
    queues: dict[str, list[str]] = {}
    for name, by_variant in buckets.items():
        lists = [by_variant[k] for k in sorted(by_variant)]
        for lst in lists:
            rng.shuffle(lst)
        mixed: list[str] = []
        for i in range(max((len(lst) for lst in lists), default=0)):
            mixed.extend(lst[i] for lst in lists if i < len(lst))
        queues[name] = mixed
    quotas = {name: n // 3 + (1 if i < n % 3 else 0) for i, name in enumerate(VERDICTS)}
    taken = {name: min(quotas[name], len(queues[name])) for name in VERDICTS}
    spare = n - sum(taken.values())
    while spare > 0:
        grew = False
        for name in VERDICTS:
            if spare > 0 and taken[name] < len(queues[name]):
                taken[name] += 1
                spare -= 1
                grew = True
        if not grew:
            break
    return sorted(uid for name in VERDICTS for uid in queues[name][: taken[name]])


def job_record(job: Job) -> dict[str, Any]:
    """Posting fields only. No score, reason or variant."""
    return {
        "uid": job.uid,
        "title": job.title,
        "company": job.company,
        "url": job.url,
        "locations": list(job.locations),
        "remote": job.remote,
        "pay": {
            "min": job.pay_min,
            "max": job.pay_max,
            "currency": job.pay_currency,
            "period": job.pay_period,
        },
        "description": html_to_text(job.description_html),
    }


def labels_template(records: Sequence[Mapping[str, Any]]) -> str:
    rows = [
        {
            "uid": r["uid"],
            "title": r["title"],
            "company": r["company"],
            "verdict": "",
            "variant": "",
            "note": "",
        }
        for r in records
    ]
    return LABELS_HEADER + yaml.safe_dump(rows, sort_keys=False, allow_unicode=True)


def export(db: str, n: int, out_dir: Path = EVALS_DIR, *, seed: int = SEED) -> int:
    """Write jobs.jsonl and labels.yaml into `out_dir`. Returns the number of jobs picked."""
    with open_store(db) as store:
        records = store.search_jobs(min_score=0, include_closed=True, limit=1_000_000)
    scored = {
        r.job.uid: r
        for r in records
        if r.score is not None and html_to_text(r.job.description_html)
    }
    picked = pick_sample(
        [(uid, r.score.value, r.score.variant) for uid, r in scored.items() if r.score],
        n,
        seed=seed,
    )
    rows = [job_record(scored[uid].job) for uid in picked]
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "jobs.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )
    (out_dir / "labels.yaml").write_text(labels_template(rows), encoding="utf-8")
    return len(rows)


# --------------------------------------------------------------------------- run


@dataclass(frozen=True)
class Label:
    uid: str
    verdict: str
    variant: str


@dataclass(frozen=True)
class Result:
    uid: str
    label: str
    label_variant: str
    score: int | None  # None = the job failed
    variant: str | None
    input_tokens: int = 0
    output_tokens: int = 0
    latency: float = 0.0

    @property
    def predicted(self) -> str | None:
        return None if self.score is None else verdict_for(self.score)


def load_jobs(path: Path) -> dict[str, Job]:
    """Rebuild Jobs from jobs.jsonl. The description is plain text; it is escaped so that
    `html_to_text` inside the prompt builder gives the same text back."""
    jobs: dict[str, Job] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        pay = r.get("pay") or {}
        body = html.escape(r.get("description") or "", quote=False)
        jobs[r["uid"]] = Job(
            board=BoardRef("eval", "eval"),
            external_id=r["uid"],
            title=r["title"],
            company=r["company"],
            url=r.get("url", ""),
            locations=list(r.get("locations") or []),
            remote=r.get("remote"),
            description_html=body or None,
            pay_min=pay.get("min"),
            pay_max=pay.get("max"),
            pay_currency=pay.get("currency"),
            pay_period=pay.get("period"),
        )
    return jobs


def load_labels(path: Path) -> tuple[list[Label], int]:
    """Labeled rows and the number of unlabeled ones. Bad values raise ValueError."""
    rows = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    labels: list[Label] = []
    unlabeled = 0
    for row in rows:
        verdict = str(row.get("verdict") or "").strip().lower()
        variant = str(row.get("variant") or "").strip().lower()
        if not verdict:
            unlabeled += 1
            continue
        if verdict not in _VERDICT_RANK:
            raise ValueError(f"{row.get('uid')}: verdict must be strong, maybe or no")
        if variant not in ("", "se", "fde"):
            raise ValueError(f"{row.get('uid')}: variant must be se or fde")
        labels.append(Label(uid=str(row["uid"]), verdict=verdict, variant=variant))
    return labels, unlabeled


_FATAL = (
    anthropic.AuthenticationError,
    anthropic.PermissionDeniedError,
    anthropic.NotFoundError,
)


def run_eval(
    client: Any,
    jobs: Mapping[str, Job],
    labels: Sequence[Label],
    resumes: Resumes,
    model: str,
    *,
    clock: Callable[[], float] = time.perf_counter,
) -> list[Result]:
    """Score each labeled job once, one at a time, recording tokens and latency."""
    results: list[Result] = []
    stopped = False
    for lab in labels:
        job = jobs.get(lab.uid)
        failed = Result(lab.uid, lab.verdict, lab.variant, None, None)
        if job is None or stopped:
            results.append(failed)
            continue
        start = clock()
        try:
            message = client.messages.create(**eval_request(job, resumes, model))
            latency = clock() - start
            score = parse_score(message, job)
        except _FATAL:
            stopped = True  # would fail the same way for every job
            results.append(failed)
            continue
        except (anthropic.APIError, ScoringError):
            results.append(failed)
            continue
        usage = message.usage
        tokens_in = (
            (usage.input_tokens or 0)
            + (getattr(usage, "cache_read_input_tokens", 0) or 0)
            + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
        )
        results.append(
            Result(
                lab.uid,
                lab.verdict,
                lab.variant,
                score.value,
                score.variant,
                tokens_in,
                usage.output_tokens or 0,
                latency,
            )
        )
    return results


def _fmt(value: float | None, spec: str = ".2f") -> str:
    return "n/a" if value is None else format(value, spec)


def render_report(model: str, results: Sequence[Result], unlabeled: int) -> str:
    """Markdown report: metrics, confusion matrix, per-job table. Numbers and uids only."""
    ok = [r for r in results if r.score is not None]
    failed = len(results) - len(ok)
    agree = sum(1 for r in ok if r.predicted == r.label)
    rho = spearman(
        [r.score for r in ok if r.score is not None],
        [_VERDICT_RANK[r.label] for r in ok],
    )
    with_variant = [r for r in ok if r.label_variant]
    variant_hits = sum(1 for r in with_variant if r.variant == r.label_variant)
    tokens_in = sum(r.input_tokens for r in ok)
    tokens_out = sum(r.output_tokens for r in ok)
    price = price_for(model)
    latencies = [r.latency for r in ok]

    lines = [
        f"# Scorer eval: {model}",
        "",
        f"- Jobs labeled: {len(results)} ({unlabeled} unlabeled rows skipped)",
        f"- Scored: {len(ok)}, failed: {failed}",
        (
            f"- Buckets (fixed in code): strong >= {STRONG_MIN}, "
            f"maybe {MAYBE_MIN}-{STRONG_MIN - 1}, no < {MAYBE_MIN}"
        ),
        f"- Verdict agreement: {agree}/{len(ok)}" + (f" ({agree / len(ok):.0%})" if ok else ""),
        f"- Spearman rho (score vs verdict rank): {_fmt(rho)}",
        f"- Variant accuracy: {variant_hits}/{len(with_variant)}"
        + (f" ({variant_hits / len(with_variant):.0%})" if with_variant else ""),
        f"- Tokens: {tokens_in:,} input, {tokens_out:,} output",
    ]
    if price is not None and ok:
        total = (tokens_in * price[0] + tokens_out * price[1]) / 1_000_000
        lines.append(
            f"- Approximate cost: ${total:.3f} total, ${total / len(ok):.4f} per job "
            f"(list prices as of {PRICES_AS_OF}, cache discounts ignored)"
        )
    else:
        lines.append("- Approximate cost: n/a (no price for this model in the table)")
    lines.append(
        f"- Latency: p50 {_fmt(percentile(latencies, 50))}s, p95 {_fmt(percentile(latencies, 95))}s"
    )

    lines += ["", "## Confusion matrix", "", "Rows are labels, columns are predicted verdicts.", ""]
    lines += ["| label | " + " | ".join(VERDICTS) + " |", "|---|" + "---|" * len(VERDICTS)]
    counts = Counter((r.label, r.predicted) for r in ok)
    for label in VERDICTS:
        cells = " | ".join(str(counts[(label, p)]) for p in VERDICTS)
        lines.append(f"| {label} | {cells} |")

    lines += [
        "",
        "## Per job",
        "",
        "| uid | label | score | predicted | variant |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        if r.score is None:
            lines.append(f"| {r.uid} | {r.label} | - | failed | - |")
            continue
        mark = "-" if not r.label_variant else ("✓" if r.variant == r.label_variant else "✗")
        lines.append(f"| {r.uid} | {r.label} | {r.score} | {r.predicted} | {mark} |")
    return "\n".join(lines) + "\n"


def result_path(model: str, out_dir: Path = EVALS_DIR) -> Path:
    return out_dir / "results" / (re.sub(r"[^A-Za-z0-9._-]", "_", model) + ".md")


def run(model: str, out_dir: Path = EVALS_DIR, *, client: Any = None) -> Path:
    jobs = load_jobs(out_dir / "jobs.jsonl")
    labels, unlabeled = load_labels(out_dir / "labels.yaml")
    print(f"{len(labels)} labeled, {unlabeled} unlabeled rows skipped.")
    if not labels:
        raise SystemExit("No labeled rows: fill in verdicts in evals/labels.yaml first.")
    resumes = load_resumes()
    client = client or anthropic.Anthropic()
    results = run_eval(client, jobs, labels, resumes, model)
    path = result_path(model, out_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_report(model, results, unlabeled), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- CLI


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jobhunt.evals", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    exp = sub.add_parser("export", help="write evals/jobs.jsonl and evals/labels.yaml")
    exp.add_argument("-n", type=int, default=50, help="jobs to pick (default 50)")
    exp.add_argument(
        "--db",
        default=os.environ.get("DATABASE_URL") or "jobhunt.db",
        help="SQLite path or postgres:// URL (default: $DATABASE_URL, else jobhunt.db)",
    )
    runp = sub.add_parser("run", help="score the labeled jobs and write results")
    runp.add_argument("--model", default=None, help=f"default: ${MODEL_ENV}, else scorer default")
    args = parser.parse_args(argv)

    if args.command == "export":
        count = export(args.db, args.n)
        print(f"Wrote {count} jobs to {EVALS_DIR / 'jobs.jsonl'} and {EVALS_DIR / 'labels.yaml'}.")
        return 0
    print(f"Wrote {run(args.model or scorer_model())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
