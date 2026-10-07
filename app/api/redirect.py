import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.api.deps import DbSession, client_ip, limit_redirect_by_ip
from app.models import URL
from app.models.base import utcnow
from app.services.analytics_service import build_click_event
from app.services.link_cache import CachedLink

logger = logging.getLogger(__name__)

router = APIRouter(tags=["redirect"])


def _record_click(request: Request, link: CachedLink, code: str) -> None:
    """Queue a click event. Analytics must never affect the redirect, so errors are swallowed."""
    try:
        event = build_click_event(
            url_id=link.url_id,
            code=code,
            client_ip=client_ip(request),
            user_agent=request.headers.get("user-agent"),
            referrer=request.headers.get("referer"),
            pepper=request.app.state.settings.api_key_pepper,
            clicked_at=utcnow(),
        )
        request.app.state.click_recorder.record(event)
    except Exception:
        logger.exception("failed to queue click event")


@router.get(
    "/{code}",
    status_code=status.HTTP_302_FOUND,
    response_class=RedirectResponse,
    dependencies=[Depends(limit_redirect_by_ip)],
    responses={
        404: {"description": "Unknown or deleted code"},
        410: {"description": "Expired or disabled"},
        429: {"description": "Rate limit exceeded"},
    },
)
def redirect_to_long_url(code: str, request: Request, db: DbSession) -> RedirectResponse:
    cache = request.app.state.link_cache
    link = cache.get(code)

    if link is None:
        # The ticket must exist before the query so a concurrent invalidation voids our store.
        with cache.loading(code) as ticket:
            row = db.execute(
                select(URL.id, URL.long_url, URL.expires_at, URL.is_active).where(URL.code == code)
            ).first()
            if row is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "short URL not found")
            if not row.is_active:
                raise HTTPException(status.HTTP_410_GONE, "short URL is no longer available")
            link = CachedLink(url_id=row.id, long_url=row.long_url, expires_at=row.expires_at)
            ticket.store(link)

    # Expiry is evaluated on every request, including cache hits.
    if link.expires_at is not None and link.expires_at <= utcnow():
        raise HTTPException(status.HTTP_410_GONE, "short URL is no longer available")

    _record_click(request, link, code)
    return RedirectResponse(link.long_url, status_code=status.HTTP_302_FOUND)
