"""The hosted MCP server: Streamable HTTP behind the GitHub OAuth, plus the digest's links.

`build_app(env)` returns the one Starlette app Cloud Run serves (`jobhunt-remote` runs it with
uvicorn on `$PORT`):

* `/mcp`: the MCP server, stateless with JSON responses (instances scale to zero and there
  may be two of them, so nothing lives in memory between requests). Needs a bearer token.
* `/register`, `/authorize`, `/token`, `/revoke` and the `.well-known` OAuth metadata: the
  SDK's, backed by `jobhunt.auth.GitHubOAuthProvider`.
* `/oauth/consent`, `/oauth/github/callback`: the provider's own pages.
* `/a/{token}`: the Approve/Skip links from the daily digest (`jobhunt.web`).
* `/healthz`: 200 without touching the database.

The app is `FastMCP.streamable_http_app()` itself, not mounted inside another app: mounting
would drop the session manager's lifespan and move the `.well-known` paths.

Every setting comes from the environment and is checked before anything is built; a missing
or malformed one stops the process with a message on stderr instead of a half-working server.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from jobhunt import auth
from jobhunt.auth import (
    AuthConfigError,
    GitHubAuthConfig,
    GitHubOAuthProvider,
    auth_settings,
    normalize_public_url,
    transport_security,
)
from jobhunt.links import PUBLIC_URL_ENV, SECRET_ENV, LinkConfigError, link_config
from jobhunt.mcp_server import create_server
from jobhunt.store import open_store
from jobhunt.web import link_routes

log = logging.getLogger(__name__)

DATABASE_ENV = "DATABASE_URL"
HEALTH_PATH = "/healthz"
DEFAULT_PORT = 8080

# Everything the hosted server refuses to start without. The GitHub variables are M's.
REQUIRED_ENV: tuple[str, ...] = tuple(
    dict.fromkeys((DATABASE_ENV, PUBLIC_URL_ENV, SECRET_ENV, *auth.ENV_VARS))
)


class RemoteConfigError(ValueError):
    """The hosted server can't start. The message names variables, never their values."""


def check_env(env: Mapping[str, str]) -> tuple[str, str]:
    """``(database_url, public_url)`` once every variable in `REQUIRED_ENV` is set and
    `JOBHUNT_PUBLIC_URL` is https scheme and host (http only for localhost).

    The URL is checked here, once, by the OAuth rule. `links.link_config` alone would also
    accept a path or plain http, and the OAuth issuer and the links must agree on one URL.
    """
    missing = [name for name in REQUIRED_ENV if not (env.get(name) or "").strip()]
    if missing:
        raise RemoteConfigError(f"the remote server needs {', '.join(missing)} to be set")
    try:
        public_url = normalize_public_url(env[PUBLIC_URL_ENV])
    except AuthConfigError as exc:
        raise RemoteConfigError(str(exc)) from None
    database = env[DATABASE_ENV].strip()
    if database == ":memory:":
        raise RemoteConfigError(f"{DATABASE_ENV} can't be :memory: (one connection per call)")
    return database, public_url


def build_app(
    env: Mapping[str, str] | None = None,
    *,
    root: str | Path = ".",
    http_client: httpx.AsyncClient | None = None,
) -> Starlette:
    """The hosted app. Raises `RemoteConfigError` for bad settings, before any connection.

    `root` holds `config/roles.yaml` and `config/seeds.yaml` (the image's working directory).
    Verified facts and resumes come from `VERIFIED_FACTS`, `RESUME_SE` and `RESUME_FDE` in
    the process environment, as for the CLI. `http_client` talks to GitHub (tests mock it).
    """
    env = os.environ if env is None else env
    database, public_url = check_env(env)
    # Both halves get the one normalized URL, so they can't disagree.
    checked = {**env, PUBLIC_URL_ENV: public_url}
    try:
        config = GitHubAuthConfig.from_env(checked)
        links = link_config(checked)
    except (AuthConfigError, LinkConfigError) as exc:
        raise RemoteConfigError(str(exc)) from None
    assert links is not None  # both variables are set: check_env saw to that
    _, link_secret = links

    def store_factory():
        return open_store(database)

    # Connect once now: runs the store's migrations and creates the OAuth tables, so a bad
    # DATABASE_URL fails the start (and Cloud Run's rollout) instead of the first request.
    with store_factory():
        pass
    provider = GitHubOAuthProvider(config, database, http_client=http_client)

    server = create_server(
        store_factory=store_factory,
        root=Path(root),
        auth_provider=provider,
        auth_settings=auth_settings(config),
        transport_security=transport_security(config),
        host="0.0.0.0",
        stateless_http=True,
        json_response=True,
    )
    provider.register_routes(server)
    for route in link_routes(store_factory, secret=link_secret):
        server.custom_route(route.path, methods=route.methods)(route.endpoint)
    server.custom_route(HEALTH_PATH, methods=["GET"])(healthz)
    return server.streamable_http_app()


async def healthz(request: Request) -> Response:
    """Liveness only. No database call: Neon may be asleep, and waking it on every probe
    would keep it (and the bill) awake."""
    return PlainTextResponse("ok", headers={"Cache-Control": "no-store"})


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jobhunt-remote",
        description="jobhunt MCP server over Streamable HTTP, with GitHub OAuth (Cloud Run)",
    )
    parser.add_argument(
        "--root", default=".", help="directory holding config/ (default: current directory)"
    )
    parser.add_argument("--host", default="0.0.0.0", help="bind address (default: 0.0.0.0)")
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"port (default: $PORT, else {DEFAULT_PORT})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for `jobhunt-remote`. Logs go to stderr."""
    logging.basicConfig(
        stream=sys.stderr, level=logging.INFO, format="jobhunt-remote: %(levelname)s %(message)s"
    )
    args = build_arg_parser().parse_args(argv)
    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        print(f"jobhunt-remote: --root {root} is not a directory", file=sys.stderr)
        return 2
    try:
        port = args.port or int(os.environ.get("PORT") or DEFAULT_PORT)
    except ValueError:
        print("jobhunt-remote: PORT must be a number", file=sys.stderr)
        return 2

    try:
        app = build_app(os.environ, root=root)
    except RemoteConfigError as exc:
        print(f"jobhunt-remote: {exc}. Refusing to start.", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 -- e.g. the database is unreachable; say so and stop
        print(f"jobhunt-remote: can't start: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if not os.environ[DATABASE_ENV].startswith(("postgres://", "postgresql://")):
        log.warning("DATABASE_URL is a SQLite path: fine locally, lost on every Cloud Run restart")

    log.info("serving %s on %s:%d", root, args.host, port)
    uvicorn.run(
        app,
        host=args.host,
        port=port,
        # None: no uvicorn logging config of its own, so its loggers use ours (stderr).
        log_config=None,
        # Cloud Run's front end is the only thing that can reach the container, so trust its
        # X-Forwarded-Proto: redirects the app builds from the request then stay on https.
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
