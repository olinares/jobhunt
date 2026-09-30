"""End-to-end pipeline runs against the recorded fixtures (no network)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from jobhunt.adapters import ADAPTERS, build_adapters
from jobhunt.adapters.gem import GRAPHQL_URL as GEM_URL
from jobhunt.adapters.http import PoliteClient
from jobhunt.config import ConfigError, load_config
from jobhunt.models import BoardRef
from jobhunt.pipeline import discover, load_seeds, run
from jobhunt.store import SqliteStore

ROOT = Path(__file__).parent.parent
FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)

GH_URL = "https://boards-api.greenhouse.io/v1/boards/anthropic/jobs"
LEVER_URL = "https://api.lever.co/v0/postings/zoox"
ASHBY_URL = "https://api.ashbyhq.com/posting-api/job-board/openai"
WD_HOST = "nvidia.wd5.myworkdayjobs.com"
WD_SITE = "NVIDIAExternalCareerSite"
WD_BASE = f"https://{WD_HOST}/wday/cxs/nvidia/{WD_SITE}"
WD_DETAIL_PATH = (
    "/job/US-VA-Remote/Senior-Solutions-Architect--Ethernet-Networking---NVIS_JR2016460"
)

SEEDS = [
    BoardRef("greenhouse", "anthropic", company_name="Anthropic"),
    BoardRef("lever", "zoox", company_name="Zoox"),
    BoardRef("ashby", "openai", company_name="OpenAI"),
    BoardRef("workday", "nvidia", host=WD_HOST, site=WD_SITE, company_name="NVIDIA"),
]

# What the filter should keep from the fixtures, given config/roles.yaml.
EXPECTED_RELEVANT = {
    "greenhouse:anthropic:5302966008",  # Forward Deployed Engineer, SF/NYC/Seattle
    "greenhouse:anthropic:5057647008",  # Applied AI Engineer, SF | NYC | Seattle
    "ashby:openai:00207abc-49b7-465c-a219-f7c1140f8047",  # FDSE - SF
    # Listed as "N Locations"; its detail page says "US, VA, Remote".
    f"workday:{WD_HOST}/{WD_SITE}:JR2016460",
}


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


def workday_page1(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=load("workday_nvidia_page1.json"))


@pytest.fixture
def cfg():
    return load_config(ROOT / "config" / "roles.yaml")


@pytest.fixture
def client():
    with PoliteClient(min_interval=0, backoff=0, sleep=lambda _: None) as c:
        yield c


@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "jobs.db") as s:
        yield s


@pytest.fixture
def boards():
    """Serve all four seed boards; Workday details exist only for the recorded job."""
    with respx.mock(assert_all_called=False) as router:
        router.get(GH_URL).respond(json=load("greenhouse_anthropic.json"))
        router.get(LEVER_URL).respond(json=load("lever_zoox.json"))
        router.get(ASHBY_URL).respond(json=load("ashby_openai.json"))
        router.post(f"{WD_BASE}/jobs").mock(side_effect=workday_page1)
        router.get(f"{WD_BASE}{WD_DETAIL_PATH}").respond(json=load("workday_nvidia_job.json"))
        router.get(url__startswith=f"{WD_BASE}/job/").respond(404)
        yield router


def uids(jobs) -> set[str]:
    return {job.uid for job in jobs}


def db_rows(store_path: Path, sql: str) -> list[tuple]:
    with sqlite3.connect(store_path) as conn:
        return conn.execute(sql).fetchall()


# --- registry ---------------------------------------------------------------------------


def test_every_ats_has_an_adapter_sharing_one_client(client):
    adapters = build_adapters(client)
    assert set(adapters) == set(ADAPTERS) == {"greenhouse", "lever", "ashby", "workday", "gem"}
    assert all(adapter._client is client for adapter in adapters.values())


# --- run --------------------------------------------------------------------------------


def test_run_keeps_only_relevant_jobs(boards, client, store, cfg, tmp_path):
    report = run(store, build_adapters(client), cfg, seeds=SEEDS, now=NOW)

    assert not report.failed
    assert uids(report.new_jobs) == uids(report.matched_jobs) == EXPECTED_RELEVANT
    stored = {row[0] for row in db_rows(tmp_path / "jobs.db", "SELECT uid FROM jobs")}
    assert stored == EXPECTED_RELEVANT
    by_board = {b.board.key(): b for b in report.boards}
    assert by_board["lever:zoox"].matched == 0
    assert by_board["greenhouse:anthropic"].fetched == 4
    assert {job.company for job in report.matched_jobs} == {"Anthropic", "OpenAI", "NVIDIA"}


def test_workday_details_fetched_only_for_title_matches_without_location(
    boards, client, store, cfg
):
    run(store, build_adapters(client), cfg, seeds=SEEDS, now=NOW)

    detail_calls = [
        call
        for call in boards.calls
        if call.request.url.path.startswith("/wday/cxs/nvidia/")
        and "/job/" in call.request.url.path
    ]
    # 10 postings on the page: 3 name a location, 7 say "N Locations".
    assert len(detail_calls) == 7


def test_second_run_finds_nothing_new(boards, client, store, cfg):
    adapters = build_adapters(client)
    run(store, adapters, cfg, seeds=SEEDS, now=NOW)
    again = run(store, adapters, cfg, seeds=SEEDS, now=NOW + timedelta(days=1))

    assert again.new_jobs == []
    assert uids(again.matched_jobs) == EXPECTED_RELEVANT


def test_job_gone_from_board_is_closed(boards, client, store, cfg, tmp_path):
    adapters = build_adapters(client)
    run(store, adapters, cfg, seeds=SEEDS, now=NOW)

    data = load("greenhouse_anthropic.json")
    data["jobs"] = [job for job in data["jobs"] if str(job["id"]) != "5302966008"]
    boards.get(GH_URL).respond(json=data)
    report = run(store, adapters, cfg, seeds=SEEDS, now=NOW + timedelta(days=1))

    by_board = {b.board.key(): b for b in report.boards}
    assert by_board["greenhouse:anthropic"].closed == 1
    closed = db_rows(
        tmp_path / "jobs.db", "SELECT uid, status FROM jobs WHERE closed_at IS NOT NULL"
    )
    assert closed == [("greenhouse:anthropic:5302966008", "closed")]


def test_missing_board_is_reported_and_run_continues(boards, client, store, cfg):
    boards.get(LEVER_URL).respond(404, json={"ok": False, "error": "Document not found"})
    report = run(store, build_adapters(client), cfg, seeds=SEEDS, now=NOW)

    assert [(b.board.key(), b.error) for b in report.failed] == [("lever:zoox", "board not found")]
    assert uids(report.matched_jobs) == EXPECTED_RELEVANT
    assert not report.all_failed


def test_unexpected_error_on_one_board_does_not_stop_the_run(boards, client, store, cfg):
    boards.get(GH_URL).respond(500)
    report = run(store, build_adapters(client), cfg, seeds=SEEDS, now=NOW)

    assert [b.board.key() for b in report.failed] == ["greenhouse:anthropic"]
    assert "HTTPStatusError" in report.failed[0].error
    assert len(report.boards) == 4


def test_relevant_hits_recorded_only_for_boards_with_matches(boards, client, store, cfg, tmp_path):
    run(store, build_adapters(client), cfg, seeds=SEEDS, now=NOW)

    rows = dict(db_rows(tmp_path / "jobs.db", "SELECT board_key, last_relevant_hit FROM companies"))
    assert rows["lever:zoox"] is None
    assert rows["greenhouse:anthropic"] is not None


def test_relevant_jobs_are_stored_with_descriptions(boards, client, store, cfg, tmp_path):
    run(store, build_adapters(client), cfg, seeds=SEEDS, now=NOW)

    rows = db_rows(tmp_path / "jobs.db", "SELECT uid, description_html FROM jobs")
    assert {uid for uid, _ in rows} == EXPECTED_RELEVANT
    assert all(html for _, html in rows)
    # The scorer reads jobs back from the store, descriptions included.
    assert {job.uid for job in store.jobs_to_score(10) if job.description_html} == (
        EXPECTED_RELEVANT
    )


def test_listing_payload_descriptions_cost_no_extra_requests(boards, client, store, cfg):
    run(store, build_adapters(client), cfg, seeds=SEEDS, now=NOW)

    for url in (GH_URL, ASHBY_URL, LEVER_URL):
        assert [str(c.request.url).split("?")[0] for c in boards.calls].count(url) == 1


# --- gem: details only for new relevant jobs ---------------------------------------------

GEM_SEED = BoardRef("gem", "gem", company_name="Gem")


def gem_handler(request: httpx.Request) -> httpx.Response:
    details = {
        item["data"]["oatsExternalJobPosting"]["extId"]: item for item in load("gem_gem_jobs.json")
    }
    out = []
    for op in json.loads(request.content):
        if op["operationName"] == "JobBoardList":
            out.append(load("gem_gem_board.json")[0])
        else:
            out.append(
                details.get(op["variables"]["extId"], {"data": {"oatsExternalJobPosting": None}})
            )
    return httpx.Response(200, json=out)


def detail_ext_ids(route, *, since: int = 0) -> list[list[str]]:
    batches = [json.loads(call.request.content) for call in list(route.calls)[since:]]
    return [
        [op["variables"]["extId"] for op in batch]
        for batch in batches
        if batch[0]["operationName"] == "ExternalJobPosting"
    ]


@respx.mock
def test_gem_details_fetched_only_for_new_relevant_jobs(client, store, cfg, tmp_path):
    route = respx.post(GEM_URL).mock(side_effect=gem_handler)
    # No SE/FDE titles on Gem's own board, so match its engineers for this test.
    cfg = replace(cfg, titles=(*cfg.titles, "software engineer"))
    adapters = build_adapters(client)

    first = run(store, adapters, cfg, seeds=[GEM_SEED], now=NOW)
    matched = [job.external_id for job in first.new_jobs]
    assert matched and len(matched) < 4  # some postings are filtered out
    assert detail_ext_ids(route) == [matched]  # one batch, relevant jobs only
    rows = db_rows(tmp_path / "jobs.db", "SELECT description_html FROM jobs")
    assert rows and all(html for (html,) in rows)

    seen = route.call_count
    run(store, adapters, cfg, seeds=[GEM_SEED], now=NOW + timedelta(days=1))
    assert route.call_count == seen + 1  # the listing only
    assert detail_ext_ids(route, since=seen) == []  # nothing new, no detail requests
    rows = db_rows(tmp_path / "jobs.db", "SELECT description_html FROM jobs")
    assert all(html for (html,) in rows)  # re-listing without details keeps them


@respx.mock
def test_failed_gem_details_do_not_fail_the_board_and_are_retried(client, store, cfg, tmp_path):
    details_down = True

    def handler(request: httpx.Request) -> httpx.Response:
        ops = json.loads(request.content)
        if details_down and ops[0]["operationName"] == "ExternalJobPosting":
            return httpx.Response(503)
        return gem_handler(request)

    route = respx.post(GEM_URL).mock(side_effect=handler)
    cfg = replace(cfg, titles=(*cfg.titles, "software engineer"))
    adapters = build_adapters(client)

    first = run(store, adapters, cfg, seeds=[GEM_SEED], now=NOW)
    [board] = first.boards
    assert board.error is None and board.new > 0  # the listing still counts
    assert "HTTPStatusError" in board.detail_error
    assert first.detail_failures == [board]
    assert not first.failed
    rows = db_rows(tmp_path / "jobs.db", "SELECT description_html FROM jobs")
    assert rows and all(html is None for (html,) in rows)
    # Held back from scoring while the description may still arrive.
    assert store.jobs_to_score(10, now=NOW) == []

    details_down = False
    seen = route.call_count
    second = run(store, adapters, cfg, seeds=[GEM_SEED], now=NOW + timedelta(days=1))
    assert second.new_jobs == []  # not new any more, but still detailed
    assert detail_ext_ids(route, since=seen) == [[job.external_id for job in first.new_jobs]]
    assert not second.detail_failures
    rows = db_rows(tmp_path / "jobs.db", "SELECT description_html FROM jobs")
    assert all(html for (html,) in rows)


# --- discovery --------------------------------------------------------------------------


class FakeSearch:
    def __init__(self, urls: list[str], fail: bool = False) -> None:
        self.urls = urls
        self.fail = fail
        self.queries: list[str] = []

    def search(self, query: str, *, count: int = 10) -> list[str]:
        if self.fail:
            raise httpx.ConnectError("search is down")
        self.queries.append(query)
        return self.urls


def test_discovery_registers_only_new_boards(store):
    store.upsert_board(SEEDS[0], now=NOW)
    search = FakeSearch(
        [
            "https://job-boards.greenhouse.io/anthropic/jobs/5302966008",  # already known
            "https://jobs.ashbyhq.com/newco/1234",
            "https://jobs.ashbyhq.com/newco",  # same board again
            "https://example.com/careers",  # not a board
        ]
    )
    found, report = discover(
        store, search, ["solutions engineer"], ["Remote"], max_queries=1, now=NOW
    )

    assert [b.key() for b in found] == ["ashby:newco"]
    known = {b.key(): b.company_name for b in store.boards_to_poll()}
    assert known == {"greenhouse:anthropic": "Anthropic", "ashby:newco": None}
    assert report.queries_run == 1


def test_discovery_cap_rotates_by_day(store):
    titles = ["solutions engineer", "forward deployed engineer"]
    first, second = FakeSearch([]), FakeSearch([])
    discover(store, first, titles, ["Remote"], max_queries=3, now=NOW)
    _, report = discover(store, second, titles, ["Remote"], max_queries=3, now=NOW + timedelta(1))

    # 6 domains x 2 titles = 12 queries; each day runs 3, starting 3 further along.
    assert len(first.queries) == len(second.queries) == 3
    assert report.capped and report.queries_skipped == 9
    assert not set(first.queries) & set(second.queries)


def test_discovered_board_is_polled_in_the_same_run(boards, client, store, cfg):
    boards.get("https://api.ashbyhq.com/posting-api/job-board/newco").respond(
        json=load("ashby_openai.json")
    )
    search = FakeSearch(["https://jobs.ashbyhq.com/newco"])
    report = run(
        store,
        build_adapters(client),
        cfg,
        seeds=SEEDS,
        search_client=search,
        region_terms=["Remote"],
        max_queries=1,
        now=NOW,
    )

    assert [b.key() for b in report.discovered] == ["ashby:newco"]
    assert "ashby:newco" in {b.board.key() for b in report.boards}
    newco_jobs = [job for job in report.new_jobs if job.board.slug == "newco"]
    assert [job.company for job in newco_jobs] == ["newco"]  # slug until a name is known


def test_search_failure_is_reported_and_polling_continues(boards, client, store, cfg):
    report = run(
        store,
        build_adapters(client),
        cfg,
        seeds=SEEDS,
        search_client=FakeSearch([], fail=True),
        region_terms=["Remote"],
        now=NOW,
    )

    assert "ConnectError" in report.search_error
    assert uids(report.matched_jobs) == EXPECTED_RELEVANT


def test_prune_is_opt_in(boards, client, store, cfg):
    adapters = build_adapters(client)
    run(store, adapters, cfg, seeds=SEEDS, now=NOW)
    later = NOW + timedelta(weeks=10)

    assert run(store, adapters, cfg, now=later).pruned == []
    pruned = run(store, adapters, cfg, prune_weeks=8, now=later + timedelta(days=1)).pruned
    assert pruned == ["lever:zoox"]  # the only board that never had a relevant job


# --- seeds ------------------------------------------------------------------------------


def test_repo_seeds_file_loads():
    seeds = load_seeds(ROOT / "config" / "seeds.yaml")
    assert len(seeds) >= 10
    assert all(seed.company_name for seed in seeds)
    assert {seed.ats for seed in seeds} == {"greenhouse", "lever", "ashby", "workday", "gem"}


def test_seed_with_unrecognised_url_is_a_config_error(tmp_path):
    path = tmp_path / "seeds.yaml"
    path.write_text('boards:\n  - {company: Acme, url: "https://acme.com/careers"}\n')
    with pytest.raises(ConfigError, match="boards\\[0\\]"):
        load_seeds(path)
