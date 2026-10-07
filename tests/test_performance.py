"""Concurrency and burst-traffic verification.

Requests go through httpx's ASGI transport inside one event loop; the sync route handlers run
in AnyIO's worker threads, so database access is genuinely concurrent against a file-backed
SQLite database (WAL), as in production.
"""

import asyncio
import itertools
import logging
import threading
import time
from collections import Counter
from contextlib import asynccontextmanager

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import URL, Click, User
from app.services import token_service
from app.services.analytics_service import ClickRecorder

LONG = "https://example.com/performance"
URLS = "/api/v1/urls"


@pytest.fixture
def perf_app(app_factory):
    """App tuned for load: rate limits off (all traffic comes from one client IP)."""
    return app_factory(
        rate_limit_enabled=False,
        analytics_batch_size=50,
        analytics_flush_interval_seconds=0.05,
    )


@asynccontextmanager
async def served(app):
    """HTTP client for `app` with the click recorder running (ASGITransport skips lifespan)."""
    app.state.click_recorder.start()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


async def register(http) -> dict[str, str]:
    resp = await http.post("/api/v1/users")
    return {"X-API-Key": resp.json()["api_key"]}


def seed_links(app, count: int) -> list[tuple[str, str]]:
    """Insert `count` links directly; returns [(code, long_url)]."""
    with Session(app.state.engine) as session:
        owner = User(api_key_hash="perf-owner")
        session.add(owner)
        session.flush()
        links = [(f"perf{i:04d}", f"https://example.com/target/{i}") for i in range(count)]
        session.add_all(URL(code=c, long_url=u, owner_id=owner.id) for c, u in links)
        session.commit()
    return links


def count_rows(app, model) -> int:
    with Session(app.state.engine) as session:
        return session.scalar(select(func.count()).select_from(model))


def assert_no_lock_errors(caplog, recorder) -> None:
    messages = " ".join(r.getMessage().lower() for r in caplog.records)
    assert "database is locked" not in messages
    assert "database table is locked" not in messages
    assert recorder.stats()["failed"] == 0


# --- concurrent redirects ------------------------------------------------------------------


def test_concurrent_redirects_to_one_cached_link(perf_app, caplog):
    [(code, target)] = seed_links(perf_app, 1)
    total = 300

    async def scenario():
        async with served(perf_app) as http:
            assert (await http.get(f"/{code}", follow_redirects=False)).status_code == 302
            assert perf_app.state.link_cache.get(code) is not None  # warmed
            return await asyncio.gather(
                *(http.get(f"/{code}", follow_redirects=False) for _ in range(total))
            )

    with caplog.at_level(logging.WARNING):
        responses = asyncio.run(scenario())

    assert Counter(r.status_code for r in responses) == {302: total}
    assert {r.headers["location"] for r in responses} == {target}
    assert perf_app.state.click_recorder.shutdown(timeout=15)
    assert_no_lock_errors(caplog, perf_app.state.click_recorder)
    assert count_rows(perf_app, Click) == total + 1


def test_concurrent_redirects_to_uncached_links(perf_app, caplog):
    links = seed_links(perf_app, 150)  # every first request is a cache miss -> database read

    async def scenario():
        async with served(perf_app) as http:
            assert len(perf_app.state.link_cache) == 0
            return await asyncio.gather(
                *(http.get(f"/{code}", follow_redirects=False) for code, _ in links)
            )

    with caplog.at_level(logging.WARNING):
        responses = asyncio.run(scenario())

    assert Counter(r.status_code for r in responses) == {302: len(links)}
    for (code, target), response in zip(links, responses, strict=True):
        assert response.headers["location"] == target, code  # each code maps to its own target
    assert perf_app.state.click_recorder.shutdown(timeout=15)
    assert_no_lock_errors(caplog, perf_app.state.click_recorder)
    assert count_rows(perf_app, Click) == len(links)


def test_concurrent_redirects_with_constant_cache_misses(app_factory, caplog):
    app = app_factory(
        rate_limit_enabled=False,
        redirect_cache_size=1,  # a one-entry cache evicts on almost every request
        analytics_batch_size=50,
        analytics_flush_interval_seconds=0.05,
    )
    links = seed_links(app, 8)
    requests = [links[i % len(links)] for i in range(240)]

    async def scenario():
        async with served(app) as http:
            return await asyncio.gather(
                *(http.get(f"/{code}", follow_redirects=False) for code, _ in requests)
            )

    with caplog.at_level(logging.WARNING):
        responses = asyncio.run(scenario())

    assert Counter(r.status_code for r in responses) == {302: len(requests)}
    for (code, target), response in zip(requests, responses, strict=True):
        assert response.headers["location"] == target, code
    assert app.state.click_recorder.shutdown(timeout=15)
    assert_no_lock_errors(caplog, app.state.click_recorder)
    assert count_rows(app, Click) == len(requests)


def test_redirects_mixed_with_management_changes(perf_app, caplog):
    """Readers and writers at once: disabling links mid-burst never yields a server error."""
    links = seed_links(perf_app, 20)

    async def scenario():
        async with served(perf_app) as http:
            headers = await register(http)
            with Session(perf_app.state.engine) as session:
                owner = session.scalar(select(User).where(User.api_key_hash != "perf-owner"))
                for code, _ in links:
                    session.execute(
                        URL.__table__.update().where(URL.code == code).values(owner_id=owner.id)
                    )
                session.commit()
            reads = [http.get(f"/{code}", follow_redirects=False) for code, _ in links * 5]
            writes = [
                http.patch(f"{URLS}/{code}", json={"is_active": False}, headers=headers)
                for code, _ in links
            ]
            return await asyncio.gather(*reads, *writes)

    with caplog.at_level(logging.WARNING):
        responses = asyncio.run(scenario())

    statuses = Counter(r.status_code for r in responses)
    assert set(statuses) <= {302, 410, 200}  # redirect before/after the change, or PATCH ok
    assert statuses[200] == len(links)
    assert perf_app.state.click_recorder.shutdown(timeout=15)
    assert_no_lock_errors(caplog, perf_app.state.click_recorder)

    # after the dust settles every link is consistently disabled
    async def after():
        async with served(perf_app) as http:
            return [(await http.get(f"/{c}", follow_redirects=False)).status_code for c, _ in links]

    assert set(asyncio.run(after())) == {410}


def test_mixed_burst_does_not_exhaust_the_connection_pool(app_factory):
    """Regression: connections held across worker-thread hops used to deadlock the pool.

    The pool allows 15 connections but AnyIO runs up to 40 handler threads. A burst of
    authenticated and uncached requests used to stall until the 30 s pool timeout and then
    fail with 500s. Covers every read path: auth lookup, list, analytics and redirect misses.
    """
    app = app_factory(rate_limit_enabled=False, redirect_cache_size=1)
    links = seed_links(app, 5)
    per_kind = 120

    async def scenario():
        async with served(app) as http:
            headers = await register(http)
            code = (await http.post(URLS, json={"long_url": LONG}, headers=headers)).json()["code"]
            jobs = []
            for i in range(per_kind):
                jobs.append(http.get(URLS, headers=headers))
                jobs.append(http.get(f"{URLS}/{code}/analytics", headers=headers))
                jobs.append(http.get(f"/{links[i % 5][0]}", follow_redirects=False))
            return await asyncio.gather(*jobs)

    started = time.monotonic()
    responses = asyncio.run(scenario())

    assert Counter(r.status_code for r in responses) == {200: per_kind * 2, 302: per_kind}
    assert time.monotonic() - started < 15  # far below the 30 s pool timeout
    assert app.state.click_recorder.shutdown(timeout=15)
    assert app.state.engine.pool.checkedout() == 0  # nothing leaked


# --- concurrent creation ---------------------------------------------------------------------


def test_concurrent_creates_with_real_tokens(perf_app):
    total = 150

    async def scenario():
        async with served(perf_app) as http:
            headers = await register(http)
            return await asyncio.gather(
                *(
                    http.post(URLS, json={"long_url": f"{LONG}/{i}"}, headers=headers)
                    for i in range(total)
                )
            )

    responses = asyncio.run(scenario())

    assert Counter(r.status_code for r in responses) == {201: total}
    codes = [r.json()["code"] for r in responses]
    assert len(set(codes)) == total
    assert all(len(c) == 7 and c.isalnum() for c in codes)
    assert count_rows(perf_app, URL) == total
    # every code still maps to the URL it was created for
    with Session(perf_app.state.engine) as session:
        stored = dict(session.execute(select(URL.code, URL.long_url)).all())
    for i, response in enumerate(responses):
        assert stored[response.json()["code"]] == f"{LONG}/{i}"


def test_token_collisions_are_retried_under_concurrency(perf_app, monkeypatch):
    """Force frequent collisions: every token is handed out twice, to different requests."""
    counter = itertools.count()
    lock = threading.Lock()
    draws = []

    def paired_tokens(length: int = 7) -> str:
        with lock:
            n = next(counter)
            draws.append(n)
        return f"P{n // 2:06d}"

    monkeypatch.setattr(token_service, "generate_token", paired_tokens)
    total = 100

    async def scenario():
        async with served(perf_app) as http:
            headers = await register(http)
            return await asyncio.gather(
                *(
                    http.post(URLS, json={"long_url": f"{LONG}/{i}"}, headers=headers)
                    for i in range(total)
                )
            )

    responses = asyncio.run(scenario())
    statuses = Counter(r.status_code for r in responses)

    assert set(statuses) <= {201, 503}  # a client may be told to retry, never a 500
    # Which requests exhaust their 3 attempts depends on thread scheduling, so only require
    # that retries let most requests through.
    assert statuses[201] >= total * 0.5
    created = [r.json()["code"] for r in responses if r.status_code == 201]
    assert len(set(created)) == len(created)  # no duplicate codes were ever issued
    assert count_rows(perf_app, URL) == len(created)
    assert len(draws) > total  # collisions really happened and were retried


def test_alias_race_has_exactly_one_winner(perf_app):
    racers = 40

    async def scenario():
        async with served(perf_app) as http:
            users = [await register(http) for _ in range(racers)]
            return await asyncio.gather(
                *(
                    http.post(
                        URLS,
                        json={"long_url": f"{LONG}/{i}", "custom_alias": "the-one"},
                        headers=users[i],
                    )
                    for i in range(racers)
                )
            )

    responses = asyncio.run(scenario())

    assert Counter(r.status_code for r in responses) == {201: 1, 409: racers - 1}
    winner = next(i for i, r in enumerate(responses) if r.status_code == 201)
    with Session(perf_app.state.engine) as session:
        rows = session.execute(select(URL.long_url).where(URL.code == "the-one")).all()
    assert rows == [(f"{LONG}/{winner}",)]  # the stored target is the winner's, not a mix


def test_many_alias_races_in_parallel_stay_isolated(perf_app):
    aliases, per_alias = 15, 6

    async def scenario():
        async with served(perf_app) as http:
            headers = await register(http)
            jobs = [
                http.post(
                    URLS,
                    json={"long_url": f"{LONG}/{a}/{k}", "custom_alias": f"alias-{a}"},
                    headers=headers,
                )
                for a in range(aliases)
                for k in range(per_alias)
            ]
            return await asyncio.gather(*jobs)

    responses = asyncio.run(scenario())

    assert Counter(r.status_code for r in responses) == {
        201: aliases,
        409: aliases * (per_alias - 1),
    }
    assert sorted(r.json()["code"] for r in responses if r.status_code == 201) == sorted(
        f"alias-{a}" for a in range(aliases)
    )
    assert count_rows(perf_app, URL) == aliases


def test_generated_and_custom_codes_never_collide_with_each_other(perf_app):
    async def scenario():
        async with served(perf_app) as http:
            headers = await register(http)
            jobs = [
                http.post(URLS, json={"long_url": f"{LONG}/g{i}"}, headers=headers)
                for i in range(60)
            ]
            jobs += [
                http.post(
                    URLS,
                    json={"long_url": f"{LONG}/c{i}", "custom_alias": f"custom{i}"},
                    headers=headers,
                )
                for i in range(60)
            ]
            return await asyncio.gather(*jobs)

    responses = asyncio.run(scenario())
    assert Counter(r.status_code for r in responses) == {201: 120}
    assert len({r.json()["code"] for r in responses}) == 120


# --- burst traffic and click batching --------------------------------------------------------


def test_click_burst_is_batched_and_never_drops_redirects(perf_app, caplog, monkeypatch):
    links = seed_links(perf_app, 5)
    total = 600
    batch_sizes: list[int] = []
    original = ClickRecorder._insert

    def spy(self, batch):
        batch_sizes.append(len(batch))
        return original(self, batch)

    monkeypatch.setattr(ClickRecorder, "_insert", spy)

    async def scenario():
        async with served(perf_app) as http:
            return await asyncio.gather(
                *(http.get(f"/{links[i % 5][0]}", follow_redirects=False) for i in range(total))
            )

    with caplog.at_level(logging.WARNING):
        responses = asyncio.run(scenario())

    assert Counter(r.status_code for r in responses) == {302: total}
    recorder = perf_app.state.click_recorder
    assert recorder.shutdown(timeout=20)
    stats = recorder.stats()
    assert stats["written"] == total
    assert stats["dropped"] == 0 and stats["failed"] == 0 and stats["skipped"] == 0
    assert_no_lock_errors(caplog, recorder)
    assert count_rows(perf_app, Click) == total

    assert sum(batch_sizes) == total
    assert max(batch_sizes) <= 50  # configured batch size is respected
    assert len(batch_sizes) <= total / 5  # at least ~5 clicks per write on average
    with Session(perf_app.state.engine) as session:
        per_link = Counter(session.scalars(select(Click.url_id)).all())
    assert sorted(per_link.values()) == [total // 5] * 5  # nothing mis-attributed


def test_burst_larger_than_the_buffer_degrades_gracefully(app_factory):
    app = app_factory(
        rate_limit_enabled=False,
        analytics_queue_max=20,
        analytics_batch_size=1000,  # larger than the buffer, so nothing drains mid-burst
        analytics_flush_interval_seconds=60,  # nothing drains during the burst
    )
    [(code, target)] = seed_links(app, 1)
    total = 100

    async def scenario():
        async with served(app) as http:
            return await asyncio.gather(
                *(http.get(f"/{code}", follow_redirects=False) for _ in range(total))
            )

    responses = asyncio.run(scenario())

    assert Counter(r.status_code for r in responses) == {302: total}  # redirects unaffected
    assert {r.headers["location"] for r in responses} == {target}
    recorder = app.state.click_recorder
    assert recorder.shutdown(timeout=15)
    stats = recorder.stats()
    assert stats["written"] + stats["dropped"] == total  # every click accounted for
    assert stats["written"] == 20 and stats["dropped"] == total - 20
    assert count_rows(app, Click) == stats["written"]


def test_sustained_bursts_in_waves_lose_nothing(perf_app, caplog):
    links = seed_links(perf_app, 3)
    waves, per_wave = 5, 100

    async def scenario():
        async with served(perf_app) as http:
            statuses = []
            for _ in range(waves):
                batch = await asyncio.gather(
                    *(
                        http.get(f"/{links[i % 3][0]}", follow_redirects=False)
                        for i in range(per_wave)
                    )
                )
                statuses += [r.status_code for r in batch]
                await asyncio.sleep(0.1)
            return statuses

    with caplog.at_level(logging.WARNING):
        statuses = asyncio.run(scenario())

    assert Counter(statuses) == {302: waves * per_wave}
    assert perf_app.state.click_recorder.shutdown(timeout=15)
    assert_no_lock_errors(caplog, perf_app.state.click_recorder)
    assert count_rows(perf_app, Click) == waves * per_wave


def test_analytics_totals_match_after_a_burst(perf_app):
    total = 120

    async def scenario():
        async with served(perf_app) as http:
            headers = await register(http)
            code = (await http.post(URLS, json={"long_url": LONG}, headers=headers)).json()["code"]
            await asyncio.gather(
                *(
                    http.get(
                        f"/{code}",
                        follow_redirects=False,
                        headers={"Referer": "https://r.example/p?x=1"},
                    )
                    for _ in range(total)
                )
            )
            assert await asyncio.to_thread(perf_app.state.click_recorder.flush, 15)
            return (await http.get(f"{URLS}/{code}/analytics", headers=headers)).json()

    body = asyncio.run(scenario())
    perf_app.state.click_recorder.shutdown(timeout=15)

    assert body["total_clicks"] == total
    assert sum(b["clicks"] for b in body["time_series"]) == total
    assert body["top_referrers"] == [{"referrer": "https://r.example", "clicks": total}]
    assert len(body["recent_clicks"]["items"]) == 20
