"""Normalize messy ATS location strings into named region hits.

Region definitions (names + match phrases) live in config/roles.yaml and are
loaded via `jobhunt.config.load_config`. This module turns a job's raw
`locations` list (plus its `remote` flag) into the set of configured region
names that job qualifies for.

Matching is done on whitespace/punctuation-normalized *tokens*, not raw
substrings, so a short match term like "sf" only matches a standalone "SF"
token and never fires inside an unrelated word (tokenizing "Austin" yields
one token, "austin", which is never equal to the token "us"). Multi-word
match terms ("san francisco") match as a contiguous run of tokens, so
punctuation differences ("San Francisco, CA", "San Francisco / CA",
"Hybrid - San Francisco") don't matter, and "Sales Engineering" never
collides with a "sales engineer" match term the way naive substring search
would (their tokens differ: "engineering" != "engineer").

Special case -- bare "remote": a location string that just says "Remote"
with no country/region qualifier is ambiguous. Per the approved plan, it
counts as `remote_us` only when no non-US qualifier appears anywhere in the
job's location strings (e.g. "Remote (Canada)", "Remote - UK", "Remote,
EMEA", "Remote - India" do NOT count as remote_us). `Job.remote` is used as
an additional signal for that same bare-remote case (useful when the ATS
gives no location strings at all), but an explicit non-US qualifier always
wins over both the location text and the flag.
"""

from __future__ import annotations

import re

from jobhunt.config import Region

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Country / macro-region words that, when present, mean a job is NOT tied to
# the US. Deliberately excludes 2-letter US state codes (e.g. "ca", "ga") and
# names that collide with US places (e.g. "Georgia") to avoid false
# positives. This is a pragmatic, hardcoded list rather than a
# config/roles.yaml key -- see the PR's "Flags for review" for a proposal to
# make it configurable.
NON_US_QUALIFIERS: tuple[str, ...] = (
    "canada",
    "uk",
    "united kingdom",
    "emea",
    "europe",
    "european union",
    "eu",
    "apac",
    "asia",
    "asia pacific",
    "latam",
    "latin america",
    "india",
    "australia",
    "new zealand",
    "mexico",
    "brazil",
    "argentina",
    "germany",
    "france",
    "spain",
    "italy",
    "netherlands",
    "ireland",
    "poland",
    "portugal",
    "philippines",
    "singapore",
    "japan",
    "china",
    "africa",
    "nigeria",
    "south africa",
    "uae",
    "middle east",
    "ukraine",
    "romania",
    "israel",
    "pakistan",
    "vietnam",
    "indonesia",
    "colombia",
    "chile",
    "peru",
    "egypt",
    "kenya",
    "sweden",
    "norway",
    "denmark",
    "finland",
    "switzerland",
    "austria",
    "belgium",
    "international",
    "worldwide",
    "anywhere",
)

_BARE_REMOTE = "remote"


def tokenize(text: str) -> tuple[str, ...]:
    """Lowercase `text` and split it into alphanumeric tokens.

    Any run of punctuation/whitespace (commas, slashes, hyphens, parens,
    "or", ...) becomes a token boundary, e.g. "Remote-US" and "Remote (US)"
    both tokenize to ("remote", "us").
    """
    return tuple(_TOKEN_RE.findall(text.lower()))


def contains_phrase(tokens: tuple[str, ...], phrase: str) -> bool:
    """True if `phrase`, tokenized, appears as a contiguous run in `tokens`."""
    phrase_tokens = tokenize(phrase)
    if not phrase_tokens:
        return False
    n = len(phrase_tokens)
    return any(tokens[i : i + n] == phrase_tokens for i in range(len(tokens) - n + 1))


def normalize_locations(locations: list[str]) -> tuple[str, ...]:
    """Tokenize every raw location string into one flat token tuple."""
    return tokenize(" | ".join(locations))


def has_non_us_qualifier(tokens: tuple[str, ...]) -> bool:
    """True if any known non-US country/region word is present in `tokens`."""
    return any(contains_phrase(tokens, qualifier) for qualifier in NON_US_QUALIFIERS)


def region_matches(
    locations: list[str],
    remote: bool | None,
    regions: tuple[Region, ...],
) -> tuple[str, ...]:
    """Return the names of every region whose match terms are satisfied.

    `locations` is a job's raw location strings (as the ATS gives them) and
    `remote` is `Job.remote`. The result follows `regions` order and never
    contains duplicates.
    """
    tokens = normalize_locations(locations)
    qualifier_present = has_non_us_qualifier(tokens)

    return tuple(
        region.name for region in regions if _region_hit(region, tokens, remote, qualifier_present)
    )


def _region_hit(
    region: Region,
    tokens: tuple[str, ...],
    remote: bool | None,
    qualifier_present: bool,
) -> bool:
    for term in region.match:
        if term == _BARE_REMOTE:
            # Bare "remote" is ambiguous. Count it only when nothing in the
            # job's locations says otherwise; a direct flag from the ATS
            # (Job.remote) can stand in when there's no location text at all.
            if qualifier_present:
                continue
            if contains_phrase(tokens, term) or remote is True:
                return True
        elif contains_phrase(tokens, term):
            return True
    return False
