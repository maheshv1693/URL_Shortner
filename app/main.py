from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.redirect import router as redirect_router
from app.api.v1 import api_router
from app.core.config import Settings, get_settings
from app.core.database import create_db_engine, create_session_factory
from app.core.rate_limit import RateLimiters
from app.core.security import HostResolver, load_blocked_domains, resolve_host
from app.middleware.security_headers import SecurityHeadersMiddleware
from app.services.link_cache import LinkCache


def create_app(settings: Settings | None = None, resolver: HostResolver | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="URL Shortener")

    engine = create_db_engine(settings)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    app.state.link_cache = LinkCache(settings.redirect_cache_size)
    app.state.limiters = RateLimiters.from_settings(settings)
    app.state.blocked_domains = load_blocked_domains(settings.blocked_domains_file)
    app.state.host_resolver = resolver or resolve_host

    app.include_router(api_router, prefix=settings.api_v1_prefix)
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
