"""Scorer tests: replay recorded responses through a fake client. No network, no API key.

The fixtures in tests/fixtures/scoring/ are synthetic until re-recorded with record.py
(see the `_synthetic` flag in each file).
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path

import anthropic
import httpx
import pytest
from anthropic.types import Message

from jobhunt.models import BoardRef, Job, Score
from jobhunt.scoring import (
    DEFAULT_MODEL,
    DESCRIPTION_CAP,
    MAX_TOKENS,
    RUBRIC,
    ResumeNotFound,
    Resumes,
    ScoringError,
    build_request,
    html_to_text,
    load_resumes,
    parse_score,
    pay_suspect,
    score_job,
    score_many,
    user_message,
)

FIXTURES = Path(__file__).parent / "fixtures" / "scoring"
CASES = sorted(p.stem for p in FIXTURES.glob("*.json"))

# Stand-in resume text. Distinctive so tests can assert it never leaks anywhere.
RESUMES = Resumes(se="SE-RESUME-SENTINEL demo text", fde="FDE-RESUME-SENTINEL deploy text")


def load_case(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def job_from(data: dict) -> Job:
    fields = dict(data)
    fields["board"] = BoardRef(**fields["board"])
    return Job(**fields)


def message_from(data: dict) -> Message:
    return Message.model_validate(data)


def make_job(**overrides) -> Job:
    fields = {
        "board": BoardRef("greenhouse", "acme"),
        "external_id": "1",
        "title": "Solutions Engineer",
        "company": "Acme",
        "url": "https://example.com/jobs/1",
    }
    return Job(**(fields | overrides))


class FakeMessages:
    def __init__(self, responses: list):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeClient:
    def __init__(self, responses: list):
        self.messages = FakeMessages(responses)


def api_error(cls: type[anthropic.APIStatusError], status: int) -> anthropic.APIStatusError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status, request=request)
    return cls(f"error {status}", response=response, body=None)


# --------------------------------------------------------------------------- fixtures


def test_fixtures_are_present_and_flagged():
    assert set(CASES) == {"poor_fit", "strong_fde", "strong_se", "suspect_pay", "truncated"}
    for name in CASES:
        doc = load_case(name)
        # Either still synthetic (clearly marked) or re-recorded (flag removed).
        assert doc.get("_synthetic", False) in (True, False)
        assert "SENTINEL" not in json.dumps(doc)


@pytest.mark.parametrize("name", [c for c in CASES if "error" not in load_case(c)["expected"]])
def test_replayed_fixture_parses_into_score(name):
    doc = load_case(name)
    job = job_from(doc["job"])
    client = FakeClient([message_from(doc["response"])])

    score = score_job(client, job, RESUMES)

    expected = json.loads(doc["response"]["content"][0]["text"])
    assert score == Score(
        value=expected["value"],
        variant=expected["variant"],
        reason=expected["reason"],
        pay_suspect=doc["expected"]["pay_suspect"],
    )


def test_variant_choice_follows_the_model():
    se = score_job(
        FakeClient([message_from(load_case("strong_se")["response"])]),
        job_from(load_case("strong_se")["job"]),
        RESUMES,
    )
    fde = score_job(
        FakeClient([message_from(load_case("strong_fde")["response"])]),
        job_from(load_case("strong_fde")["job"]),
        RESUMES,
    )
    assert (se.variant, fde.variant) == ("se", "fde")
    assert se.value >= 85 and fde.value >= 85


def test_poor_fit_scores_low():
    doc = load_case("poor_fit")
    score = score_job(FakeClient([message_from(doc["response"])]), job_from(doc["job"]), RESUMES)
    assert score.value < 40


def test_truncated_response_is_a_failed_score():
    doc = load_case("truncated")
    with pytest.raises(ScoringError, match="stop_reason=max_tokens"):
        score_job(FakeClient([message_from(doc["response"])]), job_from(doc["job"]), RESUMES)


# --------------------------------------------------------------------------- parsing


def _response_with(text: str, **overrides) -> Message:
    data = copy.deepcopy(load_case("strong_se")["response"])
    data["content"][0]["text"] = text
    data.update(overrides)
    return message_from(data)


def test_refusal_is_a_failed_score():
    message = _response_with(
        "",
        stop_reason="refusal",
        stop_details={"type": "refusal", "category": None, "explanation": None},
    )
    with pytest.raises(ScoringError, match="stop_reason=refusal"):
        parse_score(message, make_job())


@pytest.mark.parametrize(
    "payload, error",
    [
        ("not json", "not valid JSON"),
        ("[1, 2]", "not an object"),
        ('{"value": 101, "variant": "se", "reason": "x"}', "0 to 100"),
        ('{"value": -1, "variant": "se", "reason": "x"}', "0 to 100"),
        ('{"value": 80.5, "variant": "se", "reason": "x"}', "0 to 100"),
        ('{"value": true, "variant": "se", "reason": "x"}', "0 to 100"),
        ('{"value": 80, "variant": "sales", "reason": "x"}', "variant"),
        ('{"value": 80, "variant": "fde", "reason": "  "}', "reason"),
        ('{"value": 80, "variant": "fde"}', "reason"),
    ],
)
def test_schema_violations_are_rejected(payload, error):
    with pytest.raises(ScoringError, match=error):
        parse_score(_response_with(payload), make_job())


def test_parse_error_does_not_echo_model_text():
    with pytest.raises(ScoringError) as info:
        parse_score(_response_with("SE-RESUME-SENTINEL leaked"), make_job())
    assert "SENTINEL" not in str(info.value)


def test_reason_is_stripped_and_bounds_are_inclusive():
    for value in (0, 100):
        payload = json.dumps({"value": value, "variant": "fde", "reason": "  Fits.  "})
        score = parse_score(_response_with(payload), make_job())
        assert (score.value, score.reason) == (value, "Fits.")


# --------------------------------------------------------------------------- request


def test_request_shape(monkeypatch):
    monkeypatch.delenv("JOBHUNT_SCORER_MODEL", raising=False)
    request = build_request(job_from(load_case("strong_se")["job"]), RESUMES)

    assert request["model"] == DEFAULT_MODEL == "claude-haiku-4-5"
    assert request["max_tokens"] == MAX_TOKENS
    assert "thinking" not in request
    assert "temperature" not in request
    assert request["output_config"] == {
        "format": {"type": "json_schema", "schema": request["output_config"]["format"]["schema"]}
    }
    assert "effort" not in request["output_config"]
    schema = request["output_config"]["format"]["schema"]
    assert schema["required"] == ["value", "variant", "reason"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["variant"]["enum"] == ["se", "fde"]

    [block] = request["system"]
    assert block["cache_control"] == {"type": "ephemeral"}
    assert block["text"].startswith(RUBRIC)
    assert "SE-RESUME-SENTINEL" in block["text"] and "FDE-RESUME-SENTINEL" in block["text"]
    # Job data stays out of the cached block.
    assert "Lumenfield" not in block["text"]
    [message] = request["messages"]
    assert message["role"] == "user" and "Lumenfield" in message["content"]
    assert "SENTINEL" not in message["content"]


def test_model_override_from_env(monkeypatch):
    monkeypatch.setenv("JOBHUNT_SCORER_MODEL", "claude-sonnet-5-5")
    assert build_request(make_job(), RESUMES)["model"] == "claude-sonnet-5-5"


def test_system_block_is_identical_across_jobs():
    client = FakeClient(
        [message_from(load_case(name)["response"]) for name in ("strong_se", "strong_fde")]
    )
    for name in ("strong_se", "strong_fde"):
        score_job(client, job_from(load_case(name)["job"]), RESUMES)
    first, second = client.messages.calls
    assert first["system"] == second["system"]
    assert first["messages"] != second["messages"]


def test_user_message_fields():
    job = make_job(
        title="Forward Deployed Engineer",
        company="Tessellate",
        locations=["New York, NY", "Remote (US)"],
        remote=True,
        pay_min=170000,
        pay_max=230000,
        pay_currency="USD",
        pay_period="year",
        department="Deployment",
        description_html="<p>Ship it &amp; own it.</p>",
    )
    text = user_message(job)
    assert "Title: Forward Deployed Engineer" in text
    assert "Company: Tessellate" in text
    assert "Locations: New York, NY; Remote (US)" in text
    assert "Remote: yes" in text
    assert "Pay: USD 170,000-230,000 per year" in text
    assert "Department: Deployment" in text
    assert "<description>\nShip it & own it.\n</description>" in text


def test_user_message_without_description_or_pay():
    text = user_message(make_job())
    assert "Locations: not listed" in text
    assert "Remote: not stated" in text
    assert "Pay: not listed" in text
    assert "No description was provided" in text
    assert "<description>" not in text


def test_huge_description_is_capped_and_says_so():
    body = "word " * (DESCRIPTION_CAP // 2)
    text = user_message(make_job(description_html=f"<p>{body}</p>"))
    assert "only the first 20,000 are included" in text
    description = text.split("<description>\n", 1)[1].split("\n</description>", 1)[0]
    assert len(description) == DESCRIPTION_CAP


# --------------------------------------------------------------------------- HTML


def test_html_to_text_structure():
    html = (
        "<h2>About</h2><p>We build <b>APIs</b>&nbsp;for data.</p>"
        "<ul><li>Run demos</li><li>Lead POCs &amp; pilots</li><li></li></ul>"
        "<p>Line one<br>line two<br/>line three</p>"
        "<script>alert('x')</script><style>p{}</style>"
        "<table><tr><td>Pay</td><td>$1</td></tr></table>"
    )
    assert html_to_text(html) == (
        "About\n\nWe build APIs for data.\n\n- Run demos\n- Lead POCs & pilots\n\n"
        "Line one\nline two\nline three\n\nPay $1"
    )


def test_html_to_text_plain_and_empty():
    assert html_to_text(None) == ""
    assert html_to_text("") == ""
    assert html_to_text("Just   text\n\n\n\nhere") == "Just text\n\nhere"
    assert html_to_text("caf&eacute; &#39;quoted&#39;") == "café 'quoted'"


# --------------------------------------------------------------------------- pay_suspect


@pytest.mark.parametrize(
    "pay, suspect",
    [
        ({}, False),
        ({"pay_min": 150_000, "pay_max": 190_000, "pay_period": "year"}, False),
        ({"pay_min": 1, "pay_max": 2}, True),  # placeholder $1-$2
        ({"pay_min": 1, "pay_max": 2, "pay_period": "hour"}, True),  # 4,160/yr
        ({"pay_min": 0, "pay_max": 500_000}, True),  # $0-$500K
        ({"pay_min": 0, "pay_max": 0}, True),  # below the floor
        ({"pay_min": 50_000, "pay_max": 200_000}, False),  # ratio exactly 4
        ({"pay_min": 50_000, "pay_max": 200_001}, True),  # ratio just above 4
        ({"pay_min": 19_000, "pay_max": 19_999, "pay_period": "year"}, True),
        ({"pay_min": 19_000, "pay_max": 20_000, "pay_period": "year"}, False),
        ({"pay_min": 60, "pay_max": 90, "pay_period": "hour"}, False),  # 187k/yr
        ({"pay_min": 12_000, "pay_max": 15_000, "pay_period": "month"}, False),
        ({"pay_max": 5_000}, True),  # only a max, below the floor
        ({"pay_min": 150_000}, False),  # only a min
        ({"pay_min": 1_000_000, "pay_max": 1_500_000, "pay_currency": "JPY"}, False),
        ({"pay_min": 10_000, "pay_max": 15_000, "pay_currency": "JPY"}, False),  # no floor
        ({"pay_min": 1, "pay_max": 20, "pay_currency": "EUR"}, True),  # ratio still applies
        ({"pay_min": 1, "pay_max": 2, "pay_period": "commission"}, False),  # unknown period
    ],
)
def test_pay_suspect(pay, suspect):
    assert pay_suspect(make_job(**pay)) is suspect


def test_pay_suspect_is_set_from_code_not_model():
    doc = load_case("strong_se")
    job = job_from(doc["job"])
    job.pay_min, job.pay_max = 1.0, 2.0
    score = score_job(FakeClient([message_from(doc["response"])]), job, RESUMES)
    assert score.pay_suspect is True


# --------------------------------------------------------------------------- score_many


def test_score_many_isolates_failures():
    jobs = [job_from(load_case(n)["job"]) for n in ("strong_se", "truncated", "strong_fde")]
    jobs.insert(2, make_job(external_id="rate-limited"))
    client = FakeClient(
        [
            message_from(load_case("strong_se")["response"]),
            message_from(load_case("truncated")["response"]),
            api_error(anthropic.RateLimitError, 429),
            message_from(load_case("strong_fde")["response"]),
        ]
    )

    scores, failures = score_many(client, jobs, RESUMES, limit=10)

    assert list(scores) == [jobs[0].uid, jobs[3].uid]
    assert [uid for uid, _ in failures] == [jobs[1].uid, jobs[2].uid]
    assert "stop_reason=max_tokens" in failures[0][1]
    assert failures[1][1].startswith("RateLimitError (429)")


def test_score_many_respects_limit():
    jobs = [make_job(external_id=str(i)) for i in range(5)]
    response = message_from(load_case("strong_se")["response"])
    client = FakeClient([response, response])
    scores, failures = score_many(client, jobs, RESUMES, limit=2)
    assert len(scores) == 2 and failures == []
    assert len(client.messages.calls) == 2


def test_score_many_stops_calling_after_auth_error():
    jobs = [make_job(external_id=str(i)) for i in range(3)]
    client = FakeClient([api_error(anthropic.AuthenticationError, 401)])
    scores, failures = score_many(client, jobs, RESUMES)
    assert scores == {}
    assert len(client.messages.calls) == 1
    assert [uid for uid, _ in failures] == [j.uid for j in jobs]
    assert failures[1][1].startswith("skipped: AuthenticationError (401)")


def test_score_job_lets_api_errors_propagate():
    client = FakeClient([api_error(anthropic.InternalServerError, 500)])
    with pytest.raises(anthropic.InternalServerError):
        score_job(client, make_job(), RESUMES)


def test_nothing_logged_contains_resume_text(caplog):
    caplog.set_level(logging.DEBUG, logger="jobhunt.scoring")
    jobs = [job_from(load_case(n)["job"]) for n in ("strong_se", "truncated")]
    client = FakeClient(
        [message_from(load_case(n)["response"]) for n in ("strong_se", "truncated")]
    )
    score_many(client, jobs, RESUMES)
    assert "input=1742" in caplog.text
    assert "SENTINEL" not in caplog.text


# --------------------------------------------------------------------------- resumes


def test_resumes_repr_hides_text():
    assert "SENTINEL" not in repr(RESUMES)
    assert "SENTINEL" not in str(RESUMES)


def _write_files(tmp_path: Path) -> Path:
    resume_dir = tmp_path / "resumes"
    resume_dir.mkdir()
    (resume_dir / "se.md").write_text("se from file\n")
    (resume_dir / "fde.md").write_text("fde from file\n")
    return resume_dir


def test_load_resumes_env_wins_over_files(tmp_path):
    resume_dir = _write_files(tmp_path)
    env = {"RESUME_SE": "se from env", "RESUME_FDE": "fde from env"}
    resumes = load_resumes(environ=env, resume_dir=resume_dir)
    assert (resumes.se, resumes.fde) == ("se from env", "fde from env")


def test_load_resumes_falls_back_to_files_per_variant(tmp_path):
    resume_dir = _write_files(tmp_path)
    resumes = load_resumes(
        environ={"RESUME_SE": "se from env", "RESUME_FDE": ""}, resume_dir=resume_dir
    )
    assert (resumes.se, resumes.fde) == ("se from env", "fde from file")


def test_load_resumes_default_dir_is_private_resumes(tmp_path, monkeypatch):
    (tmp_path / "private" / "resumes").mkdir(parents=True)
    (tmp_path / "private" / "resumes" / "se.md").write_text("se")
    (tmp_path / "private" / "resumes" / "fde.md").write_text("fde")
    monkeypatch.chdir(tmp_path)
    resumes = load_resumes(environ={})
    assert (resumes.se, resumes.fde) == ("se", "fde")


def test_load_resumes_missing_names_what_is_missing(tmp_path):
    resume_dir = tmp_path / "resumes"
    resume_dir.mkdir()
    (resume_dir / "se.md").write_text("se")
    with pytest.raises(ResumeNotFound) as info:
        load_resumes(environ={}, resume_dir=resume_dir)
    message = str(info.value)
    assert "RESUME_FDE" in message and "fde.md" in message
    assert "RESUME_SE" not in message
