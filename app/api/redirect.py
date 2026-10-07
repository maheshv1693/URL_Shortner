from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.api.deps import DbSession
from app.models import URL
from app.models.base import utcnow
from app.services.link_cache import CachedLink

router = APIRouter(tags=["redirect"])


@router.get(
    "/{code}",
    status_code=status.HTTP_302_FOUND,
    response_class=RedirectResponse,
    responses={
        404: {"description": "Unknown or deleted code"},
        410: {"description": "Expired or disabled"},
    },
)
def redirect_to_long_url(code: str, request: Request, db: DbSession) -> RedirectResponse:
    cache = request.app.state.link_cache
    link = cache.get(code)

    if link is None:
        row = db.execute(
            select(URL.long_url, URL.expires_at, URL.is_active).where(URL.code == code)
        ).first()
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "short URL not found")
        if not row.is_active:
            raise HTTPException(status.HTTP_410_GONE, "short URL is no longer available")
        link = CachedLink(long_url=row.long_url, expires_at=row.expires_at)
        cache.put(code, link)

    # Expiry is evaluated on every request, including cache hits.
    if link.expires_at is not None and link.expires_at <= utcnow():
        raise HTTPException(status.HTTP_410_GONE, "short URL is no longer available")

    return RedirectResponse(link.long_url, status_code=status.HTTP_302_FOUND)
