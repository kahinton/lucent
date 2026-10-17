"""Where the daemon writes its log.

This matters more than it looks: the default used to be "next to the daemon
module", which is inside site-packages once the daemon ships in the wheel — not
writable by the runtime user. The daemon then died at startup with a
PermissionError before it could parse a single argument.
"""

from pathlib import Path

import daemon.daemon as daemon_module


def test_writable_directory_detects_a_real_directory(tmp_path):
    assert daemon_module._is_writable_directory(tmp_path) is True


def test_writable_directory_rejects_a_missing_directory():
    assert daemon_module._is_writable_directory(Path("/nonexistent/lucent")) is False


def test_log_file_prefers_the_daemon_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon_module, "DAEMON_DIR", tmp_path)

    assert daemon_module._resolve_log_file() == str(tmp_path / "daemon.log")


def test_log_file_falls_back_to_the_working_directory(monkeypatch):
    """An installed wheel's package directory is not writable."""
    monkeypatch.setattr(daemon_module, "DAEMON_DIR", Path("/nonexistent/lucent/daemon"))

    assert daemon_module._resolve_log_file() == str(Path.cwd() / "daemon.log")


def test_log_file_is_disabled_when_nothing_is_writable(monkeypatch):
    """No file anywhere means stderr-only, not a crash."""
    monkeypatch.setattr(daemon_module, "_is_writable_directory", lambda _dir: False)

    assert daemon_module._resolve_log_file() == ""
