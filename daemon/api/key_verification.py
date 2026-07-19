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


async def verify_api_key(api_key: str) -> bool:
    """Check whether an API key is accepted by the server."""
    from daemon.runtime.module_proxy import runtime

    if not api_key or not api_key.startswith("hs_"):
        return False
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(
                f"{runtime.API_BASE}/search",
                headers={"Authorization": f"Bearer {api_key}"},
                params={"q": "_verify"},
            )
            return response.status_code == 200
    except Exception:
        runtime.log("API key verification failed", "DEBUG")
        return False


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