"""Unit tests for the non-interactive bootstrap guards (lucent.bootstrap).

The guard conditions around the env-driven container bootstrap are the security
surface: they must refuse to create an admin account unless every condition
holds. These tests pin that behaviour without a database.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from lucent import bootstrap


class _FakeModelPool:
    """Just enough of a pool for the dry-run model-planning path."""

    def __init__(self, model_rows=()):
        self.model_rows = {row[0]: row[1] for row in model_rows}

    async def fetchval(self, query, *args):
        if "from models" in query.lower():
            return args[0] in self.model_rows
        return None

    async def fetchrow(self, query, *args):
        if "organization_id" in query.lower():
            return {"organization_id": self.model_rows.get(args[0])}
        return None


class TestStartupFlag:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv(bootstrap.STARTUP_FLAG_ENV, raising=False)
        assert bootstrap.startup_bootstrap_enabled() is False

    def test_truthy_values(self, monkeypatch):
        for value in ("1", "true", "TRUE", "yes", "on"):
            monkeypatch.setenv(bootstrap.STARTUP_FLAG_ENV, value)
            assert bootstrap.startup_bootstrap_enabled() is True

    def test_falsey_values(self, monkeypatch):
        for value in ("0", "false", "no", "off", ""):
            monkeypatch.setenv(bootstrap.STARTUP_FLAG_ENV, value)
            assert bootstrap.startup_bootstrap_enabled() is False


class TestRunStartupBootstrap:
    async def test_noop_when_disabled(self, monkeypatch):
        monkeypatch.delenv(bootstrap.STARTUP_FLAG_ENV, raising=False)
        assert await bootstrap.run_startup_bootstrap(MagicMock()) is None

    async def test_refuses_without_password(self, monkeypatch):
        monkeypatch.setenv(bootstrap.STARTUP_FLAG_ENV, "true")
        monkeypatch.delenv(bootstrap.PASSWORD_ENV, raising=False)
        assert await bootstrap.run_startup_bootstrap(MagicMock()) is None

    async def test_skips_when_a_human_user_exists(self, monkeypatch):
        monkeypatch.setenv(bootstrap.STARTUP_FLAG_ENV, "true")
        monkeypatch.setenv(bootstrap.PASSWORD_ENV, "ValidPass1")
        with patch("lucent.auth_providers.is_first_run", return_value=False) as first_run:
            assert await bootstrap.run_startup_bootstrap(MagicMock()) is None
        first_run.assert_awaited_once()

    async def test_refuses_weak_password(self, monkeypatch):
        monkeypatch.setenv(bootstrap.STARTUP_FLAG_ENV, "true")
        monkeypatch.setenv(bootstrap.PASSWORD_ENV, "weak")
        with patch("lucent.auth_providers.is_first_run", return_value=True):
            assert await bootstrap.run_startup_bootstrap(MagicMock()) is None

    async def test_provisions_when_all_guards_pass(self, monkeypatch):
        monkeypatch.setenv(bootstrap.STARTUP_FLAG_ENV, "true")
        monkeypatch.setenv(bootstrap.PASSWORD_ENV, "ValidPass1")
        monkeypatch.setenv(bootstrap.DISPLAY_NAME_ENV, "Ops Admin")
        fake_result = bootstrap.ProvisionResult(created_user=True)
        with (
            patch("lucent.auth_providers.is_first_run", return_value=True),
            patch.object(
                bootstrap, "provision_instance", AsyncMock(return_value=fake_result)
            ) as provision,
        ):
            result = await bootstrap.run_startup_bootstrap(MagicMock())

        assert result is fake_result
        provision.assert_awaited_once()
        assert provision.await_args.kwargs["display_name"] == "Ops Admin"


class TestStartupApiKeyDelivery:
    def test_writes_key_to_file(self, monkeypatch, tmp_path):
        target = tmp_path / "api-key"
        monkeypatch.setenv(bootstrap.API_KEY_FILE_ENV, str(target))
        result = bootstrap.ProvisionResult(api_key="hs_supersecret")
        bootstrap._deliver_startup_api_key(result)
        assert target.read_text().strip() == "hs_supersecret"
        # The plaintext is dropped from the result after delivery.
        assert result.api_key is None
        assert oct(target.stat().st_mode & 0o777) == "0o600"

    def test_key_not_written_without_path(self, monkeypatch):
        monkeypatch.delenv(bootstrap.API_KEY_FILE_ENV, raising=False)
        result = bootstrap.ProvisionResult(api_key="hs_supersecret")
        bootstrap._deliver_startup_api_key(result)  # logs a warning, writes nothing
        assert result.api_key == "hs_supersecret"


class TestProvisionDryRun:
    async def test_dry_run_creates_nothing(self, monkeypatch):
        monkeypatch.delenv(bootstrap.MODEL_ENV, raising=False)
        pool = _FakeModelPool(model_rows=[("gpt-5.1", None)])
        with (
            patch("lucent.auth_providers.is_first_run", return_value=True),
            patch("lucent.auth_providers.create_initial_user") as create,
        ):
            result = await bootstrap.provision_instance(
                pool,
                display_name="Admin",
                email=None,
                password="ValidPass1",
                model_id="gpt-5.1",
                dry_run=True,
            )
        create.assert_not_called()
        assert result.dry_run
        assert result.user is None
        assert result.model is not None
        assert result.model.adopted is True  # global seed -> would be adopted
        assert result.model.enabled and result.model.granted
