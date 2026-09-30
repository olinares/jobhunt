"""Tests for PoliteClient and the Greenhouse, Lever and Ashby adapters.

No test touches the network: PoliteClient tests use httpx.MockTransport, and adapter
tests use respx to serve responses recorded once from real boards (tests/fixtures/).
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from jobhunt.adapters.ashby import AshbyAdapter
from jobhunt.adapters.greenhouse import GreenhouseAdapter
from jobhunt.adapters.http import DEFAULT_USER_AGENT, PoliteClient
from jobhunt.adapters.lever import LeverAdapter
from jobhunt.models import BoardNotFound, BoardRef

FIXTURES = Path(__file__).parent / "fixtures"

GREENHOUSE_URL = "https://boards-api.greenhouse.io/v1/boards/anthropic/jobs"
LEVER_URL = "https://api.lever.co/v0/postings/zoox"
ASHBY_URL = "https://api.ashbyhq.com/posting-api/job-board/openai"


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


def fast_client(**kwargs) -> PoliteClient:
    kwargs.setdefault("min_interval", 0)
    kwargs.setdefault("sleep", lambda _seconds: None)
    return PoliteClient(**kwargs)


def by_id(jobs):
    return {job.external_id: job for job in jobs}


# --------------------------------------------------------------------------- PoliteClient


class Recorder:
    """A MockTransport handler that replays a list of responses and records requests."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0)


def test_client_sends_user_agent_and_decodes_json():
    handler = Recorder(httpx.Response(200, json={"ok": True}))
    with PoliteClient(transport=httpx.MockTransport(handler), min_interval=0) as client:
        assert client.get_json("https://a.example/x", params={"q": "1"}) == {"ok": True}
    assert handler.requests[0].headers["User-Agent"] == DEFAULT_USER_AGENT
    assert handler.requests[0].url.params["q"] == "1"


def test_client_post_json_sends_payload():
    handler = Recorder(httpx.Response(200, json=[1, 2]))
    client = PoliteClient(transport=httpx.MockTransport(handler), min_interval=0)
    assert client.post_json("https://a.example/x", {"limit": 20}) == [1, 2]
    assert handler.requests[0].method == "POST"
    assert json.loads(handler.requests[0].content) == {"limit": 20}
    client.close()


def test_client_waits_between_requests_to_the_same_host_only():
    sleeps: list[float] = []
    handler = Recorder(*(httpx.Response(200, json={}) for _ in range(3)))
    client = PoliteClient(
        transport=httpx.MockTransport(handler), min_interval=1.0, sleep=sleeps.append
    )
    client.get_json("https://a.example/1")
    assert sleeps == []  # first request to a host never waits
    client.get_json("https://b.example/1")
    assert sleeps == []  # different host, no wait
    client.get_json("https://a.example/2")
    assert len(sleeps) == 1 and 0.5 < sleeps[0] <= 1.0


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_client_retries_retryable_statuses_with_exponential_backoff(status):
    sleeps: list[float] = []
    handler = Recorder(
        httpx.Response(status), httpx.Response(status), httpx.Response(200, json={"ok": 1})
    )
    client = fast_client(transport=httpx.MockTransport(handler), backoff=0.5, sleep=sleeps.append)
    assert client.get_json("https://a.example/x") == {"ok": 1}
    assert sleeps == [0.5, 1.0]
    assert len(handler.requests) == 3


def test_client_honours_retry_after_seconds():
    sleeps: list[float] = []
    handler = Recorder(
        httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, json={})
    )
    client = fast_client(transport=httpx.MockTransport(handler), sleep=sleeps.append)
    client.get_json("https://a.example/x")
    assert sleeps == [7.0]


def test_client_gives_up_after_max_retries():
    sleeps: list[float] = []
    handler = Recorder(*(httpx.Response(503) for _ in range(3)))
    client = fast_client(transport=httpx.MockTransport(handler), max_retries=2, sleep=sleeps.append)
    with pytest.raises(httpx.HTTPStatusError) as exc:
        client.get_json("https://a.example/x")
    assert exc.value.response.status_code == 503
    assert len(handler.requests) == 3  # one try + two retries
    assert sleeps == [1.0, 2.0]


@pytest.mark.parametrize("status", [400, 403, 404])
def test_client_raises_immediately_on_other_errors(status):
    handler = Recorder(httpx.Response(status))
    client = fast_client(transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPStatusError) as exc:
        client.get_json("https://a.example/x")
    assert exc.value.response.status_code == status
    assert len(handler.requests) == 1


# --------------------------------------------------------------------------- Greenhouse

GH_BOARD = BoardRef("greenhouse", "anthropic")


@respx.mock
def test_greenhouse_maps_jobs_and_parses_pay_from_content():
    route = respx.get(GREENHOUSE_URL).respond(json=load("greenhouse_anthropic.json"))
    jobs = GreenhouseAdapter(fast_client()).fetch(GH_BOARD)

    assert route.calls.last.request.url.params["content"] == "true"
    assert len(jobs) == 4
    jobs = by_id(jobs)

    fde = jobs["5302966008"]
    assert fde.title == "Forward Deployed Engineer"
    assert fde.company == "Anthropic"
    assert fde.url == "https://job-boards.greenhouse.io/anthropic/jobs/5302966008"
    assert fde.locations == ["New York City, NY; San Francisco, CA; Seattle, WA"]
    assert fde.remote is False
    assert fde.department == "Applied AI"
    assert fde.posted_at == datetime.fromisoformat("2026-08-12T16:21:16-04:00")
    assert (fde.pay_min, fde.pay_max, fde.pay_currency, fde.pay_period) == (
        280000.0,
        320000.0,
        "USD",
        "year",
    )
    assert fde.uid == "greenhouse:anthropic:5302966008"
    assert fde.description_html is None
    assert "content" not in fde.raw

    munich = jobs["5391016008"]
    assert munich.locations == ["Munich, Germany"]
    assert (munich.pay_min, munich.pay_max, munich.pay_currency) == (None, None, None)

    assert jobs["5057647008"].remote is None  # "Location Type" present but empty
    assert jobs["5436615008"].remote is True
    assert jobs["5436615008"].pay_min == 230000.0


@respx.mock
def test_greenhouse_with_descriptions_returns_unescaped_html():
    respx.get(GREENHOUSE_URL).respond(json=load("greenhouse_anthropic.json"))
    job = by_id(GreenhouseAdapter(fast_client()).fetch(GH_BOARD, with_descriptions=True))[
        "5302966008"
    ]
    assert job.description_html.startswith("<div")
    assert "&lt;" not in job.description_html[:50]
    assert "content" in job.raw


@respx.mock
def test_greenhouse_404_raises_board_not_found():
    respx.get(GREENHOUSE_URL).respond(404, json={"status": 404, "error": "Job not found"})
    with pytest.raises(BoardNotFound):
        GreenhouseAdapter(fast_client()).fetch(GH_BOARD)


# --------------------------------------------------------------------------- Lever

LV_BOARD = BoardRef("lever", "zoox", company_name="Zoox")


@respx.mock
def test_lever_maps_jobs_and_salary_range():
    route = respx.get(LEVER_URL).respond(json=load("lever_zoox.json"))
    jobs = LeverAdapter(fast_client()).fetch(LV_BOARD)

    assert route.calls.last.request.url.params["mode"] == "json"
    assert len(jobs) == 4
    jobs = by_id(jobs)

    first = jobs["f4746da4-8eb8-43e2-b7ce-bf3c7cf9640d"]
    assert first.title == "Autonomy System Test Engineer"
    assert first.company == "Zoox"
    assert first.url == "https://jobs.lever.co/zoox/f4746da4-8eb8-43e2-b7ce-bf3c7cf9640d"
    assert first.locations == ["Foster City, CA"]
    assert first.remote is False  # hybrid
    assert first.department == "Software"
    assert first.posted_at == datetime.fromtimestamp(1777936261.125, tz=UTC)
    assert (first.pay_min, first.pay_max, first.pay_currency, first.pay_period) == (
        144000.0,
        193000.0,
        "USD",
        "year",
    )
    assert first.description_html is None
    assert "description" not in first.raw and "salaryRange" in first.raw

    hourly = jobs["d238c5ef-cb10-4b64-954b-443ae0e792ba"]
    assert (hourly.pay_min, hourly.pay_max, hourly.pay_period) == (66.0, 84.0, "hour")

    multi = jobs["082ed20c-b8e1-4b1c-9c22-4738ad94055d"]
    assert multi.locations == ["San Diego, CA", "Foster City, CA"]

    unpaid = jobs["5fad000d-791f-4a0a-96da-65ca594160a9"]
    assert (unpaid.pay_min, unpaid.pay_max, unpaid.pay_currency, unpaid.pay_period) == (
        None,
        None,
        None,
        None,
    )


@respx.mock
def test_lever_company_falls_back_to_slug_and_descriptions_include_lists():
    respx.get(LEVER_URL).respond(json=load("lever_zoox.json"))
    jobs = LeverAdapter(fast_client()).fetch(BoardRef("lever", "zoox"), with_descriptions=True)
    job = by_id(jobs)["f4746da4-8eb8-43e2-b7ce-bf3c7cf9640d"]
    assert job.company == "zoox"
    raw = load("lever_zoox.json")[0]
    assert raw["description"] in job.description_html
    assert raw["lists"][0]["text"] in job.description_html
    assert raw["additional"] in job.description_html


@respx.mock
def test_lever_404_raises_board_not_found():
    respx.get(LEVER_URL).respond(404, json={"ok": False, "error": "Document not found"})
    with pytest.raises(BoardNotFound):
        LeverAdapter(fast_client()).fetch(LV_BOARD)


# --------------------------------------------------------------------------- Ashby

AB_BOARD = BoardRef("ashby", "openai", company_name="OpenAI")


@respx.mock
def test_ashby_maps_jobs_and_compensation():
    route = respx.get(ASHBY_URL).respond(json=load("ashby_openai.json"))
    jobs = AshbyAdapter(fast_client()).fetch(AB_BOARD)

    assert route.calls.last.request.url.params["includeCompensation"] == "true"
    assert len(jobs) == 5
    jobs = by_id(jobs)

    fde = jobs["00207abc-49b7-465c-a219-f7c1140f8047"]
    assert fde.title == "Forward Deployed Software Engineer - SF"
    assert fde.company == "OpenAI"
    assert fde.url == "https://jobs.ashbyhq.com/openai/00207abc-49b7-465c-a219-f7c1140f8047"
    assert fde.locations == ["San Francisco"]
    assert fde.remote is False  # Hybrid, even though isRemote is true
    assert fde.department == "Forward Deployed Engineering"
    assert fde.posted_at == datetime(2025, 11, 15, 1, 25, 39, 198000, tzinfo=UTC)
    assert (fde.pay_min, fde.pay_max, fde.pay_currency, fde.pay_period) == (
        185000.0,
        325000.0,
        "USD",
        "year",
    )
    assert fde.description_html is None
    assert "descriptionHtml" not in fde.raw

    remote = jobs["c414f15f-60c4-40b5-a896-d2ad6c7f4415"]
    assert remote.remote is True
    assert remote.locations == [
        "US - Remote",
        "New York City",
        "Seattle",
        "Washington, DC",
        "San Francisco",
    ]
    assert (remote.pay_min, remote.pay_max) == (165400.0, 285000.0)

    hourly = jobs["49ae54dc-3d33-4107-8112-63fac1ee86ca"]
    assert (hourly.pay_min, hourly.pay_max, hourly.pay_period) == (60.58, 108.17, "hour")

    tokyo = jobs["51b17595-3a70-43be-a333-3a3952303284"]
    assert (tokyo.pay_min, tokyo.pay_max, tokyo.pay_currency, tokyo.pay_period) == (
        None,
        None,
        None,
        None,
    )

    assert jobs["f763c6b3-5167-4a67-b691-4c3fa2c44156"].remote is None  # no workplace info


@respx.mock
def test_ashby_falls_back_to_tiers_and_skips_unlisted_jobs():
    data = copy.deepcopy(load("ashby_openai.json"))
    for job in data["jobs"]:
        if job["id"] == "c414f15f-60c4-40b5-a896-d2ad6c7f4415":
            job["compensation"]["summaryComponents"] = []
        if job["id"] == "51b17595-3a70-43be-a333-3a3952303284":
            job["isListed"] = False
    respx.get(ASHBY_URL).respond(json=data)

    jobs = by_id(AshbyAdapter(fast_client()).fetch(AB_BOARD, with_descriptions=True))
    assert "51b17595-3a70-43be-a333-3a3952303284" not in jobs
    remote = jobs["c414f15f-60c4-40b5-a896-d2ad6c7f4415"]
    assert (remote.pay_min, remote.pay_max, remote.pay_currency) == (165400.0, 285000.0, "USD")
    assert remote.description_html.startswith("<")


@respx.mock
def test_ashby_404_raises_board_not_found():
    respx.get(ASHBY_URL).respond(404, text="Not Found")
    with pytest.raises(BoardNotFound):
        AshbyAdapter(fast_client()).fetch(AB_BOARD)


@respx.mock
def test_adapters_propagate_non_404_errors():
    respx.get(ASHBY_URL).respond(403)
    with pytest.raises(httpx.HTTPStatusError):
        AshbyAdapter(fast_client()).fetch(AB_BOARD)
