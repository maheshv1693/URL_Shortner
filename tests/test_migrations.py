import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext

from alembic import command
from app.models import Base


def test_migration_upgrades_and_matches_models(tmp_path, monkeypatch):
    db_url = f"sqlite:///{tmp_path / 'mig.db'}"
    monkeypatch.setenv("URLSHORT_DATABASE_URL", db_url)
    from app.core.config import get_settings

    get_settings.cache_clear()
    try:
        command.upgrade(Config("alembic.ini"), "head")
    finally:
        get_settings.cache_clear()

    engine = sa.create_engine(db_url)
    with engine.connect() as conn:
        assert {"users", "urls", "clicks"} <= set(sa.inspect(conn).get_table_names())
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    engine.dispose()
    assert diff == []
