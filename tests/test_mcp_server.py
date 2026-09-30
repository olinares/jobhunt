"""The MCP server, driven in-process through the SDK's in-memory client session.

Everything is synthetic: a SQLite file in tmp_path, the invented facts in
facts/verified.example.md, resumes from RESUME_SE / RESUME_FDE, a fake search client and fake
adapters. Nothing touches the network or private/.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import anyio
import httpx
import pytest
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.memory import create_connected_server_and_client_session
from starlette.testclient import TestClient

from jobhunt import mcp_server
from jobhunt.discovery.search import SearchConfigError
from jobhunt.mcp_server import create_server
from jobhunt.models import BoardNotFound, BoardRef, Job, Score
from jobhunt.store import SqliteStore

REPO = Path(__file__).parent.parent
EXAMPLE_FACTS = REPO / "facts" / "verified.example.md"
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)

ACME = BoardRef("greenhouse", "acme", company_name="Acme")
GLOBEX = BoardRef("lever", "globex", company_name="Globex")
INITECH = BoardRef("ashby", "initech", company_name="Initech")

RESUME_SE = "SE RESUME: synthetic solutions engineer resume."
RESUME_FDE = "FDE RESUME: synthetic forward deployed resume."

# Text of unticked or ignored items in the example facts file; none may reach a packet.
UNTICKED = [
    "9 billion",
    "renewed for 10 years",
    "Partly true",
    "check mark",
    "JavaScript",
    "Kubernetes",
    "12 demos a week",
    "sits before the first section",
    "inside a code fence",
]
TICKED = [
    "Solutions Engineer at Initrode Widgets, 2031 to 2034.",
    "Languages: Python",
    "Tools: Terraform",
]


def make_job(board: BoardRef, n: int, title: str, **kw) -> Job:
    return Job(
        board=board,
        external_id=str(n),
        title=title,
        company=board.company_name or board.slug,
        url=f"https://example.test/{board.slug}/{n}",
        locations=["San Francisco, CA"],
        description_html="<p>Help customers adopt the product.</p>",
        **kw,
    )


# Digest 1: [A1, A2]; digest 2: [G1, G2, A3]. UNSCORED is stored but never scored.
A1 = make_job(ACME, 1, "Solutions Engineer", pay_min=150000, pay_max=180000, pay_currency="USD")
A2 = make_job(ACME, 2, "Sales Engineer")
A3 = make_job(ACME, 3, "Forward Deployed Engineer")
G1 = make_job(GLOBEX, 1, "Solutions Architect")
G2 = make_job(GLOBEX, 2, "Customer Engineer")
UNSCORED = make_job(GLOBEX, 3, "Deployment Engineer")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    (root / "config").mkdir(parents=True)
    shutil.copy(REPO / "config" / "roles.yaml", root / "config" / "roles.yaml")
    return root


@pytest.fixture
def facts_file(tmp_path: Path) -> Path:
    path = tmp_path / "verified.md"
    shutil.copy(EXAMPLE_FACTS, path)
    return path


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESUME_SE", RESUME_SE)
    monkeypatch.setenv("RESUME_FDE", RESUME_FDE)
    monkeypatch.delenv("JOBHUNT_FACTS", raising=False)
    monkeypatch.delenv("SEARCH_API_KEY", raising=False)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "jobs.db"
    with SqliteStore(path) as store:
        store.upsert_jobs([A1, A2, A3, G1, G2, UNSCORED], now=NOW)
        for job, value, variant in [
            (A1, 91, "se"),
            (A2, 72, "se"),
            (A3, 88, "fde"),
            (G1, 80, "se"),
            (G2, 65, "se"),
        ]:
            store.save_score(job.uid, Score(value, variant, f"fits {job.title}"), now=NOW)
        store.record_digest([A1.uid, A2.uid], now=NOW)
        store.record_digest([G1.uid, G2.uid, A3.uid], now=NOW)
    return path


def server_for(db: Path, root: Path, **kw):
    kw.setdefault("facts_path", None)
    return create_server(store_factory=lambda: SqliteStore(db), root=root, **kw)


async def _session_call(server, method: str, *args):
    async with create_connected_server_and_client_session(server, raise_exceptions=True) as s:
        return await getattr(s, method)(*args)


def call(server, tool: str, arguments: dict | None = None) -> str:
    result = anyio.run(_session_call, server, "call_tool", tool, arguments or {})
    assert not result.isError, result.content
    return result.content[0].text


def read(server, uri: str) -> str:
    result = anyio.run(_session_call, server, "read_resource", uri)
    return result.contents[0].text


def prompt(server, name: str, arguments: dict | None = None) -> str:
    result = anyio.run(_session_call, server, "get_prompt", name, arguments)
    return result.messages[0].content.text


def status_of(db: Path, job: Job) -> str:
    with SqliteStore(db) as store:
        return store.get_job(job.uid).status


# --------------------------------------------------------------------------- listing


def test_exposes_the_tools_resources_and_prompts(db, root):
    server = server_for(db, root)
    tools = anyio.run(_session_call, server, "list_tools")
    resources = anyio.run(_session_call, server, "list_resources")
    prompts = anyio.run(_session_call, server, "list_prompts")
    assert {t.name for t in tools.tools} == {
        "search_jobs",
        "get_job",
        "list_pipeline",
        "update_status",
        "build_packet",
        "discover_companies",
        "refresh_boards",
    }
    assert {str(r.uri) for r in resources.resources} == {
        "jobhunt://config/roles",
        "jobhunt://facts/verified",
        "jobhunt://boards",
    }
    assert {p.name for p in prompts.prompts} == {"morning_triage", "prep_application"}


# --------------------------------------------------------------------------- refs


def test_number_ref_is_item_of_latest_digest(db, root):
    text = call(server_for(db, root), "get_job", {"ref": "3"})
    assert text.startswith("# #3 Forward Deployed Engineer at Acme")
    assert A3.uid in text
    assert "score 88 (fde)" in text
    assert "fits Forward Deployed Engineer" in text
    assert "Help customers adopt the product." in text


def test_digest_hash_ref_names_an_older_digest(db, root):
    text = call(server_for(db, root), "get_job", {"ref": "1#2"})
    assert text.startswith("# 1#2 Sales Engineer at Acme")


def test_uid_ref(db, root):
    text = call(server_for(db, root), "get_job", {"ref": A1.uid})
    assert "Solutions Engineer at Acme" in text
    assert "USD 150,000–180,000" in text


@pytest.mark.parametrize(
    ("ref", "message"),
    [
        ("9", "#9 not in digest 2"),
        ("#9", "#9 not in digest 2"),
        ("7#1", "#1 not in digest 7"),
        ("greenhouse:acme:404", "greenhouse:acme:404: no job with that uid"),
        ("", "empty ref"),
    ],
)
def test_unknown_refs_are_reported_not_raised(db, root, ref, message):
    assert message in call(server_for(db, root), "get_job", {"ref": ref})


def test_empty_latest_digest_points_at_older_ones(db, root):
    with SqliteStore(db) as store:
        store.record_digest([], now=NOW)  # digest 3, a "no new jobs" day
    text = call(server_for(db, root), "get_job", {"ref": "3"})
    assert text == "#3: digest #3 has 0 items; use 12#3 for an older one"
    assert "Customer Engineer" in call(server_for(db, root), "get_job", {"ref": "2#2"})


def test_number_ref_before_any_digest(tmp_path, root):
    empty = tmp_path / "empty.db"
    SqliteStore(empty).close()
    assert "no digest has been sent yet" in call(server_for(empty, root), "get_job", {"ref": "1"})


# --------------------------------------------------------------------------- update_status


def test_update_status_batch(db, root):
    text = call(
        server_for(db, root),
        "update_status",
        {"refs": ["1", "3", "9", "1#1", "greenhouse:acme:404"], "status": "approved"},
    )
    assert text.splitlines() == [
        "#1 Globex — Solutions Architect → approved",
        "#3 Acme — Forward Deployed Engineer → approved",
        "#9 not in digest 2",
        "1#1 Acme — Solutions Engineer → approved",
        "greenhouse:acme:404: no job with that uid",
    ]
    assert status_of(db, G1) == "approved"
    assert status_of(db, A3) == "approved"
    assert status_of(db, A1) == "approved"
    assert status_of(db, G2) == "new"


@pytest.mark.parametrize("status", ["new", "closed", "bogus"])
def test_update_status_rejects_pipeline_and_unknown_statuses(db, root, status):
    text = call(server_for(db, root), "update_status", {"refs": ["1"], "status": status})
    assert "can't be set from chat" in text
    assert "Nothing was changed" in text
    assert status_of(db, G1) == "new"


# --------------------------------------------------------------------------- search / pipeline


def test_search_jobs_filters_and_orders(db, root):
    server = server_for(db, root)
    text = call(server, "search_jobs", {"min_score": 80})
    lines = [line for line in text.splitlines() if line.startswith("- ")]
    assert [line.split(" · ")[0] for line in lines] == [
        "- Acme — Solutions Engineer",
        "- Acme — Forward Deployed Engineer",
        "- Globex — Solutions Architect",
    ]
    assert "Forward Deployed" in call(server, "search_jobs", {"variant": "fde"})
    assert "Forward Deployed" not in call(server, "search_jobs", {"variant": "se"})
    assert "Globex" not in call(server, "search_jobs", {"query": "acme"})
    assert "Unknown status" in call(server, "search_jobs", {"status": "maybe"})
    assert "Unknown variant" in call(server, "search_jobs", {"variant": "pm"})


def test_list_pipeline_defaults_and_includes_closed(db, root):
    with SqliteStore(db) as store:
        store.set_status(A1.uid, "applied")
        store.set_status(G1.uid, "approved")
        store.set_status(G2.uid, "skipped")
        store.mark_closed(ACME.key(), {A2.uid, A3.uid}, now=NOW)  # A1 closes on the board
    text = call(server_for(db, root), "list_pipeline")
    assert "## approved (1)" in text
    assert "## applied (1)" in text
    assert "Acme — Solutions Engineer" in text  # closed on the board, still in the pipeline
    assert "Customer Engineer" not in text  # skipped isn't a default
    skipped = call(server_for(db, root), "list_pipeline", {"statuses": ["skipped"]})
    assert "Customer Engineer" in skipped


# --------------------------------------------------------------------------- packet / facts


def test_build_packet_uses_only_ticked_facts(db, root, facts_file):
    text = call(server_for(db, root, facts_path=facts_file), "build_packet", {"ref": "3"})
    assert text.startswith("# Application packet: Forward Deployed Engineer at Acme")
    for fact in TICKED:
        assert fact in text
    for unticked in UNTICKED:
        assert unticked not in text
    assert RESUME_FDE in text  # the scorer picked fde for this job
    assert RESUME_SE not in text


def test_build_packet_reads_facts_from_root_by_default(db, root, facts_file):
    (root / "private").mkdir()
    shutil.copy(facts_file, root / "private" / "verified.md")
    text = call(server_for(db, root), "build_packet", {"ref": "1#1"})
    assert TICKED[0] in text
    assert RESUME_SE in text


def test_build_packet_without_facts_file_explains(db, root, tmp_path):
    text = call(
        server_for(db, root, facts_path=tmp_path / "missing.md"), "build_packet", {"ref": "1"}
    )
    assert text.startswith("No verified facts yet")
    assert "private/verified.md" in text


def test_build_packet_with_nothing_ticked_explains(db, root, tmp_path):
    path = tmp_path / "verified.md"
    path.write_text("## Experience\n\n- [ ] Not confirmed yet.\n")
    text = call(server_for(db, root, facts_path=path), "build_packet", {"ref": "1"})
    assert "no ticked facts" in text
    assert "private/verified.md" in text


def test_build_packet_unknown_ref(db, root, facts_file):
    text = call(server_for(db, root, facts_path=facts_file), "build_packet", {"ref": "9"})
    assert text == "#9 not in digest 2"


def test_facts_resource_has_ticked_items_only(db, root, facts_file):
    text = read(server_for(db, root, facts_path=facts_file), "jobhunt://facts/verified")
    for fact in TICKED:
        assert fact in text
    for unticked in UNTICKED:
        assert unticked not in text


def test_facts_resource_without_file(db, root):
    text = read(server_for(db, root), "jobhunt://facts/verified")
    assert text.startswith("No verified facts yet")


def test_roles_and_boards_resources(db, root):
    server = server_for(db, root)
    assert "solutions engineer" in read(server, "jobhunt://config/roles")
    boards = read(server, "jobhunt://boards")
    assert "greenhouse:acme (Acme)" in boards
    assert "lever:globex (Globex)" in boards


# --------------------------------------------------------------------------- refresh_boards


class FakeAdapter:
    def __init__(self, ats, result):
        self.ats = ats
        self.result = result
        self.calls: list[str] = []

    def fetch(self, board, *, with_descriptions=False):
        self.calls.append(board.key())
        if isinstance(self.result, Exception):
            raise self.result
        return list(self.result)


def test_refresh_boards_polls_and_reports_errors_per_board(db, root):
    with SqliteStore(db) as store:
        store.upsert_board(INITECH, now=NOW)
    new_job = make_job(ACME, 4, "Solutions Engineer")
    adapters = {
        "greenhouse": FakeAdapter("greenhouse", [A1, A2, A3, new_job]),
        "lever": FakeAdapter("lever", RuntimeError("boom")),
        "ashby": FakeAdapter("ashby", BoardNotFound()),
    }
    text = call(
        server_for(db, root, adapters=adapters),
        "refresh_boards",
        {"board_keys": ["greenhouse:acme", "lever:globex", "ashby:initech", "gem:nope"]},
    )
    lines = text.splitlines()
    assert lines[0] == "- greenhouse:acme: 4 fetched, 4 relevant, 1 new, 0 closed"
    assert lines[1] == f"  - new: Acme — Solutions Engineer ({new_job.uid})"
    assert lines[2] == "- lever:globex: failed: RuntimeError: boom"
    assert lines[3] == "- ashby:initech: board not found"
    assert lines[4] == "- gem:nope: not a registered board (see jobhunt://boards)"
    assert lines[5] == "New jobs stay unscored until the next daily run."
    with SqliteStore(db) as store:
        assert store.get_job(new_job.uid).score is None


def test_refresh_boards_refuses_more_than_max_boards(db, root):
    adapter = FakeAdapter("greenhouse", [])
    server = server_for(db, root, adapters={"greenhouse": adapter})
    keys = ["greenhouse:a", "greenhouse:b", "greenhouse:c"]
    text = call(server, "refresh_boards", {"board_keys": keys, "max_boards": 2})
    assert text.startswith("Refusing to poll 3 boards; the limit is 2")
    many = [f"greenhouse:b{i}" for i in range(11)]
    text = call(server, "refresh_boards", {"board_keys": many, "max_boards": 50})
    assert text.startswith("Refusing to poll 11 boards; the limit is 10")
    assert adapter.calls == []


# --------------------------------------------------------------------------- discover_companies


class FakeSearch:
    def __init__(self, urls=(), error=None):
        self.urls = list(urls)
        self.error = error
        self.queries: list[str] = []
        self.closed = False

    def search(self, query, *, count=10):
        self.queries.append(query)
        if self.error:
            raise self.error
        return self.urls

    def close(self):
        self.closed = True


def test_discover_companies_registers_new_boards(db, root):
    search = FakeSearch(
        ["https://boards.greenhouse.io/newco/jobs/1", "https://boards.greenhouse.io/acme"]
    )
    server = server_for(db, root, search_client_factory=lambda: search)
    text = call(server, "discover_companies", {"max_queries": 3})
    assert text.startswith("Ran 3 search queries")
    assert "1 new board(s)" in text
    assert "- greenhouse:newco" in text
    assert "only registered" in text
    assert len(search.queries) == 3
    assert search.closed
    assert "greenhouse:newco" in read(server, "jobhunt://boards")


def test_discover_companies_caps_queries(db, root):
    search = FakeSearch()
    call(
        server_for(db, root, search_client_factory=lambda: search),
        "discover_companies",
        {"max_queries": 500},
    )
    assert len(search.queries) == mcp_server.MAX_QUERIES


def test_discover_companies_errors_become_messages(db, root):
    def unconfigured():
        raise SearchConfigError("SEARCH_API_KEY is not set.")

    text = call(server_for(db, root, search_client_factory=unconfigured), "discover_companies")
    assert text == "Discovery is not configured: SEARCH_API_KEY is not set."

    failing = FakeSearch(error=httpx.ConnectError("offline"))
    text = call(server_for(db, root, search_client_factory=lambda: failing), "discover_companies")
    assert text == "Search failed: ConnectError: offline"
    assert failing.closed


# --------------------------------------------------------------------------- prompts


def test_morning_triage_lists_digest_and_pipeline(db, root):
    with SqliteStore(db) as store:
        store.set_status(A1.uid, "applied")
    text = prompt(server_for(db, root), "morning_triage")
    assert "Digest #2 (3 items):" in text
    assert "- #1 Globex — Solutions Architect · score 80 (se)" in text
    assert "why: fits Customer Engineer" in text
    assert "## applied (1)" in text
    assert "update_status" in text


def test_prep_application_never_submits(db, root):
    text = prompt(server_for(db, root), "prep_application", {"ref": "3"})
    assert 'build_packet with ref "3"' in text
    assert "never click submit" in text
    assert 'refs ["3"]' in text
    assert '"applied"' in text


# --------------------------------------------------------------------------- Streamable HTTP

REMOTE = "https://jobhunt.example.test"


def http_tools_list(server, base_url: str):
    """POST tools/list to the server's Streamable HTTP app with the given Host."""
    with TestClient(server.streamable_http_app(), base_url=base_url) as client:
        return client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-06-18",
            },
        )


def test_defaults_keep_the_stdio_settings(db, root):
    settings = server_for(db, root).settings
    assert settings.host == "127.0.0.1"
    assert settings.stateless_http is False
    assert settings.json_response is False
    assert settings.auth is None


@pytest.mark.parametrize(("base_url", "status"), [(REMOTE, 421), ("http://127.0.0.1:8000", 200)])
def test_http_allows_only_localhost_by_default(db, root, base_url, status):
    server = server_for(db, root, stateless_http=True, json_response=True)
    assert http_tools_list(server, base_url).status_code == status


def test_http_accepts_the_host_transport_security_allows(db, root):
    security = TransportSecuritySettings(
        allowed_hosts=["jobhunt.example.test"], allowed_origins=[REMOTE]
    )
    server = server_for(
        db,
        root,
        transport_security=security,
        host="0.0.0.0",
        stateless_http=True,
        json_response=True,
    )
    reply = http_tools_list(server, REMOTE)
    assert reply.status_code == 200, reply.text
    assert "search_jobs" in {tool["name"] for tool in reply.json()["result"]["tools"]}


# --------------------------------------------------------------------------- main


def test_main_refuses_to_start_without_a_database(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert mcp_server.main([]) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert "DATABASE_URL" in err
    assert "--db" in err


def test_main_serves_the_given_database(monkeypatch, tmp_path, root, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    served = {}

    def fake_run(self, transport="stdio", mount_path=None):
        served["transport"] = transport
        served["server"] = self

    monkeypatch.setattr(mcp_server.FastMCP, "run", fake_run)
    assert mcp_server.main(["--db", "jobs.db", "--root", str(root)]) == 0
    assert served["transport"] == "stdio"
    assert (root / "jobs.db").is_file()  # a relative --db is resolved against --root
    assert capsys.readouterr().out == ""
