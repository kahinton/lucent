"""Tests for the shared user-scoped memory access helpers.

These names are a cross-process contract: the daemon attaches them as MCP retry
metadata and re-reads them to re-mint a scoped key, while the integration
dispatch path emits the same headers. Locking them here prevents silent drift.
"""

from lucent.memory_scope import (
    MEMORY_SCOPE_HEADER,
    MEMORY_SCOPE_ORG_SHARED_ONLY,
    MEMORY_SCOPE_USER,
    MEMORY_SCOPE_USER_ID_HEADER,
    ORG_ID_HEADER,
    VALID_MEMORY_SCOPES,
    build_memory_scope_headers,
)


def test_scope_constants_are_stable():
    assert MEMORY_SCOPE_USER == "user"
    assert MEMORY_SCOPE_ORG_SHARED_ONLY == "org_shared_only"
    assert VALID_MEMORY_SCOPES == {"user", "org_shared_only"}
    assert MEMORY_SCOPE_HEADER == "X-Lucent-Memory-Scope"
    assert MEMORY_SCOPE_USER_ID_HEADER == "X-Lucent-Memory-Scope-User-Id"
    assert ORG_ID_HEADER == "X-Lucent-Org-Id"


def test_build_headers_for_user_scope():
    headers = build_memory_scope_headers(
        MEMORY_SCOPE_USER,
        org_id="org-1",
        memory_scope_user_id="user-1",
    )
    assert headers == {
        "X-Lucent-Memory-Scope": "user",
        "X-Lucent-Memory-Scope-User-Id": "user-1",
        "X-Lucent-Org-Id": "org-1",
    }


def test_build_headers_for_shared_scope_has_empty_user_id():
    headers = build_memory_scope_headers(
        MEMORY_SCOPE_ORG_SHARED_ONLY,
        org_id="org-1",
    )
    assert headers == {
        "X-Lucent-Memory-Scope": "org_shared_only",
        "X-Lucent-Memory-Scope-User-Id": "",
        "X-Lucent-Org-Id": "org-1",
    }


def test_build_headers_coerces_org_id_to_str():
    headers = build_memory_scope_headers(MEMORY_SCOPE_USER, org_id=12345, memory_scope_user_id=None)
    assert headers["X-Lucent-Org-Id"] == "12345"
    assert headers["X-Lucent-Memory-Scope-User-Id"] == ""
