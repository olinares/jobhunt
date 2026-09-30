"""Workday and Gem adapters, tested against recorded responses (no network)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
import respx

from jobhunt.adapters.gem import GRAPHQL_URL, GemAdapter, GemError
from jobhunt.adapters.http import PoliteClient
from jobhunt.adapters.workday import WorkdayAdapter, parse_posted_on
from jobhunt.models import BoardNotFound, BoardRef

FIXTURES = Path(__file__).parent / "fixtures"

WD_HOST = "nvidia.wd5.myworkdayjobs.com"
WD_SITE = "NVIDIAExternalCareerSite"
WD_BASE = f"https://{WD_HOST}/wday/cxs/nvidia/{WD_SITE}"
WD_BOARD = BoardRef("workday", "nvidia", host=WD_HOST, site=WD_SITE, company_name="NVIDIA")
WD_DETAIL_PATH = (
    "/job/US-VA-Remote/Senior-Solutions-Architect--Ethernet-Networking---NVIS_JR2016460"
)
# The clock the recorded fixtures were captured on.
RECORDED_ON = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)

GEM_BOARD = BoardRef("gem", "gem")


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def client():
    with PoliteClient(min_interval=0, backoff=0, sleep=lambda _: None) as c:
        yield c


def workday_pages_by_offset(request: httpx.Request) -> httpx.Response:
    """Serve the three recorded pages (page size 10) keyed by the requested offset."""
    body = json.loads(request.content)
    page = {0: 1, 10: 2, 20: 3}.get(body["offset"])
    if page is None:
        # Real Workday wraps around to page one past the end; mimic that.
        page = 1
    return httpx.Response(200, json=load(f"workday_nvidia_page{page}.json"))


# --- Workday ----------------------------------------------------------------------------


@respx.mock
def test_workday_pages_until_total(client):
    route = respx.post(f"{WD_BASE}/jobs").mock(side_effect=workday_pages_by_offset)
    adapter = WorkdayAdapter(client, page_size=10, clock=lambda: RECORDED_ON)

    jobs = adapter.fetch(WD_BOARD)

    assert len(jobs) == 23  # "total" on the first recorded page
    assert len({job.uid for job in jobs}) == 23
    sent = [json.loads(call.request.content) for call in route.calls]
    assert [body["offset"] for body in sent] == [0, 10, 20]
    assert all(body["limit"] == 10 and body["appliedFacets"] == {} for body in sent)


@respx.mock
def test_workday_stops_when_offset_wraps_to_first_page(client):
    """If total were wrong, a past-the-end offset repeats page one; we must not loop."""
    first = load("workday_nvidia_page1.json")
    first["total"] = 1000  # claim more jobs than exist
    pages = {0: first, 10: load("workday_nvidia_page2.json"), 20: first}

    def handler(request):
        return httpx.Response(200, json=pages[json.loads(request.content)["offset"]])

    route = respx.post(f"{WD_BASE}/jobs").mock(side_effect=handler)
    jobs = WorkdayAdapter(client, page_size=10).fetch(WD_BOARD)

    assert len(jobs) == 20
    assert route.call_count == 3


@respx.mock
def test_workday_skips_placeholder_postings_without_ending_paging(client):
    """Some tenants list entries with only bulletFields (seen live on Proofpoint)."""
    second = load("workday_nvidia_page2.json")
    second["jobPostings"][3] = {"bulletFields": ["R14546"]}
    second["jobPostings"][7] = {"bulletFields": ["R14547"]}

    def handler(request):
        offset = json.loads(request.content)["offset"]
        if offset == 10:
            return httpx.Response(200, json=second)
        return workday_pages_by_offset(request)

    route = respx.post(f"{WD_BASE}/jobs").mock(side_effect=handler)
    jobs = WorkdayAdapter(client, page_size=10, clock=lambda: RECORDED_ON).fetch(WD_BOARD)

    # Two placeholders dropped; the third page is still fetched.
    assert len(jobs) == 21
    assert [json.loads(c.request.content)["offset"] for c in route.calls] == [0, 10, 20]
    assert all(job.raw.get("externalPath") for job in jobs)


@respx.mock
def test_workday_maps_listing_fields(client):
    respx.post(f"{WD_BASE}/jobs").mock(side_effect=workday_pages_by_offset)
    jobs = WorkdayAdapter(client, page_size=10, clock=lambda: RECORDED_ON).fetch(WD_BOARD)
    by_id = {job.external_id: job for job in jobs}

    multi = by_id["JR2016460"]
    assert multi.title == "Senior Solutions Architect, Ethernet Networking - NVIS"
    assert multi.company == "NVIDIA"
    assert multi.url == f"https://{WD_HOST}/{WD_SITE}{WD_DETAIL_PATH}"
    assert multi.locations == []  # "6 Locations" is a count, not a place
    assert multi.remote is None
    assert multi.posted_at == datetime(2026, 9, 18, tzinfo=UTC)  # "Posted 11 Days Ago"
    assert multi.description_html is None
    assert multi.uid == f"workday:{WD_HOST}/{WD_SITE}:JR2016460"

    single = by_id["JR2021239"]
    assert single.locations == ["US, CA, Santa Clara"]
    assert single.posted_at == datetime(2026, 9, 29, tzinfo=UTC)  # "Posted Today"

    remote = by_id["JR2023367"]
    assert remote.locations == ["Australia, Remote"]
    assert remote.remote is True
    assert remote.posted_at is None  # "Posted 30+ Days Ago"


@respx.mock
def test_workday_descriptions_come_from_external_path(client):
    respx.post(f"{WD_BASE}/jobs").mock(side_effect=workday_pages_by_offset)
    detail = respx.get(f"{WD_BASE}{WD_DETAIL_PATH}").mock(
        return_value=httpx.Response(200, json=load("workday_nvidia_job.json"))
    )
    # Every other posting 404s, as it would if it closed between listing and detail.
    respx.get(url__startswith=f"{WD_BASE}/job/").mock(return_value=httpx.Response(404))

    jobs = WorkdayAdapter(client, page_size=10).fetch(WD_BOARD, with_descriptions=True)
    job = next(j for j in jobs if j.external_id == "JR2016460")

    assert detail.called
    assert job.description_html.startswith("<p><span>As a key member of the NVIDIA")
    assert job.locations == [
        "US, VA, Remote",
        "US, TX, Remote",
        "US, NY, Remote",
        "US, PA, Remote",
        "US, NJ, Remote",
        "US, SC, Remote",
    ]
    assert job.remote is True
    assert job.posted_at == datetime(2026, 9, 18, tzinfo=UTC)
    assert job.raw["detail"]["jobPostingInfo"]["jobReqId"] == "JR2016460"
    others = [j for j in jobs if j.external_id != "JR2016460"]
    assert all(j.description_html is None for j in others)


@respx.mock
def test_workday_no_detail_requests_without_descriptions(client):
    respx.post(f"{WD_BASE}/jobs").mock(side_effect=workday_pages_by_offset)
    detail = respx.get(url__startswith=f"{WD_BASE}/job/")
    WorkdayAdapter(client, page_size=10).fetch(WD_BOARD)
    assert not detail.called


@respx.mock
def test_workday_unknown_site_is_board_not_found(client):
    board = BoardRef("workday", "nvidia", host=WD_HOST, site="NoSuchSite")
    respx.post(f"https://{WD_HOST}/wday/cxs/nvidia/NoSuchSite/jobs").mock(
        return_value=httpx.Response(404, json=load("workday_nvidia_site_not_found.json"))
    )
    with pytest.raises(BoardNotFound):
        WorkdayAdapter(client).fetch(board)


@respx.mock
def test_workday_unknown_tenant_is_board_not_found(client):
    # Recorded live: an unknown tenant answers 422 with an empty-message error body.
    board = BoardRef("workday", "nosuchtenant", host=WD_HOST, site=WD_SITE)
    respx.post(f"https://{WD_HOST}/wday/cxs/nosuchtenant/{WD_SITE}/jobs").mock(
        return_value=httpx.Response(422, json={"errorCode": "HTTP_422", "httpStatus": 422})
    )
    with pytest.raises(BoardNotFound):
        WorkdayAdapter(client).fetch(board)


def test_workday_requires_host_and_site(client):
    with pytest.raises(ValueError):
        WorkdayAdapter(client).fetch(BoardRef("workday", "nvidia"))


def test_workday_rejects_page_size_over_twenty(client):
    with pytest.raises(ValueError):
        WorkdayAdapter(client, page_size=21)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Posted Today", date(2026, 9, 29)),
        ("Posted Yesterday", date(2026, 9, 28)),
        ("Posted 1 Day Ago", date(2026, 9, 28)),
        ("Posted 7 Days Ago", date(2026, 9, 22)),
        ("Posted 30+ Days Ago", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_posted_on(text, expected):
    result = parse_posted_on(text, date(2026, 9, 29))
    assert (result.date() if result else None) == expected


# --- Gem --------------------------------------------------------------------------------


def gem_handler(request: httpx.Request) -> httpx.Response:
    """Answer a batch with the recorded list or per-extId detail responses."""
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


@respx.mock
def test_gem_lists_board(client):
    route = respx.post(GRAPHQL_URL).mock(side_effect=gem_handler)

    jobs = GemAdapter(client).fetch(GEM_BOARD)

    assert route.call_count == 1
    sent = json.loads(route.calls[0].request.content)
    assert sent[0]["operationName"] == "JobBoardList"
    assert sent[0]["variables"] == {"boardId": "gem"}
    assert len(jobs) == 4

    job = jobs[0]
    assert job.external_id == "4965519002"
    assert job.title == "Software Engineer"
    assert job.company == "Gem"  # from jobBoardExternal.teamDisplayName
    assert job.url == "https://jobs.gem.com/gem/4965519002"
    assert job.locations == ["San Francisco"]
    assert job.remote is False  # HYBRID
    assert job.department == "Engineering"
    assert job.description_html is None
    assert job.uid == "gem:gem:4965519002"
    assert jobs[2].title == "Product Manager"  # trailing space stripped


@respx.mock
def test_gem_descriptions_are_batched(client):
    route = respx.post(GRAPHQL_URL).mock(side_effect=gem_handler)

    jobs = GemAdapter(client).fetch(GEM_BOARD, with_descriptions=True)

    assert route.call_count == 2  # one listing call, one batch with all four details
    detail_ops = json.loads(route.calls[1].request.content)
    assert [op["variables"]["extId"] for op in detail_ops] == [j.external_id for j in jobs]
    assert all(op["operationName"] == "ExternalJobPosting" for op in detail_ops)
    job = jobs[0]
    assert job.description_html.startswith("<h2>Role Details</h2>")
    assert job.posted_at == datetime.fromtimestamp(1605627443, tz=UTC)
    assert all(j.description_html for j in jobs)


@respx.mock
def test_gem_board_name_override(client):
    respx.post(GRAPHQL_URL).mock(side_effect=gem_handler)
    jobs = GemAdapter(client).fetch(BoardRef("gem", "gem", company_name="Gem Inc."))
    assert {j.company for j in jobs} == {"Gem Inc."}


@respx.mock
def test_gem_unknown_board_is_board_not_found(client):
    respx.post(GRAPHQL_URL).mock(
        return_value=httpx.Response(200, json=load("gem_missing_board.json"))
    )
    with pytest.raises(BoardNotFound):
        GemAdapter(client).fetch(BoardRef("gem", "no-such-board-zzq"))


@respx.mock
def test_gem_graphql_errors_raise(client):
    respx.post(GRAPHQL_URL).mock(
        return_value=httpx.Response(200, json=[{"errors": [{"message": "Cannot query field"}]}])
    )
    with pytest.raises(GemError):
        GemAdapter(client).fetch(GEM_BOARD)
