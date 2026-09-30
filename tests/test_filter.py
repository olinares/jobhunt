from pathlib import Path

import pytest

from jobhunt.config import RolesConfig, load_config, parse_config
from jobhunt.filter import matches
from jobhunt.models import BoardRef, Job

CONFIG_PATH = Path(__file__).parent.parent / "config" / "roles.yaml"
BOARD = BoardRef("greenhouse", "acme")


def make_job(title: str, locations: list[str] | None = None, remote: bool | None = None) -> Job:
    return Job(
        BOARD,
        "1",
        title,
        "Acme",
        "https://x",
        locations=locations or [],
        remote=remote,
    )


@pytest.fixture(scope="module")
def cfg() -> RolesConfig:
    return load_config(CONFIG_PATH)


# --- titles that should match, given a qualifying region -------------------------


@pytest.mark.parametrize(
    "title",
    [
        "Solutions Engineer",
        "solutions engineer",  # case-insensitive
        "SOLUTIONS ENGINEER",
        "Senior Solutions Engineer",
        "Staff Solutions Engineer",
        "Lead Solutions Engineer",
        "Principal Solutions Engineer",
        "Sales Engineer",
        "Senior Sales Engineer",
        "Client Solutions Engineer",
        "Forward Deployed Engineer",
        "Forward Deployed Software Engineer",
        "Deployment Engineer",
        "Agent Engineer",
        "Applied AI Engineer",
        "Applied AI Architect",
        "Solutions Architect",
        "Senior Solutions Architect",
        "Customer Engineer",
        "Technical Consultant",
        "Technical Account Manager",
        "Solutions Engineer, Enterprise",
        "Solutions Engineer - West",
        "Solutions Engineer (Remote)",
        "Solutions Engineer II",
    ],
)
def test_title_matches_with_qualifying_region(cfg, title):
    job = make_job(title, locations=["San Francisco, CA"])
    result = matches(job, cfg)
    assert result.matched is True
    assert result.title_rule is not None
    assert "bay_area" in result.regions


# --- titles that should NOT match: false positives called out in the brief -------


@pytest.mark.parametrize(
    "title",
    [
        "Solutions Engineer Manager",
        "Sales Engineering Director",
        "Solutions Engineering Manager",
        "Solutions Engineer Head of Enablement",
    ],
)
def test_title_false_positive_examples_are_rejected(cfg, title):
    job = make_job(title, locations=["San Francisco, CA"])
    result = matches(job, cfg)
    assert result.matched is False
    assert result.title_rule is None


# --- titles excluded by exclude_title_terms --------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "Solutions Engineer Intern",
        "Solutions Engineering Internship",
        "New Grad Solutions Engineer",
        "Manager of Solutions Engineering",
        "Director of Solutions Engineering",
        "Solutions Engineering Director",
        "VP of Solutions Engineering",
        "Solutions Engineering Recruiter",
    ],
)
def test_titles_excluded_by_exclude_terms(cfg, title):
    job = make_job(title, locations=["San Francisco, CA"])
    result = matches(job, cfg)
    assert result.matched is False
    assert result.title_rule is None
    assert "excluded" in result.reason


# --- disallowed seniority prefixes ------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "Junior Solutions Engineer",
        "Associate Solutions Engineer",
        "Entry Solutions Engineer",
    ],
)
def test_disallowed_seniority_prefix_does_not_match(cfg, title):
    job = make_job(title, locations=["San Francisco, CA"])
    result = matches(job, cfg)
    assert result.matched is False
    assert result.title_rule is None


# --- titles with no configured phrase at all --------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "Software Engineer",
        "Product Manager",
        "Data Scientist",
        "Account Executive",
    ],
)
def test_unrelated_titles_do_not_match(cfg, title):
    job = make_job(title, locations=["San Francisco, CA"])
    result = matches(job, cfg)
    assert result.matched is False
    assert result.title_rule is None
    assert result.regions == ()


# --- region gating: right title, wrong (or no) region -----------------------------


@pytest.mark.parametrize(
    "locations,remote",
    [
        (["New York, NY"], None),
        (["Remote (Canada)"], None),
        (["Remote (Canada)"], True),
        ([], None),
    ],
)
def test_matching_title_without_matching_region_is_not_matched(cfg, locations, remote):
    job = make_job("Solutions Engineer", locations=locations, remote=remote)
    result = matches(job, cfg)
    assert result.matched is False
    assert result.title_rule == "solutions engineer"
    assert result.regions == ()
    assert "no configured region matched" in result.reason


@pytest.mark.parametrize(
    "locations,remote,expected_region",
    [
        (["Remote - US"], None, "remote_us"),
        ([], True, "remote_us"),
        (["Remote"], None, "remote_us"),
        (["Bay Area"], None, "bay_area"),
        (["SF or NYC / Remote-US"], None, "bay_area"),
    ],
)
def test_matching_title_with_matching_region_is_matched(cfg, locations, remote, expected_region):
    job = make_job("Solutions Engineer", locations=locations, remote=remote)
    result = matches(job, cfg)
    assert result.matched is True
    assert expected_region in result.regions


# --- MatchResult shape -------------------------------------------------------------


def test_match_result_is_frozen(cfg):
    job = make_job("Solutions Engineer", locations=["San Francisco, CA"])
    result = matches(job, cfg)
    with pytest.raises(AttributeError):
        result.matched = False  # type: ignore[misc]


def test_reason_is_always_populated(cfg):
    for title, locations in [
        ("Solutions Engineer", ["San Francisco, CA"]),
        ("Solutions Engineer Manager", ["San Francisco, CA"]),
        ("Solutions Engineer", ["New York, NY"]),
        ("Software Engineer", ["San Francisco, CA"]),
    ]:
        result = matches(make_job(title, locations=locations), cfg)
        assert isinstance(result.reason, str) and result.reason


# --- minimal config, independent of config/roles.yaml wording ---------------------


@pytest.fixture
def minimal_cfg() -> RolesConfig:
    return parse_config(
        {
            "titles": ["solutions engineer"],
            "seniority_ok": ["senior", ""],
            "exclude_title_terms": ["manager of", "director"],
            "regions": {"bay_area": {"match": ["san francisco", "sf"]}},
        }
    )


def test_minimal_config_positive_match(minimal_cfg):
    job = make_job("Senior Solutions Engineer", locations=["SF"])
    result = matches(job, minimal_cfg)
    assert result.matched is True
    assert result.title_rule == "solutions engineer"
    assert result.regions == ("bay_area",)


def test_minimal_config_rejects_disallowed_seniority(minimal_cfg):
    job = make_job("Staff Solutions Engineer", locations=["SF"])
    result = matches(job, minimal_cfg)
    assert result.matched is False
