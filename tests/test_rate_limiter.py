from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from rate_limiter import RateLimiter


def test_first_request_is_allowed():
    rl = RateLimiter(max_requests=5, window_seconds=60)
    assert rl.is_allowed(user_id=1) is True


def test_requests_within_limit_are_allowed():
    rl = RateLimiter(max_requests=3, window_seconds=60)
    assert rl.is_allowed(1) is True
    assert rl.is_allowed(1) is True
    assert rl.is_allowed(1) is True


def test_request_exceeding_limit_is_denied():
    rl = RateLimiter(max_requests=3, window_seconds=60)
    rl.is_allowed(1)
    rl.is_allowed(1)
    rl.is_allowed(1)
    assert rl.is_allowed(1) is False


def test_different_users_have_independent_buckets():
    rl = RateLimiter(max_requests=1, window_seconds=60)
    assert rl.is_allowed(user_id=1) is True
    assert rl.is_allowed(user_id=2) is True


def test_requests_outside_window_are_allowed_again():
    rl = RateLimiter(max_requests=2, window_seconds=60)
    past = datetime.now(timezone.utc) - timedelta(seconds=61)

    rl._buckets[1].append(past)
    rl._buckets[1].append(past)

    assert rl.is_allowed(1) is True


def test_seconds_until_reset_returns_zero_when_no_requests():
    rl = RateLimiter(max_requests=5, window_seconds=60)
    assert rl.seconds_until_reset(user_id=99) == 0


def test_seconds_until_reset_returns_positive_when_rate_limited():
    rl = RateLimiter(max_requests=1, window_seconds=60)
    rl.is_allowed(1)
    rl.is_allowed(1)  # now limited
    assert rl.seconds_until_reset(1) > 0
    assert rl.seconds_until_reset(1) <= 60
