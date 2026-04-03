"""Tests for database connection pool management (lucent.db.pool)."""

import hashlib
import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

import lucent.db.pool as pool_module
from lucent.db.pool import (
    _bootstrap_schema_migrations,
    _discover_forward_migration_files,
    _file_checksum,
    _parse_migration_metadata,
    _rollback_migrations,
    _init_connection,
    _run_migrations,
    close_db,
    get_pool,
    init_db,
)


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
        mock_conn.fetchval.return_value = False  # no legacy _migrations table
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
        assert any("schema_migrations" in str(c) for c in execute_calls)

    async def test_skips_already_applied_migrations(self, tmp_path):
        """Test that already-applied migrations are skipped."""
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()
        f1 = migrations_dir / "001_init.sql"
        f1.write_text("CREATE TABLE test (id INT);")
        (migrations_dir / "002_new.sql").write_text("ALTER TABLE test ADD COLUMN name TEXT;")

        checksum = hashlib.sha256(f1.read_text().encode()).hexdigest()
        mock_row = {"name": "001_init.sql", "checksum": checksum}
        mock_conn = AsyncMock()
        mock_conn.fetch.return_value = [mock_row]
        mock_conn.fetchval.return_value = False  # no legacy _migrations table
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

    async def test_records_checksum_on_apply(self, tmp_path):
        """Test that SHA-256 checksum is stored when a migration is applied."""
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()
        content = "CREATE TABLE test (id INT);"
        (migrations_dir / "001_init.sql").write_text(content)

        expected_checksum = hashlib.sha256(content.encode()).hexdigest()

        mock_conn = AsyncMock()
        mock_conn.fetch.return_value = []
        mock_conn.fetchval.return_value = False
        mock_tx = MagicMock()
        mock_tx.__aenter__ = AsyncMock()
        mock_tx.__aexit__ = AsyncMock(return_value=False)
        mock_conn.transaction = MagicMock(return_value=mock_tx)

        mock_pool = _make_mock_pool(mock_conn)

        with patch("lucent.db.pool.Path") as mock_path_cls:
            mock_path_cls.return_value.parent.__truediv__ = MagicMock(return_value=migrations_dir)
            await _run_migrations(mock_pool)

        # Find the INSERT call with checksum
        insert_calls = [
            c for c in mock_conn.execute.call_args_list
            if "INSERT INTO schema_migrations" in str(c)
        ]
        assert len(insert_calls) == 1
        assert insert_calls[0][0][1] == "001_init.sql"
        assert insert_calls[0][0][2] == expected_checksum

    async def test_warns_on_checksum_mismatch(self, tmp_path, caplog):
        """Test that a warning is logged when file content changed after application."""
        import logging

        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()
        (migrations_dir / "001_init.sql").write_text("CREATE TABLE test (id INT);")

        # Recorded checksum doesn't match current file content
        mock_row = {"name": "001_init.sql", "checksum": "0" * 64}
        mock_conn = AsyncMock()
        mock_conn.fetch.return_value = [mock_row]
        mock_conn.fetchval.return_value = False
        mock_pool = _make_mock_pool(mock_conn)

        with patch("lucent.db.pool.Path") as mock_path_cls:
            mock_path_cls.return_value.parent.__truediv__ = MagicMock(return_value=migrations_dir)
            with caplog.at_level(logging.WARNING, logger="lucent.db.pool"):
                await _run_migrations(mock_pool)

        assert any("modified after application" in r.message for r in caplog.records)


class TestBootstrapSchemaMigrations:
    """Tests for _bootstrap_schema_migrations()."""

    async def test_creates_table_fresh(self):
        """Test that schema_migrations table is created on fresh database."""
        mock_conn = AsyncMock()
        mock_conn.fetchval.return_value = False  # no legacy table

        await _bootstrap_schema_migrations(mock_conn, [])

        execute_calls = [str(c) for c in mock_conn.execute.call_args_list]
        assert any("CREATE TABLE IF NOT EXISTS schema_migrations" in s for s in execute_calls)

    async def test_migrates_from_legacy_table(self, tmp_path):
        """Test that data is copied from _migrations to schema_migrations."""
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()
        f1 = migrations_dir / "001_init.sql"
        f1.write_text("CREATE TABLE test (id INT);")

        mock_conn = AsyncMock()
        mock_conn.fetchval.return_value = True  # legacy table exists
        mock_conn.fetch.return_value = [{"name": "001_init.sql"}]  # migrated rows

        await _bootstrap_schema_migrations(mock_conn, [f1])

        execute_calls = [str(c) for c in mock_conn.execute.call_args_list]
        # Should have: CREATE schema_migrations, INSERT from _migrations,
        # UPDATE checksum, DROP _migrations
        assert any("DROP TABLE _migrations" in s for s in execute_calls)
        assert any("UPDATE schema_migrations SET checksum" in s for s in execute_calls)

    async def test_skips_when_no_legacy_table(self):
        """Test that bootstrap skips legacy migration when _migrations doesn't exist."""
        mock_conn = AsyncMock()
        mock_conn.fetchval.return_value = False

        await _bootstrap_schema_migrations(mock_conn, [])

        execute_calls = [str(c) for c in mock_conn.execute.call_args_list]
        assert not any("DROP TABLE" in s for s in execute_calls)


class TestRollbackMigrations:
    """Tests for _rollback_migrations()."""

    async def test_single_rollback(self, tmp_path):
        """Rollback latest migration and delete tracking row."""
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()
        (migrations_dir / "001_init.sql").write_text("CREATE TABLE test (id INT);")
        (migrations_dir / "001_init.down.sql").write_text("DROP TABLE test;")
        (migrations_dir / "002_add_col.sql").write_text("ALTER TABLE test ADD COLUMN name TEXT;")
        (migrations_dir / "002_add_col.down.sql").write_text("ALTER TABLE test DROP COLUMN name;")

        mock_conn = AsyncMock()
        mock_conn.fetchval.return_value = False
        mock_conn.fetch.return_value = [
            {"name": "002_add_col.sql", "checksum": hashlib.sha256("ALTER TABLE test ADD COLUMN name TEXT;".encode()).hexdigest()},
            {"name": "001_init.sql", "checksum": hashlib.sha256("CREATE TABLE test (id INT);".encode()).hexdigest()},
        ]
        mock_tx = MagicMock()
        mock_tx.__aenter__ = AsyncMock()
        mock_tx.__aexit__ = AsyncMock(return_value=False)
        mock_conn.transaction = MagicMock(return_value=mock_tx)
        mock_pool = _make_mock_pool(mock_conn)

        with patch("lucent.db.pool.Path") as mock_path_cls:
            mock_path_cls.return_value.parent.__truediv__ = MagicMock(return_value=migrations_dir)
            rolled_back = await _rollback_migrations(mock_pool, steps=1)

        assert rolled_back == 1
        execute_calls = [str(c) for c in mock_conn.execute.call_args_list]
        assert any("ALTER TABLE test DROP COLUMN name;" in s for s in execute_calls)
        assert any("DELETE FROM schema_migrations WHERE name = $1" in s for s in execute_calls)

    async def test_multi_step_rollback(self, tmp_path):
        """Rollback multiple migrations in reverse order."""
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()
        (migrations_dir / "001_base.sql").write_text("CREATE TABLE test (id INT);")
        (migrations_dir / "001_base.down.sql").write_text("DROP TABLE test;")
        (migrations_dir / "002_a.sql").write_text("ALTER TABLE test ADD COLUMN a INT;")
        (migrations_dir / "002_a.down.sql").write_text("ALTER TABLE test DROP COLUMN a;")
        (migrations_dir / "003_b.sql").write_text("ALTER TABLE test ADD COLUMN b INT;")
        (migrations_dir / "003_b.down.sql").write_text("ALTER TABLE test DROP COLUMN b;")

        mock_conn = AsyncMock()
        mock_conn.fetchval.return_value = False
        mock_conn.fetch.return_value = [
            {"name": "003_b.sql", "checksum": hashlib.sha256("ALTER TABLE test ADD COLUMN b INT;".encode()).hexdigest()},
            {"name": "002_a.sql", "checksum": hashlib.sha256("ALTER TABLE test ADD COLUMN a INT;".encode()).hexdigest()},
            {"name": "001_base.sql", "checksum": hashlib.sha256("CREATE TABLE test (id INT);".encode()).hexdigest()},
        ]
        mock_tx = MagicMock()
        mock_tx.__aenter__ = AsyncMock()
        mock_tx.__aexit__ = AsyncMock(return_value=False)
        mock_conn.transaction = MagicMock(return_value=mock_tx)
        mock_pool = _make_mock_pool(mock_conn)

        with patch("lucent.db.pool.Path") as mock_path_cls:
            mock_path_cls.return_value.parent.__truediv__ = MagicMock(return_value=migrations_dir)
            rolled_back = await _rollback_migrations(mock_pool, steps=2)

        assert rolled_back == 2
        sql_calls = [c[0][0] for c in mock_conn.execute.call_args_list if c[0]]
        rollback_sqls = [s for s in sql_calls if "DROP COLUMN" in s]
        assert rollback_sqls == [
            "ALTER TABLE test DROP COLUMN b;",
            "ALTER TABLE test DROP COLUMN a;",
        ]

    async def test_rollback_irreversible_migration_fails(self, tmp_path, caplog):
        """Rollback fails for irreversible migrations without allow_irreversible."""
        import logging

        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()
        (migrations_dir / "001_base.sql").write_text("CREATE TABLE test (id INT);")
        (migrations_dir / "001_base.down.sql").write_text("DROP TABLE test;")
        (migrations_dir / "002_data.sql").write_text(
            "-- lucent: rollback=irreversible\nINSERT INTO test (id) VALUES (1);"
        )

        mock_conn = AsyncMock()
        mock_conn.fetchval.return_value = False
        mock_conn.fetch.return_value = [
            {"name": "002_data.sql", "checksum": hashlib.sha256("-- lucent: rollback=irreversible\nINSERT INTO test (id) VALUES (1);".encode()).hexdigest()},
            {"name": "001_base.sql", "checksum": hashlib.sha256("CREATE TABLE test (id INT);".encode()).hexdigest()},
        ]
        mock_tx = MagicMock()
        mock_tx.__aenter__ = AsyncMock()
        mock_tx.__aexit__ = AsyncMock(return_value=False)
        mock_conn.transaction = MagicMock(return_value=mock_tx)
        mock_pool = _make_mock_pool(mock_conn)

        with patch("lucent.db.pool.Path") as mock_path_cls:
            mock_path_cls.return_value.parent.__truediv__ = MagicMock(return_value=migrations_dir)
            with caplog.at_level(logging.WARNING, logger="lucent.db.pool"):
                with pytest.raises(RuntimeError, match="irreversible"):
                    await _rollback_migrations(mock_pool, steps=1)

        assert any("irreversible" in r.message for r in caplog.records)

    async def test_rollback_no_migrations_to_reverse(self, tmp_path):
        """Rollback is a no-op when no migrations were applied."""
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()
        (migrations_dir / "001_init.sql").write_text("CREATE TABLE test (id INT);")
        (migrations_dir / "001_init.down.sql").write_text("DROP TABLE test;")

        mock_conn = AsyncMock()
        mock_conn.fetchval.return_value = False
        mock_conn.fetch.return_value = []
        mock_pool = _make_mock_pool(mock_conn)

        with patch("lucent.db.pool.Path") as mock_path_cls:
            mock_path_cls.return_value.parent.__truediv__ = MagicMock(return_value=migrations_dir)
            rolled_back = await _rollback_migrations(mock_pool, steps=1)

        assert rolled_back == 0
        execute_sqls = [str(c) for c in mock_conn.execute.call_args_list]
        assert not any("DELETE FROM schema_migrations" in s for s in execute_sqls)


class TestMigrationFileMetadata:
    """Tests for migration file discovery and metadata parsing."""

    def test_forward_discovery_excludes_down_files(self, tmp_path):
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()
        up = migrations_dir / "001_init.sql"
        down = migrations_dir / "001_init.down.sql"
        up.write_text("SELECT 1;")
        down.write_text("SELECT 2;")

        files = _discover_forward_migration_files(migrations_dir)
        assert files == [up]

    def test_parse_metadata(self, tmp_path):
        migration = tmp_path / "001_test.sql"
        migration.write_text(
            "-- lucent: rollback=irreversible\n"
            "-- lucent: warning=Data loss expected\n"
            "CREATE TABLE x (id INT);\n"
        )
        metadata = _parse_migration_metadata(migration)
        assert metadata["rollback"] == "irreversible"
        assert metadata["warning"] == "Data loss expected"


class TestFileChecksum:
    """Tests for _file_checksum()."""

    def test_computes_sha256(self, tmp_path):
        """Test that _file_checksum returns correct SHA-256."""
        f = tmp_path / "test.sql"
        content = "CREATE TABLE test (id INT);"
        f.write_text(content)

        expected = hashlib.sha256(content.encode()).hexdigest()
        assert _file_checksum(f) == expected

    def test_deterministic(self, tmp_path):
        """Test that same content produces same checksum."""
        f = tmp_path / "test.sql"
        f.write_text("hello world")

        assert _file_checksum(f) == _file_checksum(f)
