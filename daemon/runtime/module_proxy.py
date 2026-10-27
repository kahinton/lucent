"""Lazy access to the live daemon runtime module.

The daemon can start either as ``python daemon/daemon.py`` or as
``python -m daemon.daemon``.  In module mode the executing runtime initially
exists only as ``__main__``; importing ``daemon.daemon`` from a mixin during
startup would execute the entrypoint a second time and create a circular
import.  This proxy resolves the already-running module first and imports the
canonical module only when no entrypoint is currently initializing.
"""

from __future__ import annotations

import importlib
import sys
from types import ModuleType


class RuntimeModuleProxy:
    """Resolve attributes against the currently executing daemon module."""

    @staticmethod
    def _entrypoint() -> ModuleType | None:
        """Return ``__main__`` when it IS the daemon entrypoint, else None."""
        main = sys.modules.get("__main__")
        if main is None:
            return None
        main_file = str(getattr(main, "__file__", ""))
        return main if main_file.endswith("daemon/daemon.py") else None

    @classmethod
    def _module(cls) -> ModuleType:
        # The live entrypoint wins even if ``daemon.daemon`` is registered.
        #
        # ``python -m daemon.daemon`` executes the file as ``__main__`` and
        # does NOT register it under its canonical name, so ANY
        # ``from daemon.daemon import X`` executed at runtime imports a
        # SECOND, freshly-initialized copy of this module. That copy owns
        # module-level state of its own — ``API_HEADERS``/``MCP_CONFIG`` are
        # still the unauthenticated defaults and ``MCP_API_KEY`` is empty —
        # so preferring it silently strips the auth header from every REST
        # call and makes the daemon believe its key was rejected (which, on
        # the recovery path, revokes the live daemon key).
        entrypoint = cls._entrypoint()
        if entrypoint is not None:
            return entrypoint

        canonical = sys.modules.get("daemon.daemon")
        if canonical is not None:
            return canonical

        return importlib.import_module("daemon.daemon")

    def __getattr__(self, name: str):
        return getattr(self._module(), name)

    def __setattr__(self, name: str, value) -> None:
        setattr(self._module(), name, value)


runtime = RuntimeModuleProxy()
