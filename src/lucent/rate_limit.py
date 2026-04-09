"""Rate limiting for API key authentication and integration channels.

This module provides a simple in-memory rate limiter using a sliding window algorithm.
Default: 100 requests per minute per API key, configurable via LUCENT_RATE_LIMIT_PER_MINUTE.

Supports unified per-user rate limiting across all channels (web, MCP, Slack, Discord)
using key prefixes:
    - ``user:{user_id}``     — 100 req/min per user (unified across channels)
    - ``org:{org_id}``       — 500 req/min per organization (integration-level)
    - ``webhook:{ip}``       — 1000 req/min per IP (webhook ingress)
    - ``pairing:code:{id}``  — 5 attempts per code (pairing challenge)
    - ``pairing:user:{id}``  — 10 attempts per hour per user (pairing issuance)

Keys without a recognized prefix use the default limit (100 req/min).

Note: This implementation is designed for single-process async deployments (uvicorn
with a single worker). The asyncio event loop is single-threaded, so no locking is
needed for coroutine-safe access. For multi-worker or distributed deployments,
upgrade to Redis-based rate limiting.
"""

import ipaddress
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import NamedTuple
from uuid import UUID

from starlette.requests import Request

logger = logging.getLogger(__name__)


def _parse_trusted_proxies() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Parse LUCENT_TRUSTED_PROXIES env var into a list of network objects."""
    raw = os.environ.get("LUCENT_TRUSTED_PROXIES", "").strip()
    if not raw:
        return []
    networks = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            logger.warning("Invalid CIDR/IP in LUCENT_TRUSTED_PROXIES: %s", entry)
    return networks


def _is_trusted(ip_str: str, trusted: list[ipaddress.IPv4Network | ipaddress.IPv6Network]) -> bool:
    """Check if an IP address falls within any trusted proxy network."""
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return any(addr in net for net in trusted)


def get_client_ip(request: Request) -> str:
    """Extract the real client IP, respecting X-Forwarded-For behind trusted proxies.

    When LUCENT_TRUSTED_PROXIES is configured and the direct connection comes
    from a trusted proxy, the rightmost untrusted IP in the X-Forwarded-For
    chain is returned. Otherwise, falls back to the direct connection IP.
    """
    direct_ip = request.client.host if request.client else "unknown"

    trusted = _parse_trusted_proxies()
    if not trusted:
        return direct_ip

    if not _is_trusted(direct_ip, trusted):
        return direct_ip

    xff = request.headers.get("x-forwarded-for", "")
    if not xff:
        return direct_ip

    # Walk the XFF chain from right to left, skipping trusted proxies.
    # The rightmost untrusted entry is the real client IP.
    ips = [ip.strip() for ip in xff.split(",") if ip.strip()]
    for ip in reversed(ips):
        if not _is_trusted(ip, trusted):
            return ip

    # All IPs in the chain are trusted — fall back to direct IP
    return direct_ip


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


@dataclass(frozen=True)
class LimitConfig:
    """Rate limit configuration for a key prefix."""

    limit: int
    window_seconds: int


class RateLimiter:
    """In-memory rate limiter using sliding window algorithm.

    Supports prefix-based rate limits for unified per-user limiting across
    channels (web, MCP, Slack, Discord), org-level integration limits,
    webhook ingress limits, and pairing attempt limits.

    Coroutine-safe implementation suitable for single-process async deployments.
    Not thread-safe — relies on the asyncio event loop being single-threaded.
    For multi-worker or distributed deployments, consider upgrading to Redis.

    Usage:
        limiter = RateLimiter()
        # Legacy API key limiting
        allowed, headers = limiter.check_rate_limit(api_key_id)
        # Unified per-user limiting
        allowed, headers = limiter.check_user_rate_limit(user_id)
        # Org-level integration limiting
        allowed, headers = limiter.check_org_rate_limit(org_id)
    """

    # Per-scope rate limit overrides (requests per minute)
    SCOPE_LIMITS: dict[str, int] = {
        "daemon-tasks": 300,  # Higher limit for agent access patterns
    }

    # Default prefix-based rate limits (longest prefix matched first)
    DEFAULT_PREFIX_LIMITS: dict[str, LimitConfig] = {
        "user:": LimitConfig(limit=100, window_seconds=60),
        "org:": LimitConfig(limit=500, window_seconds=60),
        "webhook:": LimitConfig(limit=1000, window_seconds=60),
        "pairing:code:": LimitConfig(limit=5, window_seconds=600),
        "pairing:user:": LimitConfig(limit=10, window_seconds=3600),
    }

    def __init__(
        self,
        requests_per_minute: int | None = None,
        window_seconds: int = 60,
        prefix_limits: dict[str, LimitConfig] | None = None,
    ):
        """Initialize the rate limiter.

        Args:
            requests_per_minute: Max requests per minute. Defaults to LUCENT_RATE_LIMIT_PER_MINUTE
                                 env var or 100.
            window_seconds: Sliding window size in seconds. Default 60.
            prefix_limits: Optional override for prefix-based limits. Defaults to
                           DEFAULT_PREFIX_LIMITS.
        """
        if requests_per_minute is None:
            requests_per_minute = int(os.environ.get("LUCENT_RATE_LIMIT_PER_MINUTE", "100"))

        self.limit = requests_per_minute
        self.window_seconds = window_seconds
        self._prefix_limits = (
            dict(prefix_limits) if prefix_limits is not None else dict(self.DEFAULT_PREFIX_LIMITS)
        )
        # Pre-sort prefixes longest-first for matching
        self._sorted_prefixes = sorted(self._prefix_limits, key=len, reverse=True)
        self._buckets: dict[str, RateLimitBucket] = defaultdict(RateLimitBucket)

    def _resolve_limit(self, key: str) -> tuple[int, int]:
        """Resolve effective limit and window for a key based on its prefix.

        Returns:
            Tuple of (limit, window_seconds).
        """
        for prefix in self._sorted_prefixes:
            if key.startswith(prefix):
                config = self._prefix_limits[prefix]
                return config.limit, config.window_seconds
        return self.limit, self.window_seconds

    def check_rate_limit(
        self,
        key: str | UUID,
        scopes: list[str] | None = None,
    ) -> RateLimitResult:
        """Check if a request is allowed under the rate limit.

        The key determines which bucket and limit applies. Keys with a recognized
        prefix (``user:``, ``org:``, ``webhook:``, ``pairing:code:``,
        ``pairing:user:``) use prefix-specific limits. Other keys use the default
        limit.

        Args:
            key: Rate limit key — a string with optional prefix, or a UUID for
                 legacy API-key-based limiting.
            scopes: API key scopes, used to determine per-scope rate limits
                    (only applies to keys using the default limit).

        Returns:
            RateLimitResult with allowed status and headers to include in response.
        """
        key_str = str(key)

        effective_limit, effective_window = self._resolve_limit(key_str)

        # Scope overrides only apply to keys using the default limit/window
        if scopes and effective_window == self.window_seconds and effective_limit == self.limit:
            for scope in scopes:
                scope_limit = self.SCOPE_LIMITS.get(scope)
                if scope_limit and scope_limit > effective_limit:
                    effective_limit = scope_limit

        bucket = self._buckets[key_str]
        allowed, remaining, reset_at = bucket.check_and_record(effective_limit, effective_window)

        headers = {
            "X-RateLimit-Limit": str(effective_limit),
            "X-RateLimit-Remaining": str(remaining),
            "X-RateLimit-Reset": str(reset_at),
        }

        if not allowed:
            retry_after = max(1, reset_at - int(time.time()))
            headers["Retry-After"] = str(retry_after)

        return RateLimitResult(allowed=allowed, headers=headers)

    # -- Convenience methods for integration rate limiting --

    def check_user_rate_limit(self, user_id: str | UUID) -> RateLimitResult:
        """Unified per-user rate limit across all channels (web, MCP, Slack, Discord)."""
        return self.check_rate_limit(f"user:{user_id}")

    def check_org_rate_limit(self, org_id: str | UUID) -> RateLimitResult:
        """Integration-level rate limit (500 req/min per organization)."""
        return self.check_rate_limit(f"org:{org_id}")

    def check_webhook_rate_limit(self, client_ip: str) -> RateLimitResult:
        """Webhook ingress rate limit (1000 req/min per IP)."""
        return self.check_rate_limit(f"webhook:{client_ip}")

    def check_pairing_code_rate_limit(self, challenge_id: str | UUID) -> RateLimitResult:
        """Per-code pairing attempt limit (5 attempts per code)."""
        return self.check_rate_limit(f"pairing:code:{challenge_id}")

    def check_pairing_user_rate_limit(self, user_id: str | UUID) -> RateLimitResult:
        """Per-user pairing issuance limit (10 codes per user per hour)."""
        return self.check_rate_limit(f"pairing:user:{user_id}")

    def cleanup_expired(self) -> int:
        """Remove expired buckets that have no recent requests.

        Call this periodically to prevent memory growth. Uses each bucket's
        prefix-specific window to determine expiry.

        Returns:
            Number of buckets cleaned up.
        """
        now = time.time()
        removed = 0

        keys_to_remove = []
        for key, bucket in self._buckets.items():
            _, window = self._resolve_limit(key)
            window_start = now - window
            bucket.requests = [t for t in bucket.requests if t > window_start]
            if not bucket.requests:
                keys_to_remove.append(key)

        for key in keys_to_remove:
            del self._buckets[key]
            removed += 1

        return removed

    def reset(self, key: str | UUID) -> None:
        """Reset rate limit for a specific key.

        Useful for testing or manual intervention.

        Args:
            key: The rate limit key to reset.
        """
        key_str = str(key)
        if key_str in self._buckets:
            del self._buckets[key_str]

    def get_usage(self, key: str | UUID) -> dict[str, int]:
        """Get current usage stats for a key.

        Args:
            key: The rate limit key to check.

        Returns:
            Dict with 'used', 'limit', and 'remaining' counts.
        """
        key_str = str(key)
        effective_limit, effective_window = self._resolve_limit(key_str)

        now = time.time()
        window_start = now - effective_window

        if key_str not in self._buckets:
            return {"used": 0, "limit": effective_limit, "remaining": effective_limit}
        bucket = self._buckets[key_str]

        current = len([t for t in bucket.requests if t > window_start])

        return {
            "used": current,
            "limit": effective_limit,
            "remaining": max(0, effective_limit - current),
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
