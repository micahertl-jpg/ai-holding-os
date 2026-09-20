"""
rate_limiter.py — a minimal in-memory rate limiter for public,
unauthenticated endpoints (currently just /store/checkout, the only
endpoint in this system that both takes no auth and does real work on
every call: creates a real Stripe Checkout Session and writes a
pending_payment order row).

Stdlib only, no external dependency — this runs as a single uvicorn
process with no `--workers` flag (see Dockerfile), so there's no
distributed state to coordinate; a Redis-backed limiter for one
endpoint would be pure overhead for what this actually needs to stop:
someone spamming the endpoint to flood the orders table or burn
through Stripe API calls, not fraud (Stripe never actually charges
anyone until they complete checkout with a real card, regardless of
how many sessions get created).

A sliding window over per-key timestamps, not a token bucket: simple
to reason about and to test exactly at the boundary, and correct
under concurrent requests via a single lock (a single-process
in-memory bucket has no failure mode across a network partition to
worry about, unlike a distributed limiter).
"""

import threading
import time


class RateLimiter:
    def __init__(self, max_requests: int, window_seconds: float):
        if max_requests < 1:
            raise ValueError("max_requests must be at least 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._lock = threading.Lock()
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str, now: float = None) -> bool:
        """True and records the hit if `key` is under its limit for the
        trailing window ending at `now`; False (hit NOT recorded) if
        `key` is already at the limit -- a client that's already being
        rate-limited doesn't get to keep pushing its window forward by
        continuing to hit the endpoint."""
        now = time.time() if now is None else now
        cutoff = now - self.window_seconds
        with self._lock:
            timestamps = [t for t in self._hits.get(key, ()) if t > cutoff]
            if len(timestamps) >= self.max_requests:
                self._hits[key] = timestamps
                return False
            timestamps.append(now)
            self._hits[key] = timestamps
            return True

    def reset(self):
        """Test/ops helper -- clears all tracked state."""
        with self._lock:
            self._hits.clear()
