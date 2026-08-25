from __future__ import annotations

import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(slots=True)
class _Bucket:
    requests: deque[float]
    owner_id: str | None
    expires_at: float


class RateLimiter:
    """A bounded sliding-window limiter with explicit per-owner erasure."""

    def __init__(
        self,
        *,
        max_buckets: int = 4096,
        idle_ttl_seconds: float = 3600.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if max_buckets <= 0:
            raise ValueError("max_buckets must be greater than zero")
        if idle_ttl_seconds <= 0:
            raise ValueError("idle_ttl_seconds must be greater than zero")
        self.max_buckets = int(max_buckets)
        self.idle_ttl_seconds = float(idle_ttl_seconds)
        self._clock = clock or time.monotonic
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()

    def _prune(self, now: float) -> None:
        for key in [key for key, bucket in self._buckets.items() if bucket.expires_at <= now]:
            self._buckets.pop(key, None)

    def allow(
        self,
        key: str,
        max_requests: int,
        window_seconds: int,
        *,
        owner_id: str | None = None,
    ) -> tuple[bool, int]:
        now = self._clock()
        window = max(0.0, float(window_seconds))
        self._prune(now)
        normalized_key = str(key)
        normalized_owner = str(owner_id) if owner_id is not None else None
        bucket = self._buckets.get(normalized_key)
        if bucket is None or bucket.owner_id != normalized_owner:
            bucket = _Bucket(deque(), normalized_owner, now)
            self._buckets[normalized_key] = bucket
        while bucket.requests and bucket.requests[0] <= now - window:
            bucket.requests.popleft()
        bucket.expires_at = now + max(self.idle_ttl_seconds, window)
        self._buckets.move_to_end(normalized_key)
        while len(self._buckets) > self.max_buckets:
            self._buckets.popitem(last=False)
        if len(bucket.requests) >= max_requests:
            remaining = int(max(1.0, bucket.requests[0] + window - now))
            return False, remaining
        bucket.requests.append(now)
        return True, 0

    def purge(self, owner_id: str) -> None:
        normalized = str(owner_id)
        for key in [key for key, bucket in self._buckets.items() if bucket.owner_id == normalized]:
            self._buckets.pop(key, None)

    def clear(self) -> None:
        self._buckets.clear()

    def __len__(self) -> int:
        self._prune(self._clock())
        return len(self._buckets)
