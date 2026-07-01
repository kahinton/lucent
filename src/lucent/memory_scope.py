"""Shared constants and helpers for user-scoped memory access over MCP.

A user-scoped API key (``memory_scope='user'``) is the security boundary for
multi-user memory isolation: a session holding one can only ever see a single
user's memories regardless of prompt manipulation. The bearer key — not these
headers — is the authorization boundary.

When the daemon builds a task's ``memory-server`` MCP config it also attaches
the headers defined here as internal retry metadata, so a session can re-mint
an equivalent scoped key after a credential expiry without ever falling back to
the daemon's broad key. Keeping the scope values and header names in one place
prevents drift between the daemon dispatch path and the integration dispatch
path.
"""

from __future__ import annotations

# Memory scope values (mirror the ``api_keys.memory_scope`` column).
MEMORY_SCOPE_USER = "user"
MEMORY_SCOPE_ORG_SHARED_ONLY = "org_shared_only"
VALID_MEMORY_SCOPES = frozenset({MEMORY_SCOPE_USER, MEMORY_SCOPE_ORG_SHARED_ONLY})

# Internal MCP retry-metadata header names. Not an authorization boundary —
# they let a scoped session reconstruct its own scope after an auth failure.
MEMORY_SCOPE_HEADER = "X-Lucent-Memory-Scope"
MEMORY_SCOPE_USER_ID_HEADER = "X-Lucent-Memory-Scope-User-Id"
ORG_ID_HEADER = "X-Lucent-Org-Id"


def build_memory_scope_headers(
    memory_scope: str,
    *,
    org_id: str,
    memory_scope_user_id: str | None = None,
) -> dict[str, str]:
    """Build the scope metadata headers for a user-scoped memory-server config.

    Args:
        memory_scope: ``'user'`` or ``'org_shared_only'``.
        org_id: The organization the scoped key belongs to.
        memory_scope_user_id: The user the key is scoped to (required for
            ``'user'`` scope; empty for ``'org_shared_only'``).

    Returns:
        A dict of the three scope headers. Callers merge in ``Authorization``
        (the actual credential) and any task/request identifiers.
    """
    return {
        MEMORY_SCOPE_HEADER: memory_scope,
        MEMORY_SCOPE_USER_ID_HEADER: memory_scope_user_id or "",
        ORG_ID_HEADER: str(org_id),
    }
