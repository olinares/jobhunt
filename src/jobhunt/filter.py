"""Title + region filtering.

`matches(job, cfg)` is the single entry point: it decides whether a Job's
title is one we're looking for (case-insensitive, allowing a `seniority_ok`
prefix, rejecting `exclude_title_terms`) and whether its location/remote
signals hit a configured region (see `jobhunt.location`). A job only counts
as an overall match when both a title rule and a region matched.
"""

from __future__ import annotations

from dataclasses import dataclass

from jobhunt.config import RolesConfig
from jobhunt.location import contains_phrase, region_matches, tokenize
from jobhunt.models import Job

# Level words that, when one directly precedes a matched title phrase and is
# NOT listed in `seniority_ok`, mean the role is a different level than the
# ones we're targeting (e.g. "Junior Solutions Engineer" when `seniority_ok`
# is [senior, staff, lead, principal, ""]). This vocabulary is fixed in code;
# `seniority_ok` only says which of these words are *allowed* as a prefix.
_SENIORITY_VOCAB = frozenset(
    {
        "senior",
        "staff",
        "lead",
        "principal",
        "junior",
        "associate",
        "entry",
        "jr",
        "sr",
        "i",
        "ii",
        "iii",
        "iv",
    }
)

# Words that, when one directly follows a matched title phrase, mean the role
# is a management/leadership variant rather than the IC role we're targeting
# (e.g. "Solutions Engineer Manager", "Solutions Engineer Head").
# config/roles.yaml's `exclude_title_terms` excludes "manager of" and bare
# "director"/"vp" (which already catch those words anywhere in the title);
# this closes the remaining gap for a bare trailing "Manager"/"Head"/"Lead"
# that isn't spelled "manager of ...". See the PR's "Flags for review".
_SUFFIX_DISQUALIFIERS = frozenset({"manager", "head", "lead"})


@dataclass(frozen=True)
class MatchResult:
    """Result of checking one Job against a RolesConfig.

    Attributes:
        matched: True only when a title rule AND a region both matched.
        title_rule: the `titles` phrase (from config) that matched, or None
            if no title phrase matched (or the title was excluded).
        regions: names of every configured region that matched; empty if
            none did, or if the title didn't match (region matching is
            skipped in that case).
        reason: short, human-readable explanation. Always set, for
            logging/debugging -- never parsed as a stable API.
    """

    matched: bool
    title_rule: str | None
    regions: tuple[str, ...]
    reason: str


def matches(job: Job, cfg: RolesConfig) -> MatchResult:
    """Check whether `job` passes the title and region rules in `cfg`."""
    title_rule, title_reason = _match_title(job.title, cfg)
    if title_rule is None:
        return MatchResult(matched=False, title_rule=None, regions=(), reason=title_reason)

    regions = region_matches(job.locations, job.remote, cfg.regions)
    if not regions:
        return MatchResult(
            matched=False,
            title_rule=title_rule,
            regions=(),
            reason=f"title matched '{title_rule}' but no configured region matched",
        )

    return MatchResult(
        matched=True,
        title_rule=title_rule,
        regions=regions,
        reason=f"title matched '{title_rule}'; region(s): {', '.join(regions)}",
    )


def _match_title(title: str, cfg: RolesConfig) -> tuple[str | None, str]:
    """Return (matched phrase, reason), or (None, why-not)."""
    tokens = tokenize(title)

    for term in cfg.exclude_title_terms:
        if contains_phrase(tokens, term):
            return None, f"title excluded: contains '{term}'"

    for phrase in cfg.titles:
        phrase_tokens = tokenize(phrase)
        n = len(phrase_tokens)
        if n == 0 or n > len(tokens):
            continue

        for i in range(len(tokens) - n + 1):
            if tokens[i : i + n] != phrase_tokens:
                continue

            if i > 0:
                prefix = tokens[i - 1]
                if prefix in _SENIORITY_VOCAB and prefix not in cfg.seniority_ok:
                    continue  # disallowed seniority level (e.g. "junior")

            suffix_index = i + n
            if suffix_index < len(tokens) and tokens[suffix_index] in _SUFFIX_DISQUALIFIERS:
                continue  # e.g. "... Engineer Manager"

            return phrase, f"matched title phrase '{phrase}'"

    return None, "no configured title phrase found in title"
