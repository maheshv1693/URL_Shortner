import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


class HealthOut(BaseModel):
    status: str
    database: str


@router.get(
    "/health",
    response_model=HealthOut,
    responses={503: {"model": HealthOut, "description": "Database unavailable"}},
)
def health(request: Request):
    """Liveness plus a `SELECT 1` against the database; 503 when the database is unreachable."""
    try:
        with request.app.state.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:
        logger.exception("health check: database unavailable")
        return JSONResponse(
            status_code=503,
            content=HealthOut(status="degraded", database="unavailable").model_dump(),
        )
    return HealthOut(status="ok", database="ok")
