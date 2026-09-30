# Agent B — Workday and Gem adapters

## Branch / PR
`wave1/b-workday-gem-adapters` → one PR into `main`.

## Owns
src/jobhunt/adapters/{workday.py,gem.py}, tests/test_adapters_workday_gem.py,
tests/fixtures/{workday,gem}_*.json

## Build
- Import the shared client from adapters/http.py (Agent A). If it isn't merged yet, stub the same
  interface locally and note it for Wave 2.
- Workday: POST https://{host}/wday/cxs/{tenant}/{site}/jobs with JSON
  {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}; page with offset until done.
  Descriptions come from each job's externalPath (only when with_descriptions=True).
  Undocumented endpoint: confirm the shape against a live board before coding.
- Gem: unofficial public GraphQL batch endpoint POST https://jobs.gem.com/api/public/graphql/batch
  (JobBoardList for listings, ExternalJobPostingQuery for detail). Confirm the query shape from a live
  jobs.gem.com board's network traffic. Mark the adapter EXPERIMENTAL in its docstring.

## Merge order
Depends on Agent A's PR (http.py). Open the PR anyway, marked as depending on A; rebase after A merges.

## Done when
Both pass tests against recorded fixtures; Workday pagination is covered by a test.
PR is open with the template filled in and CI green.
