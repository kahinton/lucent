"""Shared helpers for resolving secret:// references in runtime configs."""

from __future__ import annotations

from lucent.auth import get_current_user
from lucent.db import get_pool
from lucent.secrets.base import SecretProvider, SecretScope

SECRET_REF_PREFIX = "secret://"


def is_secret_reference(value: str) -> bool:
    """Return True if value uses the secret:// convention."""
    return isinstance(value, str) and value.startswith(SECRET_REF_PREFIX)


def secret_key_from_reference(value: str) -> str:
    """Extract key name from secret:// reference."""
    return value[len(SECRET_REF_PREFIX):].strip()


async def _candidate_scopes(organization_id: str, user_id: str) -> list[SecretScope]:
    scopes = [SecretScope(organization_id=organization_id, owner_user_id=user_id)]
    try:
        pool = await get_pool()
    except Exception:
        return scopes

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT group_id FROM user_groups WHERE user_id = $1",
                user_id,
            )
        for row in rows:
            scopes.append(SecretScope(organization_id=organization_id, owner_group_id=str(row["group_id"])))
    except Exception:
        return scopes
    return scopes


async def resolve_secret_reference(
    value: str, secret_provider: SecretProvider, *, env_key: str = ""
) -> str:
    """Resolve a single secret:// reference string to its secret value."""
    user = get_current_user()
    if not user or not user.get("organization_id") or not user.get("id"):
        raise ValueError("Cannot resolve secret references without authenticated user context")

    secret_key = secret_key_from_reference(value)
    if not secret_key:
        target = f" for env var '{env_key}'" if env_key else ""
        raise ValueError(f"Invalid secret reference{target}: missing key name")

    scopes = await _candidate_scopes(str(user["organization_id"]), str(user["id"]))
    for scope in scopes:
        secret_value = await secret_provider.get(secret_key, scope)
        if secret_value is not None:
            return secret_value

    target = f" for env var '{env_key}'" if env_key else ""
    raise KeyError(f"Secret not found{target} (reference '{secret_key}')")


async def resolve_env_vars(env_vars: dict[str, str], secret_provider: SecretProvider) -> dict[str, str]:
    """Resolve secret:// references in env vars using current user context.

    Plaintext values are passed through unchanged for backward compatibility.
    """
    resolved: dict[str, str] = {}
    for key, value in env_vars.items():
        if not is_secret_reference(value):
            resolved[key] = value
            continue
        resolved[key] = await resolve_secret_reference(value, secret_provider, env_key=key)

    return resolved
