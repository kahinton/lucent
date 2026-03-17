"""Tests for the core auth module (lucent.auth).

Tests cover:
- ContextVar management (current user, API key, impersonation)
- Impersonation state detection
- User ID extraction
- OAuth user get-or-create flow
- Pool initialization error handling
"""

from unittest.mock import patch
from uuid import UUID, uuid4

import pytest

from lucent.auth import (
    _current_api_key_id,
    _current_user,
    _ensure_pool,
    _impersonating_user,
    get_current_api_key_id,
    get_current_user,
    get_current_user_id,
    get_impersonating_user,
    get_or_create_user_from_oauth,
    is_impersonating,
    set_current_api_key_id,
    set_current_user,
    set_impersonating_user,
)

# ============================================================================
# Fixture to reset ContextVars between tests
# ============================================================================


@pytest.fixture(autouse=True)
def reset_context_vars():
    """Reset all auth ContextVars before and after each test."""
    _current_user.set(None)
    _current_api_key_id.set(None)
    _impersonating_user.set(None)
    yield
    _current_user.set(None)
    _current_api_key_id.set(None)
    _impersonating_user.set(None)


# ============================================================================
# Tests: Current User ContextVar
# ============================================================================


class TestCurrentUser:
    """Tests for get/set current user context variable."""

    def test_default_is_none(self):
        assert get_current_user() is None

    def test_set_and_get(self):
        user = {"id": uuid4(), "email": "test@example.com", "role": "member"}
        set_current_user(user)
        assert get_current_user() is user

    def test_set_none_clears(self):
        user = {"id": uuid4(), "email": "test@example.com"}
        set_current_user(user)
        assert get_current_user() is not None
        set_current_user(None)
        assert get_current_user() is None

    def test_overwrite_with_different_user(self):
        user_a = {"id": uuid4(), "email": "a@test.com"}
        user_b = {"id": uuid4(), "email": "b@test.com"}
        set_current_user(user_a)
        assert get_current_user() is user_a
        set_current_user(user_b)
        assert get_current_user() is user_b

    def test_set_preserves_all_fields(self):
        uid = uuid4()
        user = {
            "id": uid,
            "email": "full@test.com",
            "role": "admin",
            "display_name": "Full User",
            "organization_id": uuid4(),
        }
        set_current_user(user)
        result = get_current_user()
        assert result["id"] == uid
        assert result["email"] == "full@test.com"
        assert result["role"] == "admin"
        assert result["display_name"] == "Full User"


# ============================================================================
# Tests: Current API Key ID ContextVar
# ============================================================================


class TestCurrentApiKeyId:
    """Tests for get/set current API key ID context variable."""

    def test_default_is_none(self):
        assert get_current_api_key_id() is None

    def test_set_and_get(self):
        key_id = uuid4()
        set_current_api_key_id(key_id)
        assert get_current_api_key_id() == key_id

    def test_set_none_clears(self):
        key_id = uuid4()
        set_current_api_key_id(key_id)
        assert get_current_api_key_id() is not None
        set_current_api_key_id(None)
        assert get_current_api_key_id() is None

    def test_overwrite_with_different_key(self):
        key_a = uuid4()
        key_b = uuid4()
        set_current_api_key_id(key_a)
        assert get_current_api_key_id() == key_a
        set_current_api_key_id(key_b)
        assert get_current_api_key_id() == key_b


# ============================================================================
# Tests: Impersonation ContextVar
# ============================================================================


class TestImpersonation:
    """Tests for impersonation context management."""

    def test_get_impersonating_user_default_none(self):
        assert get_impersonating_user() is None

    def test_set_and_get_impersonating_user(self):
        admin = {"id": uuid4(), "role": "owner", "email": "admin@test.com"}
        set_impersonating_user(admin)
        assert get_impersonating_user() is admin

    def test_clear_impersonating_user(self):
        admin = {"id": uuid4(), "role": "owner"}
        set_impersonating_user(admin)
        assert get_impersonating_user() is not None
        set_impersonating_user(None)
        assert get_impersonating_user() is None

    def test_is_impersonating_false_by_default(self):
        assert is_impersonating() is False

    def test_is_impersonating_true_when_set(self):
        admin = {"id": uuid4(), "role": "admin"}
        set_impersonating_user(admin)
        assert is_impersonating() is True

    def test_is_impersonating_false_after_clear(self):
        admin = {"id": uuid4(), "role": "admin"}
        set_impersonating_user(admin)
        assert is_impersonating() is True
        set_impersonating_user(None)
        assert is_impersonating() is False


# ============================================================================
# Tests: get_current_user_id
# ============================================================================


class TestGetCurrentUserId:
    """Tests for get_current_user_id helper."""

    def test_returns_none_when_no_user(self):
        assert get_current_user_id() is None

    def test_returns_id_when_user_set(self):
        uid = uuid4()
        set_current_user({"id": uid, "email": "test@test.com"})
        assert get_current_user_id() == uid

    def test_returns_none_after_user_cleared(self):
        uid = uuid4()
        set_current_user({"id": uid, "email": "test@test.com"})
        assert get_current_user_id() == uid
        set_current_user(None)
        assert get_current_user_id() is None


# ============================================================================
# Tests: ContextVar independence
# ============================================================================


class TestContextVarIndependence:
    """Tests that different context vars don't interfere with each other."""

    def test_setting_user_does_not_affect_api_key(self):
        set_current_user({"id": uuid4()})
        assert get_current_api_key_id() is None

    def test_setting_api_key_does_not_affect_user(self):
        set_current_api_key_id(uuid4())
        assert get_current_user() is None

    def test_setting_impersonation_does_not_affect_user(self):
        admin = {"id": uuid4(), "role": "owner"}
        set_impersonating_user(admin)
        assert get_current_user() is None

    def test_setting_user_does_not_affect_impersonation(self):
        user = {"id": uuid4()}
        set_current_user(user)
        assert is_impersonating() is False
        assert get_impersonating_user() is None

    def test_all_three_vars_independent(self):
        user_id = uuid4()
        key_id = uuid4()
        admin_id = uuid4()
        set_current_user({"id": user_id})
        set_current_api_key_id(key_id)
        set_impersonating_user({"id": admin_id, "role": "owner"})

        assert get_current_user()["id"] == user_id
        assert get_current_api_key_id() == key_id
        assert get_impersonating_user()["id"] == admin_id
        assert is_impersonating() is True

    def test_clearing_one_does_not_clear_others(self):
        set_current_user({"id": uuid4()})
        set_current_api_key_id(uuid4())
        set_impersonating_user({"id": uuid4(), "role": "admin"})

        set_current_user(None)
        assert get_current_user() is None
        assert get_current_api_key_id() is not None
        assert get_impersonating_user() is not None


# ============================================================================
# Tests: _ensure_pool
# ============================================================================


class TestEnsurePool:
    """Tests for the _ensure_pool helper."""

    async def test_returns_pool_when_already_initialized(self, db_pool):
        """When pool is already initialized, _ensure_pool returns it."""
        pool = await _ensure_pool()
        assert pool is not None

    async def test_raises_without_database_url(self, monkeypatch):
        """When pool is not initialized and DATABASE_URL not set, raises RuntimeError."""
        # Patch get_pool to raise (simulating uninitialized pool)
        # and remove DATABASE_URL
        monkeypatch.delenv("DATABASE_URL", raising=False)

        async def fake_get_pool():
            raise RuntimeError("not initialized")

        with patch("lucent.auth.get_pool", side_effect=RuntimeError("not initialized")):
            with pytest.raises(RuntimeError, match="DATABASE_URL"):
                await _ensure_pool()


# ============================================================================
# Tests: get_or_create_user_from_oauth
# ============================================================================


class TestGetOrCreateUserFromOAuth:
    """Tests for OAuth user get-or-create flow."""

    async def test_creates_new_user(self, db_pool, test_organization, clean_test_data):
        """First OAuth login creates a new user record."""
        prefix = clean_test_data
        external_id = f"{prefix}oauth_new"
        org_id = test_organization["id"]

        user = await get_or_create_user_from_oauth(
            provider="github",
            external_id=external_id,
            organization_id=org_id,
            email=f"{prefix}oauth@github.com",
            display_name=f"{prefix}OAuth User",
            avatar_url="https://example.com/avatar.png",
            provider_metadata={"github_username": "testuser"},
        )

        assert user is not None
        assert isinstance(user["id"], UUID)
        assert user["email"] == f"{prefix}oauth@github.com"
        assert user["display_name"] == f"{prefix}OAuth User"

    async def test_returns_existing_user(self, db_pool, test_organization, clean_test_data):
        """Second OAuth login returns the existing user."""
        prefix = clean_test_data
        external_id = f"{prefix}oauth_existing"
        org_id = test_organization["id"]

        # First call creates
        user1 = await get_or_create_user_from_oauth(
            provider="github",
            external_id=external_id,
            organization_id=org_id,
            email=f"{prefix}existing@github.com",
            display_name=f"{prefix}Existing",
        )

        # Second call returns existing
        user2 = await get_or_create_user_from_oauth(
            provider="github",
            external_id=external_id,
            organization_id=org_id,
            email=f"{prefix}existing@github.com",
            display_name=f"{prefix}Existing",
        )

        assert user1["id"] == user2["id"]

    async def test_updates_user_info_on_login(self, db_pool, test_organization, clean_test_data):
        """Subsequent OAuth logins update user info from provider."""
        prefix = clean_test_data
        external_id = f"{prefix}oauth_update"
        org_id = test_organization["id"]

        # First login
        await get_or_create_user_from_oauth(
            provider="google",
            external_id=external_id,
            organization_id=org_id,
            email=f"{prefix}old@google.com",
            display_name=f"{prefix}Old Name",
        )

        # Second login with updated info
        user = await get_or_create_user_from_oauth(
            provider="google",
            external_id=external_id,
            organization_id=org_id,
            email=f"{prefix}new@google.com",
            display_name=f"{prefix}New Name",
        )

        assert user["email"] == f"{prefix}new@google.com"
        assert user["display_name"] == f"{prefix}New Name"

    async def test_creates_user_with_minimal_info(
        self, db_pool, test_organization, clean_test_data
    ):
        """OAuth login works with just provider, external_id, and org."""
        prefix = clean_test_data
        external_id = f"{prefix}oauth_minimal"
        org_id = test_organization["id"]

        user = await get_or_create_user_from_oauth(
            provider="github",
            external_id=external_id,
            organization_id=org_id,
        )

        assert user is not None
        assert isinstance(user["id"], UUID)

    async def test_different_providers_create_different_users(
        self, db_pool, test_organization, clean_test_data
    ):
        """Same external_id with different providers creates separate users."""
        prefix = clean_test_data
        org_id = test_organization["id"]

        user_github = await get_or_create_user_from_oauth(
            provider="github",
            external_id=f"{prefix}multi_provider",
            organization_id=org_id,
            email=f"{prefix}multi_gh@test.com",
        )

        user_google = await get_or_create_user_from_oauth(
            provider="google",
            external_id=f"{prefix}multi_provider",
            organization_id=org_id,
            email=f"{prefix}multi_gg@test.com",
        )

        assert user_github["id"] != user_google["id"]

    async def test_provider_metadata_stored(self, db_pool, test_organization, clean_test_data):
        """Provider metadata is stored on the user record."""
        prefix = clean_test_data
        external_id = f"{prefix}oauth_meta"
        org_id = test_organization["id"]
        metadata = {"github_username": "octocat", "scopes": ["repo", "user"]}

        user = await get_or_create_user_from_oauth(
            provider="github",
            external_id=external_id,
            organization_id=org_id,
            provider_metadata=metadata,
        )

        # Verify by reading the user back
        from lucent.db import UserRepository

        repo = UserRepository(db_pool)
        stored = await repo.get_by_id(user["id"])
        assert stored is not None
        assert stored.get("provider_metadata") == metadata

    async def test_avatar_url_stored(self, db_pool, test_organization, clean_test_data):
        """Avatar URL is stored on the user record."""
        prefix = clean_test_data
        external_id = f"{prefix}oauth_avatar"
        org_id = test_organization["id"]
        avatar = "https://avatars.githubusercontent.com/u/12345"

        user = await get_or_create_user_from_oauth(
            provider="github",
            external_id=external_id,
            organization_id=org_id,
            avatar_url=avatar,
        )

        from lucent.db import UserRepository

        repo = UserRepository(db_pool)
        stored = await repo.get_by_id(user["id"])
        assert stored is not None
        assert stored.get("avatar_url") == avatar
