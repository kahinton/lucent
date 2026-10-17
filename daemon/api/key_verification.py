"""Daemon-wide API-key verification and transport configuration helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx


def build_auth_config(api_key: str) -> tuple[dict, dict]:
    """Build global internal-MCP and REST headers for an authenticated daemon."""
    from daemon.runtime.module_proxy import runtime

    mcp_config = (
        {
            "memory-server": runtime.build_internal_mcp_server(
                url=runtime.MCP_URL,
                bearer_token=api_key,
                tools=["*"],
            ),
        }
        if api_key
        else {}
    )
    return mcp_config, {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


# Verification outcomes. A probe that never reaches a verdict — a read
# timeout, a dropped connection, a 5xx, any unexpected status — is
# INDETERMINATE: the key is neither confirmed good nor confirmed bad. Callers
# must not rotate on an indeterminate result. The probe targets the dedicated
# lightweight /api/auth/verify endpoint (an indexed api_keys lookup), so a
# slow/failing probe is a transport problem, never an auth verdict — treating
# it as an auth rejection was what produced the ~60s re-provisioning loop.
KEY_VALID = "valid"
KEY_REJECTED = "rejected"
KEY_UNKNOWN = "unknown"


async def classify_api_key(api_key: str) -> str:
    """Probe the server and classify ``api_key``: valid / rejected / unknown.

    Only an explicit HTTP 401/403 counts as ``rejected``. Every other non-200
    outcome (timeout, transport error, 5xx) is ``unknown`` so callers can keep
    the current key instead of churning it on a transient failure.
    """
    from daemon.runtime.module_proxy import runtime

    if not api_key or not api_key.startswith("hs_"):
        return KEY_REJECTED
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(
                f"{runtime.API_BASE}/auth/verify",
                headers={"Authorization": f"Bearer {api_key}"},
            )
    except Exception:
        runtime.log("API key verification probe failed (transport)", "DEBUG")
        return KEY_UNKNOWN
    if response.status_code == 200:
        return KEY_VALID
    if response.status_code in (401, 403):
        return KEY_REJECTED
    runtime.log(
        f"API key verification probe returned HTTP {response.status_code}",
        "DEBUG",
    )
    return KEY_UNKNOWN


async def verify_api_key(api_key: str) -> bool:
    """Check whether an API key is definitively accepted by the server."""
    return await classify_api_key(api_key) == KEY_VALID


async def is_api_key_rejected(api_key: str) -> bool:
    """Whether the server definitively rejected the key (HTTP 401/403)."""
    return await classify_api_key(api_key) == KEY_REJECTED


def get_key_time_remaining(expires_at: datetime | None) -> timedelta | None:
    """Return remaining key lifetime, normalizing naive DB timestamps to UTC."""
    if not expires_at:
        return None
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at - datetime.now(timezone.utc)


def should_rotate_proactively(
    expires_at: datetime | None, *, proactive_rotation_minutes: int
) -> bool:
    """Return whether the key is inside its proactive renewal window."""
    remaining = get_key_time_remaining(expires_at)
    return bool(
        remaining is not None
        and remaining < timedelta(minutes=proactive_rotation_minutes)
    )
