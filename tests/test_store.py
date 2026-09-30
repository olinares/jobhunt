from datetime import UTC, datetime, timedelta

import pytest

from jobhunt.models import BoardRef, Job
from jobhunt.store import SqliteStore

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)

ACME = BoardRef("greenhouse", "acme", company_name="Acme")


@pytest.fixture
def store():
    with SqliteStore(":memory:") as s:
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

    row = store._conn.execute(
        "SELECT title, pay_min FROM jobs WHERE uid = ?", (job.uid,)
    ).fetchone()
    assert row["title"] == "Senior Solutions Engineer"
    assert row["pay_min"] == 120_000.0


# -- mark_closed / reopening ----------------------------------------------


def test_mark_closed_closes_jobs_no_longer_seen(store):
    j1 = make_job(external_id="1")
    j2 = make_job(external_id="2")
    store.upsert_jobs([j1, j2], now=NOW)

    closed = store.mark_closed(ACME.key(), {j1.uid}, now=NOW + timedelta(days=1))

    assert closed == [j2.uid]
    row = store._conn.execute(
        "SELECT status, closed_at FROM jobs WHERE uid = ?", (j2.uid,)
    ).fetchone()
    assert row["status"] == "closed"
    assert row["closed_at"] is not None


def test_mark_closed_preserves_status_past_new(store):
    job = make_job()
    store.upsert_jobs([job], now=NOW)
    store.set_status(job.uid, "applied")

    closed = store.mark_closed(ACME.key(), set(), now=NOW + timedelta(days=1))

    assert closed == [job.uid]
    row = store._conn.execute("SELECT status FROM jobs WHERE uid = ?", (job.uid,)).fetchone()
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

    row = store._conn.execute("SELECT status FROM jobs WHERE uid = ?", (job.uid,)).fetchone()
    assert row["status"] == "applied"  # mark_closed didn't touch it (past "new")

    # Reappears on the board.
    store.upsert_jobs([job], now=NOW + timedelta(days=2))

    row = store._conn.execute(
        "SELECT status, closed_at FROM jobs WHERE uid = ?", (job.uid,)
    ).fetchone()
    assert row["closed_at"] is None
    assert row["status"] == "applied"  # not reset to "new"


def test_reappearing_closed_new_job_reopens_to_new(store):
    job = make_job()
    store.upsert_jobs([job], now=NOW)
    store.mark_closed(ACME.key(), set(), now=NOW + timedelta(days=1))

    row = store._conn.execute("SELECT status FROM jobs WHERE uid = ?", (job.uid,)).fetchone()
    assert row["status"] == "closed"

    store.upsert_jobs([job], now=NOW + timedelta(days=2))

    row = store._conn.execute(
        "SELECT status, closed_at FROM jobs WHERE uid = ?", (job.uid,)
    ).fetchone()
    assert row["status"] == "new"
    assert row["closed_at"] is None


# -- set_status -----------------------------------------------------------


def test_set_status_accepts_valid_values(store):
    job = make_job()
    store.upsert_jobs([job], now=NOW)

    store.set_status(job.uid, "approved")

    row = store._conn.execute("SELECT status FROM jobs WHERE uid = ?", (job.uid,)).fetchone()
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

    row = store._conn.execute(
        "SELECT relevant_hits, last_relevant_hit FROM companies WHERE board_key = ?",
        (ACME.key(),),
    ).fetchone()
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
    row = store._conn.execute("SELECT 1 FROM jobs WHERE uid = ?", (job.uid,)).fetchone()
    assert row is None
