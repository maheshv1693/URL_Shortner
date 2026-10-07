# URL Shortener - Functional & Requirements Spec

**Status:** Draft for review. Describes *what* the system does. For *how* it is built, see [TECH_DESIGN.md](./TECH_DESIGN.md). For task breakdown and sequencing, see [PLAN.md](./PLAN.md).

## 1. Purpose and scope

A URL shortener prototype: users submit a long URL and receive a short URL that redirects to the original. It must support custom aliases, expiration, disabling or deleting links, and basic click analytics.

**In scope:** everything in sections 2-4.
**Out of scope (prototype):** web UI, user passwords or OAuth, billing or quotas, custom domains, QR codes, geo or device analytics, multi-region deployment.

## 2. Functional requirements

| ID | Requirement | Acceptance criteria |
|----|-------------|---------------------|
| FR1 | Create a short URL from a long URL | `POST /api/v1/urls` with a valid URL returns 201 and a unique short code and short URL. |
| FR2 | Redirect short URL to the original | `GET /{code}` returns 302 with `Location` set to the original URL. |
| FR3 | Custom short aliases | An optional alias is used as the code if valid and available; otherwise 409 (taken) or 422 (invalid). |
| FR4 | Expiration | An optional TTL (seconds) or absolute datetime; after expiry `GET /{code}` returns 410. |
| FR5 | Delete or disable URLs | The owner can disable, re-enable or delete a URL; disabled returns 410, deleted returns 404. |
| FR6 | Basic analytics | Each redirect records click count and access time (plus hashed IP, user agent, referrer); the owner can read them. |

## 3. Non-functional requirements

| ID | Requirement | Target for the prototype |
|----|-------------|--------------------------|
| NFR1 | Low latency | Redirect path is a single indexed lookup; analytics never block the response. |
| NFR2 | Scalability | Designed for millions of URLs and read-heavy traffic; the prototype documents the path to scale beyond SQLite. |
| NFR3 | Availability | Redirects keep working if analytics writes fail; health endpoint available. |
| NFR4 | Reliability | A short code always maps to the correct original URL; enforced by DB constraints and tests. |
| NFR5 | Uniqueness | No two URLs share a code; collisions are detected and retried. |
| NFR6 | Security | Reject malicious or unsafe links, rate-limit abuse, authenticate management actions. |

## 4. Behavioural rules

### 4.1 Users and authentication
- A user is created with `POST /api/v1/users`; the response contains an API key shown **once**.
- Management endpoints require the `X-API-Key` header. Missing or invalid key returns 401.
- A user can only see and change their own URLs; another user's code returns 404.

### 4.2 Short codes
- Generated codes are 7 characters, Base62 (`A-Z`, `a-z`, `0-9`).
- Custom aliases: 3-32 characters, `[A-Za-z0-9_-]`, case-sensitive, unique.
- Reserved words (`api`, `docs`, `health`, etc.) cannot be used as aliases.
- If a unique generated code cannot be created after 3 attempts, the API returns 503.

### 4.3 Redirect outcomes

| Condition | Response |
|-----------|----------|
| Active, not expired | 302 to original URL |
| Code does not exist or was deleted | 404 |
| Expired | 410 Gone |
| Disabled by owner | 410 Gone |

### 4.4 Expiration
- Optional on creation, given as `ttl_seconds` or `expires_at` (not both). Must be in the future.
- Without an expiry the link does not expire.

### 4.5 Analytics
- Recorded per redirect: access time, hashed client IP, user agent, referrer.
- Owner analytics (`GET /api/v1/urls/{code}/analytics`): total click count, a click time series (hourly or daily), top referrers and recent clicks. Referrers are stored as origin only; the raw IP is never stored.
- Recording is best effort. A failed write does not affect the redirect, so a click may rarely be lost.

### 4.6 Link safety and abuse protection
- Only `http` and `https` URLs are accepted; maximum URL length enforced.
- URLs that target localhost, private or link-local addresses are rejected.
- Domains on a local blocklist are rejected.
- Links pointing back at the shortener itself are rejected.
- Rate limits apply per IP and per API key on create and redirect; exceeding returns 429.

## 5. API contract summary

All endpoints are under `/api/v1/`. The public redirect is `GET /{code}`.

| Method | Path | Purpose | Auth | Success | Errors |
|--------|------|---------|------|---------|--------|
| POST | `/api/v1/users` | Create user, return API key | None | 201 | 429 |
| GET | `/api/v1/users/me` | Current user | API key | 200 | 401 |
| POST | `/api/v1/urls` | Create short URL | API key | 201 | 401, 409, 422, 429, 503 |
| GET | `/api/v1/urls` | List my URLs | API key | 200 | 401 |
| PATCH | `/api/v1/urls/{code}` | Enable or disable | API key | 200 | 401, 404, 422 |
| DELETE | `/api/v1/urls/{code}` | Delete | API key | 204 | 401, 404 |
| GET | `/api/v1/urls/{code}/analytics` | Click analytics | API key | 200 | 401, 404, 422 |
| GET | `/api/v1/health` (also `/health`) | Health check | None | 200 | 503 (database unreachable) |
| GET | `/{code}` | Redirect | None | 302 | 404, 410, 429 |

## 6. Confirmed product decisions

- One API key per user, no passwords.
- 302 (temporary) redirects so every click is counted and expiry or disable apply immediately.
- Expired and disabled links return 410; unknown or deleted links return 404.
- Analytics capture access time, hashed IP, user agent and referrer.
- Link safety is limited to scheme, private-network and domain-blocklist checks (no third-party reputation service).

## 7. Open questions

None currently open. New questions are added here during review.
