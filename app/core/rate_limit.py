import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass


class TokenBucketLimiter:
    """In-memory token bucket limiter keyed by an arbitrary string (IP, user id, ...).

    Each key starts with `capacity` tokens, which refill at `refill_per_second`.
    State is per process; it is not shared across replicas.
    """

    def __init__(
        self,
        capacity: float,
        refill_per_second: float,
        *,
        max_keys: int = 100_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if capacity < 1 or refill_per_second <= 0:
            raise ValueError("capacity must be >= 1 and refill_per_second > 0")
        self._capacity = float(capacity)
        self._refill = float(refill_per_second)
        self._max_keys = max_keys
        self._clock = clock
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._lock = threading.Lock()

    @classmethod
    def per_minute(cls, limit: int, **kwargs) -> "TokenBucketLimiter":
        return cls(capacity=limit, refill_per_second=limit / 60, **kwargs)

    def consume(self, key: str) -> tuple[bool, int]:
        """Take one token. Returns (allowed, retry_after_seconds); retry_after is 0 if allowed."""
        now = self._clock()
        with self._lock:
            tokens, last = self._buckets.get(key, (self._capacity, now))
            tokens = min(self._capacity, tokens + (now - last) * self._refill)
            if tokens >= 1:
                allowed, retry_after = True, 0
                tokens -= 1
            else:
                allowed = False
                retry_after = max(1, math.ceil((1 - tokens) / self._refill))
            self._buckets[key] = (tokens, now)
            self._buckets.move_to_end(key)
            while len(self._buckets) > self._max_keys:
                self._buckets.popitem(last=False)
            return allowed, retry_after

    def __len__(self) -> int:
        with self._lock:
            return len(self._buckets)


@dataclass(frozen=True, slots=True)
class RateLimiters:
    enabled: bool
    register_ip: TokenBucketLimiter
    create_ip: TokenBucketLimiter
    create_key: TokenBucketLimiter
    redirect_ip: TokenBucketLimiter

    @classmethod
    def from_settings(cls, settings) -> "RateLimiters":
        return cls(
            enabled=settings.rate_limit_enabled,
            register_ip=TokenBucketLimiter.per_minute(settings.rate_limit_register_ip_per_minute),
            create_ip=TokenBucketLimiter.per_minute(settings.rate_limit_create_ip_per_minute),
            create_key=TokenBucketLimiter.per_minute(settings.rate_limit_create_key_per_minute),
            redirect_ip=TokenBucketLimiter.per_minute(settings.rate_limit_redirect_ip_per_minute),
        )
