from pathlib import Path

import pytest

from jobhunt.config import Region, load_config
from jobhunt.location import (
    contains_phrase,
    has_non_us_qualifier,
    region_matches,
    tokenize,
)

CONFIG_PATH = Path(__file__).parent.parent / "config" / "roles.yaml"

# Mirrors config/roles.yaml's `regions:` section so location-matching tests
# don't depend on the file's exact wording changing underneath them.
BAY_AREA = Region(
    name="bay_area",
    match=(
        "san francisco",
        "sf",
        "bay area",
        "oakland",
        "san jose",
        "palo alto",
        "mountain view",
        "menlo park",
        "sunnyvale",
        "redwood city",
        "san mateo",
        "berkeley",
        "walnut creek",
    ),
)
REMOTE_US = Region(
    name="remote_us",
    match=(
        "remote - us",
        "remote (us)",
        "remote",
        "united states",
        "usa",
        "us-remote",
        "anywhere in the us",
    ),
)
REGIONS = (BAY_AREA, REMOTE_US)


def region_names(locations: list[str], remote: bool | None = None) -> tuple[str, ...]:
    return region_matches(locations, remote, REGIONS)


# --- tokenize / contains_phrase -------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Remote-US", ("remote", "us")),
        ("Remote (US)", ("remote", "us")),
        ("Hybrid - San Francisco, CA", ("hybrid", "san", "francisco", "ca")),
        ("SF or NYC / Remote-US", ("sf", "or", "nyc", "remote", "us")),
        ("", ()),
    ],
)
def test_tokenize(text, expected):
    assert tokenize(text) == expected


@pytest.mark.parametrize(
    "text,phrase,expected",
    [
        ("San Francisco, CA", "san francisco", True),
        ("Austin, TX", "us", False),  # "austin" must not fuzzy-match "us"
        ("SF or NYC", "sf", True),
        ("San Francisco", "sf", False),  # "sf" is a distinct token, not a substring hit
        ("Sales Engineering Director", "sales engineer", False),  # engineering != engineer
        ("Remote (US)", "remote - us", True),  # punctuation-insensitive
        ("US-Remote", "us-remote", True),
    ],
)
def test_contains_phrase(text, phrase, expected):
    assert contains_phrase(tokenize(text), phrase) == expected


# --- region_matches: bay_area ----------------------------------------------------


@pytest.mark.parametrize(
    "locations",
    [
        ["San Francisco, CA"],
        ["Hybrid - San Francisco, CA"],
        ["SF"],
        ["SF or NYC / Remote-US"],
        ["Bay Area"],
        ["Oakland, CA"],
        ["San Jose, CA"],
        ["Palo Alto, CA"],
        ["Mountain View, CA"],
        ["Menlo Park, CA"],
        ["Sunnyvale, CA"],
        ["Redwood City, CA"],
        ["San Mateo, CA"],
        ["Berkeley, CA"],
        ["Walnut Creek, CA"],
        ["San Francisco, CA", "New York, NY"],
    ],
)
def test_bay_area_matches(locations):
    assert "bay_area" in region_names(locations)


@pytest.mark.parametrize(
    "locations",
    [
        ["New York, NY"],
        ["Austin, TX"],
        ["Remote (Canada)"],
    ],
)
def test_bay_area_non_matches(locations):
    assert "bay_area" not in region_names(locations)


def test_bay_area_known_false_positive_on_city_name_alone():
    # Matching on "san francisco" as a bare phrase can't distinguish the Bay
    # Area from other places sharing the name (e.g. San Francisco de Quito,
    # Ecuador). Documented here as a known, accepted limitation rather than
    # a silently-passing gap.
    assert "bay_area" in region_names(["San Francisco de Quito, Ecuador"])


# --- region_matches: remote_us, explicit US phrasing -----------------------------


@pytest.mark.parametrize(
    "locations",
    [
        ["Remote - US"],
        ["Remote (US)"],
        ["Remote-US"],
        ["United States"],
        ["USA"],
        ["US-Remote"],
        ["Anywhere in the US"],
        ["SF or NYC / Remote-US"],
        ["Remote (US) / Remote (Canada)"],  # explicit US wins even alongside another option
    ],
)
def test_remote_us_explicit_matches(locations):
    assert "remote_us" in region_names(locations)


# --- region_matches: bare "remote" ------------------------------------------------


@pytest.mark.parametrize(
    "locations,remote_flag",
    [
        (["Remote"], None),
        (["remote"], None),
        (["Remote"], True),
        (["Remote"], False),
        (["Fully Remote"], None),
    ],
)
def test_bare_remote_counts_as_remote_us_with_no_qualifier(locations, remote_flag):
    assert "remote_us" in region_names(locations, remote_flag)


@pytest.mark.parametrize(
    "locations",
    [
        ["Remote (Canada)"],
        ["Remote - UK"],
        ["Remote, EMEA"],
        ["Remote - India"],
        ["Remote (Germany)"],
        ["Remote - Mexico"],
        ["Remote, APAC"],
        ["Work from anywhere"],
    ],
)
def test_bare_remote_with_non_us_qualifier_does_not_count(locations):
    assert "remote_us" not in region_names(locations)


def test_job_remote_true_with_no_locations_counts_as_remote_us():
    assert "remote_us" in region_names([], remote=True)


@pytest.mark.parametrize(
    "locations",
    [["Tokyo"], ["Paris"], ["Dubai"], ["Stockholm", "London"], ["Korea"]],
)
def test_job_remote_true_does_not_make_a_named_non_us_city_remote_us(locations):
    # Seen live on Cohere's Ashby board: remote roles whose only location is a foreign city.
    assert "remote_us" not in region_names(locations, remote=True)


def test_bare_remote_in_korea_does_not_count():
    # Seen live on NVIDIA's Workday board.
    assert "remote_us" not in region_names(["Korea, Seoul", "Korea, Remote"])


def test_job_remote_none_with_no_locations_does_not_count():
    assert region_names([], remote=None) == ()


def test_job_remote_true_but_non_us_location_does_not_count():
    # A non-US qualifier in the locations wins over the Job.remote flag.
    assert "remote_us" not in region_names(["Remote (Canada)"], remote=True)
    assert "remote_us" not in region_names(["Canada"], remote=True)


def test_job_remote_signal_does_not_block_bay_area():
    hits = region_names(["San Francisco, CA"], remote=True)
    assert "bay_area" in hits


# --- multi-location lists / combinations -----------------------------------------


@pytest.mark.parametrize(
    "locations,expected_regions",
    [
        (["SF or NYC / Remote-US"], {"bay_area", "remote_us"}),
        (["San Francisco, CA", "Remote (Canada)"], {"bay_area"}),
        (["Remote", "San Francisco, CA"], {"bay_area", "remote_us"}),
        (["New York, NY", "Chicago, IL"], set()),
        (["Remote (Canada)", "Remote (UK)"], set()),
        (["Hybrid - San Francisco, CA"], {"bay_area"}),
    ],
)
def test_combinations(locations, expected_regions):
    assert set(region_names(locations)) == expected_regions


def test_has_non_us_qualifier_does_not_flag_us_state_codes():
    # "CA" (California) and "GA" (Georgia, the US state) must not be treated
    # as the country Canada / country Georgia.
    assert not has_non_us_qualifier(tokenize("San Francisco, CA"))
    assert not has_non_us_qualifier(tokenize("Atlanta, GA"))


def test_has_non_us_qualifier_flags_known_countries():
    assert has_non_us_qualifier(tokenize("Remote (Canada)"))
    assert has_non_us_qualifier(tokenize("Remote - India"))
    assert has_non_us_qualifier(tokenize("Remote, EMEA"))


# --- wired up against the real config/roles.yaml ---------------------------------


def test_region_matches_against_real_config():
    cfg = load_config(CONFIG_PATH)
    assert "bay_area" in region_matches(["San Francisco, CA"], None, cfg.regions)
    assert "remote_us" in region_matches(["Remote - US"], None, cfg.regions)
    assert region_matches(["Remote (Canada)"], None, cfg.regions) == ()
