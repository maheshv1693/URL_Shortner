import threading
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class CachedLink:
    url_id: int
    long_url: str
    expires_at: datetime | None


class LoadTicket:
    """Handle for one in-flight database read that intends to populate the cache."""

    __slots__ = ("_cache", "_stale", "code")

    def __init__(self, cache: "LinkCache", code: str) -> None:
        self._cache = cache
        self.code = code
        self._stale = False

    def store(self, link: CachedLink) -> bool:
        """Cache `link` unless the code was invalidated since the read began."""
        return self._cache._store_from_ticket(self, link)


class LinkCache:
    """Thread-safe in-process LRU cache of active links, keyed by short code.

    Readers populate the cache through `loading()`. An `invalidate()` or `clear()` that
    happens while a read is in flight marks that read stale, so it cannot write the
    pre-change value back. Only in-flight reads are tracked, so no tombstones accumulate.
    """

    def __init__(self, max_size: int) -> None:
        if max_size < 1:
            raise ValueError("max_size must be at least 1")
        self._max_size = max_size
        self._items: OrderedDict[str, CachedLink] = OrderedDict()
        self._inflight: dict[str, set[LoadTicket]] = {}
        self._lock = threading.Lock()

    def get(self, code: str) -> CachedLink | None:
        with self._lock:
            link = self._items.get(code)
            if link is not None:
                self._items.move_to_end(code)
            return link

    def put(self, code: str, link: CachedLink) -> None:
        with self._lock:
            self._put_locked(code, link)

    @contextmanager
    def loading(self, code: str) -> Iterator[LoadTicket]:
        """Open a guarded read; start it before querying the database."""
        ticket = LoadTicket(self, code)
        with self._lock:
            self._inflight.setdefault(code, set()).add(ticket)
        try:
            yield ticket
        finally:
            with self._lock:
                tickets = self._inflight.get(code)
                if tickets is not None:
                    tickets.discard(ticket)
                    if not tickets:
                        del self._inflight[code]

    def invalidate(self, code: str) -> None:
        with self._lock:
            self._items.pop(code, None)
            for ticket in self._inflight.get(code, ()):
                ticket._stale = True

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            for tickets in self._inflight.values():
                for ticket in tickets:
                    ticket._stale = True

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def _store_from_ticket(self, ticket: LoadTicket, link: CachedLink) -> bool:
        with self._lock:
            if ticket._stale:
                return False
            self._put_locked(ticket.code, link)
            return True

    def _put_locked(self, code: str, link: CachedLink) -> None:
        self._items[code] = link
        self._items.move_to_end(code)
        while len(self._items) > self._max_size:
            self._items.popitem(last=False)
