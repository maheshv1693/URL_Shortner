import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio.to_thread
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.health import router as health_router
from app.api.redirect import router as redirect_router
from app.api.v1 import api_router
from app.core.config import Settings, get_settings
from app.core.database import create_db_engine, create_session_factory
from app.core.rate_limit import RateLimiters
from app.core.security import HostResolver, load_blocked_domains, resolve_host
from app.middleware.security_headers import SecurityHeadersMiddleware
from app.services.analytics_service import ClickRecorder
from app.services.link_cache import LinkCache

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    recorder: ClickRecorder = app.state.click_recorder
    recorder.start()
    try:
        yield
    finally:
        # Write out clicks still buffered in memory before the process exits.
        timeout = app.state.settings.analytics_shutdown_timeout_seconds
        flushed = await anyio.to_thread.run_sync(recorder.shutdown, timeout)
        if not flushed:
            logger.error("click recorder did not flush within %.1fs: %s", timeout, recorder.stats())
        app.state.engine.dispose()


def create_app(settings: Settings | None = None, resolver: HostResolver | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="URL Shortener", lifespan=lifespan)

    engine = create_db_engine(settings)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    app.state.link_cache = LinkCache(settings.redirect_cache_size)
    app.state.click_recorder = ClickRecorder(
        app.state.session_factory,
        batch_size=settings.analytics_batch_size,
        flush_interval=settings.analytics_flush_interval_seconds,
        max_queue=settings.analytics_queue_max,
    )
    app.state.limiters = RateLimiters.from_settings(settings)
    app.state.blocked_domains = load_blocked_domains(settings.blocked_domains_file)
    app.state.host_resolver = resolver or resolve_host

    app.include_router(api_router, prefix=settings.api_v1_prefix)
    app.include_router(health_router)  # unprefixed `/health` for container probes
    # Catch-all `/{code}` must be registered after every other route.
    app.include_router(redirect_router)

    # The last middleware added is the outermost, so security headers also cover CORS responses.
    if settings.cors_allow_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allow_origins,
            allow_methods=["GET", "POST", "PATCH", "DELETE"],
            allow_headers=["X-API-Key", "Content-Type"],
            allow_credentials=False,
        )
    app.add_middleware(SecurityHeadersMiddleware)
    return app


app = create_app()
