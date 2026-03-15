"""Rate limiting for API key authentication.

This module provides a simple in-memory rate limiter using a sliding window algorithm.
Default: 100 requests per minute per API key, configurable via LUCENT_RATE_LIMIT_PER_MINUTE.

Note: This implementation is designed for single-process async deployments (uvicorn
with a single worker). The asyncio event loop is single-threaded, so no locking is
needed for coroutine-safe access. For multi-worker or distributed deployments,
upgrade to Redis-based rate limiting.
"""

import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import NamedTuple
from uuid import UUID


class RateLimitResult(NamedTuple):
    """Result of a rate limit check."""

    allowed: bool
    headers: dict[str, str]


@dataclass
class RateLimitBucket:
    """Sliding window rate limit bucket for a single API key."""

    requests: list[float] = field(default_factory=list)

    def check_and_record(self, limit: int, window_seconds: int) -> tuple[bool, int, int]:
        """Check if request is allowed and record it if so.

        Args:
            limit: Maximum requests allowed in the window.
            window_seconds: Size of the sliding window in seconds.

        Returns:
            Tuple of (allowed, remaining, reset_timestamp).
        """
        now = time.time()
        window_start = now - window_seconds

        # Remove expired requests (outside the window)
        self.requests = [t for t in self.requests if t > window_start]

        current_count = len(self.requests)
        remaining = max(0, limit - current_count)

        # Calculate when the oldest request in window will expire
        if self.requests:
            reset_at = int(self.requests[0] + window_seconds)
        else:
            reset_at = int(now + window_seconds)

        if current_count >= limit:
            # Rate limited
            return False, 0, reset_at

        # Request allowed - record it
        self.requests.append(now)
        return True, remaining - 1, reset_at


class RateLimiter:
    """In-memory rate limiter using sliding window algorithm.

    Coroutine-safe implementation suitable for single-process async deployments.
    Not thread-safe — relies on the asyncio event loop being single-threaded.
    For multi-worker or distributed deployments, consider upgrading to Redis.

    Usage:
        limiter = RateLimiter()
        allowed, headers = limiter.check_rate_limit(api_key_id)
        if not allowed:
            return Response(status_code=429, headers=headers)
    """

    # Per-scope rate limit overrides (requests per minute)
    SCOPE_LIMITS: dict[str, int] = {
        "daemon-tasks": 300,  # Higher limit for agent access patterns
    }

    def __init__(
        self,
        requests_per_minute: int | None = None,
        window_seconds: int = 60,
    ):
        """Initialize the rate limiter.

        Args:
            requests_per_minute: Max requests per minute. Defaults to LUCENT_RATE_LIMIT_PER_MINUTE
                                 env var or 100.
            window_seconds: Sliding window size in seconds. Default 60.
        """
        if requests_per_minute is None:
            requests_per_minute = int(os.environ.get("LUCENT_RATE_LIMIT_PER_MINUTE", "100"))

        self.limit = requests_per_minute
        self.window_seconds = window_seconds
        self._buckets: dict[UUID, RateLimitBucket] = defaultdict(RateLimitBucket)

    def check_rate_limit(
        self,
        api_key_id: UUID,
        scopes: list[str] | None = None,
    ) -> RateLimitResult:
        """Check if a request from an API key is allowed.

        Args:
            api_key_id: The UUID of the API key making the request.
            scopes: API key scopes, used to determine per-scope rate limits.

        Returns:
            RateLimitResult with allowed status and headers to include in response.
        """
        # Determine effective limit: use highest scope-specific limit if applicable
        effective_limit = self.limit
        if scopes:
            for scope in scopes:
                scope_limit = self.SCOPE_LIMITS.get(scope)
                if scope_limit and scope_limit > effective_limit:
                    effective_limit = scope_limit

        # Get or create bucket for this API key
        bucket = self._buckets[api_key_id]

        allowed, remaining, reset_at = bucket.check_and_record(effective_limit, self.window_seconds)

        headers = {
            "X-RateLimit-Limit": str(effective_limit),
            "X-RateLimit-Remaining": str(remaining),
            "X-RateLimit-Reset": str(reset_at),
        }

        if not allowed:
            # Add Retry-After header for 429 responses
            retry_after = max(1, reset_at - int(time.time()))
            headers["Retry-After"] = str(retry_after)

        return RateLimitResult(allowed=allowed, headers=headers)

    def cleanup_expired(self) -> int:
        """Remove expired buckets that have no recent requests.

        Call this periodically to prevent memory growth.
        Returns the number of buckets removed.

        Returns:
            Number of buckets cleaned up.
        """
        now = time.time()
        window_start = now - self.window_seconds
        removed = 0

        keys_to_remove = []
        for key, bucket in self._buckets.items():
            # Remove if no requests in the window
            bucket.requests = [t for t in bucket.requests if t > window_start]
            if not bucket.requests:
                keys_to_remove.append(key)

        for key in keys_to_remove:
            del self._buckets[key]
            removed += 1

        return removed

    def reset(self, api_key_id: UUID) -> None:
        """Reset rate limit for a specific API key.

        Useful for testing or manual intervention.

        Args:
            api_key_id: The API key to reset.
        """
        if api_key_id in self._buckets:
            del self._buckets[api_key_id]

    def get_usage(self, api_key_id: UUID) -> dict[str, int]:
        """Get current usage stats for an API key.

        Args:
            api_key_id: The API key to check.

        Returns:
            Dict with 'used', 'limit', and 'remaining' counts.
        """
        now = time.time()
        window_start = now - self.window_seconds

        if api_key_id not in self._buckets:
            return {"used": 0, "limit": self.limit, "remaining": self.limit}
        bucket = self._buckets[api_key_id]

        current = len([t for t in bucket.requests if t > window_start])

        return {
            "used": current,
            "limit": self.limit,
            "remaining": max(0, self.limit - current),
        }


class LoginRateLimiter:
    """IP-based rate limiter for login attempts.

    Uses a stricter limit than the API rate limiter: 5 attempts per minute
    per IP address. After exceeding the limit, further login attempts are
    blocked until the window expires.
    """

    def __init__(
        self,
        max_attempts: int | None = None,
        window_seconds: int = 60,
    ):
        if max_attempts is None:
            max_attempts = int(os.environ.get("LUCENT_LOGIN_RATE_LIMIT", "5"))
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._buckets: dict[str, RateLimitBucket] = defaultdict(RateLimitBucket)

    def check(self, client_ip: str) -> tuple[bool, int]:
        """Check if a login attempt from this IP is allowed.

        Returns:
            Tuple of (allowed, retry_after_seconds).
        """
        bucket = self._buckets[client_ip]
        allowed, _remaining, reset_at = bucket.check_and_record(
            self.max_attempts, self.window_seconds
        )
        retry_after = max(1, reset_at - int(time.time())) if not allowed else 0
        return allowed, retry_after

    def cleanup_expired(self) -> int:
        """Remove expired buckets."""
        now = time.time()
        window_start = now - self.window_seconds
        keys_to_remove = []
        for key, bucket in self._buckets.items():
            bucket.requests = [t for t in bucket.requests if t > window_start]
            if not bucket.requests:
                keys_to_remove.append(key)
        for key in keys_to_remove:
            del self._buckets[key]
        return len(keys_to_remove)


# Module-level login rate limiter singleton
_login_limiter: LoginRateLimiter | None = None


def get_login_limiter() -> LoginRateLimiter:
    """Get the singleton login rate limiter."""
    global _login_limiter
    if _login_limiter is None:
        _login_limiter = LoginRateLimiter()
    return _login_limiter


# Global rate limiter instance
_rate_limiter: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    """Get the global rate limiter instance.

    Creates one if it doesn't exist.
    """
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimiter()
    return _rate_limiter


def reset_rate_limiter() -> None:
    """Reset the global rate limiter (useful for testing)."""
    global _rate_limiter
    _rate_limiter = None
