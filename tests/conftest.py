import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.main import create_app
from app.models import Base

PEPPER = "test-pepper"


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        api_key_pepper=PEPPER,
        sqlite_busy_timeout_ms=5000,
    )


PUBLIC_IP = "93.184.216.34"


def public_resolver(host: str) -> list[str]:
    """Stand-in for DNS so tests never touch the network."""
    return [PUBLIC_IP]


@pytest.fixture
def app_factory(settings):
    """Build apps with settings overrides and/or a custom resolver; engines are disposed after."""
    apps = []

    def build(resolver=public_resolver, **overrides):
        app = create_app(settings.model_copy(update=overrides), resolver=resolver)
        Base.metadata.create_all(app.state.engine)
        apps.append(app)
        return app

    yield build
    for app in apps:
        app.state.engine.dispose()


@pytest.fixture
def app(app_factory):
    return app_factory()


@pytest.fixture
def client(app):
    with TestClient(app) as client:
        yield client


@pytest.fixture
def db(app):
    with Session(app.state.engine) as session:
        yield session


@pytest.fixture
def auth_headers(client) -> dict[str, str]:
    api_key = client.post("/api/v1/users").json()["api_key"]
    return {"X-API-Key": api_key}


@pytest.fixture
def make_client(app_factory):
    """Client for an app built with custom settings/resolver: make_client(**overrides)."""
    clients = []

    def build(**kwargs):
        client = TestClient(app_factory(**kwargs))
        client.__enter__()
        clients.append(client)
        return client

    yield build
    for client in clients:
        client.__exit__(None, None, None)


def register(client) -> dict[str, str]:
    """Create a user on `client` and return its auth headers."""
    return {"X-API-Key": client.post("/api/v1/users").json()["api_key"]}
