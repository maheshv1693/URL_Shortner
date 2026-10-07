import socket

import pytest
from sqlalchemy import func, select

from app.core import security
from app.core.rate_limit import TokenBucketLimiter
from app.core.security import (
    MAX_URL_LENGTH,
    UnsafeURLError,
    check_target_url,
    is_public_ip,
    load_blocked_domains,
)
from app.models import URL
from tests.conftest import PUBLIC_IP, public_resolver, register

URLS = "/api/v1/urls"


def post(client, headers, long_url="https://example.com/x", **body):
    return client.post(URLS, json={"long_url": long_url, **body}, headers=headers)


def url_count(db) -> int:
    db.expire_all()
    return db.scalar(select(func.count()).select_from(URL))


# --- SSRF / URL sanitisation ----------------------------------------------------

BLOCKED_TARGETS = [
    "http://localhost/x",
    "http://LOCALHOST./x",
    "http://api.localhost/x",
    "http://127.0.0.1/",
    "http://127.255.255.254/",
    "http://127.1/",  # shorthand, normalised to 127.0.0.1
    "http://2130706433/",  # decimal form of 127.0.0.1
    "http://0x7f.0.0.1/",
    "http://10.0.0.5/",
    "http://10.255.255.255/",
    "http://172.16.0.1/",
    "http://172.31.255.255/",
    "http://192.168.1.1/",
    "http://169.254.169.254/latest/meta-data/",
    "http://0.0.0.0/",
    "http://100.64.0.1/",  # carrier-grade NAT
    "http://224.0.0.1/",  # multicast
    "http://[::1]/",
    "http://[::1]:8080/admin",
    "http://[fc00::1]/",
    "http://[fd12:3456:789a::1]/",
    "http://[fe80::1]/",
    "http://[::ffff:127.0.0.1]/",  # IPv4-mapped loopback
    "http://[::ffff:10.0.0.1]/",
    "http://[64:ff9b::7f00:1]/",  # NAT64 wrapping 127.0.0.1
    "http://[2002:7f00:1::]/",  # 6to4 wrapping 127.0.0.1
    "http://user:pass@127.0.0.1/",
    "http://example.com@127.0.0.1/",
]


@pytest.mark.parametrize("target", BLOCKED_TARGETS)
def test_private_and_local_targets_are_rejected(client, auth_headers, db, target):
    resp = post(client, auth_headers, long_url=target)
    assert resp.status_code == 422, target
    assert url_count(db) == 0


@pytest.mark.parametrize(
    "target",
    [
        "ftp://example.com/file",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "gopher://example.com/",
        "ws://example.com/",
        "//example.com/path",
        "example.com",
    ],
)
def test_non_http_schemes_are_rejected(client, auth_headers, db, target):
    assert post(client, auth_headers, long_url=target).status_code == 422
    assert url_count(db) == 0


@pytest.mark.parametrize(
    "target",
    [
        "http://93.184.216.34/",
        "https://[2606:2800:220:1:248:1893:25c8:1946]/",
        "http://172.15.255.255/",  # just below 172.16/12
        "http://172.32.0.1/",  # just above
        "http://192.169.0.1/",
        "http://11.0.0.1/",
        "http://169.253.0.1/",
        "https://example.com/path?q=1#frag",
        "http://localhost.example.com/",  # a normal public domain
    ],
)
def test_public_targets_are_accepted(client, auth_headers, target):
    resp = post(client, auth_headers, long_url=target)
    assert resp.status_code == 201, resp.text
    assert resp.json()["long_url"] == target


def test_max_url_length_boundary(client, auth_headers, db):
    prefix = "https://example.com/"
    ok = prefix + "a" * (MAX_URL_LENGTH - len(prefix))
    too_long = ok + "a"
    assert len(ok) == MAX_URL_LENGTH
    assert post(client, auth_headers, long_url=ok).status_code == 201
    assert post(client, auth_headers, long_url=too_long).status_code == 422
    assert url_count(db) == 1


def test_oversized_url_is_rejected_before_dns(make_client):
    def resolver(host):
        raise AssertionError("DNS must not run for oversized URLs")

    client = make_client(resolver=resolver)
    headers = register(client)
    big = "https://example.com/" + "a" * MAX_URL_LENGTH
    assert post(client, headers, long_url=big).status_code == 422


def test_hostname_resolving_to_private_address_is_rejected(make_client):
    client = make_client(resolver=lambda host: ["10.1.2.3"])
    headers = register(client)
    assert post(client, headers, long_url="https://internal.example.com/").status_code == 422


def test_any_private_address_among_many_is_rejected(make_client):
    client = make_client(resolver=lambda host: [PUBLIC_IP, "192.168.0.10"])
    headers = register(client)
    assert post(client, headers, long_url="https://mixed.example.com/").status_code == 422


def test_hostname_resolving_to_ipv6_loopback_is_rejected(make_client):
    client = make_client(resolver=lambda host: ["::1"])
    headers = register(client)
    assert post(client, headers, long_url="https://v6.example.com/").status_code == 422


@pytest.mark.parametrize("failure", ["gaierror", "empty", "garbage"])
def test_unresolvable_host_fails_closed(make_client, failure):
    def resolver(host):
        if failure == "gaierror":
            raise socket.gaierror("no such host")
        return [] if failure == "empty" else ["not-an-ip"]

    client = make_client(resolver=resolver)
    headers = register(client)
    assert post(client, headers, long_url="https://nowhere.example.com/").status_code == 422


def test_resolver_receives_normalised_host_and_skips_ip_literals(make_client):
    seen = []

    def resolver(host):
        seen.append(host)
        return [PUBLIC_IP]

    client = make_client(resolver=resolver)
    headers = register(client)
    assert post(client, headers, long_url="https://Sub.Example.COM./p").status_code == 201
    assert post(client, headers, long_url="http://93.184.216.34/").status_code == 201
    assert seen == ["sub.example.com"]


def test_error_does_not_leak_resolution_details(make_client):
    client = make_client(resolver=lambda host: ["10.9.8.7"])
    headers = register(client)
    detail = post(client, headers, long_url="https://internal.example.com/").json()["detail"]
    assert "10.9.8.7" not in detail


# --- domain blocklist -----------------------------------------------------------------


@pytest.fixture
def blocklist_client(make_client, tmp_path):
    path = tmp_path / "blocked.txt"
    path.write_text(
        "# comment line\n\nevil.example   # trailing comment\n  Phish.Test.  \n", encoding="utf-8"
    )
    client = make_client(blocked_domains_file=str(path))
    return client, register(client)


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.example/x",
        "https://www.evil.example/",
        "https://a.b.evil.example/",
        "https://EVIL.example./",
        "http://phish.test/",
        "http://login.phish.test/",
    ],
)
def test_blocklisted_domains_are_rejected(blocklist_client, target):
    client, headers = blocklist_client
    assert post(client, headers, long_url=target).status_code == 422


@pytest.mark.parametrize(
    "target",
    ["https://notevil.example/", "https://evil.example.org/", "https://example/", "http://test/"],
)
def test_similar_domains_are_not_blocked(blocklist_client, target):
    client, headers = blocklist_client
    assert post(client, headers, long_url=target).status_code == 201


def test_load_blocked_domains_parsing(tmp_path):
    path = tmp_path / "b.txt"
    path.write_text("# c\n\nA.example\nb.example # x\n   \n#only\nC.EXAMPLE.\n", encoding="utf-8")
    assert load_blocked_domains(path) == {"a.example", "b.example", "c.example"}


def test_bundled_blocklist_loads(settings):
    domains = load_blocked_domains(settings.blocked_domains_file)
    assert isinstance(domains, frozenset)


def test_missing_blocklist_file_fails_at_startup(app_factory, tmp_path):
    with pytest.raises(FileNotFoundError):
        app_factory(blocked_domains_file=str(tmp_path / "missing.txt"))


# --- self-referencing links -----------------------------------------------------------


def test_link_to_request_host_is_rejected(client, auth_headers):
    assert post(client, auth_headers, long_url="http://testserver/abc").status_code == 422
    assert post(client, auth_headers, long_url="https://TESTSERVER./abc").status_code == 422


def test_link_to_custom_request_host_is_rejected(client, auth_headers):
    headers = {**auth_headers, "Host": "short.example"}
    resp = client.post(URLS, json={"long_url": "https://short.example/x"}, headers=headers)
    assert resp.status_code == 422
    resp = client.post(URLS, json={"long_url": "https://other.example/x"}, headers=headers)
    assert resp.status_code == 201


def test_public_base_url_is_used_for_self_reference_and_short_url(make_client):
    client = make_client(public_base_url="https://sho.rt/")
    headers = register(client)
    assert post(client, headers, long_url="https://sho.rt/abc").status_code == 422
    assert post(client, headers, long_url="https://SHO.RT:8443/abc").status_code == 422
    resp = post(client, headers, long_url="https://other.example/", custom_alias="mine")
    assert resp.status_code == 201
    assert resp.json()["short_url"] == "https://sho.rt/mine"


def test_public_base_url_cannot_be_bypassed_with_spoofed_host(make_client):
    client = make_client(public_base_url="https://sho.rt")
    headers = {**register(client), "Host": "attacker.example"}
    resp = client.post(URLS, json={"long_url": "https://sho.rt/x"}, headers=headers)
    assert resp.status_code == 422


# --- check_target_url / is_public_ip unit tests -----------------------------------------


def _check(url, **kwargs):
    kwargs.setdefault("own_hosts", frozenset())
    kwargs.setdefault("blocked_domains", frozenset())
    kwargs.setdefault("resolver", public_resolver)
    return check_target_url(url, **kwargs)


def test_check_target_url_accepts_public():
    _check("https://example.com/")


def test_check_target_url_rejects_non_http_even_if_called_directly():
    with pytest.raises(UnsafeURLError):
        _check("ftp://example.com/")


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("8.8.8.8", True),
        ("93.184.216.34", True),
        ("2606:4700:4700::1111", True),
        ("127.0.0.1", False),
        ("10.0.0.1", False),
        ("172.16.0.1", False),
        ("192.168.0.1", False),
        ("169.254.1.1", False),
        ("0.0.0.0", False),
        ("::", False),
        ("::1", False),
        ("fc00::1", False),
        ("fe80::1", False),
        ("::ffff:8.8.8.8", True),
        ("::ffff:127.0.0.1", False),
    ],
)
def test_is_public_ip(address, expected):
    import ipaddress

    assert is_public_ip(ipaddress.ip_address(address)) is expected


def test_default_resolver_resolves_ip_literals_without_network():
    assert security.resolve_host("127.0.0.1") == ["127.0.0.1"]


# --- token bucket ------------------------------------------------------------------------


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_bucket_allows_burst_then_denies_with_retry_after():
    clock = FakeClock()
    limiter = TokenBucketLimiter(capacity=3, refill_per_second=0.5, clock=clock)
    assert [limiter.consume("k")[0] for _ in range(3)] == [True, True, True]
    allowed, retry_after = limiter.consume("k")
    assert allowed is False
    assert retry_after == 2  # one token every 2 seconds


def test_bucket_refills_over_time():
    clock = FakeClock()
    limiter = TokenBucketLimiter(capacity=2, refill_per_second=1, clock=clock)
    limiter.consume("k")
    limiter.consume("k")
    assert limiter.consume("k")[0] is False
    clock.now += 1
    assert limiter.consume("k")[0] is True
    assert limiter.consume("k")[0] is False


def test_bucket_refill_is_capped_at_capacity():
    clock = FakeClock()
    limiter = TokenBucketLimiter(capacity=2, refill_per_second=1, clock=clock)
    limiter.consume("k")
    clock.now += 3600
    assert [limiter.consume("k")[0] for _ in range(3)] == [True, True, False]


def test_bucket_retry_after_shrinks_as_time_passes():
    clock = FakeClock()
    limiter = TokenBucketLimiter(capacity=1, refill_per_second=0.1, clock=clock)
    limiter.consume("k")
    assert limiter.consume("k") == (False, 10)
    clock.now += 4
    assert limiter.consume("k") == (False, 6)


def test_bucket_retry_after_is_at_least_one_second():
    clock = FakeClock()
    limiter = TokenBucketLimiter(capacity=1, refill_per_second=1000, clock=clock)
    limiter.consume("k")
    allowed, retry_after = limiter.consume("k")
    assert allowed is False and retry_after == 1


def test_bucket_keys_are_independent():
    limiter = TokenBucketLimiter(capacity=1, refill_per_second=0.01, clock=FakeClock())
    assert limiter.consume("a")[0] is True
    assert limiter.consume("a")[0] is False
    assert limiter.consume("b")[0] is True


def test_bucket_memory_is_bounded():
    limiter = TokenBucketLimiter(capacity=1, refill_per_second=1, max_keys=3, clock=FakeClock())
    for i in range(10):
        limiter.consume(f"k{i}")
    assert len(limiter) == 3


def test_per_minute_constructor():
    clock = FakeClock()
    limiter = TokenBucketLimiter.per_minute(60, clock=clock)
    for _ in range(60):
        assert limiter.consume("k")[0] is True
    assert limiter.consume("k") == (False, 1)


@pytest.mark.parametrize("capacity", [0, 0.5])
def test_bucket_rejects_invalid_capacity(capacity):
    with pytest.raises(ValueError):
        TokenBucketLimiter(capacity=capacity, refill_per_second=1)


def test_bucket_rejects_invalid_refill():
    with pytest.raises(ValueError):
        TokenBucketLimiter(capacity=1, refill_per_second=0)


# --- rate limiting over HTTP -----------------------------------------------------------------


def assert_limited(resp):
    assert resp.status_code == 429
    retry_after = resp.headers["Retry-After"]
    assert retry_after.isdigit() and int(retry_after) >= 1
    assert resp.json()["detail"]


def test_create_is_limited_per_ip(make_client):
    client = make_client(rate_limit_create_ip_per_minute=2)
    headers = register(client)
    assert post(client, headers).status_code == 201
    assert post(client, headers).status_code == 201
    resp = post(client, headers)
    assert_limited(resp)
    assert int(resp.headers["Retry-After"]) <= 30  # 2/min refills one token per 30 s


def test_ip_limit_applies_across_api_keys(make_client):
    client = make_client(rate_limit_create_ip_per_minute=2)
    a, b = register(client), register(client)
    assert post(client, a).status_code == 201
    assert post(client, b).status_code == 201
    assert_limited(post(client, a))
    assert_limited(post(client, b))


def test_create_is_limited_per_api_key(make_client):
    client = make_client(rate_limit_create_key_per_minute=2)
    a, b = register(client), register(client)
    assert post(client, a).status_code == 201
    assert post(client, a).status_code == 201
    assert_limited(post(client, a))
    assert post(client, b).status_code == 201  # other key unaffected


def test_invalid_key_attempts_count_against_ip_limit(make_client):
    client = make_client(rate_limit_create_ip_per_minute=2)
    bad = {"X-API-Key": "wrong"}
    assert post(client, bad).status_code == 401
    assert post(client, bad).status_code == 401
    assert_limited(post(client, bad))


def test_redirect_is_limited_per_ip(make_client):
    client = make_client(rate_limit_redirect_ip_per_minute=2)
    headers = register(client)
    code = post(client, headers).json()["code"]
    assert client.get(f"/{code}", follow_redirects=False).status_code == 302
    assert client.get(f"/{code}", follow_redirects=False).status_code == 302  # cache hit
    assert_limited(client.get(f"/{code}", follow_redirects=False))
    assert_limited(client.get("/unknown1", follow_redirects=False))
    # create bucket is separate from the redirect bucket
    assert post(client, headers).status_code == 201


def test_registration_is_limited_per_ip(make_client):
    client = make_client(rate_limit_register_ip_per_minute=2)
    assert client.post("/api/v1/users").status_code == 201
    assert client.post("/api/v1/users").status_code == 201
    assert_limited(client.post("/api/v1/users"))


def test_reads_and_management_are_not_rate_limited(make_client):
    client = make_client(rate_limit_create_ip_per_minute=1, rate_limit_create_key_per_minute=1)
    headers = register(client)
    code = post(client, headers).json()["code"]
    for _ in range(5):
        assert client.get(URLS, headers=headers).status_code == 200
        assert (
            client.patch(f"{URLS}/{code}", json={"is_active": True}, headers=headers).status_code
            == 200
        )


def test_rate_limiting_can_be_disabled(make_client):
    client = make_client(
        rate_limit_enabled=False,
        rate_limit_create_ip_per_minute=1,
        rate_limit_redirect_ip_per_minute=1,
    )
    headers = register(client)
    for _ in range(5):
        assert post(client, headers).status_code == 201
    code = post(client, headers).json()["code"]
    for _ in range(5):
        assert client.get(f"/{code}", follow_redirects=False).status_code == 302


def test_default_limits_leave_normal_use_unaffected(client, auth_headers):
    for _ in range(10):
        assert post(client, auth_headers).status_code == 201


# --- security headers and CORS ------------------------------------------------------------------

EXPECTED_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "strict-origin-when-cross-origin",
}


def assert_security_headers(resp):
    for name, value in EXPECTED_HEADERS.items():
        assert resp.headers.get(name) == value, name


def test_security_headers_on_success_redirect_and_errors(client, auth_headers):
    code = post(client, auth_headers).json()["code"]
    assert_security_headers(client.get("/api/v1/users/me", headers=auth_headers))  # 200
    assert_security_headers(client.get(f"/{code}", follow_redirects=False))  # 302
    assert_security_headers(client.get("/missing1", follow_redirects=False))  # 404
    assert_security_headers(client.get("/api/v1/users/me"))  # 401
    assert_security_headers(client.post(URLS, json={}, headers=auth_headers))  # 422
    assert_security_headers(client.get("/docs"))


def test_security_headers_on_rate_limited_response(make_client):
    client = make_client(rate_limit_redirect_ip_per_minute=1)
    client.get("/x", follow_redirects=False)
    resp = client.get("/x", follow_redirects=False)
    assert resp.status_code == 429
    assert_security_headers(resp)


def test_cors_disabled_by_default(client):
    resp = client.get("/api/v1/users/me", headers={"Origin": "https://app.example"})
    assert "access-control-allow-origin" not in resp.headers


@pytest.fixture
def cors_client(make_client):
    return make_client(cors_allow_origins=["https://app.example"])


def test_cors_preflight_for_allowed_origin(cors_client):
    resp = cors_client.options(
        URLS,
        headers={
            "Origin": "https://app.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "X-API-Key, Content-Type",
        },
    )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "https://app.example"
    assert "POST" in resp.headers["access-control-allow-methods"]
    assert "x-api-key" in resp.headers["access-control-allow-headers"].lower()
    assert "access-control-allow-credentials" not in resp.headers
    assert_security_headers(resp)


def test_cors_actual_request_for_allowed_origin(cors_client):
    resp = cors_client.get("/api/v1/users/me", headers={"Origin": "https://app.example"})
    assert resp.headers["access-control-allow-origin"] == "https://app.example"
    assert_security_headers(resp)


def test_cors_rejects_unlisted_origin(cors_client):
    resp = cors_client.options(
        URLS,
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert resp.status_code == 400
    assert "access-control-allow-origin" not in resp.headers
    plain = cors_client.get("/api/v1/users/me", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in plain.headers
