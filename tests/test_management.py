import threading

from sqlalchemy import event, func, select, update

from app.models import URL, Click
from app.services.link_cache import CachedLink, LinkCache
from tests.conftest import register

LONG = "https://example.com/some/long/path"
URLS = "/api/v1/urls"


def make(client, headers, **body):
    resp = client.post(URLS, json={"long_url": LONG, **body}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["code"]


def follow(client, code):
    return client.get(f"/{code}", follow_redirects=False)


def link(n=1) -> CachedLink:
    return CachedLink(url_id=n, long_url=f"https://example.com/{n}", expires_at=None)


# --- authentication ---------------------------------------------------------


def test_management_endpoints_require_api_key(client, auth_headers):
    code = make(client, auth_headers)
    assert client.get(URLS).status_code == 401
    assert client.patch(f"{URLS}/{code}", json={"is_active": False}).status_code == 401
    assert client.delete(f"{URLS}/{code}").status_code == 401
    bad = {"X-API-Key": "nope"}
    assert client.get(URLS, headers=bad).status_code == 401
    assert client.patch(f"{URLS}/{code}", json={"is_active": False}, headers=bad).status_code == 401
    assert client.delete(f"{URLS}/{code}", headers=bad).status_code == 401
    assert follow(client, code).status_code == 302  # untouched


# --- list -------------------------------------------------------------------


def test_list_empty(client, auth_headers):
    body = client.get(URLS, headers=auth_headers).json()
    assert body == {"items": [], "total": 0, "limit": 20, "offset": 0}


def test_list_is_newest_first_and_paginated(client, auth_headers):
    codes = [make(client, auth_headers, custom_alias=f"link-{i}") for i in range(5)]
    newest_first = list(reversed(codes))

    page1 = client.get(URLS, params={"limit": 2}, headers=auth_headers).json()
    assert [i["code"] for i in page1["items"]] == newest_first[:2]
    assert page1["total"] == 5
    assert (page1["limit"], page1["offset"]) == (2, 0)

    page2 = client.get(URLS, params={"limit": 2, "offset": 2}, headers=auth_headers).json()
    assert [i["code"] for i in page2["items"]] == newest_first[2:4]

    page3 = client.get(URLS, params={"limit": 2, "offset": 4}, headers=auth_headers).json()
    assert [i["code"] for i in page3["items"]] == newest_first[4:]

    beyond = client.get(URLS, params={"offset": 99}, headers=auth_headers).json()
    assert beyond["items"] == [] and beyond["total"] == 5


def test_list_item_shape(client, auth_headers):
    code = make(client, auth_headers, ttl_seconds=60)
    item = client.get(URLS, headers=auth_headers).json()["items"][0]
    assert item["code"] == code
    assert item["short_url"] == f"http://testserver/{code}"
    assert item["long_url"] == LONG
    assert item["is_active"] is True
    assert item["expires_at"] and item["created_at"]


def test_list_only_returns_own_urls(client, auth_headers):
    other = register(client)
    mine = make(client, auth_headers)
    make(client, other)
    body = client.get(URLS, headers=auth_headers).json()
    assert [i["code"] for i in body["items"]] == [mine]
    assert body["total"] == 1


def test_list_includes_disabled_links(client, auth_headers):
    code = make(client, auth_headers)
    client.patch(f"{URLS}/{code}", json={"is_active": False}, headers=auth_headers)
    items = client.get(URLS, headers=auth_headers).json()["items"]
    assert [(i["code"], i["is_active"]) for i in items] == [(code, False)]


def test_list_rejects_bad_pagination(client, auth_headers):
    for params in ({"limit": 0}, {"limit": 101}, {"offset": -1}, {"limit": "x"}):
        assert client.get(URLS, params=params, headers=auth_headers).status_code == 422


# --- PATCH ------------------------------------------------------------------


def test_disable_and_enable(client, auth_headers):
    code = make(client, auth_headers)
    assert follow(client, code).status_code == 302

    resp = client.patch(f"{URLS}/{code}", json={"is_active": False}, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False
    assert resp.json()["code"] == code
    assert follow(client, code).status_code == 410

    resp = client.patch(f"{URLS}/{code}", json={"is_active": True}, headers=auth_headers)
    assert resp.json()["is_active"] is True
    assert follow(client, code).status_code == 302


def test_patch_is_idempotent(client, auth_headers):
    code = make(client, auth_headers)
    for _ in range(2):
        resp = client.patch(f"{URLS}/{code}", json={"is_active": True}, headers=auth_headers)
        assert resp.status_code == 200


def test_patch_validates_body(client, auth_headers):
    code = make(client, auth_headers)
    for body in (
        {},
        {"is_active": "yes"},
        {"is_active": 1},
        {"is_active": None},
        {"is_active": False, "long_url": "https://evil.example"},
    ):
        resp = client.patch(f"{URLS}/{code}", json=body, headers=auth_headers)
        assert resp.status_code == 422, body
    assert follow(client, code).status_code == 302


def test_patch_unknown_code_is_404(client, auth_headers):
    resp = client.patch(f"{URLS}/missing", json={"is_active": False}, headers=auth_headers)
    assert resp.status_code == 404


def test_patch_other_users_link_is_404_and_unchanged(client, auth_headers):
    other = register(client)
    code = make(client, auth_headers)
    resp = client.patch(f"{URLS}/{code}", json={"is_active": False}, headers=other)
    assert resp.status_code == 404
    assert follow(client, code).status_code == 302


def test_missing_and_foreign_codes_are_indistinguishable(client, auth_headers):
    other = register(client)
    code = make(client, auth_headers)
    foreign = client.delete(f"{URLS}/{code}", headers=other)
    missing = client.delete(f"{URLS}/does-not-exist", headers=other)
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()


# --- DELETE -----------------------------------------------------------------


def test_delete_removes_link(client, auth_headers):
    code = make(client, auth_headers)
    resp = client.delete(f"{URLS}/{code}", headers=auth_headers)
    assert resp.status_code == 204
    assert resp.content == b""
    assert follow(client, code).status_code == 404
    assert client.get(URLS, headers=auth_headers).json()["total"] == 0


def test_delete_twice_is_404(client, auth_headers):
    code = make(client, auth_headers)
    assert client.delete(f"{URLS}/{code}", headers=auth_headers).status_code == 204
    assert client.delete(f"{URLS}/{code}", headers=auth_headers).status_code == 404


def test_delete_other_users_link_is_404_and_link_survives(client, auth_headers):
    other = register(client)
    code = make(client, auth_headers)
    assert client.delete(f"{URLS}/{code}", headers=other).status_code == 404
    assert follow(client, code).status_code == 302
    assert client.get(URLS, headers=auth_headers).json()["total"] == 1


def test_delete_cascades_to_clicks(client, auth_headers, db):
    code = make(client, auth_headers)
    url = db.scalar(select(URL).where(URL.code == code))
    db.add_all([Click(url_id=url.id, ip_hash="h"), Click(url_id=url.id, ip_hash="h2")])
    db.commit()
    assert db.scalar(select(func.count()).select_from(Click)) == 2

    assert client.delete(f"{URLS}/{code}", headers=auth_headers).status_code == 204
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Click)) == 0


def test_deleted_alias_can_be_reused(client, auth_headers):
    make(client, auth_headers, custom_alias="reuse-me")
    follow(client, "reuse-me")  # warm the cache
    client.delete(f"{URLS}/reuse-me", headers=auth_headers)
    resp = client.post(
        URLS,
        json={"long_url": "https://example.org/new", "custom_alias": "reuse-me"},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    assert follow(client, "reuse-me").headers["location"] == "https://example.org/new"


# --- cache invalidation -----------------------------------------------------


def test_patch_invalidates_cached_link(client, auth_headers, app):
    code = make(client, auth_headers)
    assert follow(client, code).status_code == 302
    assert app.state.link_cache.get(code) is not None

    client.patch(f"{URLS}/{code}", json={"is_active": False}, headers=auth_headers)
    assert app.state.link_cache.get(code) is None
    assert follow(client, code).status_code == 410


def test_delete_invalidates_cached_link(client, auth_headers, app):
    code = make(client, auth_headers)
    follow(client, code)
    assert app.state.link_cache.get(code) is not None

    client.delete(f"{URLS}/{code}", headers=auth_headers)
    assert app.state.link_cache.get(code) is None
    assert follow(client, code).status_code == 404


def test_invalidation_of_one_code_leaves_others_cached(client, auth_headers, app):
    a = make(client, auth_headers)
    b = make(client, auth_headers)
    follow(client, a)
    follow(client, b)
    client.patch(f"{URLS}/{a}", json={"is_active": False}, headers=auth_headers)
    assert app.state.link_cache.get(b) is not None


# --- read-after-invalidation race --------------------------------------------


def test_invalidate_during_load_voids_the_store():
    cache = LinkCache(max_size=4)
    with cache.loading("abc") as ticket:
        cache.invalidate("abc")  # owner changes the link while we are reading
        assert ticket.store(link()) is False
    assert cache.get("abc") is None


def test_store_without_invalidation_succeeds():
    cache = LinkCache(max_size=4)
    with cache.loading("abc") as ticket:
        assert ticket.store(link()) is True
    assert cache.get("abc") == link()


def test_invalidation_only_voids_loads_of_that_code():
    cache = LinkCache(max_size=4)
    with cache.loading("a") as ta, cache.loading("b") as tb:
        cache.invalidate("a")
        assert ta.store(link(1)) is False
        assert tb.store(link(2)) is True


def test_all_concurrent_loads_of_a_code_are_voided():
    cache = LinkCache(max_size=4)
    with cache.loading("a") as t1, cache.loading("a") as t2:
        cache.invalidate("a")
        assert t1.store(link()) is False
        assert t2.store(link()) is False


def test_load_started_after_invalidation_may_store():
    cache = LinkCache(max_size=4)
    cache.invalidate("a")
    with cache.loading("a") as ticket:
        assert ticket.store(link()) is True


def test_clear_voids_all_inflight_loads():
    cache = LinkCache(max_size=4)
    with cache.loading("a") as ta, cache.loading("b") as tb:
        cache.clear()
        assert ta.store(link()) is False
        assert tb.store(link()) is False
    assert len(cache) == 0


def test_no_state_left_behind_after_loads_finish():
    cache = LinkCache(max_size=4)
    with cache.loading("a"):
        cache.invalidate("a")
    with cache.loading("b"):
        pass
    assert cache._inflight == {}


def test_loading_releases_ticket_on_error():
    cache = LinkCache(max_size=4)
    try:
        with cache.loading("a"):
            raise RuntimeError
    except RuntimeError:
        pass
    assert cache._inflight == {}


def test_concurrent_loads_and_invalidations_leave_no_inflight_state():
    cache = LinkCache(max_size=8)

    def reader():
        for _ in range(500):
            with cache.loading("k") as ticket:
                ticket.store(link(1))

    def writer():
        for _ in range(500):
            cache.invalidate("k")

    threads = [threading.Thread(target=reader) for _ in range(3)] + [
        threading.Thread(target=writer)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert cache._inflight == {}


def _disable_during_first_urls_select(app, code, **changes):
    """Simulate the owner committing a change right after a redirect read the old row."""
    fired = []

    def hook(conn, cursor, statement, parameters, context, executemany):
        if fired or "FROM urls" not in statement:
            return
        fired.append(True)
        with app.state.session_factory() as other:
            other.execute(update(URL).where(URL.code == code).values(**changes))
            other.commit()
        app.state.link_cache.invalidate(code)

    event.listen(app.state.engine, "after_cursor_execute", hook)
    return fired


def test_redirect_read_in_flight_cannot_repopulate_after_disable(client, auth_headers, app):
    code = make(client, auth_headers)
    fired = _disable_during_first_urls_select(app, code, is_active=False)

    first = follow(client, code)  # read the old (active) row before the change landed
    assert fired and first.status_code == 302
    assert app.state.link_cache.get(code) is None  # stale value was not cached

    assert follow(client, code).status_code == 410


def test_redirect_read_in_flight_cannot_repopulate_after_delete(client, auth_headers, app):
    code = make(client, auth_headers)
    fired = []

    def hook(conn, cursor, statement, parameters, context, executemany):
        if fired or "FROM urls" not in statement:
            return
        fired.append(True)
        with app.state.session_factory() as other:
            other.execute(URL.__table__.delete().where(URL.code == code))
            other.commit()
        app.state.link_cache.invalidate(code)

    event.listen(app.state.engine, "after_cursor_execute", hook)
    assert follow(client, code).status_code == 302
    assert app.state.link_cache.get(code) is None
    assert follow(client, code).status_code == 404
