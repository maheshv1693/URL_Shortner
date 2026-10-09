# URL Shortener

A URL shortener prototype built with FastAPI and SQLite. Users register for an API key, create short
links (random 7-character codes or custom aliases, with optional expiry), and share them. Visitors are
redirected with `302`, every redirect is counted, and owners can read click analytics, disable or delete
their links.

- **Stack:** FastAPI, SQLAlchemy 2, SQLite (WAL), Alembic, pydantic-settings, pytest, ruff.
- **Docs:** [Docs/SPEC.md](Docs/SPEC.md) (what it does), [Docs/TECH_DESIGN.md](Docs/TECH_DESIGN.md) (how it is
  built), [Docs/PLAN.md](Docs/PLAN.md) (tasks and status).
- **Requirement workflow:** [Docs/REQUIREMENT_WORKFLOW.md](Docs/REQUIREMENT_WORKFLOW.md) describes the local
  prototype that turns requirement text into a reviewable engineering proposal.

## Requirement-to-engineering proposal

The prototype accepts a requirement and prints structured JSON; it does not modify code or require an
API key. From the repository root:

```bash
python scripts/requirement_to_output.py \
  "Build a scalable URL shortener service with APIs, persistence, and analytics." \
  --scenario greenfield
```

Pass `--input-file path\to\requirement.txt` to read a file, `--output proposal.json` to save the result,
or use `--scenario brownfield` / `--scenario ambiguous` to select a review path. Its local keyword-based
analysis is a proposal only; an engineer must resolve questions, inspect the suggestions, and validate
any implementation. See the workflow document for all three examples and limitations.

## Requirements

- Python 3.11 or newer
- `pip` (bundled with Python)
- Outbound DNS access for the server, because creating a link resolves the target host to reject private
  addresses. Docker is optional.

## Setup

Create a virtual environment and install the app with its development tools (pytest, httpx, ruff).

Windows (PowerShell):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

macOS / Linux:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Configuration

All settings are environment variables prefixed with `URLSHORT_`. They can also be placed in a `.env` file in
the working directory. A minimal development setup needs none of them, but **set a real pepper and
`URLSHORT_PUBLIC_BASE_URL` in any shared or production deployment.**

| Variable | Default | Purpose |
|----------|---------|---------|
| `URLSHORT_DATABASE_URL` | `sqlite:///./urlshortener.db` | Database location |
| `URLSHORT_SQLITE_BUSY_TIMEOUT_MS` | `5000` | How long a SQLite writer waits for the lock |
| `URLSHORT_API_KEY_PEPPER` | `change-me-in-production` | Secret for keyed hashing of API keys and visitor IPs. **Change it**; changing it later invalidates existing API keys |
| `URLSHORT_API_V1_PREFIX` | `/api/v1` | Prefix of the JSON API |
| `URLSHORT_PUBLIC_BASE_URL` | unset | Public URL of the service (for example `https://sho.rt`). Used to build `short_url` and to reject links that point back at the service. Set it in production: the `Host` header is client-controlled |
| `URLSHORT_REDIRECT_CACHE_SIZE` | `1024` | Maximum entries in the in-process redirect LRU cache |
| `URLSHORT_BLOCKED_DOMAINS_FILE` | `app/core/blocked_domains.txt` | Domain blocklist: one domain per line, `#` comments; an entry also blocks its subdomains. Read at startup |
| `URLSHORT_RATE_LIMIT_ENABLED` | `true` | Master switch for rate limiting (turn off for load tests) |
| `URLSHORT_RATE_LIMIT_REGISTER_IP_PER_MINUTE` | `10` | `POST /api/v1/users`, per client IP |
| `URLSHORT_RATE_LIMIT_CREATE_IP_PER_MINUTE` | `30` | `POST /api/v1/urls`, per client IP |
| `URLSHORT_RATE_LIMIT_CREATE_KEY_PER_MINUTE` | `60` | `POST /api/v1/urls`, per API key |
| `URLSHORT_RATE_LIMIT_REDIRECT_IP_PER_MINUTE` | `600` | `GET /{code}`, per client IP |
| `URLSHORT_CORS_ALLOW_ORIGINS` | `[]` | JSON list of allowed origins, e.g. `["https://app.example.com"]`. Empty disables CORS |
| `URLSHORT_ANALYTICS_BATCH_SIZE` | `100` | Maximum clicks per database write |
| `URLSHORT_ANALYTICS_FLUSH_INTERVAL_SECONDS` | `1.0` | Longest a click waits in memory before it is written |
| `URLSHORT_ANALYTICS_QUEUE_MAX` | `10000` | Click buffer capacity; clicks beyond it are dropped |
| `URLSHORT_ANALYTICS_SHUTDOWN_TIMEOUT_SECONDS` | `10.0` | Time allowed to flush buffered clicks at shutdown |

Rate limits are token buckets: the number is both the burst size and the refill rate per minute. A limited
request gets `429` with a `Retry-After` header.

## Initialize the database

The schema is managed by Alembic. Run the migrations before the first start and after every upgrade (the
Docker image does this automatically):

```bash
alembic upgrade head
```

Alembic reads `URLSHORT_DATABASE_URL`, so the same variable selects the database for the migration and the
server. PowerShell example with a custom file:

```powershell
$env:URLSHORT_DATABASE_URL = "sqlite:///./data/shortener.db"
alembic upgrade head
```

## Run the server

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

- Interactive API docs: <http://127.0.0.1:8000/docs>
- Health: <http://127.0.0.1:8000/health> (also `/api/v1/health`)

Stop the server with Ctrl+C or SIGTERM so that clicks still waiting in the in-memory buffer are written out
(a hard kill loses them). Behind a reverse proxy, run uvicorn with `--proxy-headers` and
`--forwarded-allow-ips` set to the proxy address so that rate limits and click analytics see the real client IP.

### Docker

```bash
docker build -t url-shortener .
docker run -p 8000:8000 -v url-data:/data \
  -e URLSHORT_API_KEY_PEPPER="$(openssl rand -hex 32)" \
  -e URLSHORT_PUBLIC_BASE_URL="http://localhost:8000" url-shortener
```

The image runs as a non-root user, applies the migrations on start and keeps the SQLite file on the `/data`
volume.


## Using the API

### Windows (PowerShell) Quickstart Walkthrough

#### Terminal 1: One-Time Setup & Start Server
```powershell
# 1. Create and activate virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. Install app with development tools
pip install -e ".[dev]"

# 3. Apply database migrations
alembic upgrade head

# 4. Start the server
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

#### Terminal 2: Test the API Lifecycle
```powershell
# 1. Register user and capture the generated API key into a variable
$response = curl.exe -s -X POST http://127.0.0.1:8000/api/v1/users
$KEY = ($response | ConvertFrom-Json).api_key
Write-Host "Active API Key: $KEY"

# 2. Create a short link with custom alias and TTL
curl.exe -s -X POST http://127.0.0.1:8000/api/v1/urls `
  -H "X-API-Key: $KEY" `
  -H "Content-Type: application/json" `
  -d '{\"long_url\": \"https://example.com/some/long/path\", \"custom_alias\": \"my-link\", \"ttl_seconds\": 86400}'

# 3. Follow redirect and inspect 302 response headers
curl.exe -i http://127.0.0.1:8000/my-link

# 4. Inspect click analytics telemetry
curl.exe -s "http://127.0.0.1:8000/api/v1/urls/my-link/analytics?granularity=day&days=7" `
  -H "X-API-Key: $KEY"

# 5. Disable the short link
curl.exe -s -X PATCH http://127.0.0.1:8000/api/v1/urls/my-link `
  -H "X-API-Key: $KEY" `
  -H "Content-Type: application/json" `
  -d '{\"is_active\": false}'

# 6. Delete the short link permanently
curl.exe -s -X DELETE http://127.0.0.1:8000/api/v1/urls/my-link `
  -H "X-API-Key: $KEY"
```

### Linux / macOS (Bash)
```bash
# 1. Register. The API key is shown only once.
curl -s -X POST http://127.0.0.1:8000/api/v1/users
# {"id":1,"created_at":"...","api_key":"<KEY>"}

# 2. Create a short link (random code, or add "custom_alias", "ttl_seconds" / "expires_at")
curl -s -X POST http://127.0.0.1:8000/api/v1/urls \
  -H "X-API-Key: <KEY>" -H "Content-Type: application/json" \
  -d '{"long_url": "https://example.com/some/long/path", "custom_alias": "my-link", "ttl_seconds": 86400}'

# 3. Follow it
curl -i http://127.0.0.1:8000/my-link

# 4. Analytics: total clicks, time series, top referrers, recent clicks
curl -s "http://127.0.0.1:8000/api/v1/urls/my-link/analytics?granularity=day&days=7" -H "X-API-Key: <KEY>"

# 5. Disable, re-enable or delete
curl -s -X PATCH http://127.0.0.1:8000/api/v1/urls/my-link \
  -H "X-API-Key: <KEY>" -H "Content-Type: application/json" -d '{"is_active": false}'
curl -s -X DELETE http://127.0.0.1:8000/api/v1/urls/my-link -H "X-API-Key: <KEY>"
```

On Windows PowerShell use `curl.exe` instead of `curl` (and escape the JSON quotes), or use the interactive docs at `/docs`.

| Method | Path | Auth | Result |
|--------|------|------|--------|
| POST | `/api/v1/users` | none | `201` with the API key (shown once) |
| GET | `/api/v1/users/me` | key | Current user |
| POST | `/api/v1/urls` | key | `201` new link; `409` alias taken; `422` invalid or unsafe input; `503` no unique code (retry) |
| GET | `/api/v1/urls?limit=&offset=` | key | Your links, newest first |
| PATCH | `/api/v1/urls/{code}` | key | `{"is_active": bool}` enables or disables |
| DELETE | `/api/v1/urls/{code}` | key | `204`; also deletes the link's clicks |
| GET | `/api/v1/urls/{code}/analytics` | key | Clicks: total, time series (`hour` or `day`), top referrers, recent clicks |
| GET | `/health`, `/api/v1/health` | none | `200`, or `503` if the database is unreachable |
| GET | `/{code}` | none | `302` to the target; `404` unknown or deleted; `410` expired or disabled |

Behaviour worth knowing:

- Links owned by someone else behave exactly like missing ones (`404`), so codes cannot be enumerated.
- Custom aliases are 3-32 characters of `A-Z a-z 0-9 _ -`, case-sensitive; `api`, `docs`, `redoc`, `health`
  and `openapi.json` are reserved.
- Targets must be `http`/`https` URLs of at most 2048 characters. Hosts that resolve to loopback, private,
  link-local or other non-public addresses, `localhost`, blocklisted domains and the service's own host are
  rejected.
- Analytics are recorded asynchronously and appear about a second after the redirect. Only the referrer's
  origin is kept, the visitor IP is stored as a keyed hash, and the user agent is truncated.
- Every response carries `X-Content-Type-Options`, `X-Frame-Options` and `Referrer-Policy` headers.



## Tests and static analysis

The test suite contains **350 passing tests** with zero external network dependencies:

```bash
pytest                 # full suite (350 passed: 342 core API + 8 CLI tool tests)
pytest tests/test_performance.py   # concurrency and burst harness (14 tests)
ruff check .           # lint
ruff format --check .  # formatting
```

The suite runs against temporary file-backed SQLite databases and never touches the network (DNS is stubbed).
It includes:

- **Unit & API Tests:** Comprehensive verification of every endpoint, auth boundary, and validation rule.
- **Requirement Tool Tests (`tests/test_requirement_to_output.py`):** 8 tests validating deterministic generation across greenfield, brownfield, and ambiguous requirements, DAG topological ordering, and file output.
- **Concurrency Harness (`tests/test_performance.py`):** 14 stress tests covering 100–300 concurrent redirects against cached/uncached links, forced token collisions, alias races, click-batching under burst traffic, and connection-pool exhaustion regression.

## Live smoke test

`scripts/smoke_test.py` exercises a **running** server through the whole lifecycle using only the Python
standard library (no virtual environment needed): registration, health probes, SSRF defence, link creation,
redirects and security headers, analytics, disable/delete and cross-tenant isolation.

```bash
uvicorn app.main:app --port 8000        # terminal 1 (after `alembic upgrade head`)
python scripts/smoke_test.py            # terminal 2
```

Options: `--base-url http://host:port` (or `SMOKE_BASE_URL`) and `--target-url <public URL>`; the server must be
able to resolve the target (default `https://example.com/smoke-test`). The exit status is `0` when all checks
pass, `1` when a check fails and `2` when the server cannot be reached. It is safe to re-run: it registers new
users each time and deletes its links; with the default limits, keep to a handful of runs per minute.

## Project layout

```
app/
  main.py               application factory, lifespan (starts/stops the click writer)
  api/                  routers: users, urls (+ analytics), health, redirect; shared dependencies
  core/                 settings, database engine (SQLite pragmas), security helpers, rate limiter
  middleware/           security headers
  models/               SQLAlchemy models: User, URL, Click
  services/             token generation, user lookup, redirect cache, click analytics
alembic/                migrations
examples/requirements/  input fixtures for greenfield, brownfield, and ambiguous scenarios
scripts/
  requirement_to_output.py  CLI requirement-to-engineering proposal prototype
  smoke_test.py             live end-to-end smoke test
tests/                  pytest suite (350 tests including API, CLI, and concurrency)
Docs/                   spec, technical design, plan, requirement workflow
```

## Scaling path

The prototype runs as a single process on SQLite. The seams for growing it are already in place:

| Limit today | Next step |
|-------------|-----------|
| SQLite allows one writer on one node | Move to Postgres with read replicas; the engine and session setup are isolated in `app/core/database.py` |
| Redirect cache and rate limiter live in process memory | Move both to Redis so replicas share them |
| Clicks are buffered in process memory and lost on a hard kill | Publish click events to a queue (Redis Streams, Kafka) with a separate consumer |
| A single instance is not highly available | Run several instances behind a load balancer once the database is external |

## Known limits

- Rate limiting, the redirect cache and the click buffer are per process, not shared across instances.
- A crash or hard kill loses the clicks that are still buffered (about one second by default); clicks are also
  dropped if the buffer is full or the database stays locked.
- The DNS check for unsafe targets runs when a link is created; it cannot stop a domain from being re-pointed
  to a private address afterwards.
- Behind a proxy, client IPs are only correct if uvicorn is configured to trust the proxy's forwarded headers.
