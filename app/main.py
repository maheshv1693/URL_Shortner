from fastapi import FastAPI

from app.api.v1 import api_router
from app.core.config import Settings, get_settings
from app.core.database import create_db_engine, create_session_factory


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="URL Shortener")

    engine = create_db_engine(settings)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)

    app.include_router(api_router, prefix=settings.api_v1_prefix)
    return app


app = create_app()
