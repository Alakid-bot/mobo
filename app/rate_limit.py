from __future__ import annotations

from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta


class RateLimiter:
    def __init__(self) -> None:
        self._buckets: dict[str, deque[datetime]] = defaultdict(deque)

    def allow(self, key: str, max_requests: int, window_seconds: int) -> tuple[bool, int]:
        now = datetime.now(UTC)
        window = timedelta(seconds=window_seconds)
        bucket = self._buckets[key]
        while bucket and bucket[0] <= now - window:
            bucket.popleft()
        if len(bucket) >= max_requests:
            remaining = int(max(1, (bucket[0] + window - now).total_seconds()))
            return False, remaining
        bucket.append(now)
        return True, 0
