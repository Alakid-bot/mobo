from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone


class RateLimiter:
    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window = timedelta(seconds=window_seconds)
        self._buckets: dict[int, deque[datetime]] = defaultdict(deque)

    def is_allowed(self, user_id: int) -> bool:
        now = datetime.now(timezone.utc)
        cutoff = now - self.window
        bucket = self._buckets[user_id]

        while bucket and bucket[0] < cutoff:
            bucket.popleft()

        if len(bucket) >= self.max_requests:
            return False

        bucket.append(now)
        return True

    def seconds_until_reset(self, user_id: int) -> int:
        bucket = self._buckets[user_id]
        if not bucket:
            return 0
        reset_at = bucket[0] + self.window
        remaining = (reset_at - datetime.now(timezone.utc)).total_seconds()
        return max(0, int(remaining))
