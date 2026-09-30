"""Command line.

`jobhunt run` polls every board and prints the relevant jobs.
`jobhunt daily` does the same, scores the new jobs and emails the digest.
"""

from __future__ import annotations

import argparse
import os
import smtplib
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import TextIO
from zoneinfo import ZoneInfo

import anthropic

from jobhunt.adapters import build_adapters
from jobhunt.adapters.http import PoliteClient
from jobhunt.config import ConfigError, RolesConfig, load_config
from jobhunt.digest import DigestConfigError, DigestStats, render_digest, send_email
from jobhunt.discovery.search import SearchClient, SearchConfigError, SerperClient
from jobhunt.formatting import format_locations, format_pay
from jobhunt.models import BoardRef, Job
from jobhunt.pipeline import RunReport, load_seeds, run
from jobhunt.scoring import ResumeNotFound, Resumes, load_resumes, score_many
from jobhunt.store import Store, open_store

DEFAULT_REGION_TERMS = ["San Francisco", "Remote"]
DEFAULT_MAX_SCORE = 100
DEFAULT_DB = "jobhunt.db"
# The digest is dated in Oz's time zone, not the runner's (UTC).
DIGEST_TZ = ZoneInfo("America/Los_Angeles")

# Seams for tests: the daily command builds its Anthropic client and SMTP connection
# through these, so tests swap in fakes without touching the network.
anthropic_client: Callable[[], anthropic.Anthropic] = anthropic.Anthropic
smtp_factory: Callable[..., smtplib.SMTP] = smtplib.SMTP_SSL


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jobhunt", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    run_cmd = commands.add_parser("run", help="discover boards, poll them, and print relevant jobs")
    _add_pipeline_args(run_cmd)
    run_cmd.add_argument(
        "--all", action="store_true", help="print every relevant job, not just new ones"
    )

    daily = commands.add_parser(
        "daily", help="run the pipeline, score new jobs, and email the numbered digest"
    )
    _add_pipeline_args(daily)
    daily.add_argument(
        "--max-score",
        type=int,
        default=DEFAULT_MAX_SCORE,
        help=f"cap on jobs scored per run (default {DEFAULT_MAX_SCORE}); the rest wait",
    )
    daily.add_argument(
        "--dry-run",
        action="store_true",
        help="print the text digest instead of emailing it, and don't record it",
    )
    return parser


def _add_pipeline_args(cmd: argparse.ArgumentParser) -> None:
    cmd.add_argument("--config", default="config/roles.yaml", help="roles config (titles, regions)")
    cmd.add_argument("--seeds", default="config/seeds.yaml", help="boards polled on every run")
    cmd.add_argument(
        "--db",
        default=os.environ.get("DATABASE_URL") or DEFAULT_DB,
        help=f"SQLite path or postgres:// URL (default: $DATABASE_URL, else {DEFAULT_DB})",
    )
    cmd.add_argument(
        "--no-discover", action="store_true", help="skip search-based discovery this run"
    )
    cmd.add_argument(
        "--max-queries", type=int, default=20, help="cap on search queries per run (default 20)"
    )
    cmd.add_argument(
        "--region-term",
        action="append",
        dest="region_terms",
        help='region phrase for discovery queries; repeatable (default: "San Francisco", Remote)',
    )
    cmd.add_argument(
        "--prune-weeks",
        type=int,
        help="delete boards with no relevant job in this many weeks (off by default)",
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "daily":
        return _daily(args, out=sys.stdout, err=sys.stderr)
    return _run(args, out=sys.stdout, err=sys.stderr)


# --------------------------------------------------------------------------- run


def _run(args: argparse.Namespace, *, out: TextIO, err: TextIO) -> int:
    try:
        cfg = load_config(args.config)
        seeds = load_seeds(args.seeds)
    except ConfigError as exc:
        print(f"error: {exc}", file=err)
        return 2

    with open_store(args.db) as store:
        report = _run_pipeline(args, store, cfg, seeds, err=err)

    jobs = report.matched_jobs if args.all else report.new_jobs
    for job in jobs:
        print(format_job(job), file=out)
    print(format_summary(report, shown=len(jobs), all_jobs=args.all), file=out)
    return 1 if report.all_failed else 0


def _run_pipeline(
    args: argparse.Namespace,
    store: Store,
    cfg: RolesConfig,
    seeds: Sequence[BoardRef],
    *,
    err: TextIO,
) -> RunReport:
    search = None if args.no_discover else _search_client(err)
    with PoliteClient() as client:
        try:
            return run(
                store,
                build_adapters(client),
                cfg,
                seeds=seeds,
                search_client=search,
                region_terms=args.region_terms or DEFAULT_REGION_TERMS,
                max_queries=args.max_queries,
                prune_weeks=args.prune_weeks,
            )
        finally:
            if search is not None:
                search.close()


def _search_client(err: TextIO) -> SearchClient | None:
    try:
        return SerperClient()
    except SearchConfigError:
        print("note: SEARCH_API_KEY is not set, so discovery is skipped this run", file=err)
        return None


# --------------------------------------------------------------------------- daily


def _daily(args: argparse.Namespace, *, out: TextIO, err: TextIO) -> int:
    """Pipeline -> score -> digest -> email -> record.

    Configuration is checked before any board is polled, so a missing secret fails in
    seconds rather than after a full run. The digest is recorded only after the email is
    sent, so a failed send leaves its jobs to go out with the next digest.
    """
    try:
        cfg = load_config(args.config)
        seeds = load_seeds(args.seeds)
        resumes = load_resumes()
        _require_env(["ANTHROPIC_API_KEY"])
        if not args.dry_run:
            _require_env(["GMAIL_ADDRESS", "GMAIL_APP_PASSWORD"])
    except (ConfigError, ResumeNotFound) as exc:
        print(f"error: {exc}", file=err)
        return 2

    with open_store(args.db) as store:
        report = _run_pipeline(args, store, cfg, seeds, err=err)
        print(format_summary(report, shown=len(report.new_jobs), all_jobs=False), file=err)

        scored, failures = _score(store, resumes, limit=args.max_score)
        for uid, error in failures:
            print(f"Scoring failed {uid}: {error}", file=err)
        print(f"Scored {scored} job(s); {len(failures)} failed.", file=err)

        items = store.jobs_for_digest()
        stats = _digest_stats(
            report, scored=scored, score_failures=len(failures), no_discover=args.no_discover
        )
        subject, text, html = render_digest(items, day=datetime.now(DIGEST_TZ).date(), stats=stats)

        if args.dry_run:
            print(text, file=out, end="")
            print("Dry run: digest not sent or recorded.", file=err)
        else:
            try:
                send_email(subject, text, html, smtp_factory=smtp_factory)
            except (smtplib.SMTPException, OSError, DigestConfigError) as exc:
                print(f"error: digest email failed: {type(exc).__name__}: {exc}", file=err)
                return 1
            digest_id = store.record_digest([item.job.uid for item in items])
            print(f"Sent digest #{digest_id} with {len(items)} job(s): {subject}", file=out)

    if report.all_failed:
        print("error: every board failed", file=err)
        return 1
    if failures and not scored:
        print("error: scoring failed for every job", file=err)
        return 1
    return 0


def _require_env(names: list[str]) -> None:
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        raise ConfigError("environment variable(s) not set: " + ", ".join(missing))


def _score(store: Store, resumes: Resumes, *, limit: int) -> tuple[int, list[tuple[str, str]]]:
    """Score up to `limit` unscored jobs and save each score. Returns (saved, failures)."""
    jobs = store.jobs_to_score(limit, now=datetime.now(UTC))
    if not jobs:
        return 0, []
    scores, failures = score_many(anthropic_client(), jobs, resumes)
    now = datetime.now(UTC)
    for uid, score in scores.items():
        store.save_score(uid, score, now=now)
    return len(scores), failures


def _digest_stats(
    report: RunReport, *, scored: int, score_failures: int, no_discover: bool = False
) -> DigestStats:
    return DigestStats(
        boards_polled=len(report.boards),
        jobs_fetched=sum(b.fetched for b in report.boards),
        relevant=len(report.matched_jobs),
        new=len(report.new_jobs),
        closed=sum(b.closed for b in report.boards),
        scored=scored,
        score_failures=score_failures,
        boards_failed=len(report.failed),
        detail_failures=len(report.detail_failures),
        discovery=_discovery_line(report, no_discover=no_discover),
    )


def _discovery_line(report: RunReport, *, no_discover: bool) -> str:
    """One footer line saying what discovery did, so a dead search key shows up in the email."""
    if report.search_error:
        return f"⚠ Discovery failed: {report.search_error}"
    if report.search is None:
        reason = "--no-discover" if no_discover else "SEARCH_API_KEY not set"
        return f"Discovery skipped ({reason})"
    capped = (
        f", {report.search.queries_skipped} left for later runs" if report.search.capped else ""
    )
    return (
        f"Discovery: {report.search.queries_run} queries{capped}; "
        f"{len(report.discovered)} new board(s)"
    )


# --------------------------------------------------------------------------- output


def format_job(job: Job) -> str:
    parts = [job.title, job.company]
    if locations := format_locations(job):
        parts.append(locations)
    if pay := format_pay(job):
        parts.append(pay)
    parts.append(job.url)
    return " · ".join(parts)


def format_summary(report: RunReport, *, shown: int, all_jobs: bool) -> str:
    fetched = sum(b.fetched for b in report.boards)
    closed = sum(b.closed for b in report.boards)
    label = "relevant" if all_jobs else "new relevant"
    lines = [
        "",
        (
            f"{shown} {label} job(s). "
            f"Polled {len(report.boards)} board(s): {fetched} jobs fetched, "
            f"{len(report.matched_jobs)} relevant, {len(report.new_jobs)} new, {closed} closed."
        ),
    ]
    if report.search is not None:
        capped = (
            f", {report.search.queries_skipped} left for later runs" if report.search.capped else ""
        )
        lines.append(
            f"Discovery: {report.search.queries_run} queries{capped}; "
            f"{len(report.discovered)} new board(s)."
        )
    if report.search_error:
        lines.append(f"Discovery failed: {report.search_error}")
    if report.pruned:
        lines.append(f"Pruned {len(report.pruned)} board(s): {', '.join(report.pruned)}")
    for result in report.failed:
        lines.append(f"Failed {result.board.key()}: {result.error}")
    for result in report.detail_failures:
        lines.append(
            f"Details failed {result.board.key()} (retried next run): {result.detail_error}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
