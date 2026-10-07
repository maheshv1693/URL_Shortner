# URL Shortener - Technical Architecture & Engineering Spec

**Status:** Draft for review. Describes *how* the system is built. For product behaviour and requirements, see [SPEC.md](./SPEC.md). For tasks and sequencing, see [PLAN.md](./PLAN.md). No code exists yet.

## 1. Stack

| Concern | Choice |
|---------|--------|
| Language and framework | Python, FastAPI |
| Storage | SQLite (WAL mode), accessed with SQLAlchemy 2 |
| Migrations | Alembic |
| Testing | pytest, httpx (FastAPI test client) |
| Lint and format | ruff |
| Packaging | `pyproject.toml`, Docker |
| Not used in the prototype | Redis, Postgres, message queues |

## 2. Architecture overview

Single stateless-looking FastAPI process with an embedded SQLite file.

```
Client --> FastAPI
            |- /api/v1/*  routers --> services --> SQLAlchemy --> SQLite (WAL)
            |- GET /{code} redirect --> in-process LRU cache --> SQLite
            |      `-- BackgroundTasks --> click insert (own DB session)
```

Layers:
- **api:** routers, request and response schemas, dependencies (DB session, current user, rate limit).
- **services:** token generation, URL creation, redirect resolution, analytics, link validation.
- **models:** SQLAlchemy models.
- **core:** settings, security helpers (API key hashing).

The redirect route is registered after the `/api/v1` routers because `/{code}` is a catch-all.

## 3. Configuration

Settings come from environment variables (prefix `URLSHORT_`), loaded through pydantic-settings.

| Setting | Default | Purpose |
|---------|---------|---------|
| `database_url` | `sqlite:///./urlshortener.db` | DB location |
| `sqlite_busy_timeout_ms` | `5000` | SQLite lock wait |
| `api_key_pepper` | placeholder | Secret for keyed API key hashing; must be changed in real use |
| `api_v1_prefix` | `/api/v1` | API prefix |

## 4. Data model

| Table | Columns | Constraints and indexes |
|-------|---------|-------------------------|
| `users` | id, api_key_hash, created_at | `api_key_hash` unique |
| `urls` | id, code, long_url, owner_id, created_at, expires_at (nullable), is_active | `code` unique; FK `owner_id` -> users (cascade delete); index (owner_id, created_at) |
| `clicks` | id, url_id, accessed_at, ip_hash, user_agent, referrer | FK `url_id` -> urls (cascade delete); index (url_id, accessed_at) |

Notes:
- Datetimes are timezone-aware and stored as UTC.
- Internal integer primary keys are used for relations only. They are not used to derive public codes.
- Schema changes go through Alembic migrations; a test verifies migrations match the models.

## 5. SQLite engine configuration

Applied to every new connection through a SQLAlchemy `connect` event:

| Pragma | Value | Reason |
|--------|-------|--------|
| `journal_mode` | `WAL` | Readers (redirects) are not blocked by writers (click inserts) |
| `busy_timeout` | `5000` ms | Writers wait for the lock instead of failing immediately |
| `foreign_keys` | `ON` | Enforce FKs and cascades (off by default in SQLite) |
| `synchronous` | `NORMAL` | Standard pairing with WAL |

## 6. Token generation (Task 4)

- 7 characters from Base62, picked with `secrets.choice` in memory (about 3.5 trillion combinations).
- No auto-increment based codes and no salted shuffle.
- Algorithm:
  1. Generate a token and insert the row.
  2. On a unique-constraint violation, regenerate and retry.
  3. Maximum 3 attempts; then raise a service error that maps to 503 and log it.
- No pre-check `SELECT`; the unique constraint is the source of truth, which is safe under concurrent requests.
- Custom aliases skip generation. A unique violation maps to 409. Reserved words are rejected before the insert.

## 7. Authentication design

- API key: `secrets.token_urlsafe(32)`, returned once on user creation.
- Stored as HMAC-SHA256 of the key using the configured pepper; the plain key is never stored.
- Lookup is by the hash on an indexed unique column.
- A FastAPI dependency reads `X-API-Key`, hashes it, loads the user, and returns 401 with `WWW-Authenticate: ApiKey` on failure.

## 8. Redirect path (Task 6)

1. Look up `code` in a small in-process LRU cache; on a miss, query the indexed `urls.code`.
2. Evaluate state at request time: missing -> 404; disabled or `expires_at <= now` -> 410; else 302.
3. Schedule the click recording with `BackgroundTasks` and return immediately.
4. Cache entries are invalidated on disable, enable or delete in the same process.

## 9. Analytics (Task 8)

- Clicks are recorded using FastAPI `BackgroundTasks`. There is no in-memory batching.
- The background function opens its own DB session, inserts one `clicks` row and commits.
- Failures are logged and swallowed; the redirect is never affected.
- The IP is hashed with the pepper before storage; the raw IP is not persisted. User agent is truncated to 512 characters.
- Stats endpoint aggregates `COUNT(*)` and returns the most recent access times for the owner's URL.

## 10. Security design (Task 9)

| Threat | Mitigation |
|--------|------------|
| Dangerous schemes (`javascript:`, `file:`) | Allow only `http` and `https` |
| SSRF / internal targets | Resolve the host; reject loopback, private, link-local and reserved addresses |
| Malicious domains | Local blocklist file checked on create |
| Redirect loops / self-reference | Reject URLs whose host is the shortener's own host |
| Abuse and floods | In-memory token bucket per IP and per API key on create and redirect; 429 with `Retry-After` |
| Oversized input | Max URL length, alias length and charset validation |
| Credential leakage | API keys and IPs stored only hashed |
| Browser-side attacks | Security headers; explicit CORS configuration |

## 11. Error model

JSON errors with a consistent shape. Status codes: 401, 404, 409, 410, 422, 429, 503 as defined in [SPEC.md](./SPEC.md).

## 12. Testing strategy

- **Unit:** token generator and retry exhaustion, alias and URL validators, API key hashing.
- **DB:** pragmas applied, constraints, cascades, UTC datetime handling, migration parity.
- **API:** create (random and alias), alias conflict, redirect states (active, expired, disabled, deleted), ownership, auth failures, `/api/v1` prefix only, rate limiting.
- **Concurrency:** parallel creates produce unique codes; forced collisions are retried.
- **Load:** Locust or k6 against `GET /{code}` to measure redirect latency.

Tests use a file-backed temporary SQLite database so WAL and multi-connection behaviour match production.

## 13. Deployment

- Dockerfile runs `alembic upgrade head` then `uvicorn`, as a non-root user.
- The SQLite file lives on a mounted volume.
- A single instance; see limits below.

## 14. Known limits and scaling path

| Limit | Consequence | Path forward |
|-------|-------------|--------------|
| SQLite single-node writes | Write throughput capped to one node | Move to Postgres with read replicas |
| In-process cache and rate limiter | Per instance, not shared | Redis for cache and rate limiting |
| `BackgroundTasks` are in-process | A crash can lose unwritten clicks | Queue (Redis Streams, Kafka) with a consumer |
| Single instance | No real high availability | Multiple instances behind a load balancer, once the DB is external |

Storage and cache access sit behind small interfaces so these swaps do not change the API.

## 15. Implementation phases

Detailed in [PLAN.md](./PLAN.md):

| Phase | Tasks |
|-------|-------|
| 1 | Setup, data model, auth (1, 2, 3) |
| 2 | Token generator, create, redirect (4, 5, 6) |
| 3 | Management endpoints, security (7, 9) |
| 4 | Analytics, reliability (8, 10) |
| 5 | Tests, docs (11, 12) |
