"""The hosted app as Cloud Run serves it, built from env against a SQLite file. No network:
GitHub is never called (a mock transport that fails the test if it is), uvicorn never runs."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from jobhunt import remote
from jobhunt.links import sign
from jobhunt.models import BoardRef, Job
from jobhunt.remote import REQUIRED_ENV, RemoteConfigError, build_app, main
from jobhunt.store import open_store

PUBLIC = "https://jobhunt.example.test"
SECRET = "k" * 48
JOB = Job(
    board=BoardRef("greenhouse", "acme"),
    external_id="1",
    title="Solutions Engineer",
    company="Acme",
    url="https://boards.greenhouse.io/acme/jobs/1",
)
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "MCP-Protocol-Version": "2025-06-18",
}
TOOLS_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


def no_github(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected request to {request.url}")


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "jobs.db"
    with open_store(path) as store:
        store.upsert_jobs([JOB])
    return path


@pytest.fixture
def env(db: Path) -> dict[str, str]:
    return {
        "DATABASE_URL": str(db),
        "JOBHUNT_PUBLIC_URL": PUBLIC,
        "JOBHUNT_LINK_SECRET": SECRET,
        "GITHUB_CLIENT_ID": "gh-client",
        "GITHUB_CLIENT_SECRET": "gh-secret",
        "JOBHUNT_ALLOWED_GITHUB_ID": "4242",
    }


@pytest.fixture
def client(env, tmp_path):
    http = httpx.AsyncClient(transport=httpx.MockTransport(no_github))
    app = build_app(env, root=tmp_path, http_client=http)
    with TestClient(app, base_url=PUBLIC, follow_redirects=False) as client:
        yield client


def status(db: Path) -> str:
    with open_store(db) as store:
        return store.get_job(JOB.uid).status


# --------------------------------------------------------------------------- the app


def test_healthz_is_200_without_touching_the_database(client, monkeypatch):
    def boom(target):
        raise AssertionError("healthz opened the database")

    monkeypatch.setattr(remote, "open_store", boom)
    reply = client.get("/healthz")
    assert reply.status_code == 200
    assert reply.text == "ok"


def test_mcp_without_a_token_is_401(client):
    reply = client.post("/mcp", json=TOOLS_LIST, headers=MCP_HEADERS)
    assert reply.status_code == 401
    assert "resource_metadata=" in reply.headers["www-authenticate"]


def test_oauth_metadata_is_served_at_the_root(client):
    server = client.get("/.well-known/oauth-authorization-server")
    assert server.status_code == 200
    assert server.json()["issuer"].rstrip("/") == PUBLIC
    assert server.json()["registration_endpoint"] == PUBLIC + "/register"
    resource = client.get("/.well-known/oauth-protected-resource/mcp")
    assert resource.json()["resource"] == PUBLIC + "/mcp"


def test_consent_and_github_callback_routes_are_registered(client):
    # Without a state both refuse the request, but they exist (not 404).
    assert client.get("/oauth/consent").status_code == 400
    assert client.get("/oauth/github/callback").status_code != 404


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_bad_link_is_400(client, db, method):
    reply = client.request(method, "/a/not-a-token")
    assert reply.status_code == 400
    assert status(db) == "new"


def test_link_signed_with_the_secret_approves_through_the_hosted_app(client, db):
    path = f"/a/{sign(JOB.uid, 'approve', secret=SECRET)}"
    page = client.get(path)
    assert page.status_code == 200
    assert "Approve this job?" in page.text
    assert status(db) == "new"  # GET never writes
    assert client.post(path).status_code == 200
    assert status(db) == "approved"


def test_link_signed_with_another_secret_is_400(client, db):
    reply = client.post(f"/a/{sign(JOB.uid, 'approve', secret='x' * 48)}")
    assert reply.status_code == 400
    assert status(db) == "new"


# --------------------------------------------------------------------------- refusing to start


@pytest.mark.parametrize("name", REQUIRED_ENV)
def test_each_missing_variable_refuses_to_start(env, tmp_path, name):
    env[name] = "  "
    with pytest.raises(RemoteConfigError, match=name):
        build_app(env, root=tmp_path)


def test_required_variables_cover_database_links_and_github():
    assert set(REQUIRED_ENV) == {
        "DATABASE_URL",
        "JOBHUNT_PUBLIC_URL",
        "JOBHUNT_LINK_SECRET",
        "GITHUB_CLIENT_ID",
        "GITHUB_CLIENT_SECRET",
        "JOBHUNT_ALLOWED_GITHUB_ID",
    }


@pytest.mark.parametrize(
    "url",
    [
        "http://jobhunt.example.test",  # plain http off localhost
        "https://jobhunt.example.test/app",  # a path: the digest's links would accept it
        "https://jobhunt.example.test/?x=1",
        "jobhunt.example.test",
    ],
)
def test_public_url_must_be_https_scheme_and_host(env, tmp_path, db, url):
    env["JOBHUNT_PUBLIC_URL"] = url
    db.unlink()
    with pytest.raises(RemoteConfigError, match="JOBHUNT_PUBLIC_URL"):
        build_app(env, root=tmp_path)
    assert not db.exists()  # refused before connecting to anything


def test_trailing_slash_is_normalized(env, tmp_path):
    env["JOBHUNT_PUBLIC_URL"] = PUBLIC + "/"
    with TestClient(build_app(env, root=tmp_path), base_url=PUBLIC) as client:
        issuer = client.get("/.well-known/oauth-authorization-server").json()["issuer"]
    assert issuer.rstrip("/") == PUBLIC


def test_short_link_secret_refuses_to_start(env, tmp_path):
    env["JOBHUNT_LINK_SECRET"] = "short"
    with pytest.raises(RemoteConfigError, match="JOBHUNT_LINK_SECRET"):
        build_app(env, root=tmp_path)


def test_non_numeric_github_id_refuses_to_start(env, tmp_path):
    env["JOBHUNT_ALLOWED_GITHUB_ID"] = "oz"
    with pytest.raises(RemoteConfigError, match="numeric"):
        build_app(env, root=tmp_path)


def test_memory_database_refuses_to_start(env, tmp_path):
    env["DATABASE_URL"] = ":memory:"
    with pytest.raises(RemoteConfigError, match="DATABASE_URL"):
        build_app(env, root=tmp_path)


# --------------------------------------------------------------------------- main


@pytest.fixture
def served(monkeypatch) -> list[dict]:
    calls: list[dict] = []
    monkeypatch.setattr(remote.uvicorn, "run", lambda app, **kw: calls.append(kw))
    return calls


def test_main_refuses_with_a_message_and_never_serves(monkeypatch, tmp_path, capsys, served):
    for name in REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    assert main(["--root", str(tmp_path)]) == 2
    err = capsys.readouterr().err
    assert "DATABASE_URL" in err and "Refusing to start" in err
    assert served == []


def test_main_refuses_an_http_public_url(monkeypatch, env, tmp_path, capsys, served):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("JOBHUNT_PUBLIC_URL", "http://jobhunt.example.test")
    assert main(["--root", str(tmp_path)]) == 2
    assert "https" in capsys.readouterr().err
    assert served == []


def test_main_serves_on_port(monkeypatch, env, tmp_path, served):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PORT", "9123")
    assert main(["--root", str(tmp_path)]) == 0
    assert served == [
        {
            "host": "0.0.0.0",
            "port": 9123,
            "log_config": None,
            "proxy_headers": True,
            "forwarded_allow_ips": "*",
        }
    ]


def test_main_reports_an_unreachable_database(monkeypatch, env, tmp_path, capsys, served):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("DATABASE_URL", str(tmp_path / "missing-dir" / "jobs.db"))
    assert main(["--root", str(tmp_path)]) == 1
    assert "can't start" in capsys.readouterr().err
    assert served == []
