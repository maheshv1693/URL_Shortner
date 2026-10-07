from datetime import UTC, datetime, timedelta
from typing import Self

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import (
    BaseModel,
    Field,
    HttpUrl,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from app.api.deps import CurrentUser, DbSession
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


class URLOut(BaseModel):
    code: str
    short_url: str
    long_url: str
    created_at: datetime
    expires_at: datetime | None
    is_active: bool


@router.post("", response_model=URLOut, status_code=status.HTTP_201_CREATED)
def create_short_url(
    payload: URLCreate, request: Request, db: DbSession, user: CurrentUser
) -> URLOut:
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

    return URLOut(
        code=url.code,
        short_url=f"{str(request.base_url).rstrip('/')}/{url.code}",
        long_url=url.long_url,
        created_at=url.created_at,
        expires_at=url.expires_at,
        is_active=url.is_active,
    )
