"""
rate_limiter.py — a minimal in-memory rate limiter, used two ways in
api.py: gating the public, unauthenticated /store/checkout endpoint
(the only one in this system that both takes no auth and does real
work on every call -- creates a real Stripe Checkout Session and
writes a pending_payment order row), and throttling failed dashboard
login attempts (the entire internal dashboard/API -- every business's
data, the ARC ledger, agent controls -- sits behind one static HTTP
Basic Auth password with no other brute-force protection).

Stdlib only, no external dependency — this runs as a single uvicorn
process with no `--workers` flag (see Dockerfile), so there's no
distributed state to coordinate; a Redis-backed limiter would be pure
overhead for what this actually needs to stop: spamming/flooding and
credential guessing, not distributed abuse across many processes.

A sliding window over per-key timestamps, not a token bucket: simple
to reason about and to test exactly at the boundary, and correct
under concurrent requests via a single lock (a single-process
in-memory bucket has no failure mode across a network partition to
worry about, unlike a distributed limiter).

Two ways to use it: allow() is a simple all-in-one gate (check the
limit and record this call as a hit in one step) for the checkout
case, where every call should count. blocked() is read-only (checks
without recording) for the login case, where only failed attempts
should count -- check blocked() before doing the real work, then call
allow() explicitly only when the outcome you care about (a failure)
actually happens, so a legitimate user's successful, repeated requests
never themselves count against the limit.
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

    def blocked(self, key: str, now: float = None) -> bool:
        """Read-only: True if `key` is already at/over its limit for the
        trailing window, without recording a new hit. Pairs with allow()
        for callers that only want certain outcomes to count toward the
        limit (e.g. only failed login attempts, not every request) --
        check blocked() first to short-circuit, then call allow()
        yourself only when the outcome you're tracking actually occurs."""
        now = time.time() if now is None else now
        cutoff = now - self.window_seconds
        with self._lock:
            timestamps = [t for t in self._hits.get(key, ()) if t > cutoff]
            self._hits[key] = timestamps
            return len(timestamps) >= self.max_requests

    def reset(self):
        """Test/ops helper -- clears all tracked state."""
        with self._lock:
            self._hits.clear()
