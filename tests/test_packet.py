"""Application packet tests. Synthetic job, resumes and facts; no I/O beyond the example file."""

from __future__ import annotations

from pathlib import Path

import pytest

from jobhunt.facts import Facts, load_facts, parse_facts
from jobhunt.models import BoardRef, Job, Score
from jobhunt.packet import MISSING, RULES, NoVerifiedFacts, Packet, build_packet
from jobhunt.scoring import Resumes

EXAMPLE = Path(__file__).resolve().parent.parent / "facts" / "verified.example.md"
RESUMES = Resumes(se="SE RESUME: demos at Initrode.", fde="FDE RESUME: shipped code at Globex.")
FACTS = parse_facts(
    "## Experience\n"
    "- [x] Ran demos at Initrode.\n"
    "- [ ] UNTICKED cut costs by 90%.\n"
    "  UNTICKED continuation.\n"
    "- [x] Python · [ ] UNTICKED Haskell\n"
    "## Conflicts\n"
    "- [x] CONFLICT 12 vs 8 demos.\n"
)


def make_job(**overrides: object) -> Job:
    fields: dict[str, object] = {
        "board": BoardRef(ats="greenhouse", slug="initrode"),
        "external_id": "123",
        "title": "Solutions Engineer",
        "company": "Initrode",
        "url": "https://boards.example.com/initrode/123",
        "locations": ["San Francisco, CA", "Remote - US"],
        "remote": True,
        "description_html": "<p>Build <b>demos</b>.</p><ul><li>Python</li></ul>",
        "pay_min": 150000,
        "pay_max": 190000,
        "pay_currency": "USD",
        "pay_period": "year",
    }
    fields.update(overrides)
    return Job(**fields)  # type: ignore[arg-type]


def score(variant: str) -> Score:
    return Score(value=80, variant=variant, reason="Good fit.")  # type: ignore[arg-type]


def test_packet_fields() -> None:
    packet = build_packet(make_job(), score("se"), RESUMES, FACTS)
    assert packet.job_uid == "greenhouse:initrode:123"
    assert packet.title == "Solutions Engineer"
    assert packet.company == "Initrode"
    assert packet.url == "https://boards.example.com/initrode/123"
    assert packet.locations == ("San Francisco, CA", "Remote - US")
    assert packet.remote is True
    assert packet.pay == "USD 150,000–190,000/year"
    assert packet.description == "Build demos.\n\n- Python"
    assert packet.facts is FACTS
    assert packet.rules == RULES


@pytest.mark.parametrize(
    ("given", "variant", "resume"),
    [
        (score("fde"), "fde", RESUMES.fde),
        (score("se"), "se", RESUMES.se),
        (None, "se", RESUMES.se),
    ],
)
def test_variant_comes_from_score_and_defaults_to_se(
    given: Score | None, variant: str, resume: str
) -> None:
    packet = build_packet(make_job(), given, RESUMES, FACTS)
    assert packet.variant == variant
    assert packet.resume == resume


def test_no_verified_facts_raises() -> None:
    with pytest.raises(NoVerifiedFacts):
        build_packet(make_job(), None, RESUMES, Facts())
    only_unticked = parse_facts("## S\n- [ ] no\n## Conflicts\n- [x] no\n")
    with pytest.raises(NoVerifiedFacts):
        build_packet(make_job(), None, RESUMES, only_unticked)


def test_rules_cover_the_contract() -> None:
    assert "verbatim" in RULES
    assert MISSING == "not in verified facts"
    assert f'"{MISSING}"' in RULES
    assert "never submits" in RULES


def test_markdown_sections_in_order() -> None:
    markdown = build_packet(make_job(), score("fde"), RESUMES, FACTS).to_markdown()
    order = [
        "# Application packet: Solutions Engineer at Initrode",
        "## Rules",
        "## Verified facts (2)",
        "## Resume (fde)",
        "## Job description",
    ]
    positions = [markdown.index(heading) for heading in order]
    assert positions == sorted(positions)
    assert "- URL: https://boards.example.com/initrode/123" in markdown
    assert "- Locations: San Francisco, CA; Remote - US" in markdown
    assert "- Pay: USD 150,000–190,000/year" in markdown
    assert "- Resume variant: fde" in markdown
    assert RULES in markdown
    assert "### Experience" in markdown
    assert "Ran demos at Initrode." in markdown
    assert "FDE RESUME" in markdown and "SE RESUME" not in markdown
    assert "<job_description>\nBuild demos.\n\n- Python\n</job_description>" in markdown


def test_markdown_holds_no_unticked_or_conflict_text() -> None:
    markdown = build_packet(make_job(), None, RESUMES, FACTS).to_markdown()
    assert "UNTICKED" not in markdown
    assert "CONFLICT" not in markdown
    assert "Python" in markdown  # the ticked half of the inline line


def test_markdown_from_example_file_holds_only_ticked_text() -> None:
    facts = load_facts(EXAMPLE)
    markdown = build_packet(make_job(), None, RESUMES, facts).to_markdown()
    for fact in facts:
        assert fact.text in markdown
    for dropped in ("$9 billion", "JavaScript", "Kubernetes", "12 demos", "renewed", "Partly"):
        assert dropped not in markdown


def test_missing_optional_job_fields() -> None:
    job = make_job(locations=[], remote=None, description_html=None, pay_min=None, pay_max=None)
    packet = build_packet(job, None, RESUMES, FACTS)
    assert packet.pay is None
    assert packet.description == ""
    markdown = packet.to_markdown()
    assert "- Locations: not listed" in markdown
    assert "- Remote: not stated" in markdown
    assert "- Pay: not listed" in markdown
    assert "No description is stored for this job." in markdown


def test_repr_hides_private_text() -> None:
    packet = build_packet(make_job(), None, RESUMES, FACTS)
    text = repr(packet)
    assert "SE RESUME" not in text
    assert "Ran demos" not in text
    assert "Build demos" not in text


def test_packet_is_frozen() -> None:
    packet = build_packet(make_job(), None, RESUMES, FACTS)
    assert isinstance(packet, Packet)
    with pytest.raises(AttributeError):
        packet.variant = "fde"  # type: ignore[misc]
