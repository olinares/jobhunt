"""Build search-engine queries that surface company job boards on known ATS domains.

Each query targets one (domain, title, region) combination, e.g.:

    site:jobs.ashbyhq.com "forward deployed engineer" "San Francisco"

The caller (Wave 2 wiring, owned by another agent) is responsible for turning
config (titles/regions) into the plain lists this module takes.
"""

from __future__ import annotations

DEFAULT_DOMAINS: tuple[str, ...] = (
    "boards.greenhouse.io",
    "job-boards.greenhouse.io",
    "jobs.lever.co",
    "jobs.ashbyhq.com",
    "myworkdayjobs.com",
    "jobs.gem.com",
)


def build_queries(
    titles: list[str],
    region_terms: list[str],
    domains: tuple[str, ...] | list[str] = DEFAULT_DOMAINS,
) -> list[str]:
    """Return one query per (domain, title, region) combination.

    Titles and regions are quoted as exact phrases. If ``region_terms`` is empty,
    one query per (domain, title) is produced with no region term appended.
    Order is deterministic: domain, then title, then region — useful for capping
    a run at N queries and getting broad domain/title coverage first.
    """
    queries: list[str] = []
    regions = region_terms or [None]
    for domain in domains:
        for title in titles:
            for region in regions:
                parts = [f"site:{domain}", f'"{title}"']
                if region:
                    parts.append(f'"{region}"')
                queries.append(" ".join(parts))
    return queries
