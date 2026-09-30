"""OAuth for the remote server, driven through the real Streamable HTTP app.

GitHub is faked with `httpx.MockTransport`; the jobs store and the OAuth tables share a SQLite
file in tmp_path (and the OAuth tables also run on Postgres when TEST_DATABASE_URL is set).
Nothing touches the network.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import anyio
import httpx
import psycopg
import pytest
from starlette.testclient import TestClient

from jobhunt import auth
from jobhunt.auth import (
    AuthConfigError,
    GitHubAuthConfig,
    GitHubOAuthProvider,
    auth_settings,
    is_allowed_redirect_uri,
    transport_security,
)
from jobhunt.mcp_server import create_server
from jobhunt.store import SqliteStore

PUBLIC = "https://jobhunt.example.test"
RESOURCE = PUBLIC + "/mcp"
CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
OZ_ID = 4242
ENV = {
    "GITHUB_CLIENT_ID": "gh-client",
    "GITHUB_CLIENT_SECRET": "gh-secret",
    "JOBHUNT_ALLOWED_GITHUB_ID": str(OZ_ID),
    "JOBHUNT_PUBLIC_URL": PUBLIC,
}
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "MCP-Protocol-Version": "2025-06-18",
}
TOOLS_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}

PG_URL = os.environ.get("TEST_DATABASE_URL")
OAUTH_TABLES = "oauth_clients, oauth_pending, oauth_codes, oauth_tokens"


# --------------------------------------------------------------------------- fakes


class Clock:
    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeGitHub:
    """GitHub's token and user endpoints. Any code except "bad" is accepted."""

    def __init__(self, user_id: int = OZ_ID) -> None:
        self.user_id = user_id
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url == auth.GITHUB_TOKEN_URL:
            form = parse_qs(request.content.decode())
            if form["code"] == ["bad"] or form["client_secret"] != ["gh-secret"]:
                return httpx.Response(200, json={"error": "bad_verification_code"})
            return httpx.Response(200, json={"access_token": "gho_fake", "scope": ""})
        if request.url == auth.GITHUB_USER_URL:
            assert request.headers["Authorization"] == "Bearer gho_fake"
            return httpx.Response(200, json={"id": self.user_id, "login": "oz"})
        return httpx.Response(404)


# --------------------------------------------------------------------------- fixtures


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param(
            "postgres", marks=pytest.mark.skipif(not PG_URL, reason="TEST_DATABASE_URL not set")
        ),
    ]
)
def oauth_db(request, tmp_path: Path) -> str:
    if request.param == "sqlite":
        return str(tmp_path / "jobs.db")
    with psycopg.connect(PG_URL, autocommit=True) as conn:
        conn.execute(f"DROP TABLE IF EXISTS {OAUTH_TABLES}")
    return PG_URL


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def github() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture
def provider(oauth_db, clock, github) -> GitHubOAuthProvider:
    http = httpx.AsyncClient(transport=httpx.MockTransport(github))
    return GitHubOAuthProvider(
        GitHubAuthConfig.from_env(ENV), oauth_db, http_client=http, clock=clock
    )


def build_app(provider: GitHubOAuthProvider, tmp_path: Path):
    jobs_db = tmp_path / "jobs.db"
    SqliteStore(jobs_db).close()
    server = create_server(
        store_factory=lambda: SqliteStore(jobs_db),
        root=tmp_path,
        auth_provider=provider,
        auth_settings=auth_settings(provider.config),
        transport_security=transport_security(provider.config),
        host="0.0.0.0",
        stateless_http=True,
        json_response=True,
    )
    provider.register_routes(server)
    return server.streamable_http_app()


@pytest.fixture
def app(provider, tmp_path):
    return build_app(provider, tmp_path)


@pytest.fixture
def client(app):
    with TestClient(app, base_url=PUBLIC, follow_redirects=False) as client:
        yield client


# --------------------------------------------------------------------------- flow helpers


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode().rstrip("=")


def register(client: TestClient, redirect_uri: str = CLAUDE_CALLBACK) -> dict:
    reply = client.post(
        "/register",
        json={
            "redirect_uris": [redirect_uri],
            "client_name": "Claude",
            "token_endpoint_auth_method": "none",
        },
    )
    assert reply.status_code == 201, reply.text
    return reply.json()


def start(client: TestClient, reg: dict, challenge: str, **extra) -> httpx.Response:
    params = {
        "response_type": "code",
        "client_id": reg["client_id"],
        "redirect_uri": reg["redirect_uris"][0],
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "client-state",
        "resource": RESOURCE,
        **extra,
    }
    return client.get("/authorize", params=params)


def consent_url(client: TestClient, reg: dict, challenge: str) -> str:
    reply = start(client, reg, challenge)
    assert reply.status_code == 302, reply.text
    location = reply.headers["location"]
    assert location.startswith(PUBLIC + auth.CONSENT_PATH + "?state=")
    return location


def state_of(url: str) -> str:
    return parse_qs(urlsplit(url).query)["state"][0]


def csrf_of(page: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', page).group(1)


def to_github(client: TestClient, reg: dict, challenge: str) -> str:
    """Register → authorize → consent GET → consent POST; returns GitHub's authorize URL."""
    url = consent_url(client, reg, challenge)
    page = client.get(url)
    assert page.status_code == 200
    reply = client.post(
        auth.CONSENT_PATH, data={"state": state_of(url), "csrf": csrf_of(page.text)}
    )
    assert reply.status_code == 303, reply.text
    return reply.headers["location"]


def callback(client: TestClient, github_url: str, code: str = "gh-code") -> httpx.Response:
    return client.get(
        auth.GITHUB_CALLBACK_PATH, params={"code": code, "state": state_of(github_url)}
    )


def our_code(reply: httpx.Response, redirect_uri: str = CLAUDE_CALLBACK) -> str:
    assert reply.status_code == 302, reply.text
    location = reply.headers["location"]
    assert location.startswith(redirect_uri + "?")
    query = parse_qs(urlsplit(location).query)
    assert query["state"] == ["client-state"]
    return query["code"][0]


def exchange(client: TestClient, reg: dict, code: str, verifier: str) -> httpx.Response:
    return client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": reg["redirect_uris"][0],
            "client_id": reg["client_id"],
            "code_verifier": verifier,
        },
    )


def refresh(client: TestClient, reg: dict, refresh_token: str) -> httpx.Response:
    return client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": reg["client_id"],
        },
    )


def login(client: TestClient) -> tuple[dict, dict]:
    """The whole flow; returns (registration, token response)."""
    reg = register(client)
    verifier, challenge = pkce()
    code = our_code(callback(client, to_github(client, reg, challenge)))
    reply = exchange(client, reg, code, verifier)
    assert reply.status_code == 200, reply.text
    return reg, reply.json()


def tools_list(client: TestClient, access_token: str | None) -> httpx.Response:
    headers = dict(MCP_HEADERS)
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    return client.post("/mcp", json=TOOLS_LIST, headers=headers)


def another_browser(app) -> TestClient:
    """A second cookie jar on the same app. Without `with`, so the app's lifespan (the MCP
    session manager, which runs once per app) isn't started again; the OAuth routes don't
    need it."""
    return TestClient(app, base_url=PUBLIC, follow_redirects=False)


def rows(provider: GitHubOAuthProvider, sql: str) -> list:
    with provider.db.connect() as conn:
        return conn.execute(sql).fetchall()


# --------------------------------------------------------------------------- the happy path


def test_full_pkce_flow_ends_in_an_authenticated_tools_list(client, provider, github):
    reg = register(client)
    verifier, challenge = pkce()
    url = consent_url(client, reg, challenge)

    # The consent GET only shows the page: nothing bound, nothing sent to GitHub.
    page = client.get(url)
    assert page.status_code == 200
    assert "Claude" in page.text
    assert "claude.ai" in page.text
    assert "Continue with GitHub" in page.text
    assert page.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert rows(provider, "SELECT browser_hash FROM oauth_pending") == [(None,)]
    assert github.requests == []

    reply = client.post(
        auth.CONSENT_PATH, data={"state": state_of(url), "csrf": csrf_of(page.text)}
    )
    assert reply.status_code == 303
    github_url = reply.headers["location"]
    assert github_url.startswith(auth.GITHUB_AUTHORIZE_URL + "?")
    query = parse_qs(urlsplit(github_url).query)
    assert query["client_id"] == ["gh-client"]
    assert query["redirect_uri"] == [PUBLIC + auth.GITHUB_CALLBACK_PATH]
    assert "scope" not in query  # no GitHub scopes: only the public profile

    code = our_code(callback(client, github_url))
    tokens = exchange(client, reg, code, verifier).json()
    assert tokens["token_type"] == "Bearer"
    assert tokens["expires_in"] == 3600
    assert tokens["refresh_token"]

    listed = tools_list(client, tokens["access_token"])
    assert listed.status_code == 200, listed.text
    names = {tool["name"] for tool in listed.json()["result"]["tools"]}
    assert {"search_jobs", "update_status", "build_packet"} <= names


def test_secrets_are_stored_hashed(client, provider):
    _, tokens = login(client)
    stored = {row[0] for row in rows(provider, "SELECT token_hash FROM oauth_tokens")}
    assert tokens["access_token"] not in stored
    assert hashlib.sha256(tokens["access_token"].encode()).hexdigest() in stored


def test_access_token_is_bound_to_this_resource(client, provider):
    _, tokens = login(client)
    assert rows(provider, "SELECT DISTINCT resource FROM oauth_tokens") == [(RESOURCE,)]
    loaded = anyio.run(provider.load_access_token, tokens["access_token"])
    assert loaded.resource == RESOURCE
    assert loaded.subject == str(OZ_ID)


def test_metadata_is_served(client):
    server = client.get("/.well-known/oauth-authorization-server").json()
    assert server["issuer"].rstrip("/") == PUBLIC
    assert server["registration_endpoint"] == PUBLIC + "/register"
    assert server["code_challenge_methods_supported"] == ["S256"]
    resource = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert resource["resource"] == RESOURCE


def test_mcp_without_a_token_is_401_with_the_resource_metadata_hint(client):
    reply = tools_list(client, None)
    assert reply.status_code == 401
    challenge = reply.headers["www-authenticate"]
    assert challenge.startswith("Bearer ")
    assert f'resource_metadata="{PUBLIC}/.well-known/oauth-protected-resource/mcp"' in challenge
    assert tools_list(client, "not-a-token").status_code == 401


# --------------------------------------------------------------------------- registration


@pytest.mark.parametrize(
    "uri",
    [
        "https://claude.ai/api/mcp/auth_callback",
        "https://claude.com/api/mcp/auth_callback",
        "http://localhost:6274/oauth/callback",
        "http://localhost/callback",
        "http://127.0.0.1:33418/",
    ],
)
def test_allowed_redirect_uris(uri):
    assert is_allowed_redirect_uri(uri)


@pytest.mark.parametrize(
    "uri",
    [
        "https://evil.example/api/mcp/auth_callback",
        "https://claude.ai/api/mcp/other",
        "https://claude.ai/api/mcp/auth_callback?next=evil",
        "https://claude.ai.evil.example/api/mcp/auth_callback",
        "http://claude.ai/api/mcp/auth_callback",
        "https://localhost:3000/callback",
        "http://localhost.evil.example/callback",
        "http://evil.example@localhost/callback",
        "http://10.0.0.5:3000/callback",
        "http://localhost:notaport/callback",
        "javascript:alert(1)",
    ],
)
def test_refused_redirect_uris(uri):
    assert not is_allowed_redirect_uri(uri)


def test_registration_outside_the_allowlist_is_refused(client, provider):
    reply = client.post(
        "/register",
        json={"redirect_uris": [CLAUDE_CALLBACK, "https://evil.example/cb"], "client_name": "x"},
    )
    assert reply.status_code == 400
    assert reply.json()["error"] == "invalid_redirect_uri"
    assert "evil.example" in reply.json()["error_description"]
    assert rows(provider, "SELECT client_id FROM oauth_clients") == []


def test_loopback_client_gets_its_code(client):
    reg = register(client, "http://127.0.0.1:33418/callback")
    verifier, challenge = pkce()
    code = our_code(
        callback(client, to_github(client, reg, challenge)), "http://127.0.0.1:33418/callback"
    )
    assert exchange(client, reg, code, verifier).status_code == 200


def test_authorize_for_another_resource_is_refused(client, provider):
    reg = register(client)
    _, challenge = pkce()
    reply = start(client, reg, challenge, resource="https://other.example/mcp")
    assert reply.status_code == 302
    query = parse_qs(urlsplit(reply.headers["location"]).query)
    assert query["error"] == ["invalid_request"]
    assert rows(provider, "SELECT state_hash FROM oauth_pending") == []


# --------------------------------------------------------------------------- GitHub login


def test_another_github_account_gets_403_and_no_code(client, provider, github):
    github.user_id = 999
    reg = register(client)
    _, challenge = pkce()
    reply = callback(client, to_github(client, reg, challenge))
    assert reply.status_code == 403
    assert "location" not in reply.headers
    assert rows(provider, "SELECT code_hash FROM oauth_codes") == []


def test_github_refusing_the_code_is_502_and_no_code(client, provider):
    reg = register(client)
    _, challenge = pkce()
    reply = callback(client, to_github(client, reg, challenge), code="bad")
    assert reply.status_code == 502
    assert rows(provider, "SELECT code_hash FROM oauth_codes") == []


def test_replayed_state_is_refused(client):
    reg = register(client)
    _, challenge = pkce()
    github_url = to_github(client, reg, challenge)
    our_code(callback(client, github_url))
    replay = callback(client, github_url)
    assert replay.status_code == 400
    assert "expired or was already used" in replay.text


def test_expired_state_is_refused(client, clock):
    reg = register(client)
    _, challenge = pkce()
    github_url = to_github(client, reg, challenge)
    clock.advance(auth.PENDING_TTL + 1)
    assert callback(client, github_url).status_code == 400


def test_expired_consent_page(client, clock):
    reg = register(client)
    _, challenge = pkce()
    url = consent_url(client, reg, challenge)
    clock.advance(auth.PENDING_TTL + 1)
    assert client.get(url).status_code == 400
    assert client.get(auth.CONSENT_PATH).status_code == 400  # no state at all


def test_consent_post_needs_the_pages_csrf_cookie(client, provider, app):
    reg = register(client)
    _, challenge = pkce()
    url = consent_url(client, reg, challenge)
    csrf = csrf_of(client.get(url).text)
    state = state_of(url)
    # A cross-site form post: the SameSite=Strict cookie isn't sent, or the origin is wrong.
    other = another_browser(app)
    assert other.post(auth.CONSENT_PATH, data={"state": state, "csrf": csrf}).status_code == 403
    evil = {"Origin": "https://evil.example"}
    reply = client.post(auth.CONSENT_PATH, data={"state": state, "csrf": csrf}, headers=evil)
    assert reply.status_code == 403
    wrong = client.post(auth.CONSENT_PATH, data={"state": state, "csrf": "guess"})
    assert wrong.status_code == 403
    assert rows(provider, "SELECT browser_hash FROM oauth_pending") == [(None,)]


def test_callback_in_a_browser_that_did_not_consent_is_refused(client, app, provider):
    """Someone who consents in their own browser can't send Oz straight to GitHub."""
    reg = register(client)
    _, challenge = pkce()
    github_url = to_github(client, reg, challenge)
    reply = callback(another_browser(app), github_url)
    assert reply.status_code == 403
    assert rows(provider, "SELECT code_hash FROM oauth_codes") == []


def test_callback_without_consent_is_refused(client, provider):
    reg = register(client)
    _, challenge = pkce()
    url = consent_url(client, reg, challenge)  # never POSTed
    github_url = f"{auth.GITHUB_AUTHORIZE_URL}?state={state_of(url)}"
    assert callback(client, github_url).status_code == 403
    assert rows(provider, "SELECT code_hash FROM oauth_codes") == []


# --------------------------------------------------------------------------- tokens


def test_wrong_pkce_verifier_is_refused(client):
    reg = register(client)
    _, challenge = pkce()
    code = our_code(callback(client, to_github(client, reg, challenge)))
    wrong, _ = pkce()
    reply = exchange(client, reg, code, wrong)
    assert reply.status_code == 400
    assert reply.json()["error"] == "invalid_grant"


def test_code_is_single_use(client):
    reg = register(client)
    verifier, challenge = pkce()
    code = our_code(callback(client, to_github(client, reg, challenge)))
    assert exchange(client, reg, code, verifier).status_code == 200
    again = exchange(client, reg, code, verifier)
    assert again.status_code == 400
    assert again.json()["error"] == "invalid_grant"


def test_code_for_another_client_is_refused(client):
    reg = register(client)
    verifier, challenge = pkce()
    code = our_code(callback(client, to_github(client, reg, challenge)))
    other = register(client)
    assert exchange(client, other, code, verifier).json()["error"] == "invalid_grant"


def test_expired_access_token_is_401(client, clock):
    _, tokens = login(client)
    assert tools_list(client, tokens["access_token"]).status_code == 200
    clock.advance(auth.ACCESS_TTL + 1)
    assert tools_list(client, tokens["access_token"]).status_code == 401


def test_revoked_access_token_is_401(client):
    reg, tokens = login(client)
    # The SDK's revocation form requires client_secret to be present, even for public clients.
    form = {"token": tokens["access_token"], "client_id": reg["client_id"], "client_secret": ""}
    reply = client.post("/revoke", data=form)
    assert reply.status_code == 200
    assert tools_list(client, tokens["access_token"]).status_code == 401
    # Revoking one token revokes its grant: the refresh token is gone too.
    assert refresh(client, reg, tokens["refresh_token"]).status_code == 400


def test_refresh_rotates_and_a_reused_refresh_token_revokes_the_grant(client):
    reg, first = login(client)
    reply = refresh(client, reg, first["refresh_token"])
    assert reply.status_code == 200, reply.text
    second = reply.json()
    assert second["refresh_token"] != first["refresh_token"]
    assert tools_list(client, second["access_token"]).status_code == 200
    assert tools_list(client, first["access_token"]).status_code == 401

    reused = refresh(client, reg, first["refresh_token"])
    assert reused.status_code == 400
    assert reused.json()["error"] == "invalid_grant"
    # The copy came back, so everything issued from it is revoked as well.
    assert tools_list(client, second["access_token"]).status_code == 401
    assert refresh(client, reg, second["refresh_token"]).status_code == 400


def test_expired_refresh_token_is_refused(client, clock):
    reg, tokens = login(client)
    clock.advance(auth.REFRESH_TTL + 1)
    assert refresh(client, reg, tokens["refresh_token"]).status_code == 400


# --------------------------------------------------------------------------- config


def test_config_refuses_missing_variables():
    with pytest.raises(AuthConfigError) as err:
        GitHubAuthConfig.from_env({"GITHUB_CLIENT_ID": "x", "JOBHUNT_PUBLIC_URL": " "})
    message = str(err.value)
    for name in ("GITHUB_CLIENT_SECRET", "JOBHUNT_ALLOWED_GITHUB_ID", "JOBHUNT_PUBLIC_URL"):
        assert name in message
    assert "GITHUB_CLIENT_ID" not in message


def test_provider_from_env_refuses_missing_variables(tmp_path):
    with pytest.raises(AuthConfigError):
        GitHubOAuthProvider.from_env({}, tmp_path / "jobs.db")
    assert not (tmp_path / "jobs.db").exists()


def test_config_needs_a_numeric_github_id():
    with pytest.raises(AuthConfigError, match="numeric"):
        GitHubAuthConfig.from_env({**ENV, "JOBHUNT_ALLOWED_GITHUB_ID": "olinares"})


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://Jobhunt.example.test/", "https://jobhunt.example.test"),
        ("http://localhost:8000", "http://localhost:8000"),
    ],
)
def test_public_url_is_normalized(url, expected):
    assert GitHubAuthConfig.from_env({**ENV, "JOBHUNT_PUBLIC_URL": url}).public_url == expected


@pytest.mark.parametrize(
    "url", ["http://jobhunt.example.test", "https://jobhunt.example.test/mcp", "jobhunt.test"]
)
def test_bad_public_urls_are_refused(url):
    with pytest.raises(AuthConfigError, match="JOBHUNT_PUBLIC_URL"):
        GitHubAuthConfig.from_env({**ENV, "JOBHUNT_PUBLIC_URL": url})


def test_tables_are_created_idempotently(tmp_path):
    config = GitHubAuthConfig.from_env(ENV)
    GitHubOAuthProvider(config, tmp_path / "a.db")
    GitHubOAuthProvider(config, tmp_path / "a.db")
    with sqlite3.connect(tmp_path / "a.db") as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"oauth_clients", "oauth_pending", "oauth_codes", "oauth_tokens"} <= names
