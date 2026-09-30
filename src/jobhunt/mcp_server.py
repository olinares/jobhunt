"""Local MCP server: the job store, pipeline and application packet as Claude tools.

Run it with `jobhunt-mcp --root /path/to/jobhunt` (stdio). `create_server` builds the server
with every side effect injected, so tests run it in memory against a SQLite file.

Two rules shape the code:

* **Nothing is written to stdout.** It carries the protocol; logging goes to stderr.
* **The store is opened per call.** Neon drops idle connections and a SQLite connection
  can't be shared across threads, so no connection outlives one tool call.

Jobs are named by a *ref*: a uid (`greenhouse:acme:123`), `3` for item 3 of the latest
digest, or `12#3` for item 3 of digest 12. A ref that doesn't resolve is reported in the
result, never raised.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import anyio
import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from jobhunt.adapters import build_adapters
from jobhunt.adapters.http import PoliteClient
from jobhunt.cli import DEFAULT_REGION_TERMS
from jobhunt.config import ConfigError, RolesConfig, load_config
from jobhunt.discovery.search import SearchClient, SearchConfigError, SerperClient
from jobhunt.facts import DEFAULT_FACTS_PATH, FactsNotFound, load_facts
from jobhunt.formatting import format_locations, format_pay
from jobhunt.models import ATS, Adapter, BoardNotFound, BoardRef
from jobhunt.packet import NoVerifiedFacts, build_packet
from jobhunt.pipeline import discover, poll_board
from jobhunt.scoring import RESUME_DIR, ResumeNotFound, html_to_text, load_resumes
from jobhunt.store import STATUSES, JobRecord, Store, open_store

log = logging.getLogger(__name__)

SERVER_NAME = "jobhunt"
ROLES_PATH = Path("config/roles.yaml")

# `new` and `closed` belong to the pipeline; chat moves a job through the rest.
CHAT_STATUSES: tuple[str, ...] = tuple(s for s in STATUSES if s not in ("new", "closed"))
PIPELINE_STATUSES: tuple[str, ...] = ("approved", "applied", "interviewing", "offer")
VARIANTS = ("se", "fde")

MAX_SEARCH_LIMIT = 100
MAX_QUERIES = 20  # ceiling on discover_companies(max_queries=...), whatever the caller asks
MAX_BOARDS = 10  # ceiling on refresh_boards(max_boards=...), whatever the caller asks
DESCRIPTION_PREVIEW = 1500

FACTS_HINT = (
    f"Fill in {DEFAULT_FACTS_PATH} (format: facts/verified.example.md) and tick [x] only the "
    "facts you would defend, then try again."
)

StoreFactory = Callable[[], Store]
SearchClientFactory = Callable[[], SearchClient]

_DIGEST_REF = re.compile(r"^#?(\d+)#(\d+)$")
_LATEST_REF = re.compile(r"^#?(\d+)$")


# --------------------------------------------------------------------------- refs


@dataclass(frozen=True)
class Resolved:
    """A ref that named a job: its uid and the label to show for it (`#3`, `12#3`, uid)."""

    uid: str
    label: str


def resolve_ref(store: Store, ref: str) -> Resolved | str:
    """Turn a ref into a uid, or return a message saying why it doesn't name a job."""
    ref = ref.strip()
    if match := _DIGEST_REF.match(ref):
        digest_id, n = int(match.group(1)), int(match.group(2))
        uid = store.digest_uid(digest_id, n)
        if uid is None:
            return f"#{n} not in digest {digest_id}"
        return Resolved(uid, f"{digest_id}#{n}")
    if match := _LATEST_REF.match(ref):
        n = int(match.group(1))
        latest = store.latest_digest_id()
        if latest is None:
            return f"#{n}: no digest has been sent yet, so there is nothing to number; use a uid"
        if not store.digest_items(latest):
            return f"#{n}: digest #{latest} has 0 items; use 12#3 for an older one"
        uid = store.digest_uid(latest, n)
        if uid is None:
            return f"#{n} not in digest {latest}"
        return Resolved(uid, f"#{n}")
    if not ref:
        return "empty ref: pass a digest number (3), digest#number (12#3) or a job uid"
    if store.get_job(ref) is None:
        return f"{ref}: no job with that uid"
    return Resolved(ref, ref)


def _lookup(store: Store, ref: str) -> tuple[Resolved, JobRecord] | str:
    resolved = resolve_ref(store, ref)
    if isinstance(resolved, str):
        return resolved
    record = store.get_job(resolved.uid)
    if record is None:  # pruned between the digest and now
        return f"{resolved.label}: job {resolved.uid} is no longer stored"
    return resolved, record


# --------------------------------------------------------------------------- formatting


def _score_text(record: JobRecord) -> str:
    if record.score is None:
        return "unscored"
    text = f"score {record.score.value} ({record.score.variant})"
    return text + ", pay looks like a placeholder" if record.score.pay_suspect else text


def _job_line(record: JobRecord, label: str | None = None) -> str:
    job = record.job
    parts = [f"{job.company} — {job.title}", _score_text(record), record.status]
    if locations := format_locations(job):
        parts.append(locations)
    if pay := format_pay(job):
        parts.append(pay)
    head = f"{label} " if label else ""
    return f"- {head}{' · '.join(parts)}\n  uid: {job.uid} · {job.url}"


def _job_details(record: JobRecord, label: str) -> str:
    job = record.job
    remote = {True: "yes", False: "no", None: "not stated"}[job.remote]
    lines = [
        f"# {label} {job.title} at {job.company}",
        "",
        f"- uid: {job.uid}",
        f"- Status: {record.status}" + (" (closed on the board)" if record.closed_at else ""),
        f"- Score: {_score_text(record)}",
        f"- Reason: {record.score.reason if record.score else 'not scored yet'}",
        f"- Locations: {format_locations(job) or 'not listed'}",
        f"- Remote: {remote}",
        f"- Pay: {format_pay(job) or 'not listed'}",
        f"- URL: {job.url}",
        f"- First seen: {record.first_seen.date().isoformat()}",
    ]
    text = html_to_text(job.description_html)
    if not text:
        preview = "No description is stored for this job."
    elif len(text) > DESCRIPTION_PREVIEW:
        preview = text[:DESCRIPTION_PREVIEW].rstrip() + " …"
    else:
        preview = text
    lines += ["", "## Description (preview)", "", preview]
    return "\n".join(lines)


def _digest_listing(store: Store) -> str:
    latest = store.latest_digest_id()
    if latest is None:
        return "No digest has been sent yet."
    items = store.digest_items(latest)
    if not items:
        return f"Digest #{latest} has 0 items."
    lines = [f"Digest #{latest} ({len(items)} items):"]
    for n, uid in items:
        record = store.get_job(uid)
        if record is None:
            lines.append(f"- #{n} (job no longer stored)")
            continue
        reason = record.score.reason if record.score else ""
        lines.append(_job_line(record, f"#{n}") + (f"\n  why: {reason}" if reason else ""))
    return "\n".join(lines)


def _pipeline_listing(store: Store, statuses: Sequence[str]) -> str:
    records = store.search_jobs(statuses=statuses, include_closed=True, limit=500)
    if not records:
        return f"Nothing in the pipeline ({', '.join(statuses)})."
    lines = []
    for status in statuses:
        group = [r for r in records if r.status == status]
        if group:
            lines.append(f"## {status} ({len(group)})")
            lines += [_job_line(r) for r in group]
    return "\n".join(lines)


def _board_line(board: BoardRef) -> str:
    return board.key() + (f" ({board.company_name})" if board.company_name else "")


def _when(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else "never"


# --------------------------------------------------------------------------- server


def create_server(
    *,
    store_factory: StoreFactory,
    root: Path,
    facts_path: Path | None = None,
    search_client_factory: SearchClientFactory | None = None,
    adapters: Mapping[ATS, Adapter] | None = None,
) -> FastMCP:
    """Build the jobhunt MCP server.

    `root` is the jobhunt checkout: `config/roles.yaml`, `private/verified.md` and
    `private/resumes/` are read from under it. `facts_path` overrides the facts file (else
    `$JOBHUNT_FACTS`, else `<root>/private/verified.md`). Without `search_client_factory`
    discovery uses `SerperClient`; without `adapters` each refresh builds real ones around a
    fresh `PoliteClient`.
    """
    root = Path(root)
    make_search = search_client_factory or SerperClient
    mcp = FastMCP(
        SERVER_NAME,
        instructions=(
            "Oz's job search: jobs found on company boards, scored and emailed as a numbered "
            "daily digest. Refer to a job by its digest number (3 = item 3 of the latest "
            "digest), digest#number (12#3) or uid. Applications may only use verified facts; "
            "Oz reviews and submits every application himself."
        ),
    )

    def roles() -> RolesConfig:
        return load_config(root / ROLES_PATH)

    read_only = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

    # -- tools -------------------------------------------------------------

    @mcp.tool(structured_output=False, annotations=read_only)
    def search_jobs(
        query: str | None = None,
        status: str | None = None,
        min_score: int | None = None,
        variant: str | None = None,
        remote: bool | None = None,
        limit: int = 20,
    ) -> str:
        """Search stored open jobs, best score first.

        query matches title or company (substring, any case). status is one of new,
        approved, skipped, applied, interviewing, offer, rejected, closed. variant is the
        resume the scorer picked: se or fde. remote=true keeps jobs marked remote.
        """
        if status is not None and status not in STATUSES:
            return f"Unknown status {status!r}. Use one of: {', '.join(STATUSES)}."
        if variant is not None and variant not in VARIANTS:
            return f"Unknown variant {variant!r}. Use se or fde."
        limit = max(1, min(limit, MAX_SEARCH_LIMIT))
        with store_factory() as store:
            records = store.search_jobs(
                query=query or None,
                statuses=None if status is None else [status],
                min_score=min_score,
                variant=variant,
                remote=remote,
                include_closed=status == "closed",
                limit=limit,
            )
        if not records:
            return "No jobs match."
        more = " (limit reached; narrow the search or raise limit)" if len(records) == limit else ""
        return f"{len(records)} job(s){more}:\n" + "\n".join(_job_line(r) for r in records)

    @mcp.tool(structured_output=False, annotations=read_only)
    def get_job(ref: str) -> str:
        """One job: score, reason, status, pay, URL and a description preview.

        ref is a digest number (3 = item 3 of the latest digest), digest#number (12#3) or a
        job uid.
        """
        with store_factory() as store:
            found = _lookup(store, ref)
        if isinstance(found, str):
            return found
        resolved, record = found
        return _job_details(record, resolved.label)

    @mcp.tool(structured_output=False, annotations=read_only)
    def list_pipeline(statuses: list[str] | None = None) -> str:
        """Jobs Oz is acting on, grouped by status, including ones closed on the board.

        Default statuses: approved, applied, interviewing, offer.
        """
        wanted = list(dict.fromkeys(statuses or PIPELINE_STATUSES))
        unknown = [s for s in wanted if s not in STATUSES]
        if unknown:
            return f"Unknown status {unknown[0]!r}. Use any of: {', '.join(STATUSES)}."
        with store_factory() as store:
            return _pipeline_listing(store, wanted)

    @mcp.tool(
        structured_output=False,
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
        ),
    )
    def update_status(refs: list[str], status: str) -> str:
        """Set the status of one or more jobs, e.g. refs=["3", "7"], status="approved".

        status is one of approved, skipped, applied, interviewing, offer, rejected. Refs are
        digest numbers (3), digest#number (12#3) or uids. Returns one line per ref.
        """
        if status not in CHAT_STATUSES:
            return (
                f"Status {status!r} can't be set from chat. Use one of: "
                f"{', '.join(CHAT_STATUSES)}. Nothing was changed."
            )
        if not refs:
            return "No refs given. Nothing was changed."
        lines = []
        with store_factory() as store:
            for ref in refs:
                found = _lookup(store, ref)
                if isinstance(found, str):
                    lines.append(found)
                    continue
                resolved, record = found
                try:
                    store.set_status(resolved.uid, status)
                except KeyError:
                    lines.append(f"{resolved.label}: job {resolved.uid} is no longer stored")
                    continue
                job = record.job
                lines.append(f"{resolved.label} {job.company} — {job.title} → {status}")
        return "\n".join(lines)

    @mcp.tool(name="build_packet", structured_output=False, annotations=read_only)
    def build_packet_tool(ref: str) -> str:
        """The application packet for one job: rules, verified facts, resume and description.

        Draft answers only from this packet. ref is a digest number (3), digest#number
        (12#3) or a job uid.
        """
        with store_factory() as store:
            found = _lookup(store, ref)
        if isinstance(found, str):
            return found
        _, record = found
        try:
            facts = load_facts(facts_path, root=root)
        except FactsNotFound as exc:
            return f"No verified facts yet, so no packet. {FACTS_HINT}\n\n({exc})"
        try:
            resumes = load_resumes(resume_dir=root / RESUME_DIR)
        except ResumeNotFound as exc:
            return f"No resume, so no packet: {exc}"
        try:
            packet = build_packet(record.job, record.score, resumes, facts)
        except NoVerifiedFacts:
            return f"The verified facts file has no ticked facts, so no packet. {FACTS_HINT}"
        return packet.to_markdown()

    @mcp.tool(
        structured_output=False, annotations=ToolAnnotations(readOnlyHint=False, openWorldHint=True)
    )
    async def discover_companies(max_queries: int = 5) -> str:
        """Search the web for new company job boards and register them.

        Runs up to max_queries search queries (at most 20). It only registers boards; the
        next daily run (or refresh_boards) polls them for jobs.
        """
        max_queries = max(1, min(max_queries, MAX_QUERIES))
        return await anyio.to_thread.run_sync(_discover, max_queries)

    def _discover(max_queries: int) -> str:
        try:
            cfg = roles()
        except ConfigError as exc:
            return f"Can't read the roles config: {exc}"
        try:
            client = make_search()
        except SearchConfigError as exc:
            return f"Discovery is not configured: {exc}"
        try:
            with store_factory() as store:
                found, report = discover(
                    store,
                    client,
                    list(cfg.titles),
                    list(DEFAULT_REGION_TERMS),
                    max_queries=max_queries,
                    now=datetime.now(UTC),
                )
        except httpx.HTTPError as exc:
            return f"Search failed: {type(exc).__name__}: {exc}"
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()
        capped = f", {report.queries_skipped} left for later" if report.capped else ""
        lines = [f"Ran {report.queries_run} search queries{capped}; {len(found)} new board(s)."]
        lines += [f"- {_board_line(board)}" for board in found]
        lines.append(
            "These boards are only registered. The next daily run (or refresh_boards) polls "
            "them for jobs."
        )
        return "\n".join(lines)

    @mcp.tool(
        structured_output=False, annotations=ToolAnnotations(readOnlyHint=False, openWorldHint=True)
    )
    async def refresh_boards(board_keys: list[str], max_boards: int = 5) -> str:
        """Poll registered boards now and store their relevant jobs.

        board_keys are keys as the jobhunt://boards resource lists them, e.g.
        greenhouse:acme. Refuses more than max_boards (at most 10). New jobs stay unscored
        until the daily run.
        """
        cap = max(1, min(max_boards, MAX_BOARDS))
        keys = list(dict.fromkeys(k.strip() for k in board_keys if k.strip()))
        if not keys:
            return "No board keys given."
        if len(keys) > cap:
            return (
                f"Refusing to poll {len(keys)} boards; the limit is {cap} per call. "
                "Pass fewer board keys."
            )
        return await anyio.to_thread.run_sync(_refresh, keys)

    def _refresh(keys: list[str]) -> str:
        try:
            cfg = roles()
        except ConfigError as exc:
            return f"Can't read the roles config: {exc}"
        if adapters is not None:
            return _poll(keys, adapters, cfg)
        with PoliteClient() as client:
            return _poll(keys, build_adapters(client), cfg)

    def _poll(keys: list[str], ats_adapters: Mapping[ATS, Adapter], cfg: RolesConfig) -> str:
        lines = []
        now = datetime.now(UTC)
        with store_factory() as store:
            known = {record.board.key(): record.board for record in store.list_boards()}
            for key in keys:
                board = known.get(key)
                if board is None:
                    lines.append(f"- {key}: not a registered board (see jobhunt://boards)")
                    continue
                # Same per-board error handling as pipeline.run: one bad board never stops
                # the others.
                try:
                    result, _, new_jobs = poll_board(
                        board, ats_adapters[board.ats], cfg, store, now=now
                    )
                except BoardNotFound:
                    lines.append(f"- {key}: board not found")
                    continue
                except Exception as exc:  # noqa: BLE001
                    lines.append(f"- {key}: failed: {type(exc).__name__}: {exc}")
                    continue
                line = (
                    f"- {key}: {result.fetched} fetched, {result.matched} relevant, "
                    f"{result.new} new, {result.closed} closed"
                )
                if result.detail_error:
                    line += f" (descriptions failed, retried next run: {result.detail_error})"
                lines.append(line)
                lines += [f"  - new: {job.company} — {job.title} ({job.uid})" for job in new_jobs]
        lines.append("New jobs stay unscored until the next daily run.")
        return "\n".join(lines)

    # -- resources -----------------------------------------------------------

    @mcp.resource(
        "jobhunt://config/roles",
        name="roles",
        description="config/roles.yaml: the titles, levels and regions jobs are filtered by",
        mime_type="text/yaml",
    )
    def roles_resource() -> str:
        path = root / ROLES_PATH
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            return f"# Can't read {path}: {exc}\n"

    @mcp.resource(
        "jobhunt://facts/verified",
        name="verified_facts",
        description="The ticked verified facts, the only claims an application may make",
        mime_type="text/markdown",
    )
    def facts_resource() -> str:
        try:
            facts = load_facts(facts_path, root=root)
        except FactsNotFound as exc:
            return f"No verified facts yet. {FACTS_HINT}\n\n({exc})\n"
        if facts.count == 0:
            return f"The verified facts file has no ticked facts. {FACTS_HINT}\n"
        return f"# Verified facts ({facts.count})\n\n{facts.to_markdown()}\n"

    @mcp.resource(
        "jobhunt://boards",
        name="boards",
        description="Every registered job board with when it was last polled and last useful",
        mime_type="text/plain",
    )
    def boards_resource() -> str:
        with store_factory() as store:
            records = store.list_boards()
        if not records:
            return "No boards registered.\n"
        lines = [f"{len(records)} board(s):"]
        lines += [
            f"- {_board_line(r.board)} · last checked {_when(r.last_checked)} · "
            f"last relevant job {_when(r.last_relevant_hit)}"
            for r in records
        ]
        return "\n".join(lines) + "\n"

    # -- prompts -------------------------------------------------------------

    @mcp.prompt(description="Go through the latest digest and the pipeline; approve or skip.")
    def morning_triage() -> str:
        with store_factory() as store:
            digest = _digest_listing(store)
            pipeline = _pipeline_listing(store, PIPELINE_STATUSES)
        return f"""\
Morning triage for Oz's job search.

Show Oz the latest digest as a numbered list (number, title, company, score, reason) and
then the pipeline below, briefly. Ask which numbers to approve and which to skip. When he
answers, call update_status once for the approvals (status "approved") and once for the
skips (status "skipped"), passing the numbers as refs, and show him the lines it returns.
Don't change any status he didn't name.

# Latest digest

{digest}

# Pipeline

{pipeline}
"""

    @mcp.prompt(description="Draft one application from verified facts and fill the form.")
    def prep_application(ref: str) -> str:
        return f"""\
Prepare Oz's application for job {ref}.

1. Call build_packet with ref "{ref}". If it returns a message instead of a packet, show
   it to Oz and stop.
2. Draft the answers the application needs (short answers, cover note, resume bullets to
   emphasise) using only the packet's verified facts and resume, following its rules.
   Anything not in the packet is "not in verified facts": leave it for Oz. Never invent or
   estimate a number, name, title or date. Show Oz the drafts.
3. Use Claude in Chrome to open the job URL from the packet and fill in the application
   form with those drafts. Stop before the final step: never click submit or send. Oz
   reviews the form and submits it himself.
4. Once Oz confirms he submitted it, call update_status with refs ["{ref}"] and status
   "applied".
"""

    return mcp


# --------------------------------------------------------------------------- entry point


def _resolve_db(db: str, root: Path) -> str:
    """A relative SQLite path is taken relative to --root, not wherever Claude started us."""
    if db.startswith(("postgres://", "postgresql://")) or db == ":memory:":
        return db
    path = Path(db).expanduser()
    return str(path if path.is_absolute() else root / path)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jobhunt-mcp", description="jobhunt MCP server (stdio transport)"
    )
    parser.add_argument(
        "--db",
        default=os.environ.get("DATABASE_URL") or None,
        help="postgres:// URL or SQLite path (default: $DATABASE_URL; required)",
    )
    parser.add_argument(
        "--root",
        default=".",
        help="the jobhunt checkout holding config/ and private/ (default: current directory)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for `jobhunt-mcp`. Speaks MCP over stdio; everything else goes to stderr."""
    logging.basicConfig(
        stream=sys.stderr, level=logging.INFO, format="jobhunt-mcp: %(levelname)s %(message)s"
    )
    args = build_arg_parser().parse_args(argv)
    if not args.db:
        print(
            "jobhunt-mcp: no database. Set DATABASE_URL or pass --db. (The CLI's jobhunt.db "
            "fallback is not used here: it would silently show an empty database.)",
            file=sys.stderr,
        )
        return 2
    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        print(f"jobhunt-mcp: --root {root} is not a directory", file=sys.stderr)
        return 2
    db = _resolve_db(args.db, root)

    # Fail at startup, not on the first tool call, if the database can't be opened.
    try:
        with open_store(db):
            pass
    except Exception as exc:  # noqa: BLE001 -- report any connection failure and stop
        print(f"jobhunt-mcp: can't open the database: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    log.info("serving %s over stdio", root)
    server = create_server(store_factory=lambda: open_store(db), root=root)
    server.run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
