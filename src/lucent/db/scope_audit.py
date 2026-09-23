"""Temporary shadow auditing for database tenant scope.

Set ``LUCENT_DB_SCOPE_AUDIT=1`` while running after RLS rollback to log
database activity in a separate JSONL file. The wrapper does not alter
queries or query results; it records the effective tenant scope and flags
access that would not be valid under the scoped RLS contract.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from logging.handlers import RotatingFileHandler
from typing import Any

from lucent.logging import JSONFormatter

_audit_logger = logging.getLogger("lucent.db.scope_audit")
_audit_logger.setLevel(logging.INFO)
_audit_logger.propagate = False
_audit_initialized = False


def _initialize() -> bool:
    global _audit_initialized
    if _audit_initialized:
        return True
    if os.environ.get("LUCENT_DB_SCOPE_AUDIT", "").strip().lower() not in (
        "1",
        "true",
        "yes",
    ):
        return False

    path = Path(
        os.environ.get("LUCENT_DB_SCOPE_AUDIT_LOG", "/tmp/lucent-db-scope-audit.jsonl")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        path,
        maxBytes=int(os.environ.get("LUCENT_DB_SCOPE_AUDIT_MAX_BYTES", "10485760")),
        backupCount=5,
    )
    handler.setFormatter(JSONFormatter())
    _audit_logger.addHandler(handler)
    _audit_initialized = True
    return True


def audit_enabled() -> bool:
    """Return whether shadow auditing is configured and initialized."""
    return _initialize()


def effective_scope(
    scope: dict[str, str] | None,
    *,
    user_id: Any = None,
    organization_id: Any = None,
    role: str | None = None,
    access_type: str = "scoped_acquire",
) -> dict[str, str]:
    return {
        "user_id": str(user_id or (scope or {}).get("user_id", "")),
        "organization_id": str(
            organization_id or (scope or {}).get("organization_id", "")
        ),
        "role": role or (scope or {}).get("role", "member"),
        "auth_context": (scope or {}).get("auth_context", ""),
        "access_type": access_type,
    }


def _failure_for(query: str, db_scope: dict[str, str]) -> str | None:
    if db_scope.get("access_type") == "raw_pool_acquire":
        return "raw_pool_acquire"
    if db_scope["role"] == "member" and not db_scope["user_id"]:
        return "member_scope_without_user"
    if not db_scope["organization_id"] and db_scope["role"] not in {
        "daemon",
        "system",
    }:
        return "missing_tenant_organization"
    return None


def _scope_fields(
    db_scope: dict[str, str],
    operation: str,
    query: Any,
    *,
    duration_ms: float | None = None,
) -> dict[str, Any]:
    query_text = str(query) if query is not None else ""
    fields: dict[str, Any] = {
        "audit_type": "db_scope",
        "operation": operation,
        "scope": db_scope,
        "query": query_text,
        "failure": _failure_for(query_text, db_scope),
    }
    if duration_ms is not None:
        fields["duration_ms"] = round(duration_ms, 2)
    return fields


def _wrap_async_method(
    connection: Any,
    name: str,
    db_scope: dict[str, str],
):
    method = getattr(connection, name)

    async def audited(*args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        result = await method(*args)
        if _initialize():
            _audit_logger.info(
                "",
                extra={
                    **_scope_fields(
                        db_scope,
                        name,
                        args[0] if args else "",
                        duration_ms=(time.perf_counter() - started) * 1000,
                    )
                },
            )
        return result

    return audited


class ScopeAuditedConnection:
    """Proxy recording queries against the scope used to create a connection."""

    def __init__(self, connection: Any, db_scope: dict[str, str]) -> None:
        self._connection = connection
        self._db_scope = db_scope

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._connection, name)
        if name == "transaction":
            return value
        if value.__class__.__name__ in ("CoroutineFunction", "CoroutineType") or (
            callable(value)
            and getattr(value, "__code__", None)
            and value.__code__.co_flags & 0x80
        ):
            return _wrap_async_method(self._connection, name, self._db_scope)
        return value


class _ScopeAuditedTransaction:
    def __init__(self, transaction: Any, db_scope: dict[str, str]) -> None:
        self._transaction = transaction
        self._db_scope = db_scope

    def __aenter__(self) -> Any:
        return self._transaction.__aenter__()

    def __aexit__(self, *args: Any) -> Any:
        return self._transaction.__aexit__(*args)


class _ScopeAuditedAcquire:
    def __init__(
        self,
        acquire_context: Any,
        db_scope: dict[str, str],
    ) -> None:
        self._acquire_context = acquire_context
        self._db_scope = db_scope

    async def __aenter__(self) -> ScopeAuditedConnection:
        return ScopeAuditedConnection(
            await self._acquire_context.__aenter__(),
            self._db_scope,
        )

    async def __aexit__(self, *args: Any) -> Any:
        return await self._acquire_context.__aexit__(*args)

    def __await__(self) -> Any:
        acquire_context = self._acquire_context

        async def await_wrapped() -> ScopeAuditedConnection:
            connection = await acquire_context.pool._acquire(acquire_context.timeout)
            return ScopeAuditedConnection(
                connection,
                self._db_scope,
            )

        acquire_context.done = True
        return await_wrapped().__await__()


class ScopeAuditedPool:
    """Shadow wrapper for an asyncpg pool; only ``acquire`` is intercepted."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    def acquire(self, *args: Any, **kwargs: Any) -> _ScopeAuditedAcquire:
        return _ScopeAuditedAcquire(
            self._pool.acquire(*args, **kwargs),
            {
                "user_id": "",
                "organization_id": "",
                "role": "member",
                "auth_context": "",
                "access_type": "raw_pool_acquire",
            },
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._pool, name)
