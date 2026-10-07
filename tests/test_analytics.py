import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from app.core.security import hash_ip
from app.models import URL, Click
from app.services import analytics_service
from app.services.analytics_service import (
    ClickEvent,
    ClickRecorder,
    sanitize_referrer,
    sanitize_user_agent,
)
from tests.conftest import PEPPER, register

LONG = "https://example.com/some/long/path"
URLS = "/api/v1/urls"
NOW = datetime(2026, 3, 15, 10, 30, tzinfo=UTC)


def make(client, headers, **body):
    resp = client.post(URLS, json={"long_url": LONG, **body}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["code"]


def follow(client, code, **headers):
    return client.get(f"/{code}", follow_redirects=False, headers=headers)


def flush(client) -> None:
    assert client.app.state.click_recorder.flush(timeout=10)


def clicks(db) -> list[Click]:
    db.expire_all()
    return list(db.scalars(select(Click).order_by(Click.id)))


def analytics(client, headers, code, **params):
    return client.get(f"{URLS}/{code}/analytics", params=params, headers=headers)


def url_id(db, code) -> int:
    return db.scalar(select(URL.id).where(URL.code == code))


def seed(db, uid, when, n=1, referrer=None, user_agent="ua"):
    for _ in range(n):
        db.add(Click(url_id=uid, accessed_at=when, referrer=referrer, user_agent=user_agent))
    db.commit()


# --- recording clicks on redirect -------------------------------------------------------------


def test_redirect_records_a_click(client, auth_headers, db):
    code = make(client, auth_headers)
    resp = follow(
        client,
        code,
        **{"User-Agent": "Mozilla/5.0 Test", "Referer": "https://news.example/a/b?token=secret"},
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == LONG
    flush(client)

    (click,) = clicks(db)
    assert click.url_id == url_id(db, code)
    assert click.user_agent == "Mozilla/5.0 Test"
    assert click.referrer == "https://news.example"
    assert len(click.ip_hash) == 64
    assert abs(datetime.now(UTC) - click.accessed_at) < timedelta(seconds=30)
    assert click.accessed_at.tzinfo is not None


def test_each_redirect_is_one_click_including_cache_hits(client, auth_headers, db):
    a, b = make(client, auth_headers), make(client, auth_headers)
    for _ in range(3):
        assert follow(client, a).status_code == 302
    assert follow(client, b).status_code == 302
    flush(client)
    ids = [c.url_id for c in clicks(db)]
    assert ids.count(url_id(db, a)) == 3
    assert ids.count(url_id(db, b)) == 1


def test_missing_headers_are_stored_as_null(client, auth_headers, db):
    code = make(client, auth_headers)
    follow(client, code, **{"User-Agent": ""})
    flush(client)
    (click,) = clicks(db)
    assert click.user_agent is None
    assert click.referrer is None


def test_redirect_behaviour_is_unchanged_by_recording(client, auth_headers):
    code = make(client, auth_headers)
    resp = follow(client, code)
    assert resp.status_code == 302
    assert resp.headers["location"] == LONG
    assert resp.content == b""
    assert resp.headers["x-frame-options"] == "DENY"


def test_failed_lookups_and_blocked_links_record_nothing(client, auth_headers, db):
    disabled = make(client, auth_headers)
    expired = make(client, auth_headers, ttl_seconds=60)
    client.patch(f"{URLS}/{disabled}", json={"is_active": False}, headers=auth_headers)
    db.execute(
        update(URL)
        .where(URL.code == expired)
        .values(expires_at=datetime.now(UTC) - timedelta(seconds=5))
    )
    db.commit()

    assert follow(client, "nosuchcode").status_code == 404
    assert follow(client, disabled).status_code == 410
    assert follow(client, expired).status_code == 410
    flush(client)
    assert clicks(db) == []


def test_rate_limited_redirects_record_nothing(make_client):
    client = make_client(rate_limit_redirect_ip_per_minute=1)
    headers = register(client)
    code = make(client, headers)
    assert follow(client, code).status_code == 302
    assert follow(client, code).status_code == 429
    flush(client)
    with client.app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Click)) == 1


def test_redirect_does_not_wait_for_the_database_write(client, auth_headers, db, monkeypatch):
    code = make(client, auth_headers)
    release = threading.Event()
    original = ClickRecorder._insert

    def blocked_insert(self, batch):
        release.wait(timeout=10)
        return original(self, batch)

    monkeypatch.setattr(ClickRecorder, "_insert", blocked_insert)
    recorder = client.app.state.click_recorder

    started = time.monotonic()
    follow(client, code)
    recorder.flush(timeout=0.2)  # make the worker pick the batch up; it blocks in _insert
    statuses = [follow(client, code).status_code for _ in range(20)]
    elapsed = time.monotonic() - started

    assert statuses == [302] * 20
    assert not release.is_set()  # the writer is still stuck, yet every redirect completed
    assert elapsed < 5
    release.set()
    assert recorder.flush(timeout=10)
    assert len(clicks(db)) == 21


def test_click_for_deleted_link_is_skipped_without_losing_others(client, auth_headers, db):
    live, gone = make(client, auth_headers), make(client, auth_headers)
    live_id, gone_id = url_id(db, live), url_id(db, gone)
    client.delete(f"{URLS}/{gone}", headers=auth_headers)

    recorder = client.app.state.click_recorder
    for uid, code in [(gone_id, gone), (live_id, live), (gone_id, gone)]:
        recorder.record(ClickEvent(uid, code, datetime.now(UTC), "h", None, None))
    flush(client)

    assert [c.url_id for c in clicks(db)] == [live_id]
    stats = recorder.stats()
    assert stats["written"] == 1 and stats["skipped"] == 2 and stats["failed"] == 0


def test_click_with_reused_id_but_different_code_is_skipped(client, auth_headers, db):
    code = make(client, auth_headers)
    recorder = client.app.state.click_recorder
    recorder.record(
        ClickEvent(url_id(db, code), "someothercode", datetime.now(UTC), "h", None, None)
    )
    flush(client)
    assert clicks(db) == []


# --- sanitisation -----------------------------------------------------------------------------


def test_user_agent_is_truncated_and_cleaned():
    assert sanitize_user_agent("a" * 600) == "a" * 512
    assert sanitize_user_agent("Mozilla\r\nX-Injected: 1\x00") == "MozillaX-Injected: 1"
    assert sanitize_user_agent("  padded  ") == "padded"
    assert sanitize_user_agent("") is None
    assert sanitize_user_agent(None) is None
    assert sanitize_user_agent("\x00\x01") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://news.example/path?q=1#frag", "https://news.example"),
        ("http://News.Example:8080/x", "http://news.example:8080"),
        ("https://user:pass@site.example/x", "https://site.example"),
        ("https://[2001:db8::1]:444/x", "https://[2001:db8::1]:444"),
        ("https://site.example", "https://site.example"),
    ],
)
def test_referrer_is_reduced_to_origin(raw, expected):
    assert sanitize_referrer(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "javascript:alert(1)",
        "ftp://files.example/x",
        "android-app://com.example",
        "not a url",
        "https://exa mple.com/",
        "https://site.example/\r\nSet-Cookie: x=1",
        "https://site.example:99999/",
        "//no-scheme.example/",
        "https://",
    ],
)
def test_unusable_referrers_become_null(raw):
    assert sanitize_referrer(raw) is None


# --- IP anonymisation -------------------------------------------------------------------------


def test_ip_hash_properties():
    digest = hash_ip("203.0.113.9", PEPPER)
    assert len(digest) == 64 and int(digest, 16) >= 0
    assert "203.0.113.9" not in digest
    assert digest == hash_ip("203.0.113.9", PEPPER)
    assert digest != hash_ip("203.0.113.10", PEPPER)
    assert digest != hash_ip("203.0.113.9", "another-pepper")


def test_ip_hash_normalises_equivalent_addresses():
    assert hash_ip("::ffff:203.0.113.9", PEPPER) == hash_ip("203.0.113.9", PEPPER)
    assert hash_ip("2001:db8:0:0:0:0:0:1", PEPPER) == hash_ip("2001:DB8::1", PEPPER)
    assert hash_ip("fe80::1%eth0", PEPPER) == hash_ip("fe80::1", PEPPER)


def test_ip_hash_is_domain_separated_from_api_key_hash():
    from app.core.security import hash_api_key

    assert hash_ip("203.0.113.9", PEPPER) != hash_api_key("203.0.113.9", PEPPER)


def test_ip_hash_tolerates_non_ip_values():
    assert len(hash_ip("unknown", PEPPER)) == 64


def test_raw_visitor_ip_is_never_persisted(app, settings):
    visitor_ip = "203.0.113.9"
    db_path = settings.database_url.removeprefix("sqlite:///")
    with TestClient(app, client=(visitor_ip, 50000)) as client:
        headers = register(client)
        code = make(client, headers)
        follow(client, code, **{"User-Agent": "UA-Test"})
        follow(client, code)
        flush(client)
        with client.app.state.session_factory() as session:
            stored = session.scalars(select(Click.ip_hash)).all()
        assert stored == [hash_ip(visitor_ip, PEPPER)] * 2
    # engine disposal on shutdown checkpoints the WAL; inspect every file SQLite may use
    for suffix in ("", "-wal", "-shm"):
        try:
            data = open(db_path + suffix, "rb").read()
        except FileNotFoundError:
            continue
        assert visitor_ip.encode() not in data, suffix


# --- analytics route --------------------------------------------------------------------------


def test_analytics_requires_api_key(client, auth_headers):
    code = make(client, auth_headers)
    assert client.get(f"{URLS}/{code}/analytics").status_code == 401
    bad = {"X-API-Key": "nope"}
    assert client.get(f"{URLS}/{code}/analytics", headers=bad).status_code == 401


def test_analytics_unknown_code_is_404(client, auth_headers):
    assert analytics(client, auth_headers, "missing").status_code == 404


def test_analytics_of_other_users_link_is_404_and_indistinguishable(client, auth_headers, db):
    other = register(client)
    code = make(client, auth_headers)
    seed(db, url_id(db, code), NOW, n=3)
    foreign = analytics(client, other, code)
    missing = analytics(client, other, "does-not-exist")
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()
    assert analytics(client, auth_headers, code).json()["total_clicks"] == 3


def test_analytics_for_link_without_clicks(client, auth_headers):
    code = make(client, auth_headers)
    body = analytics(client, auth_headers, code).json()
    assert body["code"] == code
    assert body["total_clicks"] == 0
    assert body["granularity"] == "day"
    assert len(body["time_series"]) == 7
    assert all(b["clicks"] == 0 for b in body["time_series"])
    assert body["top_referrers"] == []
    assert body["recent_clicks"] == {"items": [], "limit": 20, "offset": 0}


def test_daily_time_series(client, auth_headers, db, monkeypatch):
    code = make(client, auth_headers)
    uid = url_id(db, code)
    seed(db, uid, datetime(2026, 3, 12, 23, 59, tzinfo=UTC))  # before the window
    seed(db, uid, datetime(2026, 3, 13, 0, 0, tzinfo=UTC))
    seed(db, uid, datetime(2026, 3, 14, 12, 0, tzinfo=UTC), n=2)
    seed(db, uid, datetime(2026, 3, 15, 9, 0, tzinfo=UTC))
    seed(db, uid, datetime(2026, 3, 15, 10, 29, tzinfo=UTC))
    seed(db, uid, datetime(2026, 3, 15, 23, 0, tzinfo=UTC))
    seed(db, uid, datetime(2026, 3, 16, 0, 0, tzinfo=UTC))  # after the last bucket
    monkeypatch.setattr("app.api.v1.urls.utcnow", lambda: NOW)

    body = analytics(client, auth_headers, code, granularity="day", days=3).json()

    assert body["total_clicks"] == 8  # all time, including outside the window
    assert body["window_start"] == "2026-03-13T00:00:00Z"
    assert [(b["bucket"], b["clicks"]) for b in body["time_series"]] == [
        ("2026-03-13T00:00:00Z", 1),
        ("2026-03-14T00:00:00Z", 2),
        ("2026-03-15T00:00:00Z", 3),
    ]


def test_hourly_time_series(client, auth_headers, db, monkeypatch):
    code = make(client, auth_headers)
    uid = url_id(db, code)
    seed(db, uid, datetime(2026, 3, 14, 10, 59, tzinfo=UTC))  # one hour too old
    seed(db, uid, datetime(2026, 3, 14, 11, 0, tzinfo=UTC))
    seed(db, uid, datetime(2026, 3, 15, 10, 0, tzinfo=UTC))
    seed(db, uid, datetime(2026, 3, 15, 10, 29, tzinfo=UTC))
    seed(db, uid, datetime(2026, 3, 15, 11, 0, tzinfo=UTC))  # next hour
    monkeypatch.setattr("app.api.v1.urls.utcnow", lambda: NOW)

    body = analytics(client, auth_headers, code, granularity="hour", days=1).json()
    series = body["time_series"]

    assert body["granularity"] == "hour"
    assert len(series) == 24
    assert series[0] == {"bucket": "2026-03-14T11:00:00Z", "clicks": 1}
    assert series[-1] == {"bucket": "2026-03-15T10:00:00Z", "clicks": 2}
    assert sum(b["clicks"] for b in series) == 3
    assert body["total_clicks"] == 5


def test_series_is_zero_filled_and_ordered(client, auth_headers, db, monkeypatch):
    code = make(client, auth_headers)
    seed(db, url_id(db, code), datetime(2026, 3, 11, 5, 0, tzinfo=UTC))
    monkeypatch.setattr("app.api.v1.urls.utcnow", lambda: NOW)
    series = analytics(client, auth_headers, code, days=5).json()["time_series"]
    assert [b["bucket"][:10] for b in series] == [f"2026-03-{d}" for d in range(11, 16)]
    assert [b["clicks"] for b in series] == [1, 0, 0, 0, 0]


def test_top_referrers(client, auth_headers, db):
    code = make(client, auth_headers)
    uid = url_id(db, code)
    seed(db, uid, NOW, n=3, referrer="https://a.example")
    seed(db, uid, NOW, n=5, referrer="https://b.example")
    seed(db, uid, NOW, n=3, referrer="https://0.example")
    seed(db, uid, NOW, n=4, referrer=None)  # direct traffic is not a referrer

    body = analytics(client, auth_headers, code).json()
    assert body["top_referrers"] == [
        {"referrer": "https://b.example", "clicks": 5},
        {"referrer": "https://0.example", "clicks": 3},  # ties break alphabetically
        {"referrer": "https://a.example", "clicks": 3},
    ]
    limited = analytics(client, auth_headers, code, top_referrers=1).json()
    assert limited["top_referrers"] == [{"referrer": "https://b.example", "clicks": 5}]


def test_recent_clicks_are_newest_first_and_paginated(client, auth_headers, db):
    code = make(client, auth_headers)
    uid = url_id(db, code)
    times = [NOW - timedelta(minutes=i) for i in range(5)]  # newest first
    for i, when in enumerate(reversed(times)):  # insert oldest first
        seed(db, uid, when, user_agent=f"ua-{i}")

    page1 = analytics(client, auth_headers, code, limit=2).json()["recent_clicks"]
    assert [c["user_agent"] for c in page1["items"]] == ["ua-4", "ua-3"]
    assert (page1["limit"], page1["offset"]) == (2, 0)
    assert page1["items"][0]["clicked_at"] == "2026-03-15T10:30:00Z"

    page2 = analytics(client, auth_headers, code, limit=2, offset=2).json()["recent_clicks"]
    assert [c["user_agent"] for c in page2["items"]] == ["ua-2", "ua-1"]

    page3 = analytics(client, auth_headers, code, limit=2, offset=4).json()["recent_clicks"]
    assert [c["user_agent"] for c in page3["items"]] == ["ua-0"]

    beyond = analytics(client, auth_headers, code, offset=50).json()["recent_clicks"]
    assert beyond["items"] == []


def test_recent_clicks_do_not_expose_ip_hashes(client, auth_headers, db):
    code = make(client, auth_headers)
    seed(db, url_id(db, code), NOW)
    item = analytics(client, auth_headers, code).json()["recent_clicks"]["items"][0]
    assert set(item) == {"clicked_at", "referrer", "user_agent"}


def test_analytics_only_counts_the_requested_link(client, auth_headers, db):
    a, b = make(client, auth_headers), make(client, auth_headers)
    seed(db, url_id(db, a), NOW, n=2, referrer="https://only-a.example")
    seed(db, url_id(db, b), NOW, n=5)
    body = analytics(client, auth_headers, b).json()
    assert body["total_clicks"] == 5
    assert body["top_referrers"] == []
    assert len(body["recent_clicks"]["items"]) == 5


@pytest.mark.parametrize(
    "params",
    [
        {"granularity": "week"},
        {"days": 0},
        {"days": 91},
        {"days": "x"},
        {"granularity": "hour", "days": 15},
        {"top_referrers": 0},
        {"top_referrers": 51},
        {"limit": 0},
        {"limit": 101},
        {"offset": -1},
    ],
)
def test_analytics_validates_parameters(client, auth_headers, params):
    code = make(client, auth_headers)
    assert analytics(client, auth_headers, code, **params).status_code == 422


def test_hourly_limit_boundary(client, auth_headers):
    code = make(client, auth_headers)
    resp = analytics(
        client, auth_headers, code, granularity="hour", days=analytics_service.MAX_HOURLY_DAYS
    )
    assert resp.status_code == 200
    assert len(resp.json()["time_series"]) == analytics_service.MAX_HOURLY_DAYS * 24


def test_disabled_link_analytics_remain_available(client, auth_headers, db):
    code = make(client, auth_headers)
    seed(db, url_id(db, code), NOW, n=2)
    client.patch(f"{URLS}/{code}", json={"is_active": False}, headers=auth_headers)
    assert analytics(client, auth_headers, code).json()["total_clicks"] == 2


def test_analytics_of_deleted_link_is_404(client, auth_headers):
    code = make(client, auth_headers)
    client.delete(f"{URLS}/{code}", headers=auth_headers)
    assert analytics(client, auth_headers, code).status_code == 404


def test_end_to_end_redirects_show_up_in_analytics(client, auth_headers):
    code = make(client, auth_headers)
    follow(client, code, Referer="https://a.example/x", **{"User-Agent": "ua-1"})
    follow(client, code, Referer="https://a.example/y", **{"User-Agent": "ua-2"})
    follow(client, code, **{"User-Agent": "ua-3"})
    flush(client)

    body = analytics(client, auth_headers, code).json()
    assert body["total_clicks"] == 3
    assert body["top_referrers"] == [{"referrer": "https://a.example", "clicks": 2}]
    assert sum(b["clicks"] for b in body["time_series"]) == 3
    assert [c["user_agent"] for c in body["recent_clicks"]["items"]] == ["ua-3", "ua-2", "ua-1"]
