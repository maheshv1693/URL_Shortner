import asyncio
import sqlite3
import threading
import time
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database import create_db_engine
from app.models import URL, Click, User
from app.services.analytics_service import ClickEvent, ClickRecorder
from tests.conftest import register

LONG = "https://example.com/some/long/path"
URLS = "/api/v1/urls"


def make(client, headers, **body):
    resp = client.post(URLS, json={"long_url": LONG, **body}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["code"]


def event(db_url_id, code, n=0) -> ClickEvent:
    return ClickEvent(db_url_id, code, datetime.now(UTC), f"hash{n}", f"ua{n}", None)


def count_clicks(app) -> int:
    with Session(app.state.engine) as session:
        return session.scalar(select(func.count()).select_from(Click))


def seed_link(app):
    """Put one link in the app's database; returns (app, url_id, code)."""
    with Session(app.state.engine) as session:
        user = User(api_key_hash="h")
        session.add(user)
        session.flush()
        url = URL(code="seeded1", long_url=LONG, owner_id=user.id)
        session.add(url)
        session.commit()
        return app, url.id, url.code


@pytest.fixture
def seeded(app):
    return seed_link(app)


@pytest.fixture
def recorder_factory(app):
    """Build ClickRecorders on the test database; all are shut down afterwards."""
    created = []

    def build(target=None, **kwargs):
        recorder = ClickRecorder((target or app).state.session_factory, **kwargs)
        recorder.start()
        created.append(recorder)
        return recorder

    yield build
    for recorder in created:
        recorder.shutdown(timeout=10)


# --- health probe -----------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/health", "/api/v1/health"])
def test_health_ok(client, path):
    resp = client.get(path)
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "database": "ok"}
    assert resp.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("path", ["/health", "/api/v1/health"])
def test_health_needs_no_auth_and_is_not_rate_limited(make_client, path):
    client = make_client(rate_limit_redirect_ip_per_minute=1, rate_limit_create_ip_per_minute=1)
    assert [client.get(path).status_code for _ in range(5)] == [200] * 5


def test_health_is_not_shadowed_by_redirect_route(client, auth_headers):
    assert client.get("/health", follow_redirects=False).status_code == 200


def test_health_reports_unavailable_database(client, tmp_path):
    app = client.app
    healthy_engine = app.state.engine
    app.state.engine = create_db_engine(
        Settings(database_url=f"sqlite:///{tmp_path / 'missing-dir' / 'x.db'}")
    )
    try:
        resp = client.get("/health")
        assert resp.status_code == 503
        assert resp.json() == {"status": "degraded", "database": "unavailable"}
        assert client.get("/api/v1/health").status_code == 503
    finally:
        app.state.engine.dispose()
        app.state.engine = healthy_engine
    assert client.get("/health").status_code == 200  # recovers once the database is back


def test_health_failure_does_not_leak_error_details(client, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("secret-connection-string")

    monkeypatch.setattr(client.app.state.engine, "connect", boom)
    resp = client.get("/health")
    assert resp.status_code == 503
    assert "secret" not in resp.text


def test_health_actually_queries_the_database(client, monkeypatch):
    statements = []
    from sqlalchemy import event as sa_event

    sa_event.listen(
        client.app.state.engine,
        "before_cursor_execute",
        lambda conn, cur, statement, *a: statements.append(statement),
    )
    client.get("/health")
    assert "SELECT 1" in statements


# --- SQLite configuration under concurrency ---------------------------------------------------


def test_worker_connections_use_wal_and_busy_timeout(app_factory):
    app = app_factory(sqlite_busy_timeout_ms=1234)
    with app.state.session_factory() as session:
        assert session.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert session.execute(text("PRAGMA busy_timeout")).scalar() == 1234
        assert session.execute(text("PRAGMA foreign_keys")).scalar() == 1


def test_redirects_keep_working_while_a_writer_holds_the_lock(client, auth_headers, app):
    code = make(client, auth_headers)
    db_file = app.state.settings.database_url.removeprefix("sqlite:///")
    blocker = sqlite3.connect(db_file, timeout=0, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")  # take the single write lock
    try:
        started = time.monotonic()
        statuses = [client.get(f"/{code}", follow_redirects=False).status_code for _ in range(10)]
        assert statuses == [302] * 10
        assert time.monotonic() - started < 3  # WAL readers are not blocked by the writer
    finally:
        blocker.rollback()
        blocker.close()
    assert app.state.click_recorder.flush(timeout=15)
    assert count_clicks(app) == 10


def test_many_concurrent_redirects_are_all_recorded(app_factory):
    app = app_factory(analytics_batch_size=25, analytics_flush_interval_seconds=0.05)
    total = 200

    async def scenario():
        recorder = app.state.click_recorder
        recorder.start()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            user = (await http.post("/api/v1/users")).json()
            headers = {"X-API-Key": user["api_key"]}
            created = await http.post(URLS, json={"long_url": LONG}, headers=headers)
            code = created.json()["code"]
            responses = await asyncio.gather(
                *(http.get(f"/{code}", follow_redirects=False) for _ in range(total))
            )
        assert {r.status_code for r in responses} == {302}
        return recorder

    recorder = asyncio.run(scenario())
    assert recorder.shutdown(timeout=15)
    stats = recorder.stats()
    assert stats["written"] == total and stats["failed"] == 0 and stats["dropped"] == 0
    assert count_clicks(app) == total


# --- write failures are absorbed --------------------------------------------------------------


def lock_database(app) -> sqlite3.Connection:
    db_file = app.state.settings.database_url.removeprefix("sqlite:///")
    blocker = sqlite3.connect(db_file, timeout=0, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    return blocker


def test_locked_database_is_retried_until_it_frees_up(seeded, recorder_factory):
    app, uid, code = seeded
    recorder = recorder_factory(write_attempts=8, retry_backoff=0.05)
    blocker = lock_database(app)
    for n in range(4):
        recorder.record(event(uid, code, n))

    assert recorder.flush(timeout=0.4) is False  # still waiting on the lock
    assert count_clicks(app) == 0

    blocker.rollback()
    blocker.close()
    assert recorder.flush(timeout=15)
    assert count_clicks(app) == 4
    assert recorder.stats()["failed"] == 0


def test_persistent_lock_drops_the_batch_but_the_worker_survives(app_factory, recorder_factory):
    app, uid, code = seed_link(app_factory(sqlite_busy_timeout_ms=50))
    recorder = recorder_factory(app, write_attempts=2, retry_backoff=0.01)
    blocker = lock_database(app)
    for n in range(3):
        recorder.record(event(uid, code, n))
    assert recorder.flush(timeout=15)  # buffer drained: the batch was given up on
    assert recorder.stats()["failed"] == 3
    assert count_clicks(app) == 0

    blocker.rollback()
    blocker.close()
    recorder.record(event(uid, code, 99))
    assert recorder.flush(timeout=10)
    assert count_clicks(app) == 1


def test_unexpected_write_error_does_not_kill_the_worker(seeded, recorder_factory, monkeypatch):
    app, uid, code = seeded
    recorder = recorder_factory()
    original = ClickRecorder._insert
    calls = []

    def flaky(self, batch):
        calls.append(len(batch))
        if len(calls) == 1:
            raise RuntimeError("disk on fire")
        return original(self, batch)

    monkeypatch.setattr(ClickRecorder, "_insert", flaky)
    recorder.record(event(uid, code, 1))
    assert recorder.flush(timeout=10)
    recorder.record(event(uid, code, 2))
    assert recorder.flush(timeout=10)

    assert recorder.stats()["failed"] == 1
    assert count_clicks(app) == 1


def test_redirects_survive_a_dead_analytics_pipeline(client, auth_headers, monkeypatch):
    code = make(client, auth_headers)

    def explode(self, event):
        raise RuntimeError("recorder broken")

    monkeypatch.setattr(ClickRecorder, "record", explode)
    resp = client.get(f"/{code}", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == LONG


# --- buffering and batching -------------------------------------------------------------------


def test_buffer_is_bounded_and_overflow_is_dropped(seeded, recorder_factory):
    app, uid, code = seeded
    recorder = recorder_factory(max_queue=3, flush_interval=60, batch_size=1000)
    results = [recorder.record(event(uid, code, n)) for n in range(5)]
    assert results == [True, True, True, False, False]
    assert recorder.stats()["dropped"] == 2
    assert recorder.pending() == 3


def test_redirects_are_unaffected_when_the_buffer_is_full(make_client):
    client = make_client(
        analytics_queue_max=1, analytics_flush_interval_seconds=60, analytics_batch_size=1000
    )
    headers = register(client)
    code = make(client, headers)
    statuses = [client.get(f"/{code}", follow_redirects=False).status_code for _ in range(4)]
    assert statuses == [302] * 4
    assert client.app.state.click_recorder.stats()["dropped"] == 3


def test_events_are_written_in_batches(seeded, recorder_factory, monkeypatch):
    app, uid, code = seeded
    recorder = recorder_factory(batch_size=3, flush_interval=60)
    sizes = []
    original = ClickRecorder._insert

    def spy(self, batch):
        sizes.append(len(batch))
        return original(self, batch)

    monkeypatch.setattr(ClickRecorder, "_insert", spy)
    for n in range(7):
        recorder.record(event(uid, code, n))
    assert recorder.flush(timeout=10)

    assert sum(sizes) == 7
    assert max(sizes) <= 3
    assert len(sizes) < 7  # grouped, not one write per click
    assert count_clicks(app) == 7


def test_a_full_batch_is_written_without_waiting_for_the_interval(seeded, recorder_factory):
    app, uid, code = seeded
    recorder = recorder_factory(batch_size=3, flush_interval=60)
    for n in range(3):
        recorder.record(event(uid, code, n))
    deadline = time.monotonic() + 5
    while count_clicks(app) < 3 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert count_clicks(app) == 3


def test_events_are_written_after_the_flush_interval(seeded, recorder_factory):
    app, uid, code = seeded
    recorder = recorder_factory(batch_size=1000, flush_interval=0.1)
    recorder.record(event(uid, code))
    deadline = time.monotonic() + 5
    while count_clicks(app) < 1 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert count_clicks(app) == 1


# --- graceful shutdown ------------------------------------------------------------------------


def test_shutdown_flushes_buffered_events(app_factory):
    app = app_factory(analytics_batch_size=1000, analytics_flush_interval_seconds=60)
    recorder = app.state.click_recorder
    with TestClient(app) as client:
        headers = register(client)
        code = make(client, headers)
        for _ in range(12):
            assert client.get(f"/{code}", follow_redirects=False).status_code == 302
        # Nothing has been written yet: the events live only in memory.
        assert recorder.pending() == 12
        assert count_clicks(app) == 0
        started = time.monotonic()
    # leaving the context runs the lifespan shutdown
    assert time.monotonic() - started < 10  # did not sit out the 60 s flush interval
    assert recorder.pending() == 0
    assert count_clicks(app) == 12
    assert recorder.stats()["written"] == 12


def test_shutdown_drains_more_events_than_one_batch(seeded, recorder_factory):
    app, uid, code = seeded
    recorder = recorder_factory(batch_size=3, flush_interval=60)
    for n in range(10):
        recorder.record(event(uid, code, n))
    assert recorder.shutdown(timeout=10) is True
    assert count_clicks(app) == 10


def test_recorder_rejects_events_after_shutdown(seeded, recorder_factory):
    app, uid, code = seeded
    recorder = recorder_factory()
    assert recorder.shutdown(timeout=10) is True
    assert recorder.record(event(uid, code)) is False
    assert recorder.stats()["dropped"] == 1


def test_recorder_can_be_restarted(seeded, recorder_factory):
    app, uid, code = seeded
    recorder = recorder_factory()
    recorder.shutdown(timeout=10)
    recorder.start()
    assert recorder.record(event(uid, code)) is True
    assert recorder.shutdown(timeout=10) is True
    assert count_clicks(app) == 1


def test_shutdown_reports_failure_when_it_cannot_finish_in_time(
    seeded, recorder_factory, monkeypatch
):
    app, uid, code = seeded
    recorder = recorder_factory(flush_interval=60)
    release = threading.Event()
    original = ClickRecorder._insert

    def slow(self, batch):
        release.wait(timeout=10)
        return original(self, batch)

    monkeypatch.setattr(ClickRecorder, "_insert", slow)
    recorder.record(event(uid, code))
    assert recorder.shutdown(timeout=0.2) is False
    release.set()
    assert recorder.shutdown(timeout=10) is True
    assert count_clicks(app) == 1


def test_shutdown_without_events_is_immediate(app_factory):
    app = app_factory()
    started = time.monotonic()
    with TestClient(app):
        pass
    assert time.monotonic() - started < 5
    assert app.state.click_recorder.stats()["written"] == 0
