from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.rate_limit import TokenBucketLimiter
from app.models import User
from app.services.users import get_user_by_api_key

DbSession = Annotated[Session, Depends(get_db)]


def get_current_user(
    request: Request,
    db: DbSession,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> User:
    user = None
    if x_api_key:
        user = get_user_by_api_key(db, x_api_key, request.app.state.settings.api_key_pepper)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _enforce(request: Request, pick_limiter, key: str) -> None:
    limiters = request.app.state.limiters
    if not limiters.enabled:
        return
    limiter: TokenBucketLimiter = pick_limiter(limiters)
    allowed, retry_after = limiter.consume(key)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded",
            headers={"Retry-After": str(retry_after)},
        )


def limit_register_by_ip(request: Request) -> None:
    _enforce(request, lambda limiters: limiters.register_ip, client_ip(request))


def limit_create_by_ip(request: Request) -> None:
    _enforce(request, lambda limiters: limiters.create_ip, client_ip(request))


def limit_create_by_key(request: Request, user: CurrentUser) -> None:
    _enforce(request, lambda limiters: limiters.create_key, f"user:{user.id}")


def limit_redirect_by_ip(request: Request) -> None:
    _enforce(request, lambda limiters: limiters.redirect_ip, client_ip(request))


def public_base_url(request: Request) -> str:
    configured = request.app.state.settings.public_base_url
    return (configured or str(request.base_url)).rstrip("/")
