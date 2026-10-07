from datetime import UTC, datetime

from sqlalchemy import func, select, text

from app.models import URL, Click, User


def test_sqlite_pragmas_applied(app):
    with app.state.engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert conn.execute(text("PRAGMA busy_timeout")).scalar() == 5000
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert conn.execute(text("PRAGMA synchronous")).scalar() == 1  # NORMAL


def test_busy_timeout_follows_settings(tmp_path):
    from app.core.config import Settings
    from app.core.database import create_db_engine

    engine = create_db_engine(
        Settings(database_url=f"sqlite:///{tmp_path / 'x.db'}", sqlite_busy_timeout_ms=1234)
    )
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA busy_timeout")).scalar() == 1234
    engine.dispose()


def test_datetimes_round_trip_as_utc(db):
    user = User(api_key_hash="h1")
    db.add(user)
    db.commit()
    db.expire_all()
    assert db.get(User, user.id).created_at.tzinfo is not None
    assert db.get(User, user.id).created_at.utcoffset().total_seconds() == 0
    assert datetime.now(UTC) >= db.get(User, user.id).created_at


def _seed(db):
    user = User(api_key_hash="h1")
    db.add(user)
    db.flush()
    url = URL(code="abc1234", long_url="https://example.com", owner_id=user.id)
    db.add(url)
    db.flush()
    db.add(Click(url_id=url.id, ip_hash="ip", user_agent="ua", referrer="ref"))
    db.commit()
    return user, url


def test_url_defaults(db):
    _, url = _seed(db)
    assert url.is_active is True
    assert url.expires_at is None


def test_deleting_url_cascades_to_clicks(db):
    _, url = _seed(db)
    db.execute(URL.__table__.delete().where(URL.id == url.id))
    db.commit()
    assert db.scalar(select(func.count()).select_from(Click)) == 0


def test_deleting_user_cascades_to_urls_and_clicks(db):
    user, _ = _seed(db)
    db.execute(User.__table__.delete().where(User.id == user.id))
    db.commit()
    assert db.scalar(select(func.count()).select_from(URL)) == 0
    assert db.scalar(select(func.count()).select_from(Click)) == 0
