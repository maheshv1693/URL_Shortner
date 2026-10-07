from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings


def create_db_engine(settings: Settings) -> Engine:
    connect_args = {}
    if settings.database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    engine = create_engine(settings.database_url, connect_args=connect_args)

    if engine.dialect.name == "sqlite":
        busy_timeout_ms = int(settings.sqlite_busy_timeout_ms)

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL;")
                cursor.execute(f"PRAGMA busy_timeout={busy_timeout_ms};")
                cursor.execute("PRAGMA foreign_keys=ON;")
                cursor.execute("PRAGMA synchronous=NORMAL;")
            finally:
                cursor.close()

    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


async def get_db(request: Request) -> AsyncIterator[Session]:
    # Rule: never hold a pooled connection across a hop to another worker thread.
    # A request hops between threads several times (dependencies, handler, response
    # serialisation, dependency cleanup). Under a burst every worker thread can end up blocked
    # waiting for a connection that a half-finished request holds while it waits for a thread,
    # which deadlocks until the pool times out. So handlers that only read end their
    # transaction (`db.close()`) before returning; commits release the connection themselves.
    # This dependency is `async` so that creating and closing the session need no thread.
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()
