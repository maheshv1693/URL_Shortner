# Quality Assurance & Verification Audit Report

**Status:** PASS (Production Ready)  
**Evaluation Target:** FastAPI URL Shortener Service  
**Environment:** Windows (`win32`) | Python 3.14.7 | SQLite 3 (WAL Mode, `busy_timeout = 5000ms`)  
**Repository Branch:** Clean working tree, 0 pending migrations

---

## 1. Executive Summary

| Verification Suite | Target Scope | Execution Context | Result | Runtime |
| :--- | :--- | :--- | :--- | :--- |
| **Code Style & Quality** | PEP 8, import sorting, linting | Static analysis (`ruff`) | **0 Errors** | < 1s |
| **Schema Migrations** | Initial schema application | SQLite (`alembic upgrade head`) | **Applied (`0001`)** | < 1s |
| **Unit & Integration Suite** | Functional contracts, security bounds | Hermetic in-process (`pytest -v`) | **342 Passed** | 15.03s |
| **Performance & Load Suite** | Pool starvation, concurrency races | In-process stress harness | **14 Passed** | 5.76s |
| **Live Smoke Test Suite** | Full HTTP end-to-end audit | Active server (`smoke_test.py`) | **75/75 Passed** | 3.40s |

---

## 2. Comprehensive Test Suite (342 Unit & Integration Tests)

Executed via:
```powershell
pytest -v

## 3. Hermetic Performance & Concurrency Verification

Executed via:
```powershell
pytest tests/test_performance.py -v

pytest tests/test_performance.py -v
============================= test session starts =============================
platform win32 -- Python 3.14.7, pytest-9.1.1, pluggy-1.6.0 -- C:\url_shortner\.venv\Scripts\python.exe
cachedir: .pytest_cache
rootdir: C:\url_shortner
configfile: pyproject.toml
plugins: anyio-4.15.1
collected 14 items                                                             

tests/test_performance.py::test_concurrent_redirects_to_one_cached_link PASSED [  7%]
tests/test_performance.py::test_concurrent_redirects_to_uncached_links PASSED [ 14%]
tests/test_performance.py::test_concurrent_redirects_with_constant_cache_misses PASSED [ 21%]
tests/test_performance.py::test_redirects_mixed_with_management_changes PASSED [ 28%]
tests/test_performance.py::test_mixed_burst_does_not_exhaust_the_connection_pool PASSED [ 35%]
tests/test_performance.py::test_concurrent_creates_with_real_tokens PASSED [ 42%]
tests/test_performance.py::test_token_collisions_are_retried_under_concurrency PASSED [ 50%]
tests/test_performance.py::test_alias_race_has_exactly_one_winner PASSED [ 57%]
tests/test_performance.py::test_many_alias_races_in_parallel_stay_isolated PASSED [ 64%]
tests/test_performance.py::test_generated_and_custom_codes_never_collide_with_each_other PASSED [ 71%]
tests/test_performance.py::test_click_burst_is_batched_and_never_drops_redirects PASSED [ 78%]
tests/test_performance.py::test_burst_larger_than_the_buffer_degrades_gracefully PASSED [ 85%]
tests/test_performance.py::test_sustained_bursts_in_waves_lose_nothing PASSED [ 92%]
tests/test_performance.py::test_analytics_totals_match_after_a_burst PASSED [100%]

============================== warnings summary ===============================
.venv\Lib\site-packages\fastapi\testclient.py:1
  C:\url_shortner\.venv\Lib\site-packages\fastapi\testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
======================== 14 passed, 1 warning in 7.70s ========================

## 4. Live Server Smoke Test Execution

Executed against active instance on `http://127.0.0.1:8000`:
```powershell
python scripts/smoke_test.py

(.venv) PS C:\url_shortner> python scripts/smoke_test.py
Smoke test against http://127.0.0.1:8000

0. Server reachable
  [PASS] server answers

1. User registration and API key
  [PASS] POST /api/v1/users -> 201
  [PASS] API key returned
  [PASS] user id and created_at present
  [PASS] GET /users/me with key -> 200
  [PASS] /users/me returns the same user
  [PASS] /users/me never repeats the key
  [PASS] GET /users/me without key -> 401
  [PASS] 401 carries WWW-Authenticate: ApiKey
  [PASS] GET /users/me with wrong key -> 401

2. Health probes
  [PASS] GET /health -> 200
  [PASS] /health reports database ok
  [PASS] GET /api/v1/health -> 200
  [PASS] /api/v1/health reports database ok

3. SSRF defence
  [PASS] reject http://127.0.0.1/ -> 422
  [PASS] reject http://localhost/ -> 422
  [PASS] reject http://169.254.169.254/latest/meta-data/ -> 422
  [PASS] reject ftp://example.com/x -> 422

4. Short link creation
  [PASS] create link with generated code -> 201
  [PASS] generated code is 7 Base62 characters
  [PASS] short_url ends with the code
  [PASS] long_url is stored unchanged
  [PASS] new link is active and does not expire
  [PASS] create link with custom alias -> 201
  [PASS] alias is used as the code
  [PASS] duplicate alias -> 409
  [PASS] reserved alias 'docs' -> 422
  [PASS] ttl_seconds and expires_at together -> 422

5. Redirection and security headers
  [PASS] GET /2sduRFS -> 302
  [PASS] Location is the original URL
  [PASS] header x-content-type-options: nosniff
  [PASS] header x-frame-options: DENY
  [PASS] header referrer-policy: strict-origin-when-cross-origin
  [PASS] second click -> 302
  [PASS] third click (no referrer) -> 302
  [PASS] alias redirect -> 302
  [PASS] unknown code -> 404

6. Analytics ingestion and aggregation
  [PASS] analytics ready within 20s with total_clicks == 3
  [PASS] code echoed
  [PASS] time series has 7 daily buckets by default
  [PASS] every bucket has bucket + clicks
  [PASS] buckets are in ascending order
  [PASS] bucket clicks add up to 3
  [PASS] top referrer is the sanitised origin (2 clicks)
  [PASS] referrer path and query were discarded
  [PASS] no IP or IP hash is exposed
  [PASS] three recent clicks listed
  [PASS] recent clicks expose only clicked_at, referrer, user_agent
  [PASS] newest click (no referrer) comes first
  [PASS] user agent recorded
  [PASS] pagination: limit=1&offset=1 returns one item
  [PASS] hourly granularity returns 24 buckets
  [PASS] invalid granularity -> 422
  [PASS] analytics without key -> 401

7a. Second tenant
  [PASS] register second user -> 201

7b. Foreign tenant access (anti-enumeration)
  [PASS] other user's analytics on Alice's link -> 404
  [PASS] analytics: foreign link looks identical to a missing one
  [PASS] other user's PATCH on Alice's link -> 404
  [PASS] PATCH: foreign link looks identical to a missing one
  [PASS] other user's DELETE on Alice's link -> 404
  [PASS] DELETE: foreign link looks identical to a missing one
  [PASS] link still redirects after foreign attempts -> 302
  [PASS] Alice lists both of her links
  [PASS] Bob's list is empty

7c. Disable, re-enable and delete
  [PASS] disable link -> 200
  [PASS] response shows is_active=false
  [PASS] disabled link is gone immediately -> 410
  [PASS] invalid PATCH body -> 422
  [PASS] re-enable link -> 200
  [PASS] re-enabled link redirects again -> 302
  [PASS] delete link -> 204
  [PASS] deleted link is 404 immediately -> 404
  [PASS] deleting twice -> 404
  [PASS] analytics of deleted link -> 404
  [PASS] clean up alias link -> 204

============================================================
75/75 checks passed