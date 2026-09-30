"""Command line: `jobhunt run` polls every board and prints the relevant jobs."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import TextIO

from jobhunt.adapters import build_adapters
from jobhunt.adapters.http import PoliteClient
from jobhunt.config import ConfigError, load_config
from jobhunt.discovery.search import SearchClient, SearchConfigError, SerperClient
from jobhunt.models import Job
from jobhunt.pipeline import RunReport, load_seeds, run
from jobhunt.store import SqliteStore

DEFAULT_REGION_TERMS = ["San Francisco", "Remote"]
MAX_LOCATIONS_SHOWN = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jobhunt", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    run_cmd = commands.add_parser("run", help="discover boards, poll them, and print relevant jobs")
    run_cmd.add_argument(
        "--config", default="config/roles.yaml", help="roles config (titles, regions)"
    )
    run_cmd.add_argument("--seeds", default="config/seeds.yaml", help="boards polled on every run")
    run_cmd.add_argument("--db", default="jobhunt.db", help="SQLite database path")
    run_cmd.add_argument(
        "--no-discover", action="store_true", help="skip search-based discovery this run"
    )
    run_cmd.add_argument(
        "--max-queries", type=int, default=20, help="cap on search queries per run (default 20)"
    )
    run_cmd.add_argument(
        "--region-term",
        action="append",
        dest="region_terms",
        help='region phrase for discovery queries; repeatable (default: "San Francisco", Remote)',
    )
    run_cmd.add_argument(
        "--all", action="store_true", help="print every relevant job, not just new ones"
    )
    run_cmd.add_argument(
        "--prune-weeks",
        type=int,
        help="delete boards with no relevant job in this many weeks (off by default)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return _run(args, out=sys.stdout, err=sys.stderr)


def _run(args: argparse.Namespace, *, out: TextIO, err: TextIO) -> int:
    try:
        cfg = load_config(args.config)
        seeds = load_seeds(args.seeds)
    except ConfigError as exc:
        print(f"error: {exc}", file=err)
        return 2

    search = None if args.no_discover else _search_client(err)
    with PoliteClient() as client, SqliteStore(args.db) as store:
        try:
            report = run(
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

    jobs = report.matched_jobs if args.all else report.new_jobs
    for job in jobs:
        print(format_job(job), file=out)
    print(format_summary(report, shown=len(jobs), all_jobs=args.all), file=out)
    return 1 if report.all_failed else 0


def _search_client(err: TextIO) -> SearchClient | None:
    try:
        return SerperClient()
    except SearchConfigError:
        print("note: SEARCH_API_KEY is not set, so discovery is skipped this run", file=err)
        return None


def format_job(job: Job) -> str:
    parts = [job.title, job.company]
    if job.locations:
        shown = job.locations[:MAX_LOCATIONS_SHOWN]
        extra = len(job.locations) - len(shown)
        parts.append("; ".join(shown) + (f" (+{extra} more)" if extra else ""))
    elif job.remote:
        parts.append("Remote")
    if pay := _format_pay(job):
        parts.append(pay)
    parts.append(job.url)
    return " · ".join(parts)


def _format_pay(job: Job) -> str | None:
    if job.pay_min is None and job.pay_max is None:
        return None
    amounts = [f"{v:,.0f}" for v in (job.pay_min, job.pay_max) if v is not None]
    text = "–".join(dict.fromkeys(amounts))
    if job.pay_currency:
        text = f"{job.pay_currency} {text}"
    if job.pay_period:
        text += f"/{job.pay_period}"
    return text


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
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
