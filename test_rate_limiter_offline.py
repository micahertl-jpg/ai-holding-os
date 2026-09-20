"""
test_rate_limiter_offline.py — real tests for rate_limiter.py's
RateLimiter, using a controlled `now` clock (never real wall-clock
sleeps) so window-boundary behavior is exact and the suite stays fast.
"""

import threading

from rate_limiter import RateLimiter


def test_allows_up_to_the_limit_then_blocks():
    limiter = RateLimiter(max_requests=3, window_seconds=60)
    assert limiter.allow("1.2.3.4", now=0) is True
    assert limiter.allow("1.2.3.4", now=1) is True
    assert limiter.allow("1.2.3.4", now=2) is True
    assert limiter.allow("1.2.3.4", now=3) is False, "4th request within the window must be blocked"
    print("PASS: allows exactly max_requests within a window, blocks the next one")


def test_blocked_requests_are_not_recorded_as_new_hits():
    """A client already at the limit must not be able to keep pushing
    its own window forward by continuing to hammer the endpoint."""
    limiter = RateLimiter(max_requests=2, window_seconds=10)
    assert limiter.allow("1.2.3.4", now=0) is True
    assert limiter.allow("1.2.3.4", now=1) is True
    assert limiter.allow("1.2.3.4", now=5) is False
    assert limiter.allow("1.2.3.4", now=9) is False
    # Only the original 2 hits (at t=0, t=1) count -- by t=11 the t=0 hit
    # has aged out of the 10s window, leaving room for exactly one more.
    assert limiter.allow("1.2.3.4", now=11) is True
    print("PASS: a blocked request doesn't extend the client's own window")


def test_old_hits_age_out_of_the_window():
    limiter = RateLimiter(max_requests=1, window_seconds=10)
    assert limiter.allow("1.2.3.4", now=0) is True
    assert limiter.allow("1.2.3.4", now=9) is False
    assert limiter.allow("1.2.3.4", now=10.1) is True, "the t=0 hit should have aged out by t=10.1"
    print("PASS: hits older than window_seconds no longer count against the limit")


def test_different_keys_are_tracked_independently():
    limiter = RateLimiter(max_requests=1, window_seconds=60)
    assert limiter.allow("1.2.3.4", now=0) is True
    assert limiter.allow("1.2.3.4", now=1) is False
    assert limiter.allow("5.6.7.8", now=1) is True, "a different key must have its own independent limit"
    print("PASS: rate limits are tracked per key, not globally")


def test_reset_clears_all_state():
    limiter = RateLimiter(max_requests=1, window_seconds=60)
    assert limiter.allow("1.2.3.4", now=0) is True
    assert limiter.allow("1.2.3.4", now=1) is False
    limiter.reset()
    assert limiter.allow("1.2.3.4", now=2) is True
    print("PASS: reset() clears previously tracked hits")


def test_thread_safety_never_admits_more_than_the_limit():
    """Regression-style test: many threads hitting allow() concurrently
    for the same key must never let more than max_requests through,
    proving the lock actually serializes the check-then-record step."""
    limiter = RateLimiter(max_requests=10, window_seconds=60)
    admitted = []
    admitted_lock = threading.Lock()

    def worker():
        if limiter.allow("shared-key", now=0):
            with admitted_lock:
                admitted.append(1)

    threads = [threading.Thread(target=worker) for _ in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(admitted) == 10, f"expected exactly 10 admitted, got {len(admitted)}"
    print("PASS: concurrent callers never push more than max_requests through the same window")


def test_rejects_invalid_construction():
    try:
        RateLimiter(max_requests=0, window_seconds=60)
        assert False, "max_requests=0 should be rejected"
    except ValueError:
        pass
    try:
        RateLimiter(max_requests=5, window_seconds=0)
        assert False, "window_seconds=0 should be rejected"
    except ValueError:
        pass
    print("PASS: invalid max_requests/window_seconds are rejected loudly, not silently accepted")


if __name__ == "__main__":
    test_allows_up_to_the_limit_then_blocks()
    test_blocked_requests_are_not_recorded_as_new_hits()
    test_old_hits_age_out_of_the_window()
    test_different_keys_are_tracked_independently()
    test_reset_clears_all_state()
    test_thread_safety_never_admits_more_than_the_limit()
    test_rejects_invalid_construction()
    print("\nAll rate_limiter.py offline tests passed.")
