from __future__ import annotations

import email
from datetime import date
from email import policy
from typing import ClassVar

import pytest

from jobhunt.digest import DigestConfigError, DigestStats, render_digest, send_email
from jobhunt.models import BoardRef, Job, Score, ScoredJob

DAY = date(2026, 9, 30)  # a Wednesday
BOARD = BoardRef(ats="greenhouse", slug="acme")
STATS = DigestStats(boards_polled=40, jobs_fetched=1200, relevant=30, new=3, closed=2, scored=3)


def scored(
    ext: str,
    title: str,
    value: int,
    *,
    variant: str = "se",
    suspect: bool = False,
    pay: tuple[float, float] | None = (150000, 190000),
    locations: list[str] | None = None,
    url: str | None = None,
) -> ScoredJob:
    job = Job(
        board=BOARD,
        external_id=ext,
        title=title,
        company="Acme",
        url=url or f"https://boards.example.com/acme/{ext}",
        locations=["San Francisco, CA"] if locations is None else locations,
        pay_min=pay[0] if pay else None,
        pay_max=pay[1] if pay else None,
        pay_currency="USD" if pay else None,
        pay_period="year" if pay else None,
    )
    score = Score(value, variant, f"Reason for {ext}.", pay_suspect=suspect)  # type: ignore[arg-type]
    return ScoredJob(job, score)


def fixture() -> list[ScoredJob]:
    return [
        scored("1", "Solutions Engineer", 88),
        scored("2", "Forward Deployed Engineer", 75, variant="fde", pay=None, locations=[]),
        scored("3", "Sales Engineer", 60, pay=(1, 2), suspect=True),
    ]


def test_subject_and_numbering_order():
    subject, text, html = render_digest(fixture(), day=DAY, stats=STATS)
    assert subject == "jobhunt · Wed Sep 30 · 3 new (top 88)"
    for body in (text, html):
        assert body.index("#1 ·") < body.index("#2 ·") < body.index("#3 ·")
    assert "#1 · 88 · SE · Solutions Engineer · Acme" in text
    assert "#2 · 75 · FDE · Forward Deployed Engineer" in text
    assert "#1 · 88 · SE · Solutions Engineer" in html


def test_top_score_is_max_not_first():
    items = [scored("1", "A", 50), scored("2", "B", 70)]
    subject, _, _ = render_digest(items, day=DAY, stats=STATS)
    assert subject.endswith("2 new (top 70)")


def test_suspect_pay_marker():
    _, text, html = render_digest(fixture(), day=DAY, stats=STATS)
    assert "⚠ USD 1–2/year (looks like a placeholder)" in text
    assert "looks like a placeholder" in html
    assert text.count("looks like a placeholder") == 1
    assert "USD 150,000–190,000/year" in text


def test_escaping():
    items = [scored("1", "<script>alert(1)</script> & Co", 80, url='https://x.test/?a="b"&c=d')]
    _, text, html = render_digest(items, day=DAY, stats=STATS)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert 'href="https://x.test/?a=&quot;b&quot;&amp;c=d"' in html
    assert "<script>" in text  # plain text is not escaped


def test_locations_truncated():
    many = scored("1", "SE", 80, locations=["A", "B", "C", "D", "E"])
    _, text, _ = render_digest([many], day=DAY, stats=STATS)
    assert "A; B; C (+2 more)" in text


def test_empty_heartbeat():
    stats = DigestStats(boards_polled=40, jobs_fetched=1200, relevant=30)
    subject, text, html = render_digest([], day=DAY, stats=stats)
    assert subject == "jobhunt · Wed Sep 30 · no new jobs"
    assert "No new jobs today." in text and "No new jobs today." in html
    assert "40 boards polled" in text and "1200 jobs fetched" in text
    assert "#1" not in text


def test_footer_reports_failures_only_when_present():
    _, healthy, _ = render_digest(fixture(), day=DAY, stats=STATS)
    assert "failed" not in healthy
    bad = DigestStats(boards_polled=40, boards_failed=2, score_failures=1)
    _, text, html = render_digest(fixture(), day=DAY, stats=bad)
    assert "2 board(s) failed to load" in text
    assert "1 job(s) failed scoring" in html


def test_footer_shows_discovery_line_and_detail_failures():
    _, healthy, _ = render_digest(fixture(), day=DAY, stats=STATS)
    assert "Discovery" not in healthy and "job details" not in healthy

    stats = DigestStats(
        boards_polled=40,
        detail_failures=1,
        discovery="⚠ Discovery failed: HTTPStatusError: 401 <Unauthorized>",
    )
    _, text, html = render_digest(fixture(), day=DAY, stats=stats)
    assert "⚠ Discovery failed: HTTPStatusError: 401 <Unauthorized>" in text
    assert "Discovery failed: HTTPStatusError: 401 &lt;Unauthorized&gt;" in html
    assert "1 board(s) couldn't load job details (will retry next run)" in text
    assert "job details" in html


def test_rendered_text_sample(capsys):
    _, text, _ = render_digest(fixture(), day=DAY, stats=STATS)
    print(text)
    assert text.startswith("jobhunt digest · Wed Sep 30")


class FakeSMTP:
    instances: ClassVar[list[FakeSMTP]] = []

    def __init__(self, host, port):
        self.host, self.port = host, port
        self.logins: list[tuple[str, str]] = []
        self.sent = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        self.logins.append((user, password))

    def send_message(self, msg):
        self.sent.append(msg)


@pytest.fixture
def smtp_env(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setenv("GMAIL_ADDRESS", "me@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-pass")
    monkeypatch.delenv("DIGEST_TO", raising=False)


def test_send_email_logs_in_and_sends_both_parts(smtp_env):
    send_email("subj", "plain body", "<p>html body</p>", smtp_factory=FakeSMTP)
    (server,) = FakeSMTP.instances
    assert (server.host, server.port) == ("smtp.gmail.com", 465)
    assert server.logins == [("me@example.com", "app-pass")]
    (msg,) = server.sent
    assert msg["Subject"] == "subj"
    assert msg["From"] == "me@example.com"
    assert msg["To"] == "me@example.com"
    parsed = email.message_from_bytes(msg.as_bytes(), policy=policy.default)
    assert parsed.get_content_type() == "multipart/alternative"
    assert parsed.get_body(preferencelist=("plain",)).get_content().strip() == "plain body"
    assert "html body" in parsed.get_body(preferencelist=("html",)).get_content()


def test_send_email_digest_to_override(smtp_env, monkeypatch):
    monkeypatch.setenv("DIGEST_TO", "other@example.com")
    send_email("s", "t", "<p>h</p>", smtp_factory=FakeSMTP)
    assert FakeSMTP.instances[0].sent[0]["To"] == "other@example.com"


@pytest.mark.parametrize("missing", ["GMAIL_ADDRESS", "GMAIL_APP_PASSWORD"])
def test_send_email_missing_env_names_variable(smtp_env, monkeypatch, missing):
    monkeypatch.delenv(missing)
    with pytest.raises(DigestConfigError, match=missing):
        send_email("s", "t", "h", smtp_factory=FakeSMTP)
    assert FakeSMTP.instances == []
