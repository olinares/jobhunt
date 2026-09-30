# Agent C — Title filter and location normalization

## Branch / PR
`wave1/c-filter-location` → one PR into `main`.

## Owns
src/jobhunt/{config.py,filter.py,location.py}, tests/test_filter.py, tests/test_location.py

## Build
- `config.py`: load and validate config/roles.yaml.
- `filter.py`: `matches(job, cfg) -> MatchResult` with which title rule and region matched, or why not.
  Title match is case-insensitive, allows seniority prefixes from `seniority_ok`, and rejects
  `exclude_title_terms`. Watch false positives (e.g. "Solutions Engineer Manager", "Sales Engineering Director").
- `location.py`: normalize messy strings — "SF or NYC / Remote-US", "Hybrid - San Francisco, CA",
  "Remote (Canada)", "United States", multi-location lists — into region hits. "Remote" outside
  the US must not count as remote_us.

## Done when
At least 40 table-driven test cases pass, covering tricky titles and locations.
PR is open with the template filled in and CI green.
