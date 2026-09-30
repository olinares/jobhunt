"""Verified-facts parser tests. Synthetic facts only; nothing under private/ is ever read."""

from __future__ import annotations

from pathlib import Path

import pytest

from jobhunt.facts import (
    FACTS_ENV,
    Fact,
    Facts,
    FactSection,
    FactsNotFound,
    load_facts,
    parse_facts,
)

EXAMPLE = Path(__file__).resolve().parent.parent / "facts" / "verified.example.md"


def texts(facts: Facts) -> list[str]:
    return [fact.text for fact in facts]


@pytest.fixture(autouse=True)
def no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(FACTS_ENV, raising=False)


# --------------------------------------------------------------------------- ticks


def test_only_x_counts_as_ticked() -> None:
    facts = parse_facts(
        "## S\n"
        "- [x] lower x\n"
        "- [X] upper X\n"
        "- [ ] empty box\n"
        "- [] no space\n"
        "- [-] dash\n"
        "- [✓] check in box\n"
        "- ✓ bare check\n"
        "- [ x ] padded x\n"
        "- [*] star\n"
        "* [x] star bullet\n"
        "1. [x] numbered\n"
    )
    assert texts(facts) == ["lower x", "upper X", "star bullet", "numbered"]


def test_plain_bullet_is_not_a_fact() -> None:
    assert parse_facts("## S\n- Python\n- [link](https://example.com)\nplain text\n").count == 0


def test_brackets_in_text_are_not_boxes() -> None:
    facts = parse_facts("## S\n- [x] Cited in [1] and [the docs](https://example.com)\n")
    assert texts(facts) == ["Cited in [1] and [the docs](https://example.com)"]


def test_empty_ticked_item_is_dropped() -> None:
    assert parse_facts("## S\n- [x]\n- [x]   \n").count == 0


# --------------------------------------------------------------------------- sections


def test_conflicts_section_is_skipped_even_when_ticked() -> None:
    facts = parse_facts(
        "## Real\n- [x] kept\n"
        "## Conflicts to resolve\n- [x] ticked conflict\n  its continuation\n"
        "## CONFLICTS\n- [x] shouting conflict\n"
        "## After\n- [x] also kept\n"
    )
    assert texts(facts) == ["kept", "also kept"]
    assert [s.title for s in facts.sections] == ["Real", "After"]


def test_conflicts_subheading_is_skipped_until_next_heading_at_its_level() -> None:
    facts = parse_facts(
        "## Skills\n"
        "### Conflicts\n- [x] disputed\n#### Detail\n- [x] disputed detail\n"
        "### Tools\n- [x] Terraform\n"
    )
    assert texts(facts) == ["Tools: Terraform"]


def test_text_before_first_section_is_ignored() -> None:
    facts = parse_facts("# Title\n- [x] preamble\nSome intro.\n## S\n- [x] kept\n")
    assert texts(facts) == ["kept"]


def test_level_one_heading_closes_the_section() -> None:
    facts = parse_facts("## S\n- [x] kept\n# Appendix\n- [x] after h1\n## T\n- [x] back\n")
    assert texts(facts) == ["kept", "back"]


def test_code_fences_are_ignored() -> None:
    facts = parse_facts(
        "## S\n- [x] before\n```\n- [x] in fence\n## Fake heading\n- [x] still fenced\n```\n"
        "- [x] after\n~~~md\n- [x] tilde fence\n~~~\n"
    )
    assert texts(facts) == ["before", "after"]
    assert [s.title for s in facts.sections] == ["S"]


def test_heading_with_hash_in_title() -> None:
    facts = parse_facts("## C# and F# ##\n- [x] .NET\n")
    assert facts.sections[0].title == "C# and F#"


def test_empty_sections_are_left_out() -> None:
    facts = parse_facts("## Empty\n- [ ] nothing\n## Full\n- [x] one\n")
    assert [s.title for s in facts.sections] == ["Full"]


def test_subheading_text_becomes_a_prefix() -> None:
    facts = parse_facts(
        "## Skills\n- [x] top level\n"
        "### Languages\n- [x] Python\n"
        "### Cloud\n#### AWS\n- [x] Lambda\n"
        "### Tools\n- [x] Terraform\n"
        "## Other\n- [x] no prefix\n"
    )
    assert texts(facts) == [
        "top level",
        "Languages: Python",
        "Cloud / AWS: Lambda",
        "Tools: Terraform",
        "no prefix",
    ]
    assert [s.title for s in facts.sections] == ["Skills", "Other"]


# --------------------------------------------------------------------------- continuations


def test_continuation_joins_ticked_item() -> None:
    facts = parse_facts("## S\n- [x] first line\n  second line\n\tthird line\n")
    assert texts(facts) == ["first line second line third line"]


def test_continuation_of_unticked_item_never_appears() -> None:
    facts = parse_facts(
        "## S\n- [x] ticked\n- [ ] unticked\n  SECRET continuation\n\n  SECRET after blank\n"
    )
    assert texts(facts) == ["ticked"]
    assert "SECRET" not in facts.to_markdown()


def test_indented_checkbox_is_its_own_item() -> None:
    facts = parse_facts(
        "## S\n- [x] parent\n  - [ ] NESTED unticked\n    NESTED continuation\n  - [x] child\n"
    )
    assert texts(facts) == ["parent", "child"]


def test_unticked_parent_does_not_hide_ticked_child() -> None:
    facts = parse_facts("## S\n- [ ] parent\n  - [x] child\n")
    assert texts(facts) == ["child"]


def test_non_indented_text_ends_the_item() -> None:
    facts = parse_facts("## S\n- [x] item\nLoose paragraph.\n  STRAY indented line\n")
    assert texts(facts) == ["item"]


def test_indented_plain_bullet_is_a_continuation() -> None:
    facts = parse_facts("## S\n- [ ] parent\n  - HIDDEN detail\n- [x] other\n  - shown detail\n")
    assert texts(facts) == ["other - shown detail"]


def test_box_inside_continuation_is_judged_alone() -> None:
    facts = parse_facts(
        "## S\n- [x] ticked\n    [ ] HIDDEN bare box\n  tail · [ ] HIDDEN inline\n"
        "  [x] shown bare box\n"
    )
    assert texts(facts) == ["ticked", "shown bare box"]
    assert "HIDDEN" not in facts.to_markdown()


# --------------------------------------------------------------------------- inline boxes


def test_inline_boxes_are_judged_alone() -> None:
    facts = parse_facts("## S\n- [x] Python · [ ] JavaScript\n")
    assert texts(facts) == ["Python"]
    assert "JavaScript" not in facts.to_markdown()


def test_inline_boxes_several_ticked() -> None:
    facts = parse_facts("## S\n- [ ] Rust · [x] Go · [X] SQL · [-] Java\n")
    assert texts(facts) == ["Go", "SQL"]


def test_inline_box_without_separator_still_splits() -> None:
    facts = parse_facts("## S\n- [x] Python [ ] JavaScript\n")
    assert texts(facts) == ["Python"]


def test_middle_dot_without_box_is_text() -> None:
    facts = parse_facts("## S\n- [x] Demos · POCs · workshops\n")
    assert texts(facts) == ["Demos · POCs · workshops"]


def test_continuation_goes_to_last_box_on_the_line() -> None:
    facts = parse_facts("## S\n- [x] Python · [ ] JavaScript\n  HIDDEN since 2030\n")
    assert texts(facts) == ["Python"]
    facts = parse_facts("## S\n- [ ] Python · [x] Go\n  since 2030\n")
    assert texts(facts) == ["Go since 2030"]


# --------------------------------------------------------------------------- ids


def test_ids_are_slug_plus_short_hash() -> None:
    facts = parse_facts("## LinkedIn Profile!\n- [x] one\n- [x] two\n")
    ids = [fact.id for fact in facts]
    assert all(i.startswith("linkedin-profile-") for i in ids)
    assert all(len(i.rsplit("-", 1)[1]) == 4 for i in ids)
    assert len(set(ids)) == 2


def test_ids_are_stable_when_an_item_is_inserted() -> None:
    before = parse_facts("## S\n- [x] alpha\n- [x] gamma\n")
    after = parse_facts("## S\n- [x] alpha\n- [x] beta\n- [ ] draft\n- [x] gamma\n")
    old = {f.text: f.id for f in before}
    new = {f.text: f.id for f in after}
    assert old["alpha"] == new["alpha"]
    assert old["gamma"] == new["gamma"]


def test_duplicate_text_gets_distinct_ids() -> None:
    facts = parse_facts("## S\n- [x] same\n- [x] same\n## S\n- [x] same\n")
    ids = [fact.id for fact in facts]
    assert len(set(ids)) == 3
    assert ids[1] == ids[0] + "-2"


def test_heading_without_letters_gets_fallback_slug() -> None:
    facts = parse_facts("## !!!\n- [x] one\n")
    assert next(iter(facts)).id.startswith("facts-")


# --------------------------------------------------------------------------- Facts object


def test_count_iter_and_markdown() -> None:
    facts = Facts(
        sections=(
            FactSection("A", (Fact("a-0001", "one"), Fact("a-0002", "two"))),
            FactSection("B", (Fact("b-0003", "three"),)),
        )
    )
    assert facts.count == 3
    assert [f.id for f in facts] == ["a-0001", "a-0002", "b-0003"]
    assert facts.to_markdown() == (
        "## A\n\n- `a-0001` one\n- `a-0002` two\n\n## B\n\n- `b-0003` three"
    )
    assert facts.to_markdown(level=3).startswith("### A\n")


def test_repr_hides_fact_text() -> None:
    facts = parse_facts("## S\n- [x] SECRET fact\n")
    assert "SECRET" not in repr(facts)
    assert repr(facts) == "Facts(<1 facts in 1 sections>)"


def test_facts_are_frozen() -> None:
    facts = parse_facts("## S\n- [x] one\n")
    with pytest.raises(AttributeError):
        facts.sections = ()  # type: ignore[misc]


# --------------------------------------------------------------------------- example file


def test_example_file_parses_to_ticked_items_only() -> None:
    facts = load_facts(EXAMPLE)
    assert [s.title for s in facts.sections] == ["Experience", "Skills", "Notes"]
    assert texts(facts) == [
        "Solutions Engineer at Initrode Widgets, 2031 to 2034.",
        (
            "Ran 40 technical demos a quarter for the Initrode Widgets sales team, "
            "mostly to fictional logistics companies."
        ),
        "Built a proof of concept for Globex that cut invoice processing from 5 days to 1 day.",
        "Nested and ticked: the proof of concept ran on the Globex staging cluster.",
        "Languages: Python",
        "Languages: SQL",
        "Tools: Terraform",
        "Speaker at the (fake) Widget Summit 2033.",
    ]
    markdown = facts.to_markdown()
    for dropped in (
        "ignored even though",  # before the first section
        "$9 billion",  # unticked
        "continuation belongs",  # unticked item's continuation
        "renewed for 10 years",  # nested unticked
        "Partly true",  # [-]
        "check mark",  # [✓]
        "JavaScript",  # inline unticked box
        "Kubernetes",
        "code fence",
        "12 demos",  # Conflicts, ticked
        "resolved fact",  # Conflicts continuation
    ):
        assert dropped not in markdown


# --------------------------------------------------------------------------- loading


def test_explicit_path_wins_over_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    explicit = tmp_path / "explicit.md"
    explicit.write_text("## S\n- [x] explicit\n", encoding="utf-8")
    env = tmp_path / "env.md"
    env.write_text("## S\n- [x] from env\n", encoding="utf-8")
    monkeypatch.setenv(FACTS_ENV, str(env))
    assert texts(load_facts(explicit)) == ["explicit"]
    assert texts(load_facts(str(explicit))) == ["explicit"]


def test_env_overrides_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "verified.md").write_text("## S\n- [x] default\n", encoding="utf-8")
    env = tmp_path / "env.md"
    env.write_text("## S\n- [x] from env\n", encoding="utf-8")
    monkeypatch.setenv(FACTS_ENV, str(env))
    assert texts(load_facts(root=tmp_path)) == ["from env"]


def test_default_path_under_root(tmp_path: Path) -> None:
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "verified.md").write_text("## S\n- [x] default\n", encoding="utf-8")
    assert texts(load_facts(root=tmp_path)) == ["default"]


def test_missing_default_raises_and_does_not_fall_back(tmp_path: Path) -> None:
    # A draft and an example sit right there; neither may be used instead.
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "verified-draft.md").write_text("## S\n- [x] d\n", encoding="utf-8")
    (tmp_path / "facts").mkdir()
    (tmp_path / "facts" / "verified.example.md").write_text("## S\n- [x] e\n", encoding="utf-8")
    with pytest.raises(FactsNotFound) as info:
        load_facts(root=tmp_path)
    message = str(info.value)
    assert str(tmp_path / "private" / "verified.md") in message
    assert "facts/verified.example.md" in message
    assert FACTS_ENV in message


def test_missing_env_path_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "verified.md").write_text("## S\n- [x] default\n", encoding="utf-8")
    monkeypatch.setenv(FACTS_ENV, str(tmp_path / "nope.md"))
    with pytest.raises(FactsNotFound, match="nope.md"):
        load_facts(root=tmp_path)


def test_missing_explicit_path_raises(tmp_path: Path) -> None:
    with pytest.raises(FactsNotFound, match="missing.md"):
        load_facts(tmp_path / "missing.md")


def test_blank_env_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "verified.md").write_text("## S\n- [x] default\n", encoding="utf-8")
    monkeypatch.setenv(FACTS_ENV, "  ")
    assert texts(load_facts(root=tmp_path)) == ["default"]


def test_draft_is_refused_even_by_explicit_path(tmp_path: Path) -> None:
    draft = tmp_path / "verified-draft.md"
    draft.write_text("## S\n- [x] unconfirmed\n", encoding="utf-8")
    with pytest.raises(FactsNotFound, match="draft"):
        load_facts(draft)


def test_facts_not_found_is_a_file_not_found_error() -> None:
    assert issubclass(FactsNotFound, FileNotFoundError)
