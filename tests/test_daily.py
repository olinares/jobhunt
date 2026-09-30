"""`jobhunt daily` end to end: recorded boards -> SQLite -> fake scorer -> fake SMTP.

No network: boards are served by respx from tests/fixtures, the Anthropic client is a fake
that replays a recorded scoring response, and SMTP is a fake that keeps the messages.
Resumes are placeholders from the environment; the working directory is a temp dir, so the
real `private/resumes/` is never read.
"""

from __future__ import annotations

import json
import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import ClassVar

import anthropic
import httpx
import pytest
import respx
from anthropic.types import Message

from jobhunt import cli
from jobhunt.adapters.http import PoliteClient
from jobhunt.store import SqliteStore

ROOT = Path(__file__).parent.parent
FIXTURES = Path(__file__).parent / "fixtures"
GH_URL = "https://boards-api.greenhouse.io/v1/boards/anthropic/jobs"
ASHBY_URL = "https://api.ashbyhq.com/posting-api/job-board/openai"
RELEVANT = 3  # new relevant jobs across the two seed boards (see test_cli.py)


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


def scored_message() -> Message:
    return Message.model_validate(load("scoring/strong_se.json")["response"])


def api_error(status: int) -> anthropic.APIStatusError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.InternalServerError(
        f"error {status}", response=httpx.Response(status, request=request), body=None
    )


class FakeAnthropic:
    """Answers every request with `responses` in turn, then with the last one."""

    calls: ClassVar[list[dict]] = []
    responses: ClassVar[list] = []

    def __init__(self) -> None:
        self.messages = self

    def create(self, **kwargs):
        FakeAnthropic.calls.append(kwargs)
        result = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(result, Exception):
            raise result
        return result


class FakeSMTP:
    sent: ClassVar[list[EmailMessage]] = []
    fail: ClassVar[bool] = False

    def __init__(self, host, port):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        pass

    def send_message(self, msg):
        if FakeSMTP.fail:
            raise smtplib.SMTPServerDisconnected("connection dropped")
        FakeSMTP.sent.append(msg)


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no private/resumes/ here
    monkeypatch.setattr(
        cli, "PoliteClient", lambda: PoliteClient(min_interval=0, backoff=0, sleep=lambda _: None)
    )
    monkeypatch.setattr(cli, "anthropic_client", FakeAnthropic)
    monkeypatch.setattr(cli, "smtp_factory", FakeSMTP)
    FakeAnthropic.calls = []
    FakeAnthropic.responses = [scored_message()]
    FakeSMTP.sent = []
    FakeSMTP.fail = False
    for name in ("SEARCH_API_KEY", "DATABASE_URL", "DIGEST_TO"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RESUME_SE", "placeholder SE resume")
    monkeypatch.setenv("RESUME_FDE", "placeholder FDE resume")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("GMAIL_ADDRESS", "me@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-pass")


@pytest.fixture
def seeds(tmp_path):
    path = tmp_path / "seeds.yaml"
    path.write_text(
        "boards:\n"
        '  - {company: Anthropic, url: "https://job-boards.greenhouse.io/anthropic"}\n'
        '  - {company: OpenAI, url: "https://jobs.ashbyhq.com/openai"}\n'
    )
    return path


@pytest.fixture
def boards():
    with respx.mock(assert_all_called=False) as router:
        router.get(GH_URL).respond(json=load("greenhouse_anthropic.json"))
        router.get(ASHBY_URL).respond(json=load("ashby_openai.json"))
        yield router


def daily(tmp_path, seeds, *extra: str, db: bool = True, discover: bool = False) -> int:
    args = ["daily", "--config", str(ROOT / "config" / "roles.yaml"), "--seeds", str(seeds)]
    if db:
        args += ["--db", str(tmp_path / "jobs.db")]
    if not discover:
        args.append("--no-discover")
    return cli.main([*args, *extra])


def digest_text(msg: EmailMessage) -> str:
    return msg.get_body(preferencelist=("plain",)).get_content()


def digests(tmp_path) -> list[list[str]]:
    """Every recorded digest's uids, in order."""
    with SqliteStore(tmp_path / "jobs.db") as store:
        out = []
        for digest_id in range(1, (store.latest_digest_id() or 0) + 1):
            uids, n = [], 1
            while uid := store.digest_uid(digest_id, n):
                uids.append(uid)
                n += 1
            out.append(uids)
        return out


# --------------------------------------------------------------------------- happy path


def test_daily_scores_emails_and_records_then_does_not_resend(boards, tmp_path, seeds):
    assert daily(tmp_path, seeds) == 0

    assert len(FakeAnthropic.calls) == RELEVANT
    # Descriptions reached the scorer.
    assert all("<description>" in c["messages"][0]["content"] for c in FakeAnthropic.calls)
    (msg,) = FakeSMTP.sent
    assert f"{RELEVANT} new (top 88)" in msg["Subject"]
    text = digest_text(msg)
    assert "#1 · 88 · SE" in text and f"#{RELEVANT} · 88 · SE" in text
    assert f"{RELEVANT} new · 0 closed · {RELEVANT} scored" in text
    (first,) = digests(tmp_path)
    assert len(first) == RELEVANT

    # Next day: the same jobs are neither scored nor sent again; a heartbeat still goes out.
    assert daily(tmp_path, seeds) == 0
    assert len(FakeAnthropic.calls) == RELEVANT
    assert len(FakeSMTP.sent) == 2
    assert "no new jobs" in FakeSMTP.sent[1]["Subject"]
    assert digests(tmp_path) == [first, []]


def test_digest_numbers_match_the_email(boards, tmp_path, seeds):
    assert daily(tmp_path, seeds) == 0

    text = digest_text(FakeSMTP.sent[0])
    (uids,) = digests(tmp_path)
    for n, uid in enumerate(uids, start=1):
        line = next(line for line in text.splitlines() if line.startswith(f"#{n} · "))
        external_id = uid.rsplit(":", 1)[1]
        block = text[text.index(line) :].split("\n\n", 1)[0]
        assert external_id in block


# --------------------------------------------------------------------------- dry run


def test_dry_run_prints_the_digest_and_records_nothing(
    boards, tmp_path, seeds, capsys, monkeypatch
):
    with monkeypatch.context() as m:
        m.delenv("GMAIL_APP_PASSWORD")  # not needed for a dry run
        assert daily(tmp_path, seeds, "--dry-run") == 0

    out = capsys.readouterr().out
    assert out.startswith("jobhunt digest · ")
    assert "#1 · 88 · SE" in out
    assert FakeSMTP.sent == []
    assert digests(tmp_path) == []

    # The real run afterwards sends the same jobs.
    assert daily(tmp_path, seeds) == 0
    assert f"{RELEVANT} new (top 88)" in FakeSMTP.sent[0]["Subject"]
    assert [len(d) for d in digests(tmp_path)] == [RELEVANT]


# --------------------------------------------------------------------------- failures


def test_failed_email_records_nothing_and_next_run_resends(boards, tmp_path, seeds, capsys):
    FakeSMTP.fail = True
    assert daily(tmp_path, seeds) == 1
    assert "digest email failed: SMTPServerDisconnected" in capsys.readouterr().err
    assert digests(tmp_path) == []

    FakeSMTP.fail = False
    assert daily(tmp_path, seeds) == 0
    assert f"{RELEVANT} new (top 88)" in FakeSMTP.sent[0]["Subject"]
    assert [len(d) for d in digests(tmp_path)] == [RELEVANT]
    assert len(FakeAnthropic.calls) == RELEVANT  # scores were kept, not paid for twice


def test_every_board_failing_exits_1_but_still_emails(boards, tmp_path, seeds):
    boards.get(GH_URL).respond(404)
    boards.get(ASHBY_URL).respond(404)

    assert daily(tmp_path, seeds) == 1
    (msg,) = FakeSMTP.sent
    assert "2 board(s) failed to load" in digest_text(msg)


class FailingSearch:
    def search(self, query: str, *, count: int = 10) -> list[str]:
        raise httpx.HTTPStatusError(
            "401 Unauthorized",
            request=httpx.Request("POST", "https://google.serper.dev/search"),
            response=httpx.Response(401),
        )

    def close(self) -> None:
        pass


def test_discovery_status_is_in_the_footer(boards, tmp_path, seeds, monkeypatch):
    assert daily(tmp_path, seeds) == 0
    assert "Discovery skipped (--no-discover)" in digest_text(FakeSMTP.sent[-1])

    # No SEARCH_API_KEY (the env fixture removes it): skipped, and the email says why.
    assert daily(tmp_path, seeds, discover=True) == 0
    assert "Discovery skipped (SEARCH_API_KEY not set)" in digest_text(FakeSMTP.sent[-1])

    # A rejected key: the run still succeeds, and the failure is in the email.
    monkeypatch.setattr(cli, "_search_client", lambda err: FailingSearch())
    assert daily(tmp_path, seeds, discover=True) == 0
    assert "⚠ Discovery failed: HTTPStatusError: 401 Unauthorized" in digest_text(FakeSMTP.sent[-1])


def test_partial_scoring_failure_is_reported_in_the_footer(boards, tmp_path, seeds):
    FakeAnthropic.responses = [scored_message(), api_error(500), scored_message()]

    assert daily(tmp_path, seeds) == 0
    text = digest_text(FakeSMTP.sent[0])
    assert "1 job(s) failed scoring" in text
    assert f"{RELEVANT - 1} scored" in text
    assert [len(d) for d in digests(tmp_path)] == [RELEVANT - 1]

    # The failed job stays unscored and is picked up (and sent) next run.
    assert daily(tmp_path, seeds) == 0
    assert "1 new (top 88)" in FakeSMTP.sent[1]["Subject"]


def test_scoring_failing_for_every_job_exits_1(boards, tmp_path, seeds, capsys):
    FakeAnthropic.responses = [api_error(500)]

    assert daily(tmp_path, seeds) == 1
    assert "scoring failed for every job" in capsys.readouterr().err
    assert f"{RELEVANT} job(s) failed scoring" in digest_text(FakeSMTP.sent[0])


# --------------------------------------------------------------------------- configuration


@pytest.mark.parametrize(
    "missing", ["RESUME_SE", "ANTHROPIC_API_KEY", "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD"]
)
def test_missing_configuration_fails_before_polling(
    boards, tmp_path, seeds, capsys, monkeypatch, missing
):
    monkeypatch.delenv(missing)

    assert daily(tmp_path, seeds) == 2
    assert missing in capsys.readouterr().err
    assert not boards.calls
    assert FakeSMTP.sent == []


def test_db_defaults_to_database_url(boards, tmp_path, seeds, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", str(tmp_path / "from-env.db"))

    assert daily(tmp_path, seeds, db=False) == 0
    assert (tmp_path / "from-env.db").exists()
    assert not (tmp_path / "jobs.db").exists()


def test_max_score_caps_scoring_and_the_rest_wait(boards, tmp_path, seeds):
    assert daily(tmp_path, seeds, "--max-score", "1") == 0
    assert len(FakeAnthropic.calls) == 1
    assert "1 new (top 88)" in FakeSMTP.sent[0]["Subject"]

    assert daily(tmp_path, seeds) == 0
    assert len(FakeAnthropic.calls) == RELEVANT
    assert f"{RELEVANT - 1} new" in FakeSMTP.sent[1]["Subject"]
