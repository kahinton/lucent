"""The runtime proxy must never resolve a shadow copy of the daemon module.

``python -m daemon.daemon`` executes the entrypoint as ``__main__`` and does
NOT register it under its canonical ``daemon.daemon`` name. Any runtime
``from daemon.daemon import X`` therefore builds a SECOND, freshly-initialized
copy of the module. That copy owns its own module-level state, so

* ``API_HEADERS`` never receives the provisioned ``Authorization`` header and
  every REST call 401s ("API key required"), and
* ``MCP_API_KEY`` is empty, which makes the key-recovery path believe the live
  daemon key was rejected and re-provision (revoking) it.

These tests pin the resolution order: the running entrypoint always wins.
"""

from __future__ import annotations

import sys
from types import ModuleType

import pytest

from daemon.runtime.module_proxy import RuntimeModuleProxy

ENTRYPOINT_FILE = "/app/daemon/daemon.py"


def _module(name: str, **attrs) -> ModuleType:
    """Fake module; dunder attrs are set with setattr (dict.update skips them)."""
    module = ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


def _install(monkeypatch, *, canonical: ModuleType | None = None) -> ModuleType:
    """Install a fake daemon entrypoint plus an optional shadow copy."""
    main = _module("__main__", __file__=ENTRYPOINT_FILE)
    monkeypatch.setitem(sys.modules, "__main__", main)
    if canonical is None:
        monkeypatch.delitem(sys.modules, "daemon.daemon", raising=False)
    else:
        monkeypatch.setitem(sys.modules, "daemon.daemon", canonical)
    return main


def test_entrypoint_wins_over_shadow_copy(monkeypatch):
    """A registered ``daemon.daemon`` shadow must not be preferred."""
    shadow = _module("daemon.daemon", API_HEADERS={"Content-Type": "application/json"})
    main = _install(monkeypatch, canonical=shadow)
    main.API_HEADERS = {"Content-Type": "application/json", "Authorization": "Bearer live"}

    assert RuntimeModuleProxy._module() is main
    assert RuntimeModuleProxy().API_HEADERS["Authorization"] == "Bearer live"


def test_primary_path_still_prefers_registered_module(monkeypatch):
    """With no daemon entrypoint, the canonical module is used (server/tests)."""
    monkeypatch.setitem(sys.modules, "__main__", _module("__main__", __file__="/app/other.py"))
    canonical = _module("daemon.daemon", API_HEADERS={"Authorization": "Bearer other"})
    monkeypatch.setitem(sys.modules, "daemon.daemon", canonical)
    assert RuntimeModuleProxy._module() is canonical


def test_falls_back_to_import_when_nothing_registered(monkeypatch):
    """Neither __main__ nor daemon.daemon present -> import the real module."""
    monkeypatch.setitem(sys.modules, "__main__", _module("__main__"))
    monkeypatch.delitem(sys.modules, "daemon.daemon", raising=False)
    sentinel = _module("daemon.daemon", marker="imported")
    monkeypatch.setattr(
        "daemon.runtime.module_proxy.importlib.import_module", lambda name: sentinel
    )

    assert RuntimeModuleProxy._module() is sentinel


def test_proxy_setattr_targets_the_entrypoint(monkeypatch):
    """Writes must land on the live module, not on a shadow copy."""
    shadow = _module("daemon.daemon", API_HEADERS={"Authorization": "Bearer shadow"})
    main = _install(monkeypatch, canonical=shadow)
    main.API_HEADERS = {}

    RuntimeModuleProxy().API_HEADERS = {"Authorization": "Bearer rotated"}

    assert main.API_HEADERS["Authorization"] == "Bearer rotated"
    assert shadow.API_HEADERS["Authorization"] == "Bearer shadow"
