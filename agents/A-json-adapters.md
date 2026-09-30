# Agent A — Greenhouse, Lever, Ashby adapters

## Branch / PR
`wave1/a-json-adapters` → one PR into `main`.

## Owns
src/jobhunt/adapters/{http.py,greenhouse.py,lever.py,ashby.py}, tests/test_adapters_json.py,
tests/fixtures/{greenhouse,lever,ashby}_*.json

## Build
- `http.py`: shared httpx client — User-Agent, ~1s per-host delay, retry with backoff on 429/5xx.
  (Agent B imports this; publish it first and keep its API small.)
- Greenhouse: GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true
- Lever: GET https://api.lever.co/v0/postings/{slug}?mode=json
- Ashby: GET https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true
- Map each response into `Job` (models.py). Fill pay fields where the ATS provides them.
- 404 → raise BoardNotFound.

## Done when
Each adapter passes tests against a recorded fixture from one real, verified company board,
including one board with pay data.
PR is open with the template filled in and CI green.
