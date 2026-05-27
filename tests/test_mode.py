"""Tests for deployment mode configuration."""

import pytest

from lucent.license import create_license
from lucent.mode import (
    DeploymentMode,
    get_mode,
    is_personal_mode,
    is_team_mode,
    require_team_mode,
)

# Matches the public key embedded in license.py
_TEST_PRIVATE_KEY = "eeddca8cb93457f6e6064745285738aef75c9a281d876677c2fb4690fca5b095"


def _valid_license_key() -> str:
    """Generate a valid signed license key for tests."""
    return create_license(_TEST_PRIVATE_KEY, "test-org", max_users=10)


@pytest.fixture(autouse=True)
def clear_mode_cache():
    """Clear the lru_cache on get_mode before each test."""
    get_mode.cache_clear()
    yield
    get_mode.cache_clear()


class TestGetMode:
    """Tests for get_mode()."""

    def test_default_is_personal(self, monkeypatch):
        monkeypatch.delenv("LUCENT_MODE", raising=False)
        monkeypatch.delenv("LUCENT_LICENSE_KEY", raising=False)
        assert get_mode() == DeploymentMode.PERSONAL

    def test_explicit_personal(self, monkeypatch):
        monkeypatch.setenv("LUCENT_MODE", "personal")
        assert get_mode() == DeploymentMode.PERSONAL

    def test_team_without_key_falls_back(self, monkeypatch):
        monkeypatch.setenv("LUCENT_MODE", "team")
        monkeypatch.delenv("LUCENT_LICENSE_KEY", raising=False)
        assert get_mode() == DeploymentMode.PERSONAL

    def test_team_with_empty_key_falls_back(self, monkeypatch):
        monkeypatch.setenv("LUCENT_MODE", "team")
        monkeypatch.setenv("LUCENT_LICENSE_KEY", "")
        assert get_mode() == DeploymentMode.PERSONAL

    def test_team_with_valid_key(self, monkeypatch):
        monkeypatch.setenv("LUCENT_MODE", "team")
        monkeypatch.setenv("LUCENT_LICENSE_KEY", _valid_license_key())
        assert get_mode() == DeploymentMode.TEAM

    def test_team_with_invalid_key_falls_back(self, monkeypatch):
        monkeypatch.setenv("LUCENT_MODE", "team")
        monkeypatch.setenv("LUCENT_LICENSE_KEY", "not-a-valid-license")
        assert get_mode() == DeploymentMode.PERSONAL

    def test_unknown_mode_defaults_to_personal(self, monkeypatch):
        monkeypatch.setenv("LUCENT_MODE", "enterprise")
        assert get_mode() == DeploymentMode.PERSONAL

    def test_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("LUCENT_MODE", "Personal")
        assert get_mode() == DeploymentMode.PERSONAL

    def test_whitespace_stripped(self, monkeypatch):
        monkeypatch.setenv("LUCENT_MODE", "  personal  ")
        assert get_mode() == DeploymentMode.PERSONAL


class TestModeHelpers:
    """Tests for is_personal_mode, is_team_mode, require_team_mode."""

    def test_is_personal_mode(self, monkeypatch):
        monkeypatch.setenv("LUCENT_MODE", "personal")
        assert is_personal_mode() is True
        assert is_team_mode() is False

    def test_is_team_mode(self, monkeypatch):
        monkeypatch.setenv("LUCENT_MODE", "team")
        monkeypatch.setenv("LUCENT_LICENSE_KEY", _valid_license_key())
        assert is_team_mode() is True
        assert is_personal_mode() is False

    def test_require_team_mode_raises_in_personal(self, monkeypatch):
        monkeypatch.setenv("LUCENT_MODE", "personal")
        with pytest.raises(PermissionError, match="team mode"):
            require_team_mode("sharing")

    def test_require_team_mode_passes_in_team(self, monkeypatch):
        monkeypatch.setenv("LUCENT_MODE", "team")
        monkeypatch.setenv("LUCENT_LICENSE_KEY", _valid_license_key())
        require_team_mode("sharing")  # Should not raise
