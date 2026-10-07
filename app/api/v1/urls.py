from datetime import UTC, datetime, timedelta
from typing import Annotated, Self

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StrictBool,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy import func, select

from app.api.deps import (
    CurrentUser,
    DbSession,
    limit_create_by_ip,
    limit_create_by_key,
    public_base_url,
)
from app.core import security
from app.models import URL
from app.models.base import utcnow
from app.services import token_service

router = APIRouter(prefix="/urls", tags=["urls"])

MAX_TTL_SECONDS = 10 * 365 * 24 * 3600
_http_url = TypeAdapter(HttpUrl)


class URLCreate(BaseModel):
    long_url: str
    custom_alias: str | None = None
    ttl_seconds: int | None = Field(default=None, gt=0, le=MAX_TTL_SECONDS)
    expires_at: datetime | None = None

    @field_validator("long_url")
    @classmethod
    def _check_long_url(cls, value: str) -> str:
        value = value.strip()
        if len(value) > security.MAX_URL_LENGTH:
            raise ValueError(f"long_url must be at most {security.MAX_URL_LENGTH} characters")
        try:
            _http_url.validate_python(value)
        except ValidationError as exc:
            raise ValueError("long_url must be a valid http or https URL") from exc
        return value

    @field_validator("custom_alias")
    @classmethod
    def _check_alias(cls, value: str | None) -> str | None:
        return None if value is None else token_service.validate_alias(value)

    @field_validator("expires_at")
    @classmethod
    def _check_expires_at(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("expires_at must include a UTC offset (for example 'Z')")
        value = value.astimezone(UTC)
        if value <= utcnow():
            raise ValueError("expires_at must be in the future")
        return value

    @model_validator(mode="after")
    def _ttl_xor_expires_at(self) -> Self:
        if self.ttl_seconds is not None and self.expires_at is not None:
            raise ValueError("provide either ttl_seconds or expires_at, not both")
        return self

    def resolve_expiry(self) -> datetime | None:
        if self.ttl_seconds is not None:
            return utcnow() + timedelta(seconds=self.ttl_seconds)
        return self.expires_at


class URLUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_active: StrictBool


class URLOut(BaseModel):
    code: str
    short_url: str
    long_url: str
    created_at: datetime
    expires_at: datetime | None
    is_active: bool


class URLPage(BaseModel):
    items: list[URLOut]
    total: int
    limit: int
    offset: int


def _to_out(url: URL, base_url: str) -> URLOut:
    return URLOut(
        code=url.code,
        short_url=f"{base_url}/{url.code}",
        long_url=url.long_url,
        created_at=url.created_at,
        expires_at=url.expires_at,
        is_active=url.is_active,
    )


def _get_owned_url(db: DbSession, code: str, user: CurrentUser) -> URL:
    # Same 404 for "missing" and "someone else's" so codes cannot be enumerated.
    url = db.scalar(select(URL).where(URL.code == code, URL.owner_id == user.id))
    if url is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "short URL not found")
    return url


@router.post(
    "",
    response_model=URLOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(limit_create_by_ip), Depends(limit_create_by_key)],
)
def create_short_url(
    payload: URLCreate, request: Request, db: DbSession, user: CurrentUser
) -> URLOut:
    state = request.app.state
    settings = state.settings
    try:
        security.check_target_url(
            payload.long_url,
            own_hosts=security.own_hostnames(request.url.hostname, settings.public_base_url),
            blocked_domains=state.blocked_domains,
            resolver=state.host_resolver,
        )
    except security.UnsafeURLError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None

    try:
        url = token_service.create_url(
            db,
            long_url=payload.long_url,
            owner_id=user.id,
            expires_at=payload.resolve_expiry(),
            custom_alias=payload.custom_alias,
        )
    except token_service.AliasTakenError:
        raise HTTPException(status.HTTP_409_CONFLICT, "custom alias is already taken") from None
    except token_service.CodeGenerationError:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "could not generate a unique short code, please retry",
        ) from None

    return _to_out(url, public_base_url(request))


@router.get("", response_model=URLPage)
def list_urls(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> URLPage:
    total = db.scalar(select(func.count()).select_from(URL).where(URL.owner_id == user.id))
    rows = db.scalars(
        select(URL)
        .where(URL.owner_id == user.id)
        .order_by(URL.created_at.desc(), URL.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    base_url = public_base_url(request)
    return URLPage(
        items=[_to_out(row, base_url) for row in rows],
        total=total or 0,
        limit=limit,
        offset=offset,
    )


@router.patch("/{code}", response_model=URLOut)
def update_url(
    code: str, payload: URLUpdate, request: Request, db: DbSession, user: CurrentUser
) -> URLOut:
    url = _get_owned_url(db, code, user)
    url.is_active = payload.is_active
    db.commit()
    # Invalidate only after the commit so a concurrent reader cannot re-cache the old state.
    request.app.state.link_cache.invalidate(code)
    return _to_out(url, public_base_url(request))


@router.delete("/{code}", status_code=status.HTTP_204_NO_CONTENT)
def delete_url(code: str, request: Request, db: DbSession, user: CurrentUser) -> Response:
    url = _get_owned_url(db, code, user)
    db.delete(url)  # clicks are removed by the database (ON DELETE CASCADE)
    db.commit()
    request.app.state.link_cache.invalidate(code)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
