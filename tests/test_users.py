from sqlalchemy import select

from app.core.security import hash_api_key
from app.models import User
from tests.conftest import PEPPER


def test_create_user_returns_key_once_and_stores_only_hash(client, db):
    resp = client.post("/api/v1/users")
    assert resp.status_code == 201
    body = resp.json()
    api_key = body["api_key"]
    assert len(api_key) >= 43  # token_urlsafe(32)

    stored = db.scalars(select(User)).all()
    assert len(stored) == 1
    assert stored[0].id == body["id"]
    assert stored[0].api_key_hash == hash_api_key(api_key, PEPPER)
    assert stored[0].api_key_hash != api_key
    assert api_key not in stored[0].api_key_hash


def test_each_user_gets_distinct_key(client):
    keys = {client.post("/api/v1/users").json()["api_key"] for _ in range(3)}
    assert len(keys) == 3


def test_me_with_valid_key(client):
    created = client.post("/api/v1/users").json()
    resp = client.get("/api/v1/users/me", headers={"X-API-Key": created["api_key"]})
    assert resp.status_code == 200
    assert resp.json()["id"] == created["id"]
    assert "api_key" not in resp.json()


def test_me_without_key_is_401(client):
    resp = client.get("/api/v1/users/me")
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == "ApiKey"


def test_me_with_invalid_key_is_401(client):
    client.post("/api/v1/users")
    resp = client.get("/api/v1/users/me", headers={"X-API-Key": "not-a-real-key"})
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == "ApiKey"


def test_me_with_empty_key_is_401(client):
    resp = client.get("/api/v1/users/me", headers={"X-API-Key": ""})
    assert resp.status_code == 401


def test_hash_depends_on_pepper():
    assert hash_api_key("k", "p1") != hash_api_key("k", "p2")
    assert hash_api_key("k", "p1") == hash_api_key("k", "p1")
