from __future__ import annotations

import threading
import time
from collections import deque


class SlidingWindowLimiter:
    """In-process sliding window limiter. Good enough for a single uvicorn worker."""

    def __init__(self, max_requests: int, window_seconds: float) -> None:
        self.max_requests = max(1, int(max_requests))
        self.window_seconds = float(window_seconds)
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            bucket = self._hits.get(key)
            if bucket is None:
                bucket = deque()
                self._hits[key] = bucket
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self.max_requests:
                return False
            bucket.append(now)
            if len(self._hits) > 10_000:
                stale = [item for item, values in self._hits.items() if not values or values[-1] <= cutoff]
                for item in stale[:2_000]:
                    self._hits.pop(item, None)
            return True


heartbeat_limiter = SlidingWindowLimiter(max_requests=1, window_seconds=5.0)
