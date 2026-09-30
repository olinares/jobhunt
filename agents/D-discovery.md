# Agent D — Discovery (titles + regions → boards)

## Branch / PR
`wave1/d-discovery` → one PR into `main`.

## Owns
src/jobhunt/discovery/{queries.py,search.py,slugs.py}, tests/test_slugs.py, tests/test_queries.py

## Build
- `queries.py`: build queries per ATS domain x title x region, e.g.
  site:jobs.ashbyhq.com "forward deployed engineer" "San Francisco".
  Domains: boards.greenhouse.io, job-boards.greenhouse.io, jobs.lever.co, jobs.ashbyhq.com,
  myworkdayjobs.com, jobs.gem.com.
- `search.py`: pluggable search client interface; first implementation reads SEARCH_API_KEY and
  calls one provider (Brave or Serper — pick one, keep the interface swappable). Cap queries per run.
- `slugs.py`: `board_from_url(url) -> BoardRef | None` for every domain above. Workday URLs look like
  {tenant}.wd{N}.myworkdayjobs.com/[locale/]{site}/job/... — strip the optional locale (e.g. en-US).
  Ignore non-board URLs.

## Done when
`board_from_url` passes 30+ real-world URL cases; query builder is tested; search client is mocked in tests.
PR is open with the template filled in and CI green.
