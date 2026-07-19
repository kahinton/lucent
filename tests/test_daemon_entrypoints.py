"""Regression tests for supported daemon startup styles."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_help(*args: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    source_path = str(REPO_ROOT / "src")
    environment["PYTHONPATH"] = os.pathsep.join(
        path for path in (source_path, environment.get("PYTHONPATH")) if path
    )
    return subprocess.run(
        [sys.executable, *args, "--help"],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_module_entrypoint_imports_without_loading_daemon_twice():
    result = _run_help("-m", "daemon.daemon")

    assert result.returncode == 0, result.stderr
    assert "Lucent Daemon" in result.stdout
    assert "partially initialized module" not in result.stderr


def test_direct_script_entrypoint_resolves_daemon_as_package():
    result = _run_help("daemon/daemon.py")

    assert result.returncode == 0, result.stderr
    assert "Lucent Daemon" in result.stdout
    assert "'daemon' is not a package" not in result.stderr
