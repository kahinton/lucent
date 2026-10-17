"""Behavioral checks for the local Docker Compose service defaults."""

from pathlib import Path

import yaml


COMPOSE_PATH = Path(__file__).resolve().parents[1] / "docker-compose.yml"


def _services() -> dict:
    compose = yaml.safe_load(COMPOSE_PATH.read_text())
    return compose["services"]


def test_single_daemon_starts_without_a_profile():
    daemon = _services()["daemon-1"]

    assert "profiles" not in daemon


def test_the_daemon_service_is_the_controller():
    """The always-on service supervises; workers are spawned, not declared.

    A second declared daemon would only duplicate supervision of the same
    organizations, so extra capacity comes from the per-org warm floor instead.
    """
    services = _services()

    assert "daemon-2" not in services
    daemon_env = services["daemon-1"]["environment"]
    assert daemon_env["LUCENT_DAEMON_ROLES"] == "${LUCENT_DAEMON_ROLES:-all}"
    assert "LUCENT_DAEMON_WORKER_MIN_WARM" in daemon_env


def test_compose_default_matches_the_setting_default():
    """The compose file sets the warm floor explicitly, so its fallback must
    agree with the runtime setting's default — otherwise the container silently
    runs a different floor than the code says, and the settings UI lies.
    """
    import re

    from lucent import settings as runtime_settings

    value = _services()["daemon-1"]["environment"]["LUCENT_DAEMON_WORKER_MIN_WARM"]
    match = re.fullmatch(r"\$\{[A-Z_]+:-(\d+)\}", value)
    assert match, f"unexpected compose default expression: {value!r}"

    assert int(match.group(1)) == runtime_settings.get_runtime_setting_definition(
        "daemon.worker_min_warm"
    ).default
