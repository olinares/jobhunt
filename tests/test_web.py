import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from jobhunt.links import sign
from jobhunt.models import BoardRef, Job, Score
from jobhunt.store import open_store
from jobhunt.web import link_routes

SECRET = "s" * 40
JOB = Job(
    board=BoardRef("greenhouse", "acme"),
    external_id="1",
    title="Solutions <Engineer>",
    company="Acme & Co",
    url="https://boards.greenhouse.io/acme/jobs/1",
)
REASON = "Private reasoning about the resume"


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "jobs.db"
    with open_store(path) as store:
        store.upsert_jobs([JOB])
        store.save_score(JOB.uid, Score(80, "se", REASON))
    return path


@pytest.fixture
def client(db):
    routes = [
        Route(r.path, r.endpoint, methods=r.methods)
        for r in link_routes(lambda: open_store(db), secret=SECRET)
    ]
    return TestClient(Starlette(routes=routes))


def status(db) -> str:
    with open_store(db) as store:
        return store.get_job(JOB.uid).status


def set_status(db, value: str) -> None:
    with open_store(db) as store:
        store.set_status(JOB.uid, value)


def url(action: str, uid: str = JOB.uid, **kwargs) -> str:
    return f"/a/{sign(uid, action, secret=SECRET, **kwargs)}"


def assert_private_headers(resp):
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["x-robots-tag"] == "noindex"
    assert resp.headers["referrer-policy"] == "no-referrer"


def test_get_shows_confirmation_and_never_writes(client, db):
    path = url("approve")
    for _ in range(3):  # scanners may prefetch more than once
        resp = client.get(path)
        assert resp.status_code == 200
    assert status(db) == "new"
    assert_private_headers(resp)
    body = resp.text
    assert "Solutions &lt;Engineer&gt;" in body and "Acme &amp; Co" in body
    assert "Current status: new" in body
    assert f'<form method="post" action="{path}">' in body
    assert "Approve" in body
    assert REASON not in body


def test_head_does_not_write(client, db):
    assert client.head(url("approve")).status_code == 200
    assert status(db) == "new"


def test_post_approves_then_repeat_says_already(client, db):
    path = url("approve")
    resp = client.post(path)
    assert resp.status_code == 200
    assert "Approved ✓" in resp.text
    assert_private_headers(resp)
    assert status(db) == "approved"

    again = client.post(path)
    assert again.status_code == 200
    assert "Already approved" in again.text
    assert status(db) == "approved"


def test_post_skip(client, db):
    resp = client.post(url("skip"))
    assert "Skipped ✓" in resp.text
    assert status(db) == "skipped"


def test_post_on_applied_job_does_not_change_it(client, db):
    set_status(db, "applied")
    resp = client.post(url("skip"))
    assert resp.status_code == 200
    assert "Status is applied; not changed" in resp.text
    assert status(db) == "applied"


def test_skip_link_cannot_undo_an_approve(client, db):
    client.post(url("approve"))
    resp = client.post(url("skip"))
    assert "Status is approved; not changed" in resp.text
    assert status(db) == "approved"


def test_get_on_non_new_job_shows_status_without_form(client, db):
    set_status(db, "applied")
    resp = client.get(url("approve"))
    assert resp.status_code == 200
    assert "Current status: applied" in resp.text
    assert "<form" not in resp.text

    set_status(db, "approved")
    assert "Already approved" in client.get(url("approve")).text


@pytest.mark.parametrize("method", ["get", "post"])
def test_bad_token_is_400_and_touches_nothing(client, db, method):
    good = sign(JOB.uid, "approve", secret=SECRET)
    for token in ["garbage", good[:-2] + "AA", sign(JOB.uid, "approve", secret="t" * 40)]:
        resp = getattr(client, method)(f"/a/{token}")
        assert resp.status_code == 400
        assert "Link not valid" in resp.text
        assert_private_headers(resp)
    assert status(db) == "new"


@pytest.mark.parametrize("method", ["get", "post"])
def test_expired_token_is_400(client, db, method):
    old = datetime.now(UTC) - timedelta(days=15)
    resp = getattr(client, method)(url("approve", now=old))
    assert resp.status_code == 400
    assert "expired" in resp.text
    assert status(db) == "new"


@pytest.mark.parametrize("method", ["get", "post"])
def test_unknown_uid_is_404(client, method):
    resp = getattr(client, method)(url("approve", uid="greenhouse:acme:missing"))
    assert resp.status_code == 404
    assert "Job not found" in resp.text


def test_store_opened_and_closed_per_request(db):
    opened = []

    def factory():
        store = open_store(db)
        opened.append(store)
        return store

    routes = [
        Route(r.path, r.endpoint, methods=r.methods) for r in link_routes(factory, secret=SECRET)
    ]
    client = TestClient(Starlette(routes=routes))
    client.get(url("approve"))
    client.post(url("approve"))
    assert len(opened) == 2
    for store in opened:
        with pytest.raises(
            sqlite3.ProgrammingError
        ):  # closed: "Cannot operate on a closed database"
            store.get_job(JOB.uid)


def test_short_secret_refused_at_build_time(db):
    with pytest.raises(ValueError, match="at least 32 bytes"):
        link_routes(lambda: open_store(db), secret="short")


def test_routes_shape_for_custom_route():
    routes = link_routes(lambda: None, secret=SECRET)  # type: ignore[arg-type,return-value]
    assert [(r.path, r.methods) for r in routes] == [
        ("/a/{token}", ["GET"]),
        ("/a/{token}", ["POST"]),
    ]
