"""Store tests. Every test runs against SQLite, and against Postgres too when
TEST_DATABASE_URL is set (CI provides a postgres:16 service). Each test gets a clean schema."""

import os
import sqlite3
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from jobhunt.models import BoardRef, Job, Score
from jobhunt.store import SCHEMA_VERSION, PostgresStore, SqliteStore, open_store

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)

ACME = BoardRef("greenhouse", "acme", company_name="Acme")

PG_URL = os.environ.get("TEST_DATABASE_URL")

BACKENDS = [
    "sqlite",
    pytest.param(
        "postgres", marks=pytest.mark.skipif(not PG_URL, reason="TEST_DATABASE_URL not set")
    ),
]

ALL_TABLES = "digest_items, digests, jobs, companies, schema_version"


def reset_postgres() -> None:
    with psycopg.connect(PG_URL, autocommit=True) as conn:
        conn.execute(f"DROP TABLE IF EXISTS {ALL_TABLES} CASCADE")


@pytest.fixture(params=BACKENDS)
def backend(request):
    return request.param


@pytest.fixture
def target(backend, tmp_path):
    """A clean database location for the backend: a SQLite file path or the Postgres URL."""
    if backend == "sqlite":
        return tmp_path / "jobs.db"
    reset_postgres()
    return PG_URL


@pytest.fixture
def store(target):
    with open_store(target) as s:
        yield s


def make_job(board=ACME, external_id="1", **kwargs) -> Job:
    defaults = {
        "title": "Solutions Engineer",
        "company": "Acme",
        "url": f"https://boards.greenhouse.io/acme/jobs/{external_id}",
    }
    defaults.update(kwargs)
    return Job(board=board, external_id=external_id, **defaults)


# -- boards -------------------------------------------------------------


def test_upsert_board_then_boards_to_poll_returns_it(store):
    store.upsert_board(ACME, now=NOW)
    boards = store.boards_to_poll(now=NOW)
    assert boards == [ACME]


def test_boards_to_poll_older_than_filters_recently_checked(store):
    stale = BoardRef("lever", "stale", company_name="Stale")
    fresh = BoardRef("lever", "fresh", company_name="Fresh")
    store.upsert_board(stale, now=NOW - timedelta(days=10))
    store.upsert_board(fresh, now=NOW - timedelta(hours=1))

    due = store.boards_to_poll(older_than=timedelta(days=1), now=NOW)

    assert due == [stale]


def test_boards_to_poll_older_than_includes_never_checked(store):
    # upsert_jobs auto-registers a board without touching last_checked.
    store.upsert_jobs([make_job()], now=NOW)

    due = store.boards_to_poll(older_than=timedelta(days=1), now=NOW)

    assert due == [ACME]


def test_upsert_board_is_idempotent_and_updates_metadata(store):
    store.upsert_board(ACME, now=NOW)
    renamed = BoardRef("greenhouse", "acme", company_name="Acme Inc")
    store.upsert_board(renamed, now=NOW + timedelta(days=1))

    boards = store.boards_to_poll(now=NOW)
    assert len(boards) == 1
    assert boards[0].company_name == "Acme Inc"


# -- upsert_jobs / dedupe -------------------------------------------------


def test_upsert_jobs_same_job_twice_returns_one_new(store):
    job = make_job()

    first = store.upsert_jobs([job], now=NOW)
    second = store.upsert_jobs([job], now=NOW + timedelta(hours=1))

    assert first == [job.uid]
    assert second == []


def test_upsert_jobs_dedupes_within_a_single_batch(store):
    job = make_job()

    new_uids = store.upsert_jobs([job, job], now=NOW)

    assert new_uids == [job.uid]


def test_upsert_jobs_returns_new_uids_in_input_order(store):
    j1 = make_job(external_id="1")
    j2 = make_job(external_id="2")
    store.upsert_jobs([j1], now=NOW)

    new_uids = store.upsert_jobs([j2, j1], now=NOW)

    assert new_uids == [j2.uid]


def test_upsert_jobs_auto_registers_unknown_board(store):
    job = make_job()
    store.upsert_jobs([job], now=NOW)

    boards = store.boards_to_poll(now=NOW)
    assert [b.key() for b in boards] == [ACME.key()]


def test_upsert_jobs_updates_mutable_fields_on_second_pass(store):
    job = make_job(pay_min=100_000.0)
    store.upsert_jobs([job], now=NOW)

    updated = make_job(pay_min=120_000.0, title="Senior Solutions Engineer")
    store.upsert_jobs([updated], now=NOW + timedelta(days=1))

    row = store._fetchone("SELECT title, pay_min FROM jobs WHERE uid = ?", (job.uid,))
    assert row["title"] == "Senior Solutions Engineer"
    assert row["pay_min"] == 120_000.0


# -- mark_closed / reopening ----------------------------------------------


def test_mark_closed_closes_jobs_no_longer_seen(store):
    j1 = make_job(external_id="1")
    j2 = make_job(external_id="2")
    store.upsert_jobs([j1, j2], now=NOW)

    closed = store.mark_closed(ACME.key(), {j1.uid}, now=NOW + timedelta(days=1))

    assert closed == [j2.uid]
    row = store._fetchone("SELECT status, closed_at FROM jobs WHERE uid = ?", (j2.uid,))
    assert row["status"] == "closed"
    assert row["closed_at"] is not None


def test_mark_closed_preserves_status_past_new(store):
    job = make_job()
    store.upsert_jobs([job], now=NOW)
    store.set_status(job.uid, "applied")

    closed = store.mark_closed(ACME.key(), set(), now=NOW + timedelta(days=1))

    assert closed == [job.uid]
    row = store._fetchone("SELECT status FROM jobs WHERE uid = ?", (job.uid,))
    assert row["status"] == "applied"


def test_mark_closed_does_not_reclose_already_closed_jobs(store):
    job = make_job()
    store.upsert_jobs([job], now=NOW)
    store.mark_closed(ACME.key(), set(), now=NOW)

    closed_again = store.mark_closed(ACME.key(), set(), now=NOW + timedelta(days=1))

    assert closed_again == []


def test_reappearing_closed_job_is_reopened_not_reset_to_new(store):
    job = make_job()
    store.upsert_jobs([job], now=NOW)
    store.set_status(job.uid, "applied")
    store.mark_closed(ACME.key(), set(), now=NOW + timedelta(days=1))

    row = store._fetchone("SELECT status FROM jobs WHERE uid = ?", (job.uid,))
    assert row["status"] == "applied"  # mark_closed didn't touch it (past "new")

    # Reappears on the board.
    store.upsert_jobs([job], now=NOW + timedelta(days=2))

    row = store._fetchone("SELECT status, closed_at FROM jobs WHERE uid = ?", (job.uid,))
    assert row["closed_at"] is None
    assert row["status"] == "applied"  # not reset to "new"


def test_reappearing_closed_new_job_reopens_to_new(store):
    job = make_job()
    store.upsert_jobs([job], now=NOW)
    store.mark_closed(ACME.key(), set(), now=NOW + timedelta(days=1))

    row = store._fetchone("SELECT status FROM jobs WHERE uid = ?", (job.uid,))
    assert row["status"] == "closed"

    store.upsert_jobs([job], now=NOW + timedelta(days=2))

    row = store._fetchone("SELECT status, closed_at FROM jobs WHERE uid = ?", (job.uid,))
    assert row["status"] == "new"
    assert row["closed_at"] is None


# -- set_status -----------------------------------------------------------


def test_set_status_accepts_valid_values(store):
    job = make_job()
    store.upsert_jobs([job], now=NOW)

    store.set_status(job.uid, "approved")

    row = store._fetchone("SELECT status FROM jobs WHERE uid = ?", (job.uid,))
    assert row["status"] == "approved"


def test_set_status_rejects_invalid_value(store):
    job = make_job()
    store.upsert_jobs([job], now=NOW)

    with pytest.raises(ValueError):
        store.set_status(job.uid, "ghosted")


def test_set_status_unknown_uid_raises_keyerror(store):
    with pytest.raises(KeyError):
        store.set_status("greenhouse:acme:missing", "approved")


# -- record_relevant_hits / prune_boards -----------------------------------


def test_record_relevant_hits_sets_timestamp_and_increments(store):
    store.upsert_board(ACME, now=NOW)

    store.record_relevant_hits(ACME.key(), 2, when=NOW)
    store.record_relevant_hits(ACME.key(), 3, when=NOW + timedelta(days=1))

    row = store._fetchone(
        "SELECT relevant_hits, last_relevant_hit FROM companies WHERE board_key = ?",
        (ACME.key(),),
    )
    assert row["relevant_hits"] == 5
    assert row["last_relevant_hit"] is not None


def test_record_relevant_hits_unknown_board_raises_keyerror(store):
    with pytest.raises(KeyError):
        store.record_relevant_hits("greenhouse:nope", 1, when=NOW)


def test_prune_boards_removes_boards_with_no_relevant_hit_past_grace_period(store):
    old_board = BoardRef("lever", "old", company_name="Old Co")
    store.upsert_board(old_board, now=NOW - timedelta(weeks=10))

    pruned = store.prune_boards(weeks=4, now=NOW)

    assert pruned == [old_board.key()]
    assert store.boards_to_poll(now=NOW) == []


def test_prune_boards_keeps_boards_with_recent_relevant_hit(store):
    board = BoardRef("lever", "active", company_name="Active Co")
    store.upsert_board(board, now=NOW - timedelta(weeks=10))
    store.record_relevant_hits(board.key(), 1, when=NOW - timedelta(weeks=1))

    pruned = store.prune_boards(weeks=4, now=NOW)

    assert pruned == []
    assert store.boards_to_poll(now=NOW) == [board]


def test_prune_boards_keeps_new_boards_within_grace_period(store):
    board = BoardRef("lever", "brand-new", company_name="Brand New")
    store.upsert_board(board, now=NOW - timedelta(days=1))

    pruned = store.prune_boards(weeks=4, now=NOW)

    assert pruned == []


def test_prune_boards_also_deletes_the_boards_jobs(store):
    board = BoardRef("lever", "old", company_name="Old Co")
    job = make_job(board=board)
    store.upsert_jobs([job], now=NOW - timedelta(weeks=10))

    pruned = store.prune_boards(weeks=4, now=NOW)

    assert pruned == [board.key()]
    row = store._fetchone("SELECT 1 FROM jobs WHERE uid = ?", (job.uid,))
    assert row is None


# -- open_store / migrations -------------------------------------------------

# The schema exactly as Phase 1 shipped it, before schema_version existed.
LEGACY_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS companies (
        board_key TEXT PRIMARY KEY,
        ats TEXT NOT NULL,
        slug TEXT NOT NULL,
        host TEXT,
        site TEXT,
        company_name TEXT,
        first_seen TEXT NOT NULL,
        last_checked TEXT,
        last_relevant_hit TEXT,
        relevant_hits INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS jobs (
        uid TEXT PRIMARY KEY,
        board_key TEXT NOT NULL REFERENCES companies (board_key),
        title TEXT NOT NULL,
        company TEXT NOT NULL,
        url TEXT NOT NULL,
        locations_json TEXT NOT NULL DEFAULT '[]',
        remote INTEGER,
        pay_min REAL,
        pay_max REAL,
        pay_currency TEXT,
        pay_period TEXT,
        posted_at TEXT,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        closed_at TEXT,
        status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'approved', 'skipped',
            'applied', 'interviewing', 'offer', 'rejected', 'closed')),
        score REAL,
        notes TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_jobs_board_key ON jobs (board_key)",
    """
    INSERT INTO companies (board_key, ats, slug, company_name, first_seen, relevant_hits)
    VALUES ('greenhouse:acme', 'greenhouse', 'acme', 'Acme', '2026-01-01T00:00:00+00:00', 1)
    """,
    """
    INSERT INTO jobs (uid, board_key, title, company, url, locations_json, remote,
                      first_seen, last_seen, status)
    VALUES ('greenhouse:acme:1', 'greenhouse:acme', 'Solutions Engineer', 'Acme',
            'https://boards.greenhouse.io/acme/jobs/1', '["Remote - US"]', 1,
            '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', 'new')
    """,
]


def create_legacy_db(backend, target) -> None:
    if backend == "sqlite":
        conn = sqlite3.connect(target)
        with conn:
            for statement in LEGACY_SCHEMA:
                conn.execute(statement)
        conn.close()
    else:
        with psycopg.connect(target, autocommit=True) as conn:
            for statement in LEGACY_SCHEMA:
                conn.execute(statement)


def test_open_store_picks_backend_from_target(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr("jobhunt.store.PostgresStore", lambda url: opened.append(url) or url)

    assert open_store("postgres://u@h/db") == "postgres://u@h/db"
    assert open_store("postgresql://u@h/db") == "postgresql://u@h/db"
    assert opened == ["postgres://u@h/db", "postgresql://u@h/db"]

    with open_store(tmp_path / "a.db") as s:
        assert isinstance(s, SqliteStore)
    with open_store(str(tmp_path / "b.db")) as s:
        assert isinstance(s, SqliteStore)


def test_postgres_target_opens_postgres_store(backend, target):
    if backend != "postgres":
        pytest.skip("Postgres only")
    with open_store(target) as s:
        assert isinstance(s, PostgresStore)


def test_new_database_is_at_latest_schema_version(store):
    assert store.schema_version() == SCHEMA_VERSION == 2


def test_reopening_does_not_reapply_migrations(target, store):
    store.upsert_jobs([make_job(description_html="<p>hi</p>")], now=NOW)
    store.close()

    with open_store(target) as again:
        assert again.schema_version() == SCHEMA_VERSION
        rows = again._fetchall("SELECT version FROM schema_version ORDER BY version")
        assert [r["version"] for r in rows] == list(range(1, SCHEMA_VERSION + 1))
        assert again.jobs_to_score(10)[0].description_html == "<p>hi</p>"


def test_legacy_database_is_migrated_in_place(backend, target):
    create_legacy_db(backend, target)

    with open_store(target) as store:
        assert store.schema_version() == SCHEMA_VERSION
        row = store._fetchone(
            "SELECT title, description_html, score_variant, pay_suspect, scored_at "
            "FROM jobs WHERE uid = ?",
            ("greenhouse:acme:1",),
        )
        assert row["title"] == "Solutions Engineer"
        assert row["description_html"] is None
        assert row["score_variant"] is None
        assert row["pay_suspect"] == 0
        assert row["scored_at"] is None

        # The old row is usable through the new API.
        [job] = store.jobs_to_score(10)
        assert job.uid == "greenhouse:acme:1"
        assert job.locations == ["Remote - US"]
        assert job.remote is True
        store.save_score(job.uid, Score(70, "se", "Fits."), now=NOW)
        assert store.jobs_for_digest()[0].job.uid == job.uid
        assert store.record_digest([job.uid], now=NOW) == store.latest_digest_id()


def test_database_newer_than_code_is_refused(target, store):
    store._execute("INSERT INTO schema_version (version, applied_at) VALUES (?, ?)", (99, "x"))
    store.close()

    with pytest.raises(RuntimeError, match="newer than this code"):
        open_store(target)


# -- descriptions ----------------------------------------------------------


def test_upsert_keeps_description_when_incoming_is_none(store):
    store.upsert_jobs([make_job(description_html="<p>Full JD</p>")], now=NOW)

    store.upsert_jobs([make_job(description_html=None)], now=NOW + timedelta(days=1))

    row = store._fetchone("SELECT description_html FROM jobs WHERE uid = ?", (make_job().uid,))
    assert row["description_html"] == "<p>Full JD</p>"


def test_upsert_replaces_description_when_a_new_one_arrives(store):
    store.upsert_jobs([make_job(description_html="<p>old</p>")], now=NOW)

    store.upsert_jobs([make_job(description_html="<p>new</p>")], now=NOW + timedelta(days=1))

    row = store._fetchone("SELECT description_html FROM jobs WHERE uid = ?", (make_job().uid,))
    assert row["description_html"] == "<p>new</p>"


# -- scoring -----------------------------------------------------------------

WORKDAY = BoardRef(
    "workday", "nvidia", host="nvidia.wd5.myworkdayjobs.com", site="Careers", company_name="NV"
)


def test_jobs_to_score_rebuilds_full_jobs(store):
    job = Job(
        board=WORKDAY,
        external_id="JR123:with-colon",
        title="Forward Deployed Engineer",
        company="NV",
        url="https://nvidia.wd5.myworkdayjobs.com/Careers/job/JR123",
        locations=["US, CA, Santa Clara", "Remote"],
        remote=False,
        description_html="<p>Build things</p>",
        posted_at=datetime(2026, 1, 10, 8, 30, tzinfo=UTC),
        pay_min=150_000.5,
        pay_max=210_000.25,
        pay_currency="USD",
        pay_period="year",
    )
    store.upsert_jobs([job], now=NOW)

    assert store.jobs_to_score(10) == [job]


def test_jobs_to_score_is_oldest_first_limited_and_skips_ineligible(store):
    oldest = make_job(external_id="1")
    middle = make_job(external_id="2")
    newest = make_job(external_id="3")
    scored = make_job(external_id="4")
    closed = make_job(external_id="5")
    skipped = make_job(external_id="6")
    store.upsert_jobs([scored, closed, skipped], now=NOW - timedelta(days=5))
    store.upsert_jobs([oldest], now=NOW - timedelta(days=3))
    store.upsert_jobs([middle], now=NOW - timedelta(days=2))
    store.upsert_jobs([newest], now=NOW - timedelta(days=1))
    store.save_score(scored.uid, Score(50, "se", "ok"), now=NOW)
    store.mark_closed(ACME.key(), {j.uid for j in (oldest, middle, newest, scored, skipped)})
    store.set_status(skipped.uid, "skipped")

    assert [j.uid for j in store.jobs_to_score(10)] == [oldest.uid, middle.uid, newest.uid]
    assert [j.uid for j in store.jobs_to_score(2)] == [oldest.uid, middle.uid]


def test_save_score_round_trips(store):
    job = make_job()
    store.upsert_jobs([job], now=NOW)

    store.save_score(job.uid, Score(88, "fde", "Strong fit.", pay_suspect=True), now=NOW)

    [item] = store.jobs_for_digest()
    assert item.job == job
    assert item.score == Score(88, "fde", "Strong fit.", pay_suspect=True)
    assert store.jobs_to_score(10) == []
    row = store._fetchone("SELECT scored_at FROM jobs WHERE uid = ?", (job.uid,))
    assert row["scored_at"] == NOW.isoformat()


def test_save_score_unknown_uid_raises_keyerror(store):
    with pytest.raises(KeyError):
        store.save_score("greenhouse:acme:missing", Score(1, "se", "x"))


# -- digests -----------------------------------------------------------------


def seed_scored(store, scores: dict[str, int]) -> dict[str, Job]:
    jobs = {ext: make_job(external_id=ext) for ext in scores}
    store.upsert_jobs(list(jobs.values()), now=NOW)
    for ext, value in scores.items():
        store.save_score(jobs[ext].uid, Score(value, "se", f"score {value}"), now=NOW)
    return jobs


def test_jobs_for_digest_highest_score_first_and_only_open_new_scored(store):
    jobs = seed_scored(store, {"a": 40, "b": 90, "c": 65, "closed": 99, "applied": 95})
    unscored = make_job(external_id="unscored")
    store.upsert_jobs([unscored], now=NOW)
    still_listed = {j.uid for k, j in jobs.items() if k != "closed"} | {unscored.uid}
    store.mark_closed(ACME.key(), still_listed)
    store.set_status(jobs["applied"].uid, "applied")

    ranked = store.jobs_for_digest()

    assert [s.job.uid for s in ranked] == [jobs["b"].uid, jobs["c"].uid, jobs["a"].uid]
    assert [s.score.value for s in ranked] == [90, 65, 40]


def test_record_digest_numbers_items_in_given_order(store):
    jobs = seed_scored(store, {"a": 40, "b": 90, "c": 65})
    assert store.latest_digest_id() is None

    order = [jobs["b"].uid, jobs["c"].uid, jobs["a"].uid]
    digest_id = store.record_digest(order, now=NOW)

    assert store.latest_digest_id() == digest_id
    assert [store.digest_uid(digest_id, n) for n in (1, 2, 3)] == order
    assert store.digest_uid(digest_id, 4) is None
    assert store.digest_uid(digest_id + 1, 1) is None
    row = store._fetchone("SELECT sent_at FROM digests WHERE id = ?", (digest_id,))
    assert row["sent_at"] == NOW.isoformat()


def test_each_digest_numbers_from_one_and_ids_increase(store):
    jobs = seed_scored(store, {"a": 40, "b": 90})

    first = store.record_digest([jobs["b"].uid], now=NOW)
    second = store.record_digest([jobs["a"].uid], now=NOW + timedelta(days=1))

    assert second > first
    assert store.latest_digest_id() == second
    assert store.digest_uid(first, 1) == jobs["b"].uid
    assert store.digest_uid(second, 1) == jobs["a"].uid


def test_sent_job_never_reappears_in_jobs_for_digest(store):
    jobs = seed_scored(store, {"a": 40, "b": 90})
    store.record_digest([jobs["b"].uid], now=NOW)

    assert [s.job.uid for s in store.jobs_for_digest()] == [jobs["a"].uid]

    # Seen again on the board, even rescored: still never re-sent.
    store.upsert_jobs([jobs["b"]], now=NOW + timedelta(days=1))
    store.save_score(jobs["b"].uid, Score(99, "fde", "rescored"), now=NOW + timedelta(days=1))
    store.record_digest([jobs["a"].uid], now=NOW + timedelta(days=1))

    assert store.jobs_for_digest() == []


def test_empty_digest_is_recorded(store):
    digest_id = store.record_digest([], now=NOW)

    assert store.latest_digest_id() == digest_id
    assert store.digest_uid(digest_id, 1) is None


def test_record_digest_rejects_unknown_or_repeated_uids_without_writing(store):
    jobs = seed_scored(store, {"a": 40})

    with pytest.raises(KeyError):
        store.record_digest([jobs["a"].uid, "greenhouse:acme:missing"], now=NOW)
    with pytest.raises(ValueError):
        store.record_digest([jobs["a"].uid, jobs["a"].uid], now=NOW)

    assert store.latest_digest_id() is None
    assert len(store.jobs_for_digest()) == 1


def test_prune_boards_removes_digest_items_of_pruned_jobs(store):
    board = BoardRef("lever", "old", company_name="Old Co")
    job = make_job(board=board)
    store.upsert_jobs([job], now=NOW - timedelta(weeks=10))
    store.save_score(job.uid, Score(70, "se", "x"), now=NOW - timedelta(weeks=10))
    digest_id = store.record_digest([job.uid], now=NOW - timedelta(weeks=10))

    assert store.prune_boards(weeks=4, now=NOW) == [board.key()]

    assert store.digest_uid(digest_id, 1) is None
    assert store.latest_digest_id() == digest_id
