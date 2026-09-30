# Agent P — Deploy to Cloud Run

## Branch / PR
`phase5/p-deploy` → one PR into `main`. Starts only after M, N and Q are merged.

## Agent model
`opus`: it wires auth, the links and the secrets together, and sets up keyless deploys.

## Owns
src/jobhunt/remote.py (new), tests/test_remote.py (new), Dockerfile (new), .dockerignore (new),
.github/workflows/deploy.yml (new), docs/deploy.md (new), pyproject.toml (a
`jobhunt-remote` script only)

## Before writing code
- Read the installed SDK's `FastMCP.streamable_http_app()` and `custom_route`, and the Cloud
  Run and `google-github-actions/auth` (Workload Identity Federation) docs.
- Check the current claude.ai custom connector docs: if a connector can be given a fixed
  OAuth client id and secret, say so in the PR. Oz may then drop dynamic registration.

## Build
### remote.py
- `build_app(env) -> Starlette`: `create_server(...)` with M's GitHub provider,
  `stateless_http=True`, `json_response=True`, and `transport_security` allowing the host from
  `JOBHUNT_PUBLIC_URL`.
- Register M's consent and GitHub callback routes, N's `/a/{token}` routes and `GET /healthz`
  (no DB call) with `custom_route`.
- Return `server.streamable_http_app()` **directly, not mounted inside another app**, so the
  session-manager lifespan and the `.well-known` OAuth metadata paths stay correct.
- `main()` runs uvicorn on `$PORT`. Logging goes to stderr.
- Refuse to start if `DATABASE_URL`, `JOBHUNT_PUBLIC_URL`, `JOBHUNT_LINK_SECRET` or M's GitHub
  variables are missing.

### Dockerfile
- `python:3.12-slim`, non-root user.
- `pip install .`, and copy `config/` (roles.yaml and seeds.yaml are read from `--root`).
- No `private/`, `.env` or tests in the image; `.dockerignore` enforces that.

### deploy.yml
- On push to main, only if `vars.DEPLOY_ENABLED == 'true'`, plus `workflow_dispatch`.
- `google-github-actions/auth` with Workload Identity Federation: no JSON key anywhere.
- Build and push to Artifact Registry, then `gcloud run deploy`:
  - `--allow-unauthenticated` (the app does its own OAuth).
  - `--min-instances 0 --max-instances 2`.
  - `--timeout 900` (`refresh_boards` and `discover_companies` can be slow).
  - Every secret from Secret Manager with `--set-secrets`: `DATABASE_URL`, `VERIFIED_FACTS`,
    `RESUME_SE`, `RESUME_FDE`, `JOBHUNT_LINK_SECRET`, `GITHUB_CLIENT_ID`,
    `GITHUB_CLIENT_SECRET`, `JOBHUNT_ALLOWED_GITHUB_ID` and `SEARCH_API_KEY`.
  - `JOBHUNT_PUBLIC_URL` as a plain env var.
- After deploy, smoke test: `/healthz` returns 200 and `/mcp` without a token returns 401.

### docs/deploy.md
Oz's one-time steps:
- The GCP project, billing and a $5 budget alert.
- Enable the APIs, the Artifact Registry repo, the WIF pool and provider bound to this repo,
  and the deploy service account.
- The GitHub OAuth app, with its callback at `<service>/oauth/github/callback`. Find the
  numeric GitHub id with `gh api user --jq .id`.
- Create each secret. `JOBHUNT_LINK_SECRET` must be **the same value** in Secret Manager and
  the GitHub repo secret. Generate it once with `openssl rand -base64 48`.
- Set `VERIFIED_FACTS` with `gcloud secrets create VERIFIED_FACTS --data-file=private/verified.md`.
- Set the repo variables `JOBHUNT_PUBLIC_URL` and `DEPLOY_ENABLED=true`.
- Add the claude.ai custom connector at `<service>/mcp`.
- Rotating and revoking: delete rows in `oauth_tokens`, rotate the GitHub secret.

## Tests
- `build_app` against SQLite: `/healthz` 200, `/mcp` 401 without a token, `/a/<bad>` 400, and
  the `.well-known/oauth-authorization-server` metadata is served.
- Missing env refuses to start.
- No network, no Docker in CI.

## Done when
PR open with the template filled in, CI green. The first real deploy happens after Oz finishes
docs/deploy.md and sets `DEPLOY_ENABLED`.
