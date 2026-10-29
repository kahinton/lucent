"""Registry mapping ``runtime_type`` values onto runtime implementations.

The registry is the *only* place that knows which runtime kinds exist. The
repository validates tool definitions against :func:`runtime_types`, the API and
web routes expose :func:`runtime_catalog` to the UI, and the executor resolves
implementations with :func:`get_runtime`.

Keeping the catalog here rather than in the database means an unsupported
``runtime_type`` fails at write time with a clear message instead of at call
time with a stack trace, and it means the UI cannot advertise a kind this build
cannot execute.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from lucent.services.tool_runtimes.base import ToolRuntime, ToolRuntimeError

__all__ = [
    "RUNTIME_CATALOG",
    "RuntimeKind",
    "get_runtime",
    "runtime_catalog",
    "runtime_requires_sandbox",
    "runtime_types",
]


@dataclass(frozen=True)
class RuntimeKind:
    """Presentation + capability metadata for one runtime kind."""

    runtime_type: str
    label: str
    summary: str
    #: True when execution needs a sandbox container, so the kind is
    #: unavailable wherever ``features.sandboxes_enabled`` is off.
    requires_sandbox: bool
    #: True when the owner supplies executable source code.
    accepts_source: bool
    #: Environment variable that must be set for optional dependencies.
    build_flag: str | None = None


RUNTIME_CATALOG: dict[str, RuntimeKind] = {
    "python": RuntimeKind(
        runtime_type="python",
        label="Sandboxed Python",
        summary=(
            "Lucent runs your Python entrypoint inside an isolated container with no "
            "network by default. Use it for computation, parsing, or any logic that "
            "needs real code."
        ),
        requires_sandbox=True,
        accepts_source=True,
    ),
    "http": RuntimeKind(
        runtime_type="http",
        label="HTTP request",
        summary=(
            "Declare one HTTPS call with templated path, query, and body. No code, no "
            "container. Restricted to the hosts you allowlist."
        ),
        requires_sandbox=False,
        accepts_source=False,
    ),
    "openapi": RuntimeKind(
        runtime_type="openapi",
        label="OpenAPI operation",
        summary=(
            "Point at an OpenAPI document and pick one operation. Lucent derives the "
            "request from the spec and sends the same HTTPS call an 'http' tool would."
        ),
        requires_sandbox=False,
        accepts_source=False,
    ),
    "builtin": RuntimeKind(
        runtime_type="builtin",
        label="Built-in action",
        summary=(
            "A reviewed Lucent-native handler — web search, page fetch, time, and "
            "similar. No code to maintain and no container to start."
        ),
        requires_sandbox=False,
        accepts_source=False,
    ),
}

#: Populated at import time below; typed loosely to avoid a circular import
#: between the registry and the runtime modules.
_RUNTIMES: dict[str, ToolRuntime] = {}


def _load_runtimes() -> dict[str, ToolRuntime]:
    """Import runtime implementations lazily and cache them.

    Each import lives inside the function so that a deployment that never uses
    a kind never pays for its module import, and so an optional dependency for
    one kind cannot break the others.
    """
    if _RUNTIMES:
        return _RUNTIMES

    from lucent.services.tool_runtimes.builtin_runtime import BuiltinRuntime
    from lucent.services.tool_runtimes.http_runtime import HttpRuntime
    from lucent.services.tool_runtimes.openapi_runtime import OpenAPIRuntime
    from lucent.services.tool_runtimes.python_runtime import PythonRuntime

    for runtime in (PythonRuntime(), HttpRuntime(), OpenAPIRuntime(), BuiltinRuntime()):
        _RUNTIMES[runtime.runtime_type] = runtime
    return _RUNTIMES


def runtime_types() -> tuple[str, ...]:
    """Every runtime kind this build can execute."""
    return tuple(RUNTIME_CATALOG)


def runtime_requires_sandbox(runtime_type: str) -> bool:
    """Whether a kind needs sandbox provisioning.

    Unknown kinds are treated as requiring a sandbox: that is the more
    restrictive answer, and it means a typo cannot quietly bypass the
    ``features.sandboxes_enabled`` gate.
    """
    kind = RUNTIME_CATALOG.get(str(runtime_type or "").strip().lower())
    return True if kind is None else kind.requires_sandbox


def runtime_catalog() -> list[dict[str, Any]]:
    """Serializable catalog for API/web consumers."""
    return [
        {
            "runtime_type": kind.runtime_type,
            "label": kind.label,
            "summary": kind.summary,
            "requires_sandbox": kind.requires_sandbox,
            "accepts_source": kind.accepts_source,
        }
        for kind in RUNTIME_CATALOG.values()
    ]


def get_runtime(runtime_type: str) -> ToolRuntime:
    """Resolve the runtime for one kind, or raise ``ToolRuntimeError``."""
    key = str(runtime_type or "").strip().lower()
    runtimes = _load_runtimes()
    try:
        return runtimes[key]
    except KeyError:
        supported = ", ".join(sorted(runtimes))
        raise ToolRuntimeError(
            f"Unsupported runtime_type {runtime_type!r}; this build supports: {supported}"
        ) from None
