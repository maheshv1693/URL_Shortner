import logging
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from urllib.parse import urlsplit

from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.core.security import hash_ip
from app.models import URL, Click

logger = logging.getLogger(__name__)

MAX_USER_AGENT_LENGTH = 512
MAX_REFERRER_LENGTH = 2048
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_REFERRER_FORBIDDEN = re.compile(r"[\x00-\x20\x7f]")


# --- click events ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClickEvent:
    url_id: int
    code: str
    clicked_at: datetime
    ip_hash: str | None
    user_agent: str | None
    referrer: str | None


def sanitize_user_agent(value: str | None) -> str | None:
    """Drop control characters (log/header injection) and truncate to the column size."""
    if not value:
        return None
    cleaned = _CONTROL_CHARS.sub("", value).strip()
    return cleaned[:MAX_USER_AGENT_LENGTH] or None


def sanitize_referrer(value: str | None) -> str | None:
    """Keep only `scheme://host[:port]` of an http(s) referrer.

    Paths, query strings, fragments and credentials can carry tokens or personal data, so
    they are discarded. Anything that is not a well-formed http(s) URL becomes None.
    """
    if not value or _REFERRER_FORBIDDEN.search(value.strip()):
        return None
    try:
        parts = urlsplit(value.strip())
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not host:
        return None
    if ":" in host:
        host = f"[{host}]"
    origin = f"{parts.scheme}://{host}" + (f":{port}" if port else "")
    return origin[:MAX_REFERRER_LENGTH]


def build_click_event(
    *,
    url_id: int,
    code: str,
    client_ip: str,
    user_agent: str | None,
    referrer: str | None,
    pepper: str,
    clicked_at: datetime,
) -> ClickEvent:
    return ClickEvent(
        url_id=url_id,
        code=code,
        clicked_at=clicked_at,
        ip_hash=hash_ip(client_ip, pepper),
        user_agent=sanitize_user_agent(user_agent),
        referrer=sanitize_referrer(referrer),
    )


# --- asynchronous batch writer ------------------------------------------------------------


class ClickRecorder:
    """Queues click events in memory and writes them to the database in batches.

    `record()` only appends to a bounded in-memory buffer, so the redirect never waits on
    the database. A single worker thread writes batches; busy/locked errors are retried and
    then the batch is dropped (analytics are best effort). `shutdown()` drains the buffer.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        batch_size: int = 100,
        flush_interval: float = 1.0,
        max_queue: int = 10_000,
        write_attempts: int = 5,
        retry_backoff: float = 0.1,
    ) -> None:
        self._session_factory = session_factory
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._max_queue = max_queue
        self._write_attempts = write_attempts
        self._retry_backoff = retry_backoff

        self._buffer: deque[ClickEvent] = deque()
        self._cond = threading.Condition()
        self._thread: threading.Thread | None = None
        self._stopping = False
        self._closed = True  # until start()
        self._flush_requested = False
        self._writing = False
        self._counts = {"written": 0, "dropped": 0, "skipped": 0, "failed": 0}

    # -- lifecycle --

    def start(self) -> None:
        with self._cond:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping = False
            self._closed = False
            self._thread = threading.Thread(target=self._run, name="click-recorder", daemon=True)
            self._thread.start()

    def shutdown(self, timeout: float | None = None) -> bool:
        """Stop accepting events, write everything still buffered, stop the worker.

        Returns True if the buffer was fully flushed within `timeout` seconds.
        """
        with self._cond:
            self._closed = True
            self._stopping = True
            self._cond.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
        with self._cond:
            return not self._buffer and not (thread is not None and thread.is_alive())

    # -- producer side --

    def record(self, event: ClickEvent) -> bool:
        """Queue an event without blocking. Returns False if it was dropped."""
        with self._cond:
            if self._closed or len(self._buffer) >= self._max_queue:
                self._counts["dropped"] += 1
                if self._counts["dropped"] % 1000 == 1:
                    logger.warning("click dropped (queue full or recorder stopped)")
                return False
            self._buffer.append(event)
            if len(self._buffer) == 1 or len(self._buffer) >= self._batch_size:
                self._cond.notify()
            return True

    def flush(self, timeout: float | None = 5.0) -> bool:
        """Write buffered events now and wait until the buffer is empty. Mostly for tests."""
        with self._cond:
            self._flush_requested = True
            self._cond.notify_all()
            return self._cond.wait_for(lambda: not self._buffer and not self._writing, timeout)

    def pending(self) -> int:
        with self._cond:
            return len(self._buffer)

    def stats(self) -> dict[str, int]:
        with self._cond:
            return {**self._counts, "pending": len(self._buffer)}

    # -- worker --

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._buffer and not self._stopping:
                    self._flush_requested = False
                    self._cond.notify_all()
                    self._cond.wait()
                if not self._buffer:
                    return
                deadline = time.monotonic() + self._flush_interval
                while (
                    len(self._buffer) < self._batch_size
                    and not self._stopping
                    and not self._flush_requested
                ):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._cond.wait(remaining)
                count = min(self._batch_size, len(self._buffer))
                batch = [self._buffer.popleft() for _ in range(count)]
                self._writing = True
            try:
                self._write(batch)
            except Exception:  # the worker must never die
                logger.exception("unexpected error while writing clicks")
                self._count("failed", len(batch))
            finally:
                with self._cond:
                    self._writing = False
                    self._cond.notify_all()

    def _count(self, name: str, amount: int = 1) -> None:
        with self._cond:
            self._counts[name] += amount

    def _write(self, batch: list[ClickEvent]) -> None:
        for attempt in range(1, self._write_attempts + 1):
            try:
                written = self._insert(batch)
            except IntegrityError:
                # A link was deleted between the existence check and the insert.
                self._write_individually(batch)
                return
            except OperationalError as exc:  # database is locked / busy
                if attempt == self._write_attempts:
                    logger.error("dropping %d clicks, database stayed busy: %s", len(batch), exc)
                    break
                logger.warning("click write failed (attempt %d), retrying: %s", attempt, exc)
                time.sleep(self._retry_backoff * 2 ** (attempt - 1))
            except Exception:
                logger.exception("dropping %d clicks after write error", len(batch))
                break
            else:
                self._count("written", written)
                self._count("skipped", len(batch) - written)
                return
        self._count("failed", len(batch))

    def _write_individually(self, batch: list[ClickEvent]) -> None:
        for event in batch:
            try:
                written = self._insert([event])
            except Exception:
                self._count("skipped")
                continue
            self._count("written", written)
            self._count("skipped", 1 - written)

    def _insert(self, batch: list[ClickEvent]) -> int:
        """Insert one batch in one transaction, skipping links that no longer exist."""
        with self._session_factory() as session:
            ids = {event.url_id for event in batch}
            live = dict(session.execute(select(URL.id, URL.code).where(URL.id.in_(ids))).all())
            # Matching the code as well guards against a deleted link's id being reused.
            rows = [
                {
                    "url_id": event.url_id,
                    "accessed_at": event.clicked_at,
                    "ip_hash": event.ip_hash,
                    "user_agent": event.user_agent,
                    "referrer": event.referrer,
                }
                for event in batch
                if live.get(event.url_id) == event.code
            ]
            if rows:
                session.execute(insert(Click), rows)
            session.commit()
            return len(rows)


# --- analytics queries --------------------------------------------------------------------

Granularity = Literal["hour", "day"]
MAX_HOURLY_DAYS = 14


@dataclass(frozen=True, slots=True)
class AnalyticsResult:
    total_clicks: int
    window_start: datetime
    time_series: list[tuple[datetime, int]]
    top_referrers: list[tuple[str, int]]
    recent: list[Click]


def _bucket_range(now: datetime, granularity: Granularity, days: int) -> list[datetime]:
    if granularity == "hour":
        step, count = timedelta(hours=1), days * 24
        last = now.replace(minute=0, second=0, microsecond=0)
    else:
        step, count = timedelta(days=1), days
        last = now.replace(hour=0, minute=0, second=0, microsecond=0)
    first = last - step * (count - 1)
    return [first + step * i for i in range(count)]


def get_click_analytics(
    db: Session,
    url_id: int,
    *,
    now: datetime,
    granularity: Granularity,
    days: int,
    top_referrers: int,
    limit: int,
    offset: int,
) -> AnalyticsResult:
    """All figures for one link. The time series covers the window; the rest is all-time."""
    for_url = Click.url_id == url_id
    total = db.scalar(select(func.count()).select_from(Click).where(for_url)) or 0

    buckets = _bucket_range(now, granularity, days)
    label_format = "%Y-%m-%dT%H:00:00Z" if granularity == "hour" else "%Y-%m-%dT00:00:00Z"
    bucket = func.strftime(label_format, Click.accessed_at)
    counted = dict(
        db.execute(
            select(bucket, func.count())
            .where(for_url, Click.accessed_at >= buckets[0])
            .group_by(bucket)
        ).all()
    )
    time_series = [(b, counted.get(b.strftime("%Y-%m-%dT%H:%M:%SZ"), 0)) for b in buckets]

    referrers = db.execute(
        select(Click.referrer, func.count().label("clicks"))
        .where(for_url, Click.referrer.is_not(None))
        .group_by(Click.referrer)
        .order_by(func.count().desc(), Click.referrer)
        .limit(top_referrers)
    ).all()

    recent = db.scalars(
        select(Click)
        .where(for_url)
        .order_by(Click.accessed_at.desc(), Click.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()

    return AnalyticsResult(
        total_clicks=total,
        window_start=buckets[0],
        time_series=time_series,
        top_referrers=[(row[0], row[1]) for row in referrers],
        recent=list(recent),
    )
