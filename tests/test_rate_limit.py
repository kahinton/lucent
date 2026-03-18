"""Tests for rate limiting module."""

import time
from unittest.mock import MagicMock
from uuid import uuid4

from lucent.rate_limit import (
    RateLimitBucket,
    RateLimiter,
    get_client_ip,
    get_rate_limiter,
    reset_rate_limiter,
)


class TestRateLimitBucket:
    """Tests for individual rate limit buckets."""

    def test_allows_under_limit(self):
        bucket = RateLimitBucket()
        allowed, remaining, _ = bucket.check_and_record(limit=5, window_seconds=60)
        assert allowed is True
        assert remaining == 4

    def test_blocks_at_limit(self):
        bucket = RateLimitBucket()
        for _ in range(5):
            bucket.check_and_record(limit=5, window_seconds=60)
        allowed, remaining, _ = bucket.check_and_record(limit=5, window_seconds=60)
        assert allowed is False
        assert remaining == 0

    def test_remaining_decrements(self):
        bucket = RateLimitBucket()
        for i in range(3):
            allowed, remaining, _ = bucket.check_and_record(limit=5, window_seconds=60)
            assert allowed is True
            assert remaining == 4 - i


class TestRateLimiter:
    """Tests for the RateLimiter class."""

    def test_basic_allow(self):
        limiter = RateLimiter(requests_per_minute=5, window_seconds=60)
        key = uuid4()
        result = limiter.check_rate_limit(key)
        assert result.allowed is True

    def test_rate_limited_after_exceeding(self):
        limiter = RateLimiter(requests_per_minute=3, window_seconds=60)
        key = uuid4()
        for _ in range(3):
            result = limiter.check_rate_limit(key)
            assert result.allowed is True
        result = limiter.check_rate_limit(key)
        assert result.allowed is False

    def test_headers_present(self):
        limiter = RateLimiter(requests_per_minute=5, window_seconds=60)
        key = uuid4()
        result = limiter.check_rate_limit(key)
        assert "X-RateLimit-Limit" in result.headers
        assert "X-RateLimit-Remaining" in result.headers
        assert "X-RateLimit-Reset" in result.headers
        assert result.headers["X-RateLimit-Limit"] == "5"

    def test_retry_after_on_429(self):
        limiter = RateLimiter(requests_per_minute=1, window_seconds=60)
        key = uuid4()
        limiter.check_rate_limit(key)  # Use the one allowed
        result = limiter.check_rate_limit(key)  # Should be denied
        assert result.allowed is False
        assert "Retry-After" in result.headers

    def test_separate_buckets_per_key(self):
        limiter = RateLimiter(requests_per_minute=1, window_seconds=60)
        key1 = uuid4()
        key2 = uuid4()
        limiter.check_rate_limit(key1)
        result = limiter.check_rate_limit(key2)
        assert result.allowed is True  # key2 has its own bucket

    def test_reset_clears_bucket(self):
        limiter = RateLimiter(requests_per_minute=1, window_seconds=60)
        key = uuid4()
        limiter.check_rate_limit(key)
        result = limiter.check_rate_limit(key)
        assert result.allowed is False
        limiter.reset(key)
        result = limiter.check_rate_limit(key)
        assert result.allowed is True

    def test_get_usage(self):
        limiter = RateLimiter(requests_per_minute=10, window_seconds=60)
        key = uuid4()
        usage = limiter.get_usage(key)
        assert usage["used"] == 0
        assert usage["remaining"] == 10

        limiter.check_rate_limit(key)
        limiter.check_rate_limit(key)
        usage = limiter.get_usage(key)
        assert usage["used"] == 2
        assert usage["remaining"] == 8

    def test_get_usage_unknown_key(self):
        limiter = RateLimiter(requests_per_minute=10, window_seconds=60)
        usage = limiter.get_usage(uuid4())
        assert usage["used"] == 0
        assert usage["limit"] == 10
        assert usage["remaining"] == 10

    def test_cleanup_expired_removes_empty_buckets(self):
        limiter = RateLimiter(requests_per_minute=10, window_seconds=1)
        key = uuid4()
        limiter.check_rate_limit(key)
        assert len(limiter._buckets) == 1

        # Wait for the window to expire
        time.sleep(1.1)
        removed = limiter.cleanup_expired()
        assert removed == 1
        assert len(limiter._buckets) == 0

    def test_cleanup_expired_preserves_active_buckets(self):
        limiter = RateLimiter(requests_per_minute=10, window_seconds=60)
        active_key = uuid4()
        expired_key = uuid4()

        # Both keys make requests
        limiter.check_rate_limit(active_key)
        limiter.check_rate_limit(expired_key)
        assert len(limiter._buckets) == 2

        # Manually expire one bucket's requests
        limiter._buckets[expired_key].requests = [time.time() - 120]
        removed = limiter.cleanup_expired()
        assert removed == 1
        assert active_key in limiter._buckets
        assert expired_key not in limiter._buckets

    def test_window_expiry_allows_again(self):
        limiter = RateLimiter(requests_per_minute=1, window_seconds=1)
        key = uuid4()
        limiter.check_rate_limit(key)
        result = limiter.check_rate_limit(key)
        assert result.allowed is False

        time.sleep(1.1)
        result = limiter.check_rate_limit(key)
        assert result.allowed is True


class TestGlobalRateLimiter:
    """Tests for the global rate limiter singleton."""

    def test_get_rate_limiter_returns_instance(self):
        reset_rate_limiter()
        limiter = get_rate_limiter()
        assert isinstance(limiter, RateLimiter)

    def test_get_rate_limiter_returns_same_instance(self):
        reset_rate_limiter()
        limiter1 = get_rate_limiter()
        limiter2 = get_rate_limiter()
        assert limiter1 is limiter2

    def test_reset_rate_limiter_clears_instance(self):
        reset_rate_limiter()
        limiter1 = get_rate_limiter()
        reset_rate_limiter()
        limiter2 = get_rate_limiter()
        assert limiter1 is not limiter2

    def test_env_var_configures_limit(self, monkeypatch):
        reset_rate_limiter()
        monkeypatch.setenv("LUCENT_RATE_LIMIT_PER_MINUTE", "42")
        limiter = RateLimiter()
        assert limiter.limit == 42

    def test_env_var_default_limit(self, monkeypatch):
        reset_rate_limiter()
        monkeypatch.delenv("LUCENT_RATE_LIMIT_PER_MINUTE", raising=False)
        limiter = RateLimiter()
        assert limiter.limit == 100


def _make_request(host: str = "1.2.3.4", xff: str | None = None) -> MagicMock:
    """Create a mock Starlette Request with client IP and optional XFF header."""
    req = MagicMock()
    req.client.host = host
    headers = {}
    if xff is not None:
        headers["x-forwarded-for"] = xff
    req.headers = headers
    return req


class TestGetClientIp:
    """Tests for get_client_ip with trusted proxy support."""

    def test_no_trusted_proxies_returns_direct_ip(self, monkeypatch):
        monkeypatch.delenv("LUCENT_TRUSTED_PROXIES", raising=False)
        req = _make_request(host="1.2.3.4", xff="10.0.0.1")
        assert get_client_ip(req) == "1.2.3.4"

    def test_untrusted_connection_ignores_xff(self, monkeypatch):
        monkeypatch.setenv("LUCENT_TRUSTED_PROXIES", "192.168.1.0/24")
        req = _make_request(host="5.6.7.8", xff="10.0.0.1")
        assert get_client_ip(req) == "5.6.7.8"

    def test_trusted_proxy_extracts_xff(self, monkeypatch):
        monkeypatch.setenv("LUCENT_TRUSTED_PROXIES", "192.168.1.0/24")
        req = _make_request(host="192.168.1.10", xff="203.0.113.50")
        assert get_client_ip(req) == "203.0.113.50"

    def test_trusted_proxy_chain_returns_rightmost_untrusted(self, monkeypatch):
        monkeypatch.setenv("LUCENT_TRUSTED_PROXIES", "10.0.0.0/8")
        req = _make_request(host="10.0.0.1", xff="203.0.113.50, 10.0.0.5, 10.0.0.6")
        assert get_client_ip(req) == "203.0.113.50"

    def test_single_trusted_ip(self, monkeypatch):
        monkeypatch.setenv("LUCENT_TRUSTED_PROXIES", "172.17.0.2")
        req = _make_request(host="172.17.0.2", xff="99.99.99.99")
        assert get_client_ip(req) == "99.99.99.99"

    def test_multiple_trusted_cidrs(self, monkeypatch):
        monkeypatch.setenv("LUCENT_TRUSTED_PROXIES", "10.0.0.0/8, 172.16.0.0/12")
        req = _make_request(host="172.16.0.5", xff="8.8.8.8, 10.0.0.3")
        assert get_client_ip(req) == "8.8.8.8"

    def test_no_xff_header_returns_direct_ip(self, monkeypatch):
        monkeypatch.setenv("LUCENT_TRUSTED_PROXIES", "192.168.1.0/24")
        req = _make_request(host="192.168.1.10")
        assert get_client_ip(req) == "192.168.1.10"

    def test_empty_xff_returns_direct_ip(self, monkeypatch):
        monkeypatch.setenv("LUCENT_TRUSTED_PROXIES", "192.168.1.0/24")
        req = _make_request(host="192.168.1.10", xff="")
        assert get_client_ip(req) == "192.168.1.10"

    def test_all_xff_trusted_returns_direct_ip(self, monkeypatch):
        monkeypatch.setenv("LUCENT_TRUSTED_PROXIES", "10.0.0.0/8")
        req = _make_request(host="10.0.0.1", xff="10.0.0.2, 10.0.0.3")
        assert get_client_ip(req) == "10.0.0.1"

    def test_invalid_cidr_in_config_is_skipped(self, monkeypatch):
        monkeypatch.setenv("LUCENT_TRUSTED_PROXIES", "not-an-ip, 10.0.0.0/8")
        req = _make_request(host="10.0.0.1", xff="203.0.113.1")
        assert get_client_ip(req) == "203.0.113.1"

    def test_no_client_returns_unknown(self, monkeypatch):
        monkeypatch.delenv("LUCENT_TRUSTED_PROXIES", raising=False)
        req = MagicMock()
        req.client = None
        assert get_client_ip(req) == "unknown"
