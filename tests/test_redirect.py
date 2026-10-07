from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from app.api import redirect as redirect_module
from app.models import URL
from app.services.link_cache import CachedLink, LinkCache

LONG = "https://example.com/some/long/path?x=1"


def make(client, headers, **body):
    resp = client.post("/api/v1/urls", json={"long_url": LONG, **body}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["code"]


def follow(client, code):
    return client.get(f"/{code}", follow_redirects=False)


def set_row(app, code, **values):
    with app.state.session_factory() as session:
        session.execute(update(URL).where(URL.code == code).values(**values))
        session.commit()


# --- redirect outcomes --------------------------------------------------------


def test_active_link_redirects_302(client, auth_headers):
    code = make(client, auth_headers)
    resp = follow(client, code)
    assert resp.status_code == 302
    assert resp.headers["location"] == LONG


def test_custom_alias_redirects(client, auth_headers):
    make(client, auth_headers, custom_alias="promo-1")
    assert follow(client, "promo-1").headers["location"] == LONG


def test_unknown_code_is_404(client):
    assert follow(client, "nope123").status_code == 404


def test_code_lookup_is_case_sensitive(client, auth_headers):
    make(client, auth_headers, custom_alias="CaseSens")
    assert follow(client, "casesens").status_code == 404
    assert follow(client, "CaseSens").status_code == 302


def test_disabled_link_is_410(client, auth_headers, app):
    code = make(client, auth_headers)
    set_row(app, code, is_active=False)
    assert follow(client, code).status_code == 410


def test_link_without_expiry_does_not_expire(client, auth_headers):
    code = make(client, auth_headers)
    assert follow(client, code).status_code == 302


def test_ttl_link_redirects_before_expiry(client, auth_headers):
    code = make(client, auth_headers, ttl_seconds=3600)
    assert follow(client, code).status_code == 302


def test_explicit_expiry_link_redirects_before_expiry(client, auth_headers):
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    code = make(client, auth_headers, expires_at=future)
    assert follow(client, code).status_code == 302


@pytest.mark.parametrize("how", ["ttl", "expires_at"])
def test_expired_link_is_410(client, auth_headers, monkeypatch, how):
    body = (
        {"ttl_seconds": 60}
        if how == "ttl"
        else {"expires_at": (datetime.now(UTC) + timedelta(seconds=60)).isoformat()}
    )
    code = make(client, auth_headers, **body)
    assert follow(client, code).status_code == 302

    later = datetime.now(UTC) + timedelta(seconds=61)
    monkeypatch.setattr(redirect_module, "utcnow", lambda: later)
    assert follow(client, code).status_code == 410


def test_expiry_boundary_is_inclusive(client, auth_headers, app, monkeypatch):
    code = make(client, auth_headers, ttl_seconds=60)
    with app.state.session_factory() as session:
        expires_at = session.get(URL, 1).expires_at
    monkeypatch.setattr(redirect_module, "utcnow", lambda: expires_at)
    assert follow(client, code).status_code == 410  # expires_at <= now


def test_expired_in_db_without_cache_is_410(client, auth_headers, app):
    code = make(client, auth_headers, ttl_seconds=60)
    set_row(app, code, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    assert follow(client, code).status_code == 410


def test_redirect_does_not_shadow_api_or_docs(client):
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/api/v1/users/me").status_code == 401


def test_redirect_does_not_record_clicks(client, auth_headers, db):
    from sqlalchemy import func, select

    from app.models import Click

    code = make(client, auth_headers)
    follow(client, code)
    assert db.scalar(select(func.count()).select_from(Click)) == 0


# --- LRU cache --------------------------------------------------------------


def test_cache_hit_avoids_database(client, auth_headers, app):
    code = make(client, auth_headers)
    assert len(app.state.link_cache) == 0
    assert follow(client, code).status_code == 302  # miss, populates cache
    assert app.state.link_cache.get(code) is not None

    # DB changes behind the cache's back are not seen until invalidation
    set_row(app, code, long_url="https://changed.example/")
    assert follow(client, code).headers["location"] == LONG


def test_invalidation_forces_fresh_lookup(client, auth_headers, app):
    code = make(client, auth_headers)
    follow(client, code)
    set_row(app, code, is_active=False)
    assert follow(client, code).status_code == 302  # stale cached entry

    app.state.link_cache.invalidate(code)
    assert follow(client, code).status_code == 410


def test_disabled_links_are_not_cached(client, auth_headers, app):
    code = make(client, auth_headers)
    set_row(app, code, is_active=False)
    follow(client, code)
    assert app.state.link_cache.get(code) is None


def test_unknown_codes_are_not_cached(client, app):
    follow(client, "missing")
    assert len(app.state.link_cache) == 0


def test_expiry_enforced_on_cache_hit(client, auth_headers, app, monkeypatch):
    code = make(client, auth_headers, ttl_seconds=60)
    follow(client, code)
    assert app.state.link_cache.get(code) is not None
    later = datetime.now(UTC) + timedelta(seconds=120)
    monkeypatch.setattr(redirect_module, "utcnow", lambda: later)
    assert follow(client, code).status_code == 410


def test_cache_size_comes_from_settings(settings):
    from app.main import create_app

    app = create_app(settings.model_copy(update={"redirect_cache_size": 7}))
    assert app.state.link_cache._max_size == 7
    app.state.engine.dispose()


def link(n: int) -> CachedLink:
    return CachedLink(long_url=f"https://example.com/{n}", expires_at=None)


def test_lru_evicts_least_recently_used():
    cache = LinkCache(max_size=2)
    cache.put("a", link(1))
    cache.put("b", link(2))
    cache.get("a")  # "b" is now least recently used
    cache.put("c", link(3))
    assert cache.get("b") is None
    assert cache.get("a") == link(1)
    assert cache.get("c") == link(3)
    assert len(cache) == 2


def test_lru_invalidate_and_clear():
    cache = LinkCache(max_size=3)
    cache.put("a", link(1))
    cache.put("b", link(2))
    cache.invalidate("a")
    cache.invalidate("missing")  # no-op
    assert cache.get("a") is None
    assert cache.get("b") is not None
    cache.clear()
    assert len(cache) == 0


def test_lru_put_overwrites_existing_entry():
    cache = LinkCache(max_size=2)
    cache.put("a", link(1))
    cache.put("a", link(9))
    assert cache.get("a") == link(9)
    assert len(cache) == 1


def test_lru_rejects_invalid_size():
    with pytest.raises(ValueError):
        LinkCache(max_size=0)
