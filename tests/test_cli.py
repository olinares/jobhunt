"""`jobhunt run` from the command line, against recorded fixtures (no network)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import respx

from jobhunt import cli
from jobhunt.adapters.http import PoliteClient
from jobhunt.models import BoardRef, Job

ROOT = Path(__file__).parent.parent
FIXTURES = Path(__file__).parent / "fixtures"
GH_URL = "https://boards-api.greenhouse.io/v1/boards/anthropic/jobs"
ASHBY_URL = "https://api.ashbyhq.com/posting-api/job-board/openai"


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(autouse=True)
def no_delays(monkeypatch):
    monkeypatch.setattr(
        cli, "PoliteClient", lambda: PoliteClient(min_interval=0, backoff=0, sleep=lambda _: None)
    )
    monkeypatch.delenv("SEARCH_API_KEY", raising=False)


@pytest.fixture
def seeds(tmp_path):
    path = tmp_path / "seeds.yaml"
    path.write_text(
        "boards:\n"
        '  - {company: Anthropic, url: "https://job-boards.greenhouse.io/anthropic"}\n'
        '  - {company: OpenAI, url: "https://jobs.ashbyhq.com/openai"}\n'
    )
    return path


def run_cli(tmp_path, seeds, *extra: str) -> int:
    return cli.main(
        [
            "run",
            "--config",
            str(ROOT / "config" / "roles.yaml"),
            "--seeds",
            str(seeds),
            "--db",
            str(tmp_path / "jobs.db"),
            *extra,
        ]
    )


@respx.mock
def test_run_prints_new_relevant_jobs_then_nothing_new(tmp_path, seeds, capsys):
    respx.get(GH_URL).respond(json=load("greenhouse_anthropic.json"))
    respx.get(ASHBY_URL).respond(json=load("ashby_openai.json"))

    assert run_cli(tmp_path, seeds) == 0
    first = capsys.readouterr()
    lines = first.out.strip().splitlines()
    assert len([line for line in lines if "https://" in line]) == 3
    assert "Forward Deployed Engineer · Anthropic" in first.out
    assert "Munich" not in first.out  # right title, wrong region
    assert "3 new relevant job(s). Polled 2 board(s)" in first.out
    assert "SEARCH_API_KEY is not set" in first.err

    assert run_cli(tmp_path, seeds) == 0
    second = capsys.readouterr().out
    assert "0 new relevant job(s)" in second
    assert "https://" not in second

    assert run_cli(tmp_path, seeds, "--all") == 0
    assert "3 relevant job(s)" in capsys.readouterr().out


@respx.mock
def test_no_discover_flag_skips_the_search_notice(tmp_path, seeds, capsys):
    respx.get(GH_URL).respond(json=load("greenhouse_anthropic.json"))
    respx.get(ASHBY_URL).respond(json=load("ashby_openai.json"))

    assert run_cli(tmp_path, seeds, "--no-discover") == 0
    assert capsys.readouterr().err == ""


@respx.mock
def test_exit_code_is_1_when_every_board_fails(tmp_path, seeds, capsys):
    respx.get(GH_URL).respond(404)
    respx.get(ASHBY_URL).respond(404)

    assert run_cli(tmp_path, seeds, "--no-discover") == 1
    out = capsys.readouterr().out
    assert "Failed greenhouse:anthropic: board not found" in out
    assert "Failed ashby:openai: board not found" in out


def test_bad_config_exits_2(tmp_path, seeds, capsys):
    bad = tmp_path / "roles.yaml"
    bad.write_text("titles: not-a-list\n")
    code = cli.main(["run", "--config", str(bad), "--seeds", str(seeds), "--db", ":memory:"])
    assert code == 2
    assert capsys.readouterr().err.startswith("error:")


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        (
            {
                "locations": ["San Francisco, CA"],
                "pay_min": 200000.0,
                "pay_max": 250000.0,
                "pay_currency": "USD",
                "pay_period": "year",
            },
            "SE · Acme · San Francisco, CA · USD 200,000–250,000/year · https://x",
        ),
        ({"remote": True}, "SE · Acme · Remote · https://x"),
        ({"locations": ["A", "B", "C", "D", "E"]}, "SE · Acme · A; B; C (+2 more) · https://x"),
        (
            {"pay_min": 90.0, "pay_max": 90.0, "pay_period": "hour"},
            "SE · Acme · 90/hour · https://x",
        ),
        ({}, "SE · Acme · https://x"),
    ],
)
def test_format_job(fields, expected):
    job = Job(BoardRef("greenhouse", "acme"), "1", "SE", "Acme", "https://x", **fields)
    assert cli.format_job(job) == expected
