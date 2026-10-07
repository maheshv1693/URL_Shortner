import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class CachedLink:
    long_url: str
    expires_at: datetime | None


class LinkCache:
    """Thread-safe in-process LRU cache of active links, keyed by short code."""

    def __init__(self, max_size: int) -> None:
        if max_size < 1:
            raise ValueError("max_size must be at least 1")
        self._max_size = max_size
        self._items: OrderedDict[str, CachedLink] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, code: str) -> CachedLink | None:
        with self._lock:
            link = self._items.get(code)
            if link is not None:
                self._items.move_to_end(code)
            return link

    def put(self, code: str, link: CachedLink) -> None:
        with self._lock:
            self._items[code] = link
            self._items.move_to_end(code)
            while len(self._items) > self._max_size:
                self._items.popitem(last=False)

    def invalidate(self, code: str) -> None:
        with self._lock:
            self._items.pop(code, None)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
