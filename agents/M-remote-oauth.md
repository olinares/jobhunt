# Agent M — Remote transport + GitHub OAuth

## Branch / PR
`phase5/m-oauth` → one PR into `main`. Runs in parallel with N, O1 and Q. Merge order M → N → Q
(each appends to `.env.example`; the later one rebases).

## Agent model
`opus`: this is the only thing between the internet and Oz's job data and status writes.

## Owns
src/jobhunt/auth.py (new), tests/test_auth.py (new), src/jobhunt/mcp_server.py,
tests/test_mcp_server.py, .env.example (append this brief's variables only)

## Before writing code
Read the MCP Python SDK docs for Streamable HTTP and auth (`mcp.server.auth`:
`OAuthAuthorizationServerProvider`, `AuthSettings`, `TransportSecuritySettings`) in the
installed SDK source, and the MCP authorization spec. Don't write auth from memory. No new
dependencies: starlette and httpx are already available.

## Build
### mcp_server.py
- `create_server(...)` gains keyword args passed through to `FastMCP`: `auth_provider`,
  `auth_settings`, `transport_security`, `host`, `stateless_http`, `json_response`. Defaults
  keep today's stdio behaviour exactly. Remote use means `stateless_http=True` and
  `json_response=True`, because Cloud Run runs several instances and scales to zero, and
  in-memory sessions don't survive that.
- The SDK's default `host="127.0.0.1"` turns on DNS-rebinding protection that allows only
  localhost Host headers. The remote app must pass `transport_security` with the service host.
- `FACTS_HINT` also names `VERIFIED_FACTS` (the secret the host uses, see brief Q).
- `main()` keeps stdio. The remote entry point is brief P's `remote.py`, not this file.

### auth.py
An `OAuthAuthorizationServerProvider` that makes the server its own MCP authorization server
and delegates login to GitHub.
- **Dynamic client registration**, but `redirect_uris` must all be in an allowlist:
  `https://claude.ai/api/mcp/auth_callback`, `https://claude.com/api/mcp/auth_callback`, and
  `http://localhost:<any port>/…` / `http://127.0.0.1:<any port>/…`. Reject anything else at
  registration.
- `authorize` shows a **consent page** (client name, redirect host, "Continue with GitHub"),
  served as a starlette route function that brief P registers with `custom_route`. Only the
  POST from that page redirects to GitHub. Without it, anyone who registers a client could get
  a code while Oz is logged into GitHub.
- GitHub OAuth: **no scopes**. `state` is random, stored with the pending authorize params
  (client id, redirect uri, PKCE challenge, resource), **single use, 10-minute TTL**.
- Callback: exchange the code, call `GET https://api.github.com/user`, and accept only if the
  numeric `id` equals `JOBHUNT_ALLOWED_GITHUB_ID` (ids survive renames; logins don't).
  Anything else gets a plain 403 page, and no code is issued.
- Tokens: random, stored **hashed** (SHA-256) with expiry. Access 1 hour, refresh 30 days,
  **rotated on every use** (the old refresh token is revoked). `AccessToken.resource` is set,
  and resource validation is on.
- Storage: `oauth_clients`, `oauth_pending`, `oauth_codes`, `oauth_tokens` tables owned by
  auth.py, not `store.py` migrations. Support SQLite and Postgres the way `store.py` does
  (same URL → dialect rule). Create tables idempotently; on Postgres, hold
  `pg_advisory_lock` while creating them (two instances can cold-start at once). Open a
  connection per call.
- Env: `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, `JOBHUNT_ALLOWED_GITHUB_ID`,
  `JOBHUNT_PUBLIC_URL` (issuer / resource URL). Refuse to build the provider if any is missing.
- The GitHub HTTP client is injected, so tests fake it.

## Tests
SQLite in `tmp_path`, GitHub faked (respx or an injected client), no network.
- Full PKCE flow: register → consent GET (no side effect) → consent POST → GitHub callback →
  token → an authenticated `tools/list` over the Streamable HTTP app.
- Rejected cases: a GitHub id outside the allowlist, a `redirect_uri` outside the allowlist,
  a replayed or expired `state`, a wrong PKCE verifier, an expired or revoked access token,
  and a reused refresh token.
- `/mcp` without a token returns 401 with the `WWW-Authenticate` resource-metadata hint.
- A non-local Host header is accepted when `transport_security` allows it, and refused by
  default.
- Existing stdio tests still pass unchanged.

## Done when
PR open with the template filled in, CI green. If a change to `store.py`, `models.py` or any
other file outside "Owns" is needed, stop and report it.
