"""Tests for API key repository (lucent.db.api_key).

Supplements the integration tests in test_db.py with unit tests
and additional edge-case coverage.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from lucent.db.api_key import ApiKeyRepository


def _make_mock_pool(mock_conn=None):
    """Create a mock pool with properly mocked acquire() context manager."""
    if mock_conn is None:
        mock_conn = AsyncMock()
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_cm.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = mock_cm
    return pool


def _make_mock_row(overrides=None):
    """Create a mock asyncpg.Record with default API key fields."""
    now = datetime.now(timezone.utc)
    uid = uuid4()
    defaults = {
        "id": uuid4(),
        "user_id": uid,
        "organization_id": None,
        "name": "test-key",
        "key_prefix": "hs_abcdefgh",
        "key_hash": "$2b$12$fakehashvalue",
        "scopes": ["read", "write"],
        "last_used_at": None,
        "use_count": 0,
        "expires_at": None,
        "is_active": True,
        "created_at": now,
        "updated_at": now,
        "revoked_at": None,
    }
    if overrides:
        defaults.update(overrides)

    row = MagicMock()
    row.__getitem__ = lambda self, key: defaults[key]
    row.keys.return_value = defaults.keys()
    return row, defaults


class TestRowToDict:
    """Tests for ApiKeyRepository._row_to_dict()."""

    def test_converts_basic_row(self):
        """Test basic row conversion to dict."""
        pool = _make_mock_pool()
        repo = ApiKeyRepository(pool)
        row, data = _make_mock_row()

        result = repo._row_to_dict(row)

        assert result["id"] == data["id"]
        assert result["name"] == data["name"]
        assert result["key_prefix"] == data["key_prefix"]
        assert result["scopes"] == ["read", "write"]
        assert result["is_active"] is True

    def test_converts_user_id_string_to_uuid(self):
        """Test that string user_id is converted to UUID."""
        pool = _make_mock_pool()
        repo = ApiKeyRepository(pool)
        uid = uuid4()
        row, _ = _make_mock_row({"user_id": str(uid)})

        result = repo._row_to_dict(row)

        assert isinstance(result["user_id"], UUID)
        assert result["user_id"] == uid

    def test_preserves_uuid_user_id(self):
        """Test that UUID user_id is preserved as-is."""
        pool = _make_mock_pool()
        repo = ApiKeyRepository(pool)
        uid = uuid4()
        row, _ = _make_mock_row({"user_id": uid})

        result = repo._row_to_dict(row)

        assert result["user_id"] == uid
        assert isinstance(result["user_id"], UUID)

    def test_handles_organization_id_none(self):
        """Test that None organization_id is preserved."""
        pool = _make_mock_pool()
        repo = ApiKeyRepository(pool)
        row, _ = _make_mock_row({"organization_id": None})

        result = repo._row_to_dict(row)

        assert result["organization_id"] is None

    def test_converts_organization_id_string_to_uuid(self):
        """Test that string organization_id is converted to UUID."""
        pool = _make_mock_pool()
        repo = ApiKeyRepository(pool)
        org_id = uuid4()
        row, _ = _make_mock_row({"organization_id": str(org_id)})

        result = repo._row_to_dict(row)

        assert isinstance(result["organization_id"], UUID)
        assert result["organization_id"] == org_id

    def test_does_not_include_key_hash(self):
        """Test that key_hash is excluded from the output dict."""
        pool = _make_mock_pool()
        repo = ApiKeyRepository(pool)
        row, _ = _make_mock_row()

        result = repo._row_to_dict(row)

        assert "key_hash" not in result


class TestVerifyEdgeCases:
    """Edge case tests for ApiKeyRepository.verify()."""

    async def test_rejects_non_hs_prefix(self):
        """Test that keys not starting with 'hs_' are rejected immediately."""
        pool = _make_mock_pool()
        repo = ApiKeyRepository(pool)

        result = await repo.verify("sk_some_other_key_format")

        assert result is None
        pool.acquire.assert_not_called()

    async def test_rejects_empty_string(self):
        """Test that an empty string is rejected."""
        pool = _make_mock_pool()
        repo = ApiKeyRepository(pool)

        result = await repo.verify("")

        assert result is None

    async def test_returns_none_when_no_matching_prefix(self):
        """Test that verify returns None when no keys match the prefix."""
        mock_conn = AsyncMock()
        mock_conn.fetch.return_value = []

        pool = _make_mock_pool(mock_conn)

        repo = ApiKeyRepository(pool)
        result = await repo.verify("hs_nonexistent_key_value")

        assert result is None

    async def test_returns_none_on_hash_mismatch(self):
        """Test that verify returns None when hash doesn't match."""
        row, _ = _make_mock_row({"key_hash": "$2b$12$doesnotmatch"})
        mock_conn = AsyncMock()
        mock_conn.fetch.return_value = [row]

        pool = _make_mock_pool(mock_conn)

        repo = ApiKeyRepository(pool)
        with patch("lucent.db.api_key.bcrypt.checkpw", return_value=False):
            result = await repo.verify("hs_test_key_value_here")

        assert result is None

    async def test_returns_none_for_expired_key(self):
        """Test that verify returns None for an expired key."""
        expired_time = datetime.now(timezone.utc) - timedelta(days=1)
        row, _ = _make_mock_row(
            {
                "expires_at": expired_time,
                "user_email": "test@example.com",
                "user_display_name": "Test",
                "user_role": "member",
            }
        )
        mock_conn = AsyncMock()
        mock_conn.fetch.return_value = [row]

        pool = _make_mock_pool(mock_conn)

        repo = ApiKeyRepository(pool)
        with patch("lucent.db.api_key.bcrypt.checkpw", return_value=True):
            result = await repo.verify("hs_test_key_value_here")

        assert result is None

    async def test_extracts_correct_prefix(self):
        """Test that the first 11 characters are used as prefix."""
        mock_conn = AsyncMock()
        mock_conn.fetch.return_value = []

        pool = _make_mock_pool(mock_conn)

        repo = ApiKeyRepository(pool)
        await repo.verify("hs_abcdefgh_rest_of_key")

        # The prefix used in the query should be first 11 chars
        query_args = mock_conn.fetch.call_args
        assert query_args[0][1] == "hs_abcdefgh"


class TestCreateEdgeCases:
    """Edge case tests for ApiKeyRepository.create()."""

    async def test_default_scopes(self):
        """Test that default scopes are ['read', 'write']."""
        row, _ = _make_mock_row()
        mock_conn = AsyncMock()
        mock_conn.fetchrow.return_value = row

        pool = _make_mock_pool(mock_conn)

        repo = ApiKeyRepository(pool)

        # Mock get_by_name to return None (no duplicate)
        repo.get_by_name = AsyncMock(return_value=None)

        with patch("lucent.db.api_key.bcrypt.hashpw", return_value=b"$2b$12$hash"):
            _, plain_key = await repo.create(
                user_id=uuid4(),
                organization_id=None,
                name="test-key",
            )

        assert plain_key.startswith("hs_")
        # Check that scopes were passed as ["read", "write"]
        insert_args = mock_conn.fetchrow.call_args[0]
        assert insert_args[6] == ["read", "write"]

    async def test_custom_scopes(self):
        """Test that custom scopes override defaults."""
        row, _ = _make_mock_row()
        mock_conn = AsyncMock()
        mock_conn.fetchrow.return_value = row

        pool = _make_mock_pool(mock_conn)

        repo = ApiKeyRepository(pool)
        repo.get_by_name = AsyncMock(return_value=None)

        with patch("lucent.db.api_key.bcrypt.hashpw", return_value=b"$2b$12$hash"):
            await repo.create(
                user_id=uuid4(),
                organization_id=None,
                name="test-key",
                scopes=["read"],
            )

        insert_args = mock_conn.fetchrow.call_args[0]
        assert insert_args[6] == ["read"]

    async def test_duplicate_name_raises(self):
        """Test that creating a key with existing name raises ValueError."""
        pool = _make_mock_pool()
        repo = ApiKeyRepository(pool)
        repo.get_by_name = AsyncMock(return_value={"id": uuid4(), "name": "existing"})

        with pytest.raises(ValueError, match="already exists"):
            await repo.create(
                user_id=uuid4(),
                organization_id=None,
                name="existing",
            )

    async def test_plain_key_format(self):
        """Test that generated plain key has hs_ prefix and sufficient length."""
        row, _ = _make_mock_row()
        mock_conn = AsyncMock()
        mock_conn.fetchrow.return_value = row

        pool = _make_mock_pool(mock_conn)

        repo = ApiKeyRepository(pool)
        repo.get_by_name = AsyncMock(return_value=None)

        with patch("lucent.db.api_key.bcrypt.hashpw", return_value=b"$2b$12$hash"):
            _, plain_key = await repo.create(
                user_id=uuid4(),
                organization_id=None,
                name="test-key",
            )

        assert plain_key.startswith("hs_")
        # token_urlsafe(32) produces ~43 chars, plus "hs_" = ~46
        assert len(plain_key) > 40


class TestRevokeEdgeCases:
    """Edge case tests for ApiKeyRepository.revoke()."""

    async def test_revoke_nonexistent_key(self):
        """Test that revoking a nonexistent key returns False."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow.return_value = None

        pool = _make_mock_pool(mock_conn)

        repo = ApiKeyRepository(pool)
        result = await repo.revoke(uuid4(), uuid4())

        assert result is False

    async def test_revoke_wrong_user(self):
        """Test that revoking with wrong user_id returns False."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow.return_value = None

        pool = _make_mock_pool(mock_conn)

        repo = ApiKeyRepository(pool)
        result = await repo.revoke(uuid4(), uuid4())

        assert result is False


class TestGetByName:
    """Tests for ApiKeyRepository.get_by_name()."""

    async def test_returns_none_when_not_found(self):
        """Test that get_by_name returns None when key doesn't exist."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow.return_value = None

        pool = _make_mock_pool(mock_conn)

        repo = ApiKeyRepository(pool)
        result = await repo.get_by_name(uuid4(), "nonexistent")

        assert result is None

    async def test_returns_dict_when_found(self):
        """Test that get_by_name returns a dict when key exists."""
        row, data = _make_mock_row()
        mock_conn = AsyncMock()
        mock_conn.fetchrow.return_value = row

        pool = _make_mock_pool(mock_conn)

        repo = ApiKeyRepository(pool)
        result = await repo.get_by_name(uuid4(), "test-key")

        assert result is not None
        assert result["name"] == "test-key"


class TestListByUser:
    """Tests for ApiKeyRepository.list_by_user()."""

    async def test_returns_empty_list_when_no_keys(self):
        """Test that list_by_user returns empty list for user with no keys."""
        mock_conn = AsyncMock()
        mock_conn.fetch.return_value = []

        pool = _make_mock_pool(mock_conn)

        repo = ApiKeyRepository(pool)
        result = await repo.list_by_user(uuid4())

        assert result == []

    async def test_returns_list_of_dicts(self):
        """Test that list_by_user returns a list of dicts."""
        row1, _ = _make_mock_row({"name": "key1"})
        row2, _ = _make_mock_row({"name": "key2"})
        mock_conn = AsyncMock()
        mock_conn.fetch.return_value = [row1, row2]

        pool = _make_mock_pool(mock_conn)

        repo = ApiKeyRepository(pool)
        result = await repo.list_by_user(uuid4())

        assert len(result) == 2
        names = [r["name"] for r in result]
        assert "key1" in names
        assert "key2" in names


class TestUpdateName:
    """Tests for ApiKeyRepository.update_name()."""

    async def test_returns_none_when_key_not_found(self):
        """Test that update_name returns None for nonexistent key."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow.return_value = None

        pool = _make_mock_pool(mock_conn)

        repo = ApiKeyRepository(pool)
        result = await repo.update_name(uuid4(), uuid4(), "new-name")

        assert result is None
