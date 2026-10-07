from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import URL
from app.services import token_service
from app.services.token_service import BASE62_ALPHABET, generate_token, validate_alias

URLS = "/api/v1/urls"
LONG = "https://example.com/some/long/path?x=1"


def create(client, headers, **body):
    return client.post(URLS, json={"long_url": LONG, **body}, headers=headers)


# --- token generation -------------------------------------------------------


def test_token_length_and_charset():
    for _ in range(200):
        token = generate_token()
        assert len(token) == 7
        assert set(token) <= set(BASE62_ALPHABET)


def test_alphabet_is_base62():
    assert len(BASE62_ALPHABET) == 62
    assert len(set(BASE62_ALPHABET)) == 62


def test_tokens_are_random():
    tokens = {generate_token() for _ in range(1000)}
    assert len(tokens) == 1000
    # all character classes show up across a large sample
    joined = "".join(tokens)
    assert any(c.isupper() for c in joined)
    assert any(c.islower() for c in joined)
    assert any(c.isdigit() for c in joined)


@pytest.mark.parametrize("alias", ["abc", "My-Alias_1", "a" * 32, "ABC123"])
def test_validate_alias_accepts(alias):
    assert validate_alias(alias) == alias


@pytest.mark.parametrize(
    "alias",
    ["ab", "a" * 33, "has space", "bad!", "dot.dot", "ünï", "abc\n", ""]
    + ["api", "docs", "health", "redoc", "openapi.json", "API", "Docs"],
)
def test_validate_alias_rejects(alias):
    with pytest.raises(token_service.InvalidAliasError):
        validate_alias(alias)


# --- POST /api/v1/urls ------------------------------------------------------


def test_create_requires_auth(client):
    resp = client.post(URLS, json={"long_url": LONG})
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == "ApiKey"


def test_create_with_generated_code(client, auth_headers, db):
    resp = create(client, auth_headers)
    assert resp.status_code == 201
    body = resp.json()
    assert len(body["code"]) == 7
    assert set(body["code"]) <= set(BASE62_ALPHABET)
    assert body["short_url"] == f"http://testserver/{body['code']}"
    assert body["long_url"] == LONG
    assert body["expires_at"] is None
    assert body["is_active"] is True
    assert body["created_at"]
    assert db.scalar(select(URL).where(URL.code == body["code"])).long_url == LONG


def test_create_stores_url_exactly_as_given(client, auth_headers):
    resp = create(client, auth_headers, long_url="https://example.com")
    assert resp.json()["long_url"] == "https://example.com"


def test_create_with_custom_alias(client, auth_headers):
    resp = create(client, auth_headers, custom_alias="my-link_1")
    assert resp.status_code == 201
    assert resp.json()["code"] == "my-link_1"
    assert resp.json()["short_url"].endswith("/my-link_1")


def test_alias_conflict_is_409(client, auth_headers):
    assert create(client, auth_headers, custom_alias="taken").status_code == 201
    assert create(client, auth_headers, custom_alias="taken").status_code == 409


def test_alias_is_case_sensitive(client, auth_headers):
    assert create(client, auth_headers, custom_alias="Taken").status_code == 201
    assert create(client, auth_headers, custom_alias="taken").status_code == 201


def test_alias_conflict_across_users(client, auth_headers):
    other = {"X-API-Key": client.post("/api/v1/users").json()["api_key"]}
    assert create(client, auth_headers, custom_alias="shared").status_code == 201
    assert create(client, other, custom_alias="shared").status_code == 409


@pytest.mark.parametrize("alias", ["api", "docs", "health", "redoc", "openapi.json"])
def test_reserved_alias_is_422(client, auth_headers, alias):
    assert create(client, auth_headers, custom_alias=alias).status_code == 422


@pytest.mark.parametrize("alias", ["ab", "a" * 33, "bad alias", "slash/y", "dollar$", ""])
def test_invalid_alias_pattern_is_422(client, auth_headers, alias):
    assert create(client, auth_headers, custom_alias=alias).status_code == 422


@pytest.mark.parametrize(
    "long_url",
    ["ftp://example.com/x", "javascript:alert(1)", "not a url", "example.com", "http://", ""],
)
def test_invalid_long_url_is_422(client, auth_headers, long_url):
    assert create(client, auth_headers, long_url=long_url).status_code == 422


def test_missing_long_url_is_422(client, auth_headers):
    assert client.post(URLS, json={}, headers=auth_headers).status_code == 422


def test_ttl_sets_expires_at(client, auth_headers):
    before = datetime.now(UTC)
    resp = create(client, auth_headers, ttl_seconds=3600)
    assert resp.status_code == 201
    expires = datetime.fromisoformat(resp.json()["expires_at"])
    assert before + timedelta(seconds=3590) <= expires <= before + timedelta(seconds=3610)


def test_explicit_expires_at_is_normalised_to_utc(client, auth_headers):
    target = datetime.now(UTC).replace(microsecond=0) + timedelta(days=1)
    local = target.astimezone(timezone(timedelta(hours=5, minutes=30)))
    resp = create(client, auth_headers, expires_at=local.isoformat())
    assert resp.status_code == 201
    assert datetime.fromisoformat(resp.json()["expires_at"]) == target


def test_ttl_and_expires_at_are_mutually_exclusive(client, auth_headers):
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    resp = create(client, auth_headers, ttl_seconds=60, expires_at=future)
    assert resp.status_code == 422


@pytest.mark.parametrize("ttl", [0, -5, 10**12, "soon"])
def test_invalid_ttl_is_422(client, auth_headers, ttl):
    assert create(client, auth_headers, ttl_seconds=ttl).status_code == 422


def test_expires_at_in_past_is_422(client, auth_headers):
    past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    assert create(client, auth_headers, expires_at=past).status_code == 422


def test_expires_at_without_timezone_is_422(client, auth_headers):
    naive = (datetime.now(UTC) + timedelta(days=1)).replace(tzinfo=None).isoformat()
    assert create(client, auth_headers, expires_at=naive).status_code == 422


# --- collision handling -----------------------------------------------------


def _fake_generator(monkeypatch, values):
    calls = []
    sequence = iter(values)

    def fake():
        value = next(sequence)
        calls.append(value)
        return value

    monkeypatch.setattr(token_service, "generate_token", fake)
    return calls


def test_collision_is_retried_until_unique(client, auth_headers, monkeypatch):
    assert create(client, auth_headers, custom_alias="AAAAAAA").status_code == 201
    calls = _fake_generator(monkeypatch, ["AAAAAAA", "AAAAAAA", "BBBBBBB"])

    resp = create(client, auth_headers)

    assert resp.status_code == 201
    assert resp.json()["code"] == "BBBBBBB"
    assert calls == ["AAAAAAA", "AAAAAAA", "BBBBBBB"]


def test_exhausted_retries_return_503(client, auth_headers, db, monkeypatch):
    assert create(client, auth_headers, custom_alias="AAAAAAA").status_code == 201
    calls = _fake_generator(monkeypatch, ["AAAAAAA"] * 5)

    resp = create(client, auth_headers)

    assert resp.status_code == 503
    assert len(calls) == 3  # exactly three attempts, no more
    assert len(db.scalars(select(URL)).all()) == 1


def test_service_works_after_failed_attempts(client, auth_headers, monkeypatch):
    # the session must be usable (rolled back) after collisions
    assert create(client, auth_headers, custom_alias="AAAAAAA").status_code == 201
    _fake_generator(monkeypatch, ["AAAAAAA", "AAAAAAA", "AAAAAAA"])
    assert create(client, auth_headers).status_code == 503
    monkeypatch.undo()
    assert create(client, auth_headers).status_code == 201


def test_custom_alias_is_not_retried(client, auth_headers, monkeypatch):
    calls = _fake_generator(monkeypatch, [])
    create(client, auth_headers, custom_alias="once")
    assert create(client, auth_headers, custom_alias="once").status_code == 409
    assert calls == []
