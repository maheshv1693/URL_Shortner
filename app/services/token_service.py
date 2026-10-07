import logging
import re
import secrets
import string
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import URL

logger = logging.getLogger(__name__)

BASE62_ALPHABET = string.ascii_uppercase + string.ascii_lowercase + string.digits
TOKEN_LENGTH = 7
MAX_INSERT_ATTEMPTS = 3
ALIAS_PATTERN = r"^[A-Za-z0-9_-]{3,32}$"
RESERVED_WORDS = frozenset({"api", "docs", "health", "redoc", "openapi.json"})

_ALIAS_RE = re.compile(r"[A-Za-z0-9_-]{3,32}")


class InvalidAliasError(ValueError):
    """Alias does not match the allowed pattern or is a reserved word (maps to 422)."""


class AliasTakenError(Exception):
    """Requested custom alias already exists (maps to 409)."""


class CodeGenerationError(Exception):
    """No unique code could be inserted within the attempt limit (maps to 503)."""


def generate_token(length: int = TOKEN_LENGTH) -> str:
    return "".join(secrets.choice(BASE62_ALPHABET) for _ in range(length))


def validate_alias(alias: str) -> str:
    # fullmatch avoids `$` accepting a trailing newline
    if not _ALIAS_RE.fullmatch(alias):
        raise InvalidAliasError("alias must be 3-32 characters of A-Z, a-z, 0-9, '_' or '-'")
    if alias.lower() in RESERVED_WORDS:
        raise InvalidAliasError("alias is a reserved word")
    return alias


def _is_code_collision(exc: IntegrityError) -> bool:
    message = str(exc.orig)
    return "urls.code" in message or "(code)" in message


def _insert(db: Session, code: str, long_url: str, owner_id: int, expires_at: datetime | None):
    url = URL(code=code, long_url=long_url, owner_id=owner_id, expires_at=expires_at)
    db.add(url)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if not _is_code_collision(exc):
            raise
        return None
    return url


def create_url(
    db: Session,
    *,
    long_url: str,
    owner_id: int,
    expires_at: datetime | None = None,
    custom_alias: str | None = None,
) -> URL:
    """Insert a URL row, relying on the unique constraint on `urls.code` for collisions."""
    if custom_alias is not None:
        validate_alias(custom_alias)
        url = _insert(db, custom_alias, long_url, owner_id, expires_at)
        if url is None:
            raise AliasTakenError(custom_alias)
        return url

    for attempt in range(1, MAX_INSERT_ATTEMPTS + 1):
        url = _insert(db, generate_token(), long_url, owner_id, expires_at)
        if url is not None:
            return url
        logger.warning("short code collision (attempt %d/%d)", attempt, MAX_INSERT_ATTEMPTS)

    logger.error("could not generate a unique short code after %d attempts", MAX_INSERT_ATTEMPTS)
    raise CodeGenerationError
