from fastapi import FastAPI

from app.api.redirect import router as redirect_router
from app.api.v1 import api_router
from app.core.config import Settings, get_settings
from app.core.database import create_db_engine, create_session_factory
from app.services.link_cache import LinkCache


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="URL Shortener")

    engine = create_db_engine(settings)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    app.state.link_cache = LinkCache(settings.redirect_cache_size)

    app.include_router(api_router, prefix=settings.api_v1_prefix)
    # Catch-all `/{code}` must be registered after every other route.
    app.include_router(redirect_router)
    return app


app = create_app()
