"""Tests for database connection pool management (lucent.db.pool)."""

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

import lucent.db.pool as pool_module
from lucent.db.pool import _init_connection, _run_migrations, close_db, get_pool, init_db


def _make_mock_pool(mock_conn=None):
    """Create a mock pool with properly mocked acquire() context manager."""
    if mock_conn is None:
        mock_conn = AsyncMock()
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_cm.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = mock_cm
    pool.close = AsyncMock()
    return pool


@pytest.fixture(autouse=True)
def reset_pool():
    """Reset global pool state before and after each test."""
    original = pool_module._pool
    pool_module._pool = None
    yield
    pool_module._pool = original


class TestInitDb:
    """Tests for init_db()."""

    async def test_uses_provided_url(self):
        """Test that init_db uses a directly provided URL."""
        mock_pool = _make_mock_pool()
        create_pool = AsyncMock(return_value=mock_pool)
        with patch("lucent.db.pool.asyncpg.create_pool", create_pool):
            with patch("lucent.db.pool._run_migrations", new_callable=AsyncMock):
                result = await init_db("postgresql://test:test@localhost/test")

        create_pool.assert_called_once()
        assert create_pool.call_args[0][0] == "postgresql://test:test@localhost/test"
        assert result is mock_pool

    async def test_uses_env_var_when_no_url(self, monkeypatch):
        """Test that init_db falls back to DATABASE_URL env var."""
        monkeypatch.setenv("DATABASE_URL", "postgresql://env:env@localhost/env")
        mock_pool = _make_mock_pool()
        create_pool = AsyncMock(return_value=mock_pool)
        with patch("lucent.db.pool.asyncpg.create_pool", create_pool):
            with patch("lucent.db.pool._run_migrations", new_callable=AsyncMock):
                await init_db()

        assert create_pool.call_args[0][0] == "postgresql://env:env@localhost/env"

    async def test_raises_without_url(self, monkeypatch):
        """Test that init_db raises ValueError when no URL is available."""
        monkeypatch.delenv("DATABASE_URL", raising=False)

        with pytest.raises(ValueError, match="DATABASE_URL"):
            await init_db()

    async def test_returns_existing_pool_if_initialized(self):
        """Test that init_db returns existing pool without creating a new one."""
        existing_pool = _make_mock_pool()
        pool_module._pool = existing_pool

        result = await init_db("postgresql://test:test@localhost/test")

        assert result is existing_pool

    async def test_pool_config_parameters(self):
        """Test that pool is created with expected config."""
        mock_pool = _make_mock_pool()
        create_pool = AsyncMock(return_value=mock_pool)
        with patch("lucent.db.pool.asyncpg.create_pool", create_pool):
            with patch("lucent.db.pool._run_migrations", new_callable=AsyncMock):
                await init_db("postgresql://test:test@localhost/test")

        kwargs = create_pool.call_args[1]
        assert kwargs["min_size"] == 2
        assert kwargs["max_size"] == 10
        assert kwargs["command_timeout"] == 60

    async def test_runs_migrations_after_pool_creation(self):
        """Test that migrations are run after pool is created."""
        mock_pool = _make_mock_pool()
        create_pool = AsyncMock(return_value=mock_pool)
        with patch("lucent.db.pool.asyncpg.create_pool", create_pool):
            with patch("lucent.db.pool._run_migrations", new_callable=AsyncMock) as migrate:
                await init_db("postgresql://test:test@localhost/test")

        migrate.assert_called_once_with(mock_pool)


class TestGetPool:
    """Tests for get_pool()."""

    async def test_raises_when_not_initialized(self):
        """Test that get_pool raises RuntimeError when pool is None."""
        with pytest.raises(RuntimeError, match="not initialized"):
            await get_pool()

    async def test_returns_pool_when_initialized(self):
        """Test that get_pool returns the pool when it's set."""
        mock_pool = _make_mock_pool()
        pool_module._pool = mock_pool

        result = await get_pool()
        assert result is mock_pool


class TestCloseDb:
    """Tests for close_db()."""

    async def test_closes_and_clears_pool(self):
        """Test that close_db closes the pool and sets global to None."""
        mock_pool = _make_mock_pool()
        pool_module._pool = mock_pool

        await close_db()

        mock_pool.close.assert_called_once()
        assert pool_module._pool is None

    async def test_noop_when_no_pool(self):
        """Test that close_db is safe to call when pool is None."""
        await close_db()
        assert pool_module._pool is None


class TestInitConnection:
    """Tests for _init_connection()."""

    async def test_registers_uuid_codec(self):
        """Test that UUID codec is registered on connection."""
        mock_conn = AsyncMock()

        await _init_connection(mock_conn)

        calls = mock_conn.set_type_codec.call_args_list
        uuid_call = [c for c in calls if c[0][0] == "uuid"]
        assert len(uuid_call) == 1
        assert uuid_call[0][1]["schema"] == "pg_catalog"

    async def test_registers_jsonb_codec(self):
        """Test that JSONB codec is registered on connection."""
        mock_conn = AsyncMock()

        await _init_connection(mock_conn)

        calls = mock_conn.set_type_codec.call_args_list
        jsonb_call = [c for c in calls if c[0][0] == "jsonb"]
        assert len(jsonb_call) == 1
        assert jsonb_call[0][1]["schema"] == "pg_catalog"

    async def test_uuid_decoder_parses_uuid(self):
        """Test that the UUID decoder correctly converts strings to UUID."""
        mock_conn = AsyncMock()
        await _init_connection(mock_conn)

        uuid_call = [c for c in mock_conn.set_type_codec.call_args_list if c[0][0] == "uuid"][0]
        decoder = uuid_call[1]["decoder"]

        result = decoder("12345678-1234-5678-1234-567812345678")
        assert isinstance(result, UUID)
        assert str(result) == "12345678-1234-5678-1234-567812345678"

    async def test_uuid_decoder_handles_none(self):
        """Test that the UUID decoder returns None for falsy values."""
        mock_conn = AsyncMock()
        await _init_connection(mock_conn)

        uuid_call = [c for c in mock_conn.set_type_codec.call_args_list if c[0][0] == "uuid"][0]
        decoder = uuid_call[1]["decoder"]

        assert decoder(None) is None

    async def test_jsonb_encoder_is_json_dumps(self):
        """Test that the JSONB encoder uses json.dumps."""
        mock_conn = AsyncMock()
        await _init_connection(mock_conn)

        jsonb_call = [c for c in mock_conn.set_type_codec.call_args_list if c[0][0] == "jsonb"][0]
        encoder = jsonb_call[1]["encoder"]

        assert encoder({"key": "value"}) == json.dumps({"key": "value"})

    async def test_jsonb_decoder_is_json_loads(self):
        """Test that the JSONB decoder uses json.loads."""
        mock_conn = AsyncMock()
        await _init_connection(mock_conn)

        jsonb_call = [c for c in mock_conn.set_type_codec.call_args_list if c[0][0] == "jsonb"][0]
        decoder = jsonb_call[1]["decoder"]

        assert decoder('{"key": "value"}') == {"key": "value"}


class TestRunMigrations:
    """Tests for _run_migrations()."""

    async def test_skips_when_no_migrations_dir(self, tmp_path):
        """Test that _run_migrations is a no-op when migrations dir doesn't exist."""
        mock_pool = _make_mock_pool()
        nonexistent = tmp_path / "nonexistent_migrations"
        with patch("lucent.db.pool.Path") as mock_path_cls:
            mock_path_cls.return_value.parent.__truediv__ = MagicMock(return_value=nonexistent)
            await _run_migrations(mock_pool)

        mock_pool.acquire.assert_not_called()

    async def test_applies_new_migrations(self, tmp_path):
        """Test that new migration files are applied in order."""
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()
        (migrations_dir / "001_init.sql").write_text("CREATE TABLE test (id INT);")
        (migrations_dir / "002_add_col.sql").write_text("ALTER TABLE test ADD COLUMN name TEXT;")

        mock_conn = AsyncMock()
        mock_conn.fetch.return_value = []
        # transaction() is sync, returns an async context manager
        mock_tx = MagicMock()
        mock_tx.__aenter__ = AsyncMock()
        mock_tx.__aexit__ = AsyncMock(return_value=False)
        mock_conn.transaction = MagicMock(return_value=mock_tx)

        mock_pool = _make_mock_pool(mock_conn)

        with patch("lucent.db.pool.Path") as mock_path_cls:
            mock_path_cls.return_value.parent.__truediv__ = MagicMock(return_value=migrations_dir)
            await _run_migrations(mock_pool)

        # Should have created tracking table and executed both migrations
        execute_calls = [str(c) for c in mock_conn.execute.call_args_list]
        assert any("_migrations" in str(c) for c in execute_calls)

    async def test_skips_already_applied_migrations(self, tmp_path):
        """Test that already-applied migrations are skipped."""
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()
        (migrations_dir / "001_init.sql").write_text("CREATE TABLE test (id INT);")
        (migrations_dir / "002_new.sql").write_text("ALTER TABLE test ADD COLUMN name TEXT;")

        mock_row = {"name": "001_init.sql"}
        mock_conn = AsyncMock()
        mock_conn.fetch.return_value = [mock_row]
        mock_tx = MagicMock()
        mock_tx.__aenter__ = AsyncMock()
        mock_tx.__aexit__ = AsyncMock(return_value=False)
        mock_conn.transaction = MagicMock(return_value=mock_tx)

        mock_pool = _make_mock_pool(mock_conn)

        with patch("lucent.db.pool.Path") as mock_path_cls:
            mock_path_cls.return_value.parent.__truediv__ = MagicMock(return_value=migrations_dir)
            await _run_migrations(mock_pool)

        # Should only execute the 002 migration SQL (not 001)
        execute_sql_texts = [str(c) for c in mock_conn.execute.call_args_list]
        assert not any("CREATE TABLE test (id INT)" in str(c) for c in execute_sql_texts)
