"""OAuth for the remote MCP server: the server is its own authorization server, GitHub logs in.

`GitHubOAuthProvider` implements the MCP SDK's `OAuthAuthorizationServerProvider`. The SDK
serves `/register`, `/authorize`, `/token`, `/revoke` and the `.well-known` metadata; this
module adds two routes of its own (`register_routes`):

* `GET/POST /oauth/consent`: the page `/authorize` redirects to. It names the client and where
  the code will be sent, and only its POST continues to GitHub.
* `GET /oauth/github/callback`: GitHub returns here. Only the one allowed GitHub account (by
  numeric id) gets an authorization code.

The flow, for a client such as claude.ai:

1. `POST /register` (dynamic client registration). Every `redirect_uri` must be claude.ai's
   or claude.com's callback, or `http://localhost` / `http://127.0.0.1` on any port.
2. `GET /authorize` stores the request under a random `state` (single use, 10 minutes) and
   redirects to the consent page.
3. The consent page's POST (CSRF-checked) binds the request to this browser with a cookie and
   redirects to GitHub, asking for no scopes.
4. The callback consumes the `state`, checks the browser cookie, exchanges GitHub's code,
   reads `GET /user`, and issues our own code only if the id matches.
5. `POST /token` swaps the code (PKCE-checked by the SDK) for an access token (1 hour) and a
   refresh token (30 days, rotated on every use).

Step 3's browser binding is what makes the consent page mean something. Without it, someone
who registered a client and started `/authorize` could send Oz straight to GitHub with their
`state`; GitHub skips its own prompt for an app Oz already authorized, and the code would go to
their redirect URI.

Storage: four tables owned by this module (`oauth_clients`, `oauth_pending`, `oauth_codes`,
`oauth_tokens`), on SQLite or Postgres by the same rule as `store.open_store`. Codes, tokens
and states are stored as SHA-256 hashes, never as issued. A connection is opened per call.
"""

from __future__ import annotations

import hashlib
import html
import json
import secrets
import sqlite3
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self
from urllib.parse import urlencode, urlsplit

import anyio
import httpx
import psycopg
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

ENV_VARS: tuple[str, ...] = (
    "GITHUB_CLIENT_ID",
    "GITHUB_CLIENT_SECRET",
    "JOBHUNT_ALLOWED_GITHUB_ID",
    "JOBHUNT_PUBLIC_URL",
)

MCP_PATH = "/mcp"
CONSENT_PATH = "/oauth/consent"
GITHUB_CALLBACK_PATH = "/oauth/github/callback"

GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"
USER_AGENT = "jobhunt/0.1 (+https://github.com/olinares/jobhunt)"

PENDING_TTL = 10 * 60
CODE_TTL = 5 * 60
ACCESS_TTL = 60 * 60
REFRESH_TTL = 30 * 24 * 60 * 60

# Redirect URIs a client may register: claude.ai / claude.com's connector callback, exactly,
# or a loopback address on any port and path (Claude Code, MCP Inspector).
HOSTED_REDIRECT_URIS = frozenset(
    {
        "https://claude.ai/api/mcp/auth_callback",
        "https://claude.com/api/mcp/auth_callback",
    }
)
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1"})

_PG_LOCK_KEY = 7415382  # store.py's migrations use 7415381

_SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS oauth_clients (
        client_id TEXT PRIMARY KEY,
        client_json TEXT NOT NULL,
        created_at BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS oauth_pending (
        state_hash TEXT PRIMARY KEY,
        client_id TEXT NOT NULL,
        params_json TEXT NOT NULL,
        browser_hash TEXT,
        expires_at BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS oauth_codes (
        code_hash TEXT PRIMARY KEY,
        client_id TEXT NOT NULL,
        code_json TEXT NOT NULL,
        expires_at BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS oauth_tokens (
        token_hash TEXT PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('access', 'refresh')),
        grant_id TEXT NOT NULL,
        client_id TEXT NOT NULL,
        subject TEXT,
        scopes TEXT NOT NULL,
        resource TEXT,
        expires_at BIGINT NOT NULL,
        revoked INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_oauth_tokens_grant ON oauth_tokens (grant_id)",
)


class AuthConfigError(ValueError):
    """The OAuth provider can't be built: a variable is missing or malformed."""


# --------------------------------------------------------------------------- config


@dataclass(frozen=True)
class GitHubAuthConfig:
    """The GitHub OAuth app, the one allowed GitHub account, and this server's public URL."""

    client_id: str
    client_secret: str
    allowed_github_id: int
    public_url: str  # scheme://host[:port], no trailing slash

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Self:
        """Read `ENV_VARS` from `env`; raise `AuthConfigError` naming anything missing."""
        values = {name: (env.get(name) or "").strip() for name in ENV_VARS}
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise AuthConfigError(f"remote auth needs {', '.join(missing)} to be set")
        try:
            allowed = int(values["JOBHUNT_ALLOWED_GITHUB_ID"])
        except ValueError:
            raise AuthConfigError(
                "JOBHUNT_ALLOWED_GITHUB_ID must be a numeric GitHub user id "
                "(gh api user --jq .id), not a login"
            ) from None
        return cls(
            client_id=values["GITHUB_CLIENT_ID"],
            client_secret=values["GITHUB_CLIENT_SECRET"],
            allowed_github_id=allowed,
            public_url=normalize_public_url(values["JOBHUNT_PUBLIC_URL"]),
        )

    @property
    def resource_url(self) -> str:
        """The MCP endpoint: the RFC 8707 resource every token is bound to."""
        return self.public_url + MCP_PATH

    @property
    def github_callback_url(self) -> str:
        return self.public_url + GITHUB_CALLBACK_PATH

    @property
    def secure_cookies(self) -> bool:
        return self.public_url.startswith("https://")


def normalize_public_url(url: str) -> str:
    """`https://host[:port]` without a trailing slash. Plain http only for loopback."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise AuthConfigError(f"JOBHUNT_PUBLIC_URL must be an http(s) URL, got {url!r}")
    if parts.scheme == "http" and parts.hostname not in LOOPBACK_HOSTS:
        raise AuthConfigError("JOBHUNT_PUBLIC_URL must use https (http only for localhost)")
    if parts.path not in ("", "/") or parts.query or parts.fragment or parts.username:
        raise AuthConfigError(
            f"JOBHUNT_PUBLIC_URL must be just scheme and host, like https://example.com; "
            f"got {url!r}"
        )
    return f"{parts.scheme}://{parts.netloc.lower()}"


def auth_settings(config: GitHubAuthConfig) -> AuthSettings:
    """`AuthSettings` for `create_server`: this server issues the tokens, for its own `/mcp`."""
    return AuthSettings(
        issuer_url=config.public_url,
        resource_server_url=config.resource_url,
        validate_token_resource=True,
        client_registration_options=ClientRegistrationOptions(enabled=True),
        revocation_options=RevocationOptions(enabled=True),
    )


def transport_security(config: GitHubAuthConfig) -> TransportSecuritySettings:
    """DNS-rebinding protection that accepts the public host (and only it) on `/mcp`."""
    parts = urlsplit(config.public_url)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[parts.netloc],
        allowed_origins=[config.public_url],
    )


def is_allowed_redirect_uri(uri: str) -> bool:
    """claude.ai / claude.com's callback exactly, or http loopback on any port and path."""
    if uri in HOSTED_REDIRECT_URIS:
        return True
    try:
        parts = urlsplit(uri)
        parts.port  # noqa: B018 -- raises ValueError on a malformed port
    except ValueError:
        return False
    return (
        parts.scheme == "http"
        and parts.hostname in LOOPBACK_HOSTS
        and parts.username is None
        and parts.password is None
        and not parts.fragment
    )


# --------------------------------------------------------------------------- storage


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class _Conn:
    """One connection, with `?` placeholders on both dialects."""

    def __init__(self, raw: Any, postgres: bool) -> None:
        self._raw = raw
        self._postgres = postgres

    def execute(self, sql: str, params: Sequence[object] = ()) -> Any:
        # The SQL in this module has no literal "?" or "%", so the swap is exact.
        return self._raw.execute(sql.replace("?", "%s") if self._postgres else sql, params)

    def one(self, sql: str, params: Sequence[object] = ()) -> Any:
        """The first row (a tuple) or None."""
        # fetchall, not fetchone: an unfinished DELETE/UPDATE ... RETURNING would block COMMIT.
        rows = self.execute(sql, params).fetchall()
        return rows[0] if rows else None

    @contextmanager
    def transaction(self) -> Iterator[None]:
        if self._postgres:
            with self._raw.transaction():
                yield
            return
        # IMMEDIATE takes the write lock up front, as store.py does.
        self._raw.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._raw.execute("ROLLBACK")
            raise
        self._raw.execute("COMMIT")


class OAuthDatabase:
    """The OAuth tables. `target` is a postgres:// URL or a SQLite path, as for `open_store`."""

    def __init__(self, target: str | Path) -> None:
        self._target = str(target)
        self._postgres = self._target.startswith(("postgres://", "postgresql://"))
        if self._target == ":memory:":
            raise AuthConfigError("OAuth storage can't be :memory: (one connection per call)")
        self.create_tables()

    @contextmanager
    def connect(self) -> Iterator[_Conn]:
        if self._postgres:
            raw = psycopg.connect(self._target, autocommit=True)
        else:
            raw = sqlite3.connect(self._target, autocommit=True)
        try:
            yield _Conn(raw, self._postgres)
        finally:
            raw.close()

    def create_tables(self) -> None:
        """Idempotent. On Postgres an advisory lock stops two cold-starting instances racing
        on CREATE TABLE IF NOT EXISTS (which isn't safe to run concurrently there)."""
        with self.connect() as conn, conn.transaction():
            if self._postgres:
                conn.execute(f"SELECT pg_advisory_xact_lock({_PG_LOCK_KEY})")
            for statement in _SCHEMA:
                conn.execute(statement)


# --------------------------------------------------------------------------- provider


Endpoint = Callable[[Request], Awaitable[Response]]


class GitHubOAuthProvider:
    """`OAuthAuthorizationServerProvider` that logs in with GitHub and allows one account.

    `http_client` is used for GitHub's token and user endpoints; tests inject one backed by
    `httpx.MockTransport`. Without it each callback opens (and closes) its own client.
    `clock` returns the current Unix time.
    """

    def __init__(
        self,
        config: GitHubAuthConfig,
        database: str | Path,
        *,
        http_client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.db = OAuthDatabase(database)
        self._http = http_client
        self._clock = clock

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str],
        database: str | Path,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> Self:
        """Refuses (`AuthConfigError`) unless every variable in `ENV_VARS` is set."""
        return cls(GitHubAuthConfig.from_env(env), database, http_client=http_client)

    def _now(self) -> int:
        return int(self._clock())

    async def _db(self, fn: Callable[[_Conn], Any]) -> Any:
        """Run `fn` with a fresh connection, off the event loop (psycopg and sqlite3 block)."""

        def run() -> Any:
            with self.db.connect() as conn:
                return fn(conn)

        return await anyio.to_thread.run_sync(run)

    # -- clients -------------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        row = await self._db(
            lambda c: c.one(
                "SELECT client_json FROM oauth_clients WHERE client_id = ?", (client_id,)
            )
        )
        return None if row is None else OAuthClientInformationFull.model_validate_json(row[0])

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = [str(uri) for uri in client_info.redirect_uris or []]
        refused = [uri for uri in uris if not is_allowed_redirect_uri(uri)]
        if not uris or refused:
            raise RegistrationError(
                "invalid_redirect_uri",
                f"redirect_uri not allowed: {', '.join(refused) or '(none given)'}. Allowed: "
                "claude.ai / claude.com's MCP callback, or http://localhost or "
                "http://127.0.0.1 on any port.",
            )
        body = client_info.model_dump_json(exclude_none=True)
        now = self._now()

        def insert(conn: _Conn) -> None:
            conn.execute(
                "INSERT INTO oauth_clients (client_id, client_json, created_at) VALUES (?, ?, ?)",
                (client_info.client_id, body, now),
            )

        await self._db(insert)

    # -- authorize: park the request until consent + GitHub --------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        if params.resource is not None and not _same_url(params.resource, self.config.resource_url):
            raise AuthorizeError("invalid_request", f"resource must be {self.config.resource_url}")
        params = params.model_copy(update={"resource": self.config.resource_url})
        state = secrets.token_urlsafe(32)
        now = self._now()

        def insert(conn: _Conn) -> None:
            with conn.transaction():
                _sweep(conn, now)
                conn.execute(
                    "INSERT INTO oauth_pending (state_hash, client_id, params_json, expires_at) "
                    "VALUES (?, ?, ?, ?)",
                    (_hash(state), client.client_id, params.model_dump_json(), now + PENDING_TTL),
                )

        await self._db(insert)
        return f"{self.config.public_url}{CONSENT_PATH}?{urlencode({'state': state})}"

    async def _pending(self, state: str) -> tuple[str, AuthorizationParams] | None:
        """The live pending request for `state`: (client_id, params). Reads only."""
        now = self._now()
        row = await self._db(
            lambda c: c.one(
                "SELECT client_id, params_json FROM oauth_pending "
                "WHERE state_hash = ? AND expires_at > ?",
                (_hash(state), now),
            )
        )
        if row is None:
            return None
        return row[0], AuthorizationParams.model_validate_json(row[1])

    # -- routes ----------------------------------------------------------------

    def register_routes(self, server: Any) -> None:
        """Register the consent page and the GitHub callback on a FastMCP server."""
        server.custom_route(CONSENT_PATH, methods=["GET", "POST"])(self.consent)
        server.custom_route(GITHUB_CALLBACK_PATH, methods=["GET"])(self.github_callback)

    def _cookie(self, name: str) -> str:
        # __Host- cookies must be Secure, path=/ and host-only: nothing else can set them.
        return f"__Host-jobhunt_{name}" if self.config.secure_cookies else f"jobhunt_{name}"

    async def consent(self, request: Request) -> Response:
        """GET shows who is asking; POST (from that page only) continues to GitHub."""
        if request.method == "POST":
            return await self._consent_post(request)
        state = request.query_params.get("state", "")
        pending = await self._pending(state) if state else None
        if pending is None:
            return _page("Sign-in link expired", _EXPIRED, 400)
        client_id, params = pending
        client = await self.get_client(client_id)
        if client is None:
            return _page("Unknown client", "<p>This app is no longer registered.</p>", 400)
        name = client.client_name or "An unnamed app"
        host = urlsplit(str(params.redirect_uri)).netloc
        csrf = secrets.token_urlsafe(32)
        body = f"""
<p><strong>{html.escape(name)}</strong> wants access to Oz's jobhunt server: reading
jobs and facts, and changing job statuses.</p>
<p>After you sign in, access is sent to <strong>{html.escape(host)}</strong>.</p>
<p>If you didn't just connect this app, close this page.</p>
<form method="post" action="{CONSENT_PATH}">
  <input type="hidden" name="state" value="{html.escape(state)}">
  <input type="hidden" name="csrf" value="{csrf}">
  <button type="submit">Continue with GitHub</button>
</form>"""
        response = _page("Connect to jobhunt?", body, 200)
        response.set_cookie(
            self._cookie("consent"),
            csrf,
            max_age=PENDING_TTL,
            path="/",
            secure=self.config.secure_cookies,
            httponly=True,
            samesite="strict",
        )
        return response

    async def _consent_post(self, request: Request) -> Response:
        origin = request.headers.get("origin")
        if origin is not None and not _same_url(origin, self.config.public_url):
            return _page("Forbidden", "<p>This form must be sent from its own page.</p>", 403)
        form = await request.form()
        state, csrf = form.get("state"), form.get("csrf")
        cookie = request.cookies.get(self._cookie("consent"))
        if not (
            isinstance(state, str)
            and isinstance(csrf, str)
            and cookie
            and secrets.compare_digest(cookie.encode(), csrf.encode())
        ):
            return _page("Forbidden", "<p>Open the sign-in link again and retry.</p>", 403)
        browser = secrets.token_urlsafe(32)
        now = self._now()

        def bind(conn: _Conn) -> int:
            return conn.execute(
                "UPDATE oauth_pending SET browser_hash = ? WHERE state_hash = ? AND expires_at > ?",
                (_hash(browser), _hash(state), now),
            ).rowcount

        if await self._db(bind) != 1:
            return _page("Sign-in link expired", _EXPIRED, 400)
        query = urlencode(
            {
                "client_id": self.config.client_id,
                "redirect_uri": self.config.github_callback_url,
                "state": state,
                "allow_signup": "false",
            }
        )
        response = RedirectResponse(f"{GITHUB_AUTHORIZE_URL}?{query}", status_code=303)
        response.headers["Cache-Control"] = "no-store"
        response.delete_cookie(
            self._cookie("consent"), path="/", secure=self.config.secure_cookies, httponly=True
        )
        # Lax, not Strict: GitHub's redirect back is a cross-site top-level GET.
        response.set_cookie(
            self._cookie("login"),
            browser,
            max_age=PENDING_TTL,
            path="/",
            secure=self.config.secure_cookies,
            httponly=True,
            samesite="lax",
        )
        return response

    async def github_callback(self, request: Request) -> Response:
        """Consume the state, check the browser, ask GitHub who this is, issue a code."""
        state = request.query_params.get("state", "")
        now = self._now()

        def consume(conn: _Conn) -> Any:
            return conn.one(
                "DELETE FROM oauth_pending WHERE state_hash = ? "
                "RETURNING client_id, params_json, browser_hash, expires_at",
                (_hash(state),),
            )

        row = await self._db(consume) if state else None
        if row is None or row[3] <= now:
            return _page("Sign-in link expired", _EXPIRED, 400)
        client_id, params_json, browser_hash = row[0], row[1], row[2]
        cookie = request.cookies.get(self._cookie("login"))
        if not (
            browser_hash
            and cookie
            and secrets.compare_digest(browser_hash.encode(), _hash(cookie).encode())
        ):
            return _page(
                "Forbidden",
                "<p>This sign-in was started in another browser. Start again from the app.</p>",
                403,
            )
        if "code" not in request.query_params:
            return _page("Sign-in cancelled", "<p>GitHub didn't sign you in.</p>", 403)

        try:
            github_id = await self._github_user_id(request.query_params["code"])
        except _GitHubError as exc:
            return _page("GitHub sign-in failed", f"<p>{html.escape(str(exc))}</p>", 502)
        if github_id != self.config.allowed_github_id:
            return _page("Forbidden", "<p>This GitHub account can't use this server.</p>", 403)

        params = AuthorizationParams.model_validate_json(params_json)
        code = secrets.token_urlsafe(32)
        stored = AuthorizationCode(
            code="",  # the code itself is never stored, only its hash
            scopes=params.scopes or [],
            expires_at=now + CODE_TTL,
            client_id=client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject=str(github_id),
        )

        def insert(conn: _Conn) -> None:
            conn.execute(
                "INSERT INTO oauth_codes (code_hash, client_id, code_json, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (_hash(code), client_id, stored.model_dump_json(), now + CODE_TTL),
            )

        await self._db(insert)
        response = RedirectResponse(
            construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state),
            status_code=302,
        )
        response.headers["Cache-Control"] = "no-store"
        response.delete_cookie(
            self._cookie("login"), path="/", secure=self.config.secure_cookies, httponly=True
        )
        return response

    async def _github_user_id(self, code: str) -> int:
        if self._http is not None:
            return await self._ask_github(self._http, code)
        async with httpx.AsyncClient(timeout=10.0) as client:
            return await self._ask_github(client, code)

    async def _ask_github(self, client: httpx.AsyncClient, code: str) -> int:
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        try:
            reply = await client.post(
                GITHUB_TOKEN_URL,
                data={
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret,
                    "code": code,
                    "redirect_uri": self.config.github_callback_url,
                },
                headers=headers,
            )
            token = reply.json().get("access_token") if reply.status_code == 200 else None
            if not token:
                raise _GitHubError("GitHub didn't accept the sign-in code. Try again.")
            user = await client.get(
                GITHUB_USER_URL,
                headers={
                    **headers,
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {token}",
                },
            )
            github_id = user.json().get("id") if user.status_code == 200 else None
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            raise _GitHubError(f"Couldn't reach GitHub ({type(exc).__name__}).") from exc
        if not isinstance(github_id, int) or isinstance(github_id, bool):
            raise _GitHubError("GitHub didn't say who you are. Try again.")
        return github_id

    # -- authorization codes -----------------------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        row = await self._db(
            lambda c: c.one(
                "SELECT code_json FROM oauth_codes WHERE code_hash = ? AND client_id = ?",
                (_hash(authorization_code), client.client_id),
            )
        )
        if row is None:
            return None
        stored = AuthorizationCode.model_validate_json(row[0])
        return stored.model_copy(update={"code": authorization_code})

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        now = self._now()
        code = authorization_code

        def exchange(conn: _Conn) -> OAuthToken | None:
            with conn.transaction():
                # Deleting is the single-use check: a concurrent second exchange finds nothing.
                used = conn.one(
                    "DELETE FROM oauth_codes WHERE code_hash = ? AND client_id = ? "
                    "AND expires_at > ? RETURNING client_id",
                    (_hash(code.code), client.client_id, now),
                )
                if used is None:
                    return None
                return _issue(
                    conn,
                    grant_id=secrets.token_urlsafe(16),
                    client_id=code.client_id,
                    subject=code.subject,
                    scopes=code.scopes,
                    resource=code.resource,
                    now=now,
                )

        tokens = await self._db(exchange)
        if tokens is None:
            raise TokenError("invalid_grant", "authorization code already used or expired")
        return tokens

    # -- tokens --------------------------------------------------------------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        now = self._now()
        row = await self._db(
            lambda c: c.one(
                "SELECT client_id, subject, scopes, resource, expires_at FROM oauth_tokens "
                "WHERE token_hash = ? AND kind = 'access' AND revoked = 0 AND expires_at > ?",
                (_hash(token), now),
            )
        )
        if row is None:
            return None
        return AccessToken(
            token=token,
            client_id=row[0],
            subject=row[1],
            scopes=json.loads(row[2]),
            resource=row[3],
            expires_at=row[4],
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        now = self._now()

        def load(conn: _Conn) -> RefreshToken | None:
            row = conn.one(
                "SELECT grant_id, client_id, subject, scopes, resource, expires_at, revoked "
                "FROM oauth_tokens WHERE token_hash = ? AND kind = 'refresh'",
                (_hash(refresh_token),),
            )
            if row is None or row[1] != client.client_id:
                return None
            if row[6]:
                # A rotated-out refresh token came back: it was copied. Revoke the whole
                # grant, including the tokens that replaced it (OAuth 2.1 reuse detection).
                conn.execute("UPDATE oauth_tokens SET revoked = 1 WHERE grant_id = ?", (row[0],))
                return None
            if row[5] <= now:
                return None
            return RefreshToken(
                token=refresh_token,
                client_id=row[1],
                subject=row[2],
                scopes=json.loads(row[3]),
                resource=row[4],
                expires_at=row[5],
            )

        return await self._db(load)

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        now = self._now()

        def rotate(conn: _Conn) -> OAuthToken | None:
            with conn.transaction():
                row = conn.one(
                    "UPDATE oauth_tokens SET revoked = 1 WHERE token_hash = ? "
                    "AND kind = 'refresh' AND client_id = ? AND revoked = 0 AND expires_at > ? "
                    "RETURNING grant_id, subject, resource",
                    (_hash(refresh_token.token), client.client_id, now),
                )
                if row is None:
                    return None
                # Access tokens from before the rotation go too.
                conn.execute("UPDATE oauth_tokens SET revoked = 1 WHERE grant_id = ?", (row[0],))
                return _issue(
                    conn,
                    grant_id=row[0],
                    client_id=client.client_id,
                    subject=row[1],
                    scopes=scopes,
                    resource=row[2],
                    now=now,
                )

        tokens = await self._db(rotate)
        if tokens is None:
            raise TokenError("invalid_grant", "refresh token already used, revoked or expired")
        return tokens

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        """Revokes every token of the grant `token` belongs to."""

        def revoke(conn: _Conn) -> None:
            conn.execute(
                "UPDATE oauth_tokens SET revoked = 1 WHERE grant_id IN "
                "(SELECT grant_id FROM oauth_tokens WHERE token_hash = ?)",
                (_hash(token.token),),
            )

        await self._db(revoke)


class _GitHubError(Exception):
    pass


def _issue(
    conn: _Conn,
    *,
    grant_id: str,
    client_id: str,
    subject: str | None,
    scopes: list[str],
    resource: str | None,
    now: int,
) -> OAuthToken:
    access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    for kind, token, ttl in (("access", access, ACCESS_TTL), ("refresh", refresh, REFRESH_TTL)):
        conn.execute(
            "INSERT INTO oauth_tokens (token_hash, kind, grant_id, client_id, subject, scopes, "
            "resource, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _hash(token),
                kind,
                grant_id,
                client_id,
                subject,
                json.dumps(scopes),
                resource,
                now + ttl,
            ),
        )
    return OAuthToken(
        access_token=access,
        expires_in=ACCESS_TTL,
        refresh_token=refresh,
        scope=" ".join(scopes) or None,
    )


def _sweep(conn: _Conn, now: int) -> None:
    """Drop expired pending requests, codes and tokens (called on each new authorization)."""
    conn.execute("DELETE FROM oauth_pending WHERE expires_at <= ?", (now,))
    conn.execute("DELETE FROM oauth_codes WHERE expires_at <= ?", (now,))
    conn.execute("DELETE FROM oauth_tokens WHERE expires_at <= ?", (now,))


def _same_url(a: str, b: str) -> bool:
    """Equal as URLs: scheme and host case-insensitive, a trailing slash aside."""

    def norm(url: str) -> tuple[str, str, str]:
        parts = urlsplit(url.strip())
        return parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/")

    return norm(a) == norm(b)


_EXPIRED = "<p>This sign-in link has expired or was already used. Start again from the app.</p>"

_PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    # form-action must allow GitHub too: browsers apply it to the POST's redirect.
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; "
        "form-action 'self' https://github.com; frame-ancestors 'none'"
    ),
}


def _page(title: str, body: str, status: int) -> HTMLResponse:
    content = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:32rem;margin:3rem auto;padding:0 1rem}}
button{{font:inherit;padding:.5rem 1rem}}</style></head>
<body><h1>{html.escape(title)}</h1>{body}</body></html>
"""
    return HTMLResponse(content, status_code=status, headers=_PAGE_HEADERS)
