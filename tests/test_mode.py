"""Tests for deployment mode reporting.

The mode is **reporting only** — it must never decide whether a feature works.
These tests pin that: an unlicensed or "personal" instance still reports a mode,
never raises, and never falls back to hiding capability. Capability itself is
covered by ``tests/test_rbac.py`` / ``tests/test_rbac_roles.py``.
"""

import pytest

from lucent.license import create_license
from lucent.mode import (
    DeploymentMode,
    deployment_label,
    get_mode,
    is_personal_mode,
    is_team_mode,
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

    def test_team_without_key_still_reports_team(self, monkeypatch):
        """A missing licence must not silently rewrite the reported mode.

        It used to fall back to PERSONAL, which meant an operator who set
        LUCENT_MODE=team but forgot the key silently lost features. The mode is
        now honest about what was configured; it just does not gate anything.
        """
        monkeypatch.setenv("LUCENT_MODE", "team")
        monkeypatch.delenv("LUCENT_LICENSE_KEY", raising=False)
        assert get_mode() == DeploymentMode.TEAM

    def test_team_with_empty_key_still_reports_team(self, monkeypatch):
        monkeypatch.setenv("LUCENT_MODE", "team")
        monkeypatch.setenv("LUCENT_LICENSE_KEY", "")
        assert get_mode() == DeploymentMode.TEAM

    def test_team_with_valid_key(self, monkeypatch):
        monkeypatch.setenv("LUCENT_MODE", "team")
        monkeypatch.setenv("LUCENT_LICENSE_KEY", _valid_license_key())
        assert get_mode() == DeploymentMode.TEAM

    def test_team_with_invalid_key_does_not_raise(self, monkeypatch):
        """An invalid licence is a log line, not a downgrade and not an error."""
        monkeypatch.setenv("LUCENT_MODE", "team")
        monkeypatch.setenv("LUCENT_LICENSE_KEY", "not-a-valid-license")
        assert get_mode() == DeploymentMode.TEAM

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
    """Tests for the reporting helpers."""

    def test_is_personal_mode(self, monkeypatch):
        monkeypatch.setenv("LUCENT_MODE", "personal")
        assert is_personal_mode() is True
        assert is_team_mode() is False

    def test_is_team_mode(self, monkeypatch):
        monkeypatch.setenv("LUCENT_MODE", "team")
        assert is_team_mode() is True
        assert is_personal_mode() is False

    def test_is_team_mode_needs_no_licence(self, monkeypatch):
        """Reporting team-ness must not depend on licence validity."""
        monkeypatch.setenv("LUCENT_MODE", "team")
        monkeypatch.delenv("LUCENT_LICENSE_KEY", raising=False)
        assert is_team_mode() is True

    def test_deployment_label(self, monkeypatch):
        monkeypatch.setenv("LUCENT_MODE", "team")
        assert deployment_label() == "Team"
        get_mode.cache_clear()
        monkeypatch.setenv("LUCENT_MODE", "personal")
        assert deployment_label() == "Personal"


class TestModeHasNoAuthority:
    """Guards against reintroducing mode-based gating."""

    def test_require_team_mode_is_gone(self):
        """The gate function was removed on purpose.

        Re-adding it would invite the same class of bug: a feature that is
        licensed, implemented, and permission-checked but unreachable because an
        environment variable says "personal". Capability belongs to
        ``lucent.rbac``.
        """
        import lucent.mode as mode_module

        assert not hasattr(mode_module, "require_team_mode")

    def test_organization_router_is_registered_regardless_of_mode(self, monkeypatch):
        """The organization API must exist in every mode.

        This is the regression that motivated removing the gate: in personal mode
        ``/api/organizations`` returned 404, so a hyperadmin could not provision
        or manage a tenant at all.
        """
        monkeypatch.setenv("LUCENT_MODE", "personal")
        monkeypatch.delenv("LUCENT_LICENSE_KEY", raising=False)
        get_mode.cache_clear()

        from lucent.api.app import create_app

        # FastAPI includes routers lazily, so inspect the generated schema
        # rather than ``app.routes`` (which still holds ``_IncludedRouter``
        # placeholders at this point).
        paths = set(create_app().openapi()["paths"])
        assert "/api/organizations/provision" in paths
        assert "/api/users/me" in paths
