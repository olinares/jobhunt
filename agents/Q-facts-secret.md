# Agent Q — Verified facts from a secret

## Branch / PR
`phase5/q-facts-env` → one PR into `main`. Runs in parallel with M, N and O1. Merge after N
(rebase for `.env.example`).

## Agent model
`sonnet`: a small change to a well-tested loader.

## Owns
src/jobhunt/facts.py, tests/test_facts.py, .env.example (append this brief's variables only)

## Background
The remote server runs on Cloud Run from a public repo, so `private/verified.md` isn't there.
The host gets the file's **text** in a `VERIFIED_FACTS` secret. Today `$JOBHUNT_FACTS` is a
path. Don't open anything under `private/`; tests use synthetic facts only.

## Build
- Precedence in `load_facts`: explicit `path` > `VERIFIED_FACTS` (text) > `JOBHUNT_FACTS`
  (path) > `<root>/private/verified.md`.
- Text from `VERIFIED_FACTS` goes through exactly the same parser: only `[x]` items, the
  Conflicts section skipped. An empty or whitespace-only value counts as unset.
- The source description (used in messages) says "from $VERIFIED_FACTS", never the text.
- `FactsNotFound`'s message lists `VERIFIED_FACTS` among the places tried.
- The draft file (`verified-draft.md`) is still refused by explicit path.

## Tests
- `VERIFIED_FACTS` loads; it beats `JOBHUNT_FACTS`; an explicit path beats both.
- Unticked items and Conflicts stay out when loaded from env.
- Blank env is ignored.
- Error messages don't echo the env value.

## Done when
PR open with the template filled in, CI green. If `mcp_server.py` or `packet.py` would need
changing, stop and report it (brief M owns the server's hint text).
