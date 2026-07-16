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
    def _module() -> ModuleType:
        canonical = sys.modules.get("daemon.daemon")
        if canonical is not None:
            return canonical

        main = sys.modules.get("__main__")
        main_file = str(getattr(main, "__file__", ""))
        if main is not None and main_file.endswith("daemon/daemon.py"):
            return main

        return importlib.import_module("daemon.daemon")

    def __getattr__(self, name: str):
        return getattr(self._module(), name)


runtime = RuntimeModuleProxy()
