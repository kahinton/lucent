"""Execution runtimes for managed tool definitions.

See :mod:`lucent.services.tool_runtimes.base` for the contract and
:mod:`lucent.services.tool_runtimes.registry` for the kind catalog.
"""

from __future__ import annotations

from lucent.services.tool_runtimes.base import (
    RuntimeExecutionContext,
    RuntimeExecutionResult,
    ToolRuntime,
    ToolRuntimeBlockedError,
    ToolRuntimeError,
)
from lucent.services.tool_runtimes.registry import (
    RUNTIME_CATALOG,
    RuntimeKind,
    get_runtime,
    runtime_catalog,
    runtime_requires_sandbox,
    runtime_types,
)

__all__ = [
    "RUNTIME_CATALOG",
    "RuntimeExecutionContext",
    "RuntimeExecutionResult",
    "RuntimeKind",
    "ToolRuntime",
    "ToolRuntimeBlockedError",
    "ToolRuntimeError",
    "get_runtime",
    "runtime_catalog",
    "runtime_requires_sandbox",
    "runtime_types",
]
