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


def test_multi_daemon_profile_only_adds_second_worker():
    services = _services()

    assert "profiles" not in services["daemon-1"]
    assert services["daemon-2"]["profiles"] == ["multi-daemon"]