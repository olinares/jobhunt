# Agent L — Local MCP server

## Branch / PR
`phase4/l-mcp-server` → one PR into `main`. Starts only after J and K are merged.

## Agent model
`opus`: integrates the store, pipeline, facts and packet behind a new SDK.

## Owns
src/jobhunt/mcp_server.py (new), tests/test_mcp_server.py (new), pyproject.toml (the `mcp`
dependency and a `jobhunt-mcp` script only), README.md (Phase 4 section), .mcp.json.example (new)

## Before writing code
Read the mcp-builder skill and the official MCP Python SDK docs for FastMCP (tools, resources,
prompts, stdio, in-memory testing). Don't write SDK code from memory. Pin `mcp>=1,<2` in the main
dependencies (CI installs `.[dev]`). `mcp` pulls in pydantic; our own types stay dataclasses.

## Build
- `create_server(*, store_factory: Callable[[], Store], root: Path, facts_path: Path | None = None,
  search_client_factory=None, adapters=None) -> FastMCP`, so tests inject everything.
- `main()` (the `jobhunt-mcp` script): args `--db` (default `$DATABASE_URL`) and `--root` (default:
  current directory). **Refuse to start** with a clear stderr message when neither `--db` nor
  `DATABASE_URL` is set: the CLI's `jobhunt.db` fallback would silently show an empty database.
  Resolve `config/roles.yaml`, `config/seeds.yaml` and `private/` against `--root`. stdio transport.
- **Never write to stdout**; it carries the protocol. Logging goes to stderr.
- **Open the store per tool call** (`with store_factory() as store:`). Neon drops idle
  connections, and SQLite connections can't be shared across threads.
- Refs: a uid; `"3"` = item 3 of the latest digest (`store.latest_digest_id()` +
  `store.digest_uid(id, 3)`); `"12#3"` = digest 12, item 3. If the latest digest has 0 items, say
  "digest #N has 0 items; use 12#3 for an older one". Unknown refs are reported, not raised.

### Tools
- `search_jobs(query?, status?, min_score?, variant?, remote?, limit=20)` → `store.search_jobs`.
- `get_job(ref)` → job, score, reason, status, pay, url, description preview.
- `list_pipeline(statuses?)` — default approved/applied/interviewing/offer, `include_closed=True`.
- `update_status(refs: list[str], status: str)` — batch. Valid: `store.STATUSES` minus `new` and
  `closed` (those are the pipeline's, not chat's). Returns one line per ref, e.g.
  `#3 Acme — Solutions Engineer → approved` or `#9 not in digest 14`.
- `build_packet(ref)` → `packet.build_packet(...)` with `load_facts(root=...)` and
  `load_resumes()`; returns `Packet.to_markdown()`. `FactsNotFound` / `NoVerifiedFacts` become a
  plain message telling Oz to fill `private/verified.md`.
- `discover_companies(max_queries=5)` → `pipeline.discover(...)` with `SerperClient`. Say in the
  result that it only registers boards; the next daily run (or `refresh_boards`) polls them.
  `SearchConfigError` and `httpx.HTTPError` become messages.
- `refresh_boards(board_keys: list[str], max_boards=5)` → `pipeline.poll_board` with
  `build_adapters(PoliteClient())`, errors caught per board the way `pipeline.run` does. Refuse
  more than `max_boards`. Say new jobs stay unscored until the daily run.
- Don't change pipeline.py, cli.py, store.py, facts.py or packet.py. If one of them needs a
  change, stop and report it.

### Resources
`jobhunt://config/roles` (roles.yaml text), `jobhunt://facts/verified` (`Facts.to_markdown()`,
ticked items only), `jobhunt://boards` (`store.list_boards()`).

### Prompts
- `morning_triage` — list the latest digest (numbers, title, company, score, reason) and the
  pipeline, then ask which numbers to approve or skip; apply them with `update_status`.
- `prep_application(ref)` — call `build_packet`, draft answers and bullets from the packet's facts
  only, then have Claude in Chrome fill the application form. Oz reviews and submits; never click
  submit. After Oz confirms, `update_status([ref], "applied")`.

### README / config
Phase 4 section: install, `claude mcp add jobhunt -- jobhunt-mcp --root /path/to/jobhunt`
(DATABASE_URL in the env), the tools and prompts, and that facts live in private/verified.md.
`.mcp.json.example` with the same setup and no secrets.

## Tests
In-process with the SDK's in-memory client session
(`mcp.shared.memory.create_connected_server_and_client_session`) against a `SqliteStore` file in
`tmp_path`, synthetic facts, a fake `Resumes` (set `RESUME_SE`/`RESUME_FDE` via monkeypatch), a
fake search client and fake adapters. Cover: ref parsing (uid, `3`, `12#3`, empty latest digest,
unknown), batch `update_status` incl. rejected statuses, packet contains no unticked fact,
missing facts file message, `refresh_boards` cap and per-board errors, refusing to start without
a db. No network.

## Done when
PR open with the template filled in, CI green. Then Oz adds the server to Claude Code and runs one
real application from digest to submitted.
