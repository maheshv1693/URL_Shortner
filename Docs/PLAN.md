# URL Shortener Prototype - Plan

**Status:** URL-shortener Tasks 1–12 and Phases 1–5 are complete. The separate requirement-to-engineering proposal CLI is documented in [REQUIREMENT_WORKFLOW.md](./REQUIREMENT_WORKFLOW.md).

**Stack:** FastAPI, SQLite (SQLAlchemy 2 + Alembic), pytest, httpx, Docker. Redis is not used in the prototype.

## Confirmed decisions

- Authentication: one API key per user (`X-API-Key`).
- Redirect status: 302 (every click is counted; expiry and disable take effect immediately).
- Expiry: TTL in seconds or absolute datetime; expired links return 410 Gone.
- Analytics: access time, hashed IP, user agent, referrer.
- Link safety: scheme / private-IP / localhost checks plus a local domain blocklist.

## Requirements covered

**Functional:** FR1 create short URL, FR2 redirect, FR3 custom alias, FR4 expiration, FR5 delete/disable, FR6 analytics.
**Non-functional:** NFR1 low latency, NFR2 scalability, NFR3 availability, NFR4 reliability, NFR5 uniqueness, NFR6 security.

## Engineering decisions (this revision)

### 1. Task 4 - Token generation

- Replaces the earlier "auto-increment ID + salted shuffle" design. Auto-increment IDs are not used for codes.
- Tokens are 7 characters of Base62 (`A-Z`, `a-z`, `0-9`), generated in memory with `secrets.choice` (about 3.5 trillion combinations).
- Collisions are handled by a retry loop backed by the unique constraint on `urls.code`:
  1. Generate a token and attempt the insert.
  2. On a unique-constraint violation, generate a new token and retry.
  3. Maximum 3 attempts; after that, return a 503 error (the client may retry) and log it.
- No pre-check `SELECT`; the database unique constraint is the source of truth, which keeps this safe under concurrent requests.
- Custom aliases skip generation and return 409 if taken. Reserved words (`api`, `docs`, `health`, etc.) are rejected as aliases.

### 2. Task 8 - Analytics

- **Revised in Phase 4:** this supersedes the earlier "`BackgroundTasks`, no in-memory buffering" decision. Clicks are now queued in a bounded in-memory buffer and written to SQLite in batches by one worker thread. Batching keeps write-lock churn low during click spikes, and the buffer is flushed on graceful shutdown.
- The redirect handler returns the 302 immediately; it only appends a click event to the buffer (no database access, no blocking). The raw visitor IP is hashed before the event is queued.
- Batches are written in one transaction on the worker's own DB session. Busy or locked errors are retried with backoff; after the last attempt the batch is dropped and logged. A failing write never affects the redirect.
- A FastAPI lifespan handler starts the worker on startup and, on shutdown, writes everything still buffered before the process exits.
- SQLite engine configuration (applied on every new connection through a SQLAlchemy `connect` event):
  - `PRAGMA journal_mode=WAL` so readers (redirects) are not blocked by writers (click inserts).
  - `PRAGMA busy_timeout=5000` (5000 ms) so writers wait for the lock instead of failing immediately. Configurable through settings, default 5000.
  - Also `PRAGMA foreign_keys=ON` and `PRAGMA synchronous=NORMAL` (the usual pairing with WAL).
- Owners read analytics with `GET /api/v1/urls/{code}/analytics` (total clicks, zero-filled hourly or daily time series, top referrers, paginated recent clicks).

### 3. API contracts

- All endpoints are under the `/api/v1/` prefix.
- The public redirect stays at `GET /{code}` (no prefix).

| Method | Path | Purpose | Auth |
|--------|------|---------|------|
| POST | `/api/v1/users` | Create user, return API key once | None |
| GET | `/api/v1/users/me` | Current user | API key |
| POST | `/api/v1/urls` | Create short URL (optional alias, TTL or expiry) | API key |
| GET | `/api/v1/urls` | List my URLs | API key |
| PATCH | `/api/v1/urls/{code}` | Enable or disable | API key |
| DELETE | `/api/v1/urls/{code}` | Delete | API key |
| GET | `/api/v1/urls/{code}/analytics` | Click analytics (totals, time series, referrers, recent clicks) | API key |
| GET | `/api/v1/health` and `/health` | Health check with database probe (503 if the database is unreachable) | None |
| GET | `/{code}` | Redirect (302, 404 unknown, 410 expired or disabled) | None |

Because `/{code}` is a catch-all, the redirect route is registered after the `/api/v1` routers.

## Tasks

| # | Task | Status | Subtasks | Covers |
|---|------|--------|----------|--------|
| 1 | Project setup | Done | `pyproject.toml`, `app/` layout (api, core, models, services), settings via env vars, ruff and pytest, Dockerfile | Foundation |
| 2 | Data model | Done | `users` (id, api_key_hash, created_at); `urls` (id, code UNIQUE, long_url, owner_id, created_at, expires_at, is_active); `clicks` (url_id, accessed_at, ip_hash, user_agent, referrer); indexes; Alembic migration; SQLite engine config (WAL, busy_timeout 5000 ms) | FR1-6, NFR4 |
| 3 | Auth | Done | `POST /api/v1/users` issues an API key (shown once, stored hashed); `X-API-Key` dependency | FR5, NFR6 |
| 4 | Token generator | Done | 7-char Base62 via `secrets.choice`; insert with 3-attempt retry on unique collision; 503 after exhaustion; reserved words rejected for aliases | FR1, NFR5 |
| 5 | Create endpoint | Done | `POST /api/v1/urls` (long_url, optional alias, optional ttl_seconds or expires_at); alias rules: 3-32 chars, `[A-Za-z0-9_-]`, 409 if taken | FR1, 3, 4 |
| 6 | Redirect endpoint | Done | `GET /{code}` returns 302; 404 unknown; 410 expired or disabled; `expires_at` checked at request time; indexed single lookup; small in-process LRU cache with invalidation on change | FR2, NFR1, 4 |
| 7 | Management endpoints | Done | List my URLs; `PATCH /api/v1/urls/{code}` to enable/disable; `DELETE /api/v1/urls/{code}`; owner-only (404 for others' links); mitigate redirect cache invalidation race | FR5 |
| 8 | Analytics | Done | Click ingestion through a bounded in-memory queue and batch writer (retries on busy DB, drops on exhaustion); WAL and `busy_timeout=5000` config; `GET /api/v1/urls/{code}/analytics` | FR6, NFR1 |
| 9 | Security | Done | Allow only http/https; max URL length; block localhost, private and link-local IPs (SSRF) after DNS resolution; domain blocklist file; block self-referencing links; in-memory rate limit per IP and per key (token bucket) on create and redirect; security headers; CORS config | NFR6 |
| 10 | Reliability and availability | Done | Transactional creates, DB constraints, `/api/v1/health` and `/health` endpoints (`SELECT 1` probe), lifespan shutdown that flushes buffered clicks, structured logging | NFR3, 4 |
| 11 | Tests | Done | Unit tests (generator, retry, validators); API tests (create, alias conflict, expiry, disable, delete, ownership, rate limit); concurrency harness in `tests/test_performance.py`: 100-300 concurrent redirects (cached, uncached, constant cache misses), concurrent creates with forced token collisions, alias races, click batching and buffer overflow under burst, connection-pool exhaustion regression. An in-process ASGI harness replaces the originally planned Locust/k6 run; external tools can still target `GET /{code}` | All |
| 12 | Docs | Done | `README.md` (requirements, setup, migrations, running, tests and ruff, configuration table, API examples, scaling path, known limits); `scripts/smoke_test.py`, a standard-library end-to-end script for a running server | NFR2 |

## Updated task sequence

Phased so each phase ends with something runnable:

| Phase | Tasks | Status | Outcome |
|-------|-------|--------|---------|
| 1 | 1 -> 2 -> 3 | Complete | Foundation, data models and migrations, API key auth |
| 2 | 4 -> 5 -> 6 | Complete | Create short URLs and redirect (working core loop) |
| 3 | 7 -> 9 | Complete | Manage URLs (disable/delete) and security hardening |
| 4 | 8 -> 10 | Complete | Analytics and reliability |
| 5 | 11 -> 12 | Complete | Full test suite, concurrency verification, smoke script, README \|

Flat order: 1 -> 2 -> 3 -> 4 -> 5 -> 6 -> 7 -> 9 -> 8 -> 10 -> 11 -> 12

### Phase rationale (architectural grouping)

- **Phase 1 (Tasks 1 -> 2 -> 3): Foundation & Authentication Gate**
  - *Rationale:* Tightly coupled infrastructure dependency chain. Data models and SQLite engine pragmas (Task 2) require environment settings (Task 1). User registration and API key hashing (Task 3) require database models. Delivers the minimal runnable state to test server boot, migrations, and auth validation via `X-API-Key`.

- **Phase 2 (Tasks 4 -> 5 -> 6): Core Value Loop (Create & Redirect)**
  - *Rationale:* Delivers the primary functional MVP. Links CSPRNG Base62 token generation directly to the write route (`POST /api/v1/urls`) and the read/redirect route (`GET /{code}`). Completes the core user journey before adding management or secondary telemetry.

- **Phase 3 (Tasks 7 -> 9): Management & Edge Defense Hardening**
  - *Rationale:* Builds lifecycle ownership (disable/delete) and locks down the application before telemetry. Adding SSRF defenses, URL sanitization, and token-bucket rate limiting here ensures edge attacks are neutralized before asynchronous background tasks are introduced.

- **Phase 4 (Tasks 8 -> 10): Telemetry & Operational Reliability**
  - *Rationale:* Decouples non-critical background click ingestion (in-memory queue with a batch writer, flushed on shutdown) and health monitoring from the critical path, testing SQLite concurrency without risking the core redirect engine.

- **Phase 5 (Tasks 11 -> 12): Verification & Production Documentation**
  - *Rationale:* End-to-end integration, concurrency collision stress tests, and deployment packaging.

## Known prototype limits (consequences of SQLite only)

- **Scale:** SQLite handles reads well, but writes are limited to a single node. Millions of URLs are fine. Very high read traffic needs Postgres, Redis and read replicas. Storage and cache will sit behind interfaces so the swap is easy.
- **Analytics durability:** clicks wait in an in-memory buffer (about one second by default) before being written. A crash or kill signal that skips graceful shutdown loses the buffered clicks, and clicks are dropped if the buffer is full or the database stays locked. This is accepted for the prototype.
- **Rate limiting and cache:** in-process, so per instance and not shared across replicas.
- **Availability:** a single instance means no real high availability yet.
