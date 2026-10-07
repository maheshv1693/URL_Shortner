#!/usr/bin/env python3
"""End-to-end smoke test for a running URL shortener.

Uses only the Python standard library, so it runs anywhere Python 3.9+ is installed:

    python scripts/smoke_test.py                      # http://127.0.0.1:8000
    python scripts/smoke_test.py --base-url http://127.0.0.1:9000

It walks the whole business lifecycle: registration, health probes, SSRF defence, link
creation, redirects and security headers, analytics, and link management (disable, delete,
cross-tenant isolation). Exit status is 0 when every check passes, 1 when any check fails and
2 when the server cannot be reached.

The server must be able to resolve and reach the public target URL (default
https://example.com/...) because link creation resolves the target host through DNS.
Re-running is safe: every run registers fresh users and cleans up the links it creates.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from typing import Any

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_TARGET_URL = "https://example.com/smoke-test"
REQUEST_TIMEOUT = 15
BASE62 = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789")
SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "strict-origin-when-cross-origin",
}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Return 3xx responses to the caller instead of following them."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _lower(headers: Any) -> dict[str, str]:
    return {name.lower(): value for name, value in headers.items()}


class Response:
    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except ValueError:
            return None

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


class Smoke:
    def __init__(self, base_url: str, target_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.target_url = target_url
        self.passed = 0
        self.failures: list[str] = []

    # -- HTTP --

    def call(
        self,
        method: str,
        path: str,
        *,
        api_key: str | None = None,
        body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        """Send a request; waits out 429 rate limits (up to three times) before giving up."""
        data = None if body is None else json.dumps(body).encode("utf-8")
        request_headers = dict(headers or {})
        if data is not None:
            request_headers["Content-Type"] = "application/json"
        if api_key:
            request_headers["X-API-Key"] = api_key

        for attempt in range(4):
            request = urllib.request.Request(
                self.base_url + path, data=data, headers=request_headers, method=method
            )
            try:
                with _OPENER.open(request, timeout=REQUEST_TIMEOUT) as raw:
                    response = Response(raw.status, _lower(raw.headers), raw.read())
            except urllib.error.HTTPError as exc:
                response = Response(exc.code, _lower(exc.headers), exc.read())
            if response.status != 429 or attempt == 3:
                return response
            wait = min(int(response.headers.get("retry-after", "5") or 5), 70)
            print(f"      (rate limited, waiting {wait}s before retrying {method} {path})")
            time.sleep(wait)
        raise AssertionError("unreachable")

    # -- reporting --

    def check(self, label: str, condition: bool, detail: str = "") -> bool:
        if condition:
            self.passed += 1
            print(f"  [PASS] {label}")
        else:
            message = label + (f" -- {detail}" if detail else "")
            self.failures.append(message)
            print(f"  [FAIL] {message}")
        return bool(condition)

    def expect_status(self, label: str, response: Response, status: int) -> bool:
        return self.check(
            f"{label} -> {status}",
            response.status == status,
            f"got {response.status}: {response.text[:200]}",
        )

    def require(self, ok: bool, why: str) -> None:
        """Abort the run when later steps cannot make sense without this one."""
        if not ok:
            raise SystemExit(self._finish(aborted=why))

    def section(self, title: str) -> None:
        print(f"\n{title}")

    def _finish(self, aborted: str | None = None) -> int:
        total = self.passed + len(self.failures)
        print("\n" + "=" * 60)
        if aborted:
            print(f"ABORTED: {aborted}")
        print(f"{self.passed}/{total} checks passed")
        for failure in self.failures:
            print(f"  FAILED: {failure}")
        return 1 if (self.failures or aborted) else 0

    # -- scenario --

    def run(self) -> int:
        print(f"Smoke test against {self.base_url}")
        self.section("0. Server reachable")
        try:
            probe = self.call("GET", "/health")
        except (urllib.error.URLError, OSError) as exc:
            print(f"  Cannot reach {self.base_url}: {exc}")
            print("  Start the server first, e.g.: uvicorn app.main:app --port 8000")
            return 2
        self.require(self.check("server answers", probe.status in (200, 503)), "no usable server")

        alice = self.step_registration()
        self.step_health()
        self.step_ssrf(alice["key"])
        link, alias_link = self.step_create(alice["key"])
        self.step_redirects(link, alias_link)
        self.step_analytics(alice["key"], link)
        bob = self.step_register_second_user()
        self.step_management(alice, bob, link, alias_link)
        return self._finish()

    def step_registration(self) -> dict[str, Any]:
        self.section("1. User registration and API key")
        created = self.call("POST", "/api/v1/users")
        self.expect_status("POST /api/v1/users", created, 201)
        body = created.json() or {}
        key = body.get("api_key", "")
        has_key = isinstance(key, str) and len(key) >= 32
        self.require(self.check("API key returned", has_key), "registration returned no API key")
        self.check("user id and created_at present", "id" in body and "created_at" in body)

        me = self.call("GET", "/api/v1/users/me", api_key=key)
        self.expect_status("GET /users/me with key", me, 200)
        self.check("/users/me returns the same user", (me.json() or {}).get("id") == body.get("id"))
        self.check("/users/me never repeats the key", "api_key" not in (me.json() or {}))
        anonymous = self.call("GET", "/api/v1/users/me")
        self.expect_status("GET /users/me without key", anonymous, 401)
        self.check(
            "401 carries WWW-Authenticate: ApiKey",
            anonymous.headers.get("www-authenticate") == "ApiKey",
        )
        wrong = self.call("GET", "/api/v1/users/me", api_key="definitely-wrong")
        self.expect_status("GET /users/me with wrong key", wrong, 401)
        return {"key": key, "id": body.get("id")}

    def step_health(self) -> None:
        self.section("2. Health probes")
        for path in ("/health", "/api/v1/health"):
            response = self.call("GET", path)
            self.expect_status(f"GET {path}", response, 200)
            self.check(
                f"{path} reports database ok",
                response.json() == {"status": "ok", "database": "ok"},
                response.text,
            )

    def step_ssrf(self, key: str) -> None:
        self.section("3. SSRF defence")
        blocked = (
            "http://127.0.0.1/",
            "http://localhost/",
            "http://169.254.169.254/latest/meta-data/",
            "ftp://example.com/x",
        )
        for target in blocked:
            response = self.call("POST", "/api/v1/urls", api_key=key, body={"long_url": target})
            self.expect_status(f"reject {target}", response, 422)

    def step_create(self, key: str) -> tuple[dict[str, Any], dict[str, Any]]:
        self.section("4. Short link creation")
        target = self.target_url
        created = self.call("POST", "/api/v1/urls", api_key=key, body={"long_url": target})
        self.expect_status("create link with generated code", created, 201)
        self.require(
            created.status == 201,
            f"cannot create a link to {target} (the server must resolve it; see --target-url)",
        )
        link = created.json()
        self.check(
            "generated code is 7 Base62 characters",
            len(link["code"]) == 7 and set(link["code"]) <= BASE62,
            link["code"],
        )
        self.check("short_url ends with the code", link["short_url"].endswith("/" + link["code"]))
        self.check("long_url is stored unchanged", link["long_url"] == target)
        self.check(
            "new link is active and does not expire",
            link["is_active"] is True and link["expires_at"] is None,
        )

        alias = "smoke-" + secrets.token_hex(4)
        body = {"long_url": target, "custom_alias": alias}
        custom = self.call("POST", "/api/v1/urls", api_key=key, body=body)
        self.expect_status("create link with custom alias", custom, 201)
        alias_link = custom.json() or {}
        self.check("alias is used as the code", alias_link.get("code") == alias)
        duplicate = self.call("POST", "/api/v1/urls", api_key=key, body=body)
        self.expect_status("duplicate alias", duplicate, 409)
        reserved = self.call(
            "POST", "/api/v1/urls", api_key=key, body={"long_url": target, "custom_alias": "docs"}
        )
        self.expect_status("reserved alias 'docs'", reserved, 422)
        both = self.call(
            "POST",
            "/api/v1/urls",
            api_key=key,
            body={"long_url": target, "ttl_seconds": 60, "expires_at": "2999-01-01T00:00:00Z"},
        )
        self.expect_status("ttl_seconds and expires_at together", both, 422)
        self.require(bool(alias_link.get("code")), "alias link was not created")
        return link, alias_link

    def step_redirects(self, link: dict[str, Any], alias_link: dict[str, Any]) -> None:
        self.section("5. Redirection and security headers")
        code = link["code"]
        click = {
            "Referer": "https://referrer.example/some/path?token=secret-value",
            "User-Agent": "smoke-test/1.0",
        }
        first = self.call("GET", f"/{code}", headers=click)
        self.expect_status(f"GET /{code}", first, 302)
        location = first.headers.get("location", "<missing>")
        self.check("Location is the original URL", location == self.target_url, location)
        for name, value in SECURITY_HEADERS.items():
            actual = first.headers.get(name, "<missing>")
            self.check(f"header {name}: {value}", actual == value, actual)
        self.expect_status("second click", self.call("GET", f"/{code}", headers=click), 302)
        direct = {"User-Agent": "smoke-test/1.0"}
        self.expect_status(
            "third click (no referrer)", self.call("GET", f"/{code}", headers=direct), 302
        )
        self.expect_status("alias redirect", self.call("GET", "/" + alias_link["code"]), 302)
        self.expect_status("unknown code", self.call("GET", "/zzzzzz9"), 404)

    def step_analytics(self, key: str, link: dict[str, Any]) -> None:
        self.section("6. Analytics ingestion and aggregation")
        path = f"/api/v1/urls/{link['code']}/analytics"
        body: dict[str, Any] = {}
        deadline = time.monotonic() + 20
        while (
            time.monotonic() < deadline
        ):  # clicks are written in batches, shortly after the redirect
            response = self.call("GET", path, api_key=key)
            body = response.json() or {}
            if response.status == 200 and body.get("total_clicks") == 3:
                break
            time.sleep(0.5)
        total = body.get("total_clicks")
        self.check("analytics ready within 20s with total_clicks == 3", total == 3, f"got {total}")
        self.check("code echoed", body.get("code") == link["code"])

        series = body.get("time_series") or []
        self.check(
            "time series has 7 daily buckets by default",
            len(series) == 7 and body.get("granularity") == "day",
            str(len(series)),
        )
        self.check(
            "every bucket has bucket + clicks",
            bool(series) and all(set(item) == {"bucket", "clicks"} for item in series),
        )
        buckets = [item.get("bucket") for item in series]
        self.check("buckets are in ascending order", buckets == sorted(buckets))
        self.check("bucket clicks add up to 3", sum(i.get("clicks", 0) for i in series) == 3)

        self.check(
            "top referrer is the sanitised origin (2 clicks)",
            body.get("top_referrers") == [{"referrer": "https://referrer.example", "clicks": 2}],
            json.dumps(body.get("top_referrers")),
        )
        raw = json.dumps(body)
        self.check(
            "referrer path and query were discarded",
            "secret-value" not in raw and "/some/path" not in raw,
        )
        self.check("no IP or IP hash is exposed", "ip_hash" not in raw and "127.0.0.1" not in raw)

        recent = (body.get("recent_clicks") or {}).get("items", [])
        self.check("three recent clicks listed", len(recent) == 3, str(len(recent)))
        self.check(
            "recent clicks expose only clicked_at, referrer, user_agent",
            bool(recent)
            and all(set(item) == {"clicked_at", "referrer", "user_agent"} for item in recent),
        )
        self.check(
            "newest click (no referrer) comes first", bool(recent) and recent[0]["referrer"] is None
        )
        self.check(
            "user agent recorded",
            bool(recent) and all(item["user_agent"] == "smoke-test/1.0" for item in recent),
        )

        page = self.call("GET", path + "?limit=1&offset=1", api_key=key).json() or {}
        items = (page.get("recent_clicks") or {}).get("items", [])
        self.check("pagination: limit=1&offset=1 returns one item", len(items) == 1)
        hourly = self.call("GET", path + "?granularity=hour&days=1", api_key=key).json() or {}
        self.check(
            "hourly granularity returns 24 buckets", len(hourly.get("time_series", [])) == 24
        )
        bad = self.call("GET", path + "?granularity=week", api_key=key)
        self.expect_status("invalid granularity", bad, 422)
        self.expect_status("analytics without key", self.call("GET", path), 401)

    def step_register_second_user(self) -> dict[str, Any]:
        self.section("7a. Second tenant")
        created = self.call("POST", "/api/v1/users")
        self.expect_status("register second user", created, 201)
        body = created.json() or {}
        self.require(bool(body.get("api_key")), "second user was not created")
        return {"key": body["api_key"], "id": body.get("id")}

    def step_management(
        self,
        alice: dict[str, Any],
        bob: dict[str, Any],
        link: dict[str, Any],
        alias_link: dict[str, Any],
    ) -> None:
        code = link["code"]
        key, other = alice["key"], bob["key"]

        self.section("7b. Foreign tenant access (anti-enumeration)")
        missing_code = "nosuch" + secrets.token_hex(3)
        probes = {
            "analytics": ("GET", "/api/v1/urls/{}/analytics", None),
            "PATCH": ("PATCH", "/api/v1/urls/{}", {"is_active": False}),
            "DELETE": ("DELETE", "/api/v1/urls/{}", None),
        }
        for label, (method, template, body) in probes.items():
            foreign = self.call(method, template.format(code), api_key=other, body=body)
            absent = self.call(method, template.format(missing_code), api_key=other, body=body)
            self.expect_status(f"other user's {label} on Alice's link", foreign, 404)
            self.check(
                f"{label}: foreign link looks identical to a missing one",
                foreign.status == absent.status and foreign.json() == absent.json(),
            )
        self.expect_status(
            "link still redirects after foreign attempts", self.call("GET", f"/{code}"), 302
        )

        own = self.call("GET", "/api/v1/urls", api_key=key).json() or {}
        theirs = self.call("GET", "/api/v1/urls", api_key=other).json() or {}
        listed = {item["code"] for item in own.get("items", [])}
        self.check("Alice lists both of her links", {code, alias_link["code"]} <= listed)
        self.check("Bob's list is empty", theirs.get("total") == 0 and theirs.get("items") == [])

        self.section("7c. Disable, re-enable and delete")
        url = f"/api/v1/urls/{code}"
        disabled = self.call("PATCH", url, api_key=key, body={"is_active": False})
        self.expect_status("disable link", disabled, 200)
        self.check(
            "response shows is_active=false", (disabled.json() or {}).get("is_active") is False
        )
        self.expect_status("disabled link is gone immediately", self.call("GET", f"/{code}"), 410)
        invalid = self.call("PATCH", url, api_key=key, body={"is_active": "yes"})
        self.expect_status("invalid PATCH body", invalid, 422)

        self.expect_status(
            "re-enable link", self.call("PATCH", url, api_key=key, body={"is_active": True}), 200
        )
        self.expect_status("re-enabled link redirects again", self.call("GET", f"/{code}"), 302)

        self.expect_status("delete link", self.call("DELETE", url, api_key=key), 204)
        self.expect_status("deleted link is 404 immediately", self.call("GET", f"/{code}"), 404)
        self.expect_status("deleting twice", self.call("DELETE", url, api_key=key), 404)
        gone = self.call("GET", url + "/analytics", api_key=key)
        self.expect_status("analytics of deleted link", gone, 404)

        cleanup = self.call("DELETE", f"/api/v1/urls/{alias_link['code']}", api_key=key)
        self.expect_status("clean up alias link", cleanup, 204)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke test a running URL shortener.")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("SMOKE_BASE_URL", DEFAULT_BASE_URL),
        help=f"server root (default: {DEFAULT_BASE_URL}, or $SMOKE_BASE_URL)",
    )
    parser.add_argument(
        "--target-url",
        default=DEFAULT_TARGET_URL,
        help="public URL the test links point at; the server must be able to resolve it",
    )
    args = parser.parse_args(argv)
    return Smoke(args.base_url, args.target_url).run()


if __name__ == "__main__":
    sys.exit(main())
