"""Tests for API authentication and authorization dependencies.

Tests the CurrentUser class, scope/permission checking, impersonation,
audit context, and dependency factory functions.
"""

from uuid import uuid4

import pytest
from fastapi import HTTPException

from lucent.api.deps import CurrentUser
from lucent.rbac import Permission, Role

# =============================================================================
# CurrentUser Construction
# =============================================================================


class TestCurrentUserConstruction:
    """Tests for creating CurrentUser instances."""

    def test_basic_construction(self):
        uid = uuid4()
        org_id = uuid4()
        user = CurrentUser(
            id=uid,
            organization_id=org_id,
            role="member",
            email="test@example.com",
            display_name="Test User",
        )
        assert user.id == uid
        assert user.organization_id == org_id
        assert user.role == Role.MEMBER
        assert user.email == "test@example.com"
        assert user.display_name == "Test User"

    def test_role_string_converted_to_enum(self):
        user = CurrentUser(
            id=uuid4(), organization_id=None, role="admin", email=None, display_name=None
        )
        assert user.role == Role.ADMIN
        assert isinstance(user.role, Role)

    def test_owner_role(self):
        user = CurrentUser(
            id=uuid4(), organization_id=None, role="owner", email=None, display_name=None
        )
        assert user.role == Role.OWNER

    def test_invalid_role_defaults_to_member(self):
        user = CurrentUser(
            id=uuid4(), organization_id=None, role="superadmin", email=None, display_name=None
        )
        assert user.role == Role.MEMBER

    def test_defaults(self):
        user = CurrentUser(
            id=uuid4(), organization_id=None, role="member", email=None, display_name=None
        )
        assert user.auth_method == "session"
        assert user.api_key_id is None
        assert user.api_key_scopes == ["read", "write"]
        assert user.impersonator_id is None
        assert user.impersonator_display_name is None

    def test_api_key_auth_method(self):
        key_id = uuid4()
        user = CurrentUser(
            id=uuid4(),
            organization_id=None,
            role="member",
            email=None,
            display_name=None,
            auth_method="api_key",
            api_key_id=key_id,
            api_key_scopes=["read"],
        )
        assert user.auth_method == "api_key"
        assert user.api_key_id == key_id
        assert user.api_key_scopes == ["read"]

    def test_none_organization_id(self):
        user = CurrentUser(
            id=uuid4(), organization_id=None, role="member", email=None, display_name=None
        )
        assert user.organization_id is None


# =============================================================================
# CurrentUser.has_permission
# =============================================================================


class TestCurrentUserHasPermission:
    """Tests for permission checking on CurrentUser."""

    def test_member_can_create_memory(self):
        user = CurrentUser(
            id=uuid4(), organization_id=None, role="member", email=None, display_name=None
        )
        assert user.has_permission(Permission.MEMORY_CREATE) is True

    def test_member_cannot_read_all(self):
        user = CurrentUser(
            id=uuid4(), organization_id=None, role="member", email=None, display_name=None
        )
        assert user.has_permission(Permission.MEMORY_READ_ALL) is False

    def test_admin_can_read_all(self):
        user = CurrentUser(
            id=uuid4(), organization_id=None, role="admin", email=None, display_name=None
        )
        assert user.has_permission(Permission.MEMORY_READ_ALL) is True

    def test_admin_cannot_delete_org(self):
        user = CurrentUser(
            id=uuid4(), organization_id=None, role="admin", email=None, display_name=None
        )
        assert user.has_permission(Permission.ORG_DELETE) is False

    def test_owner_can_delete_org(self):
        user = CurrentUser(
            id=uuid4(), organization_id=None, role="owner", email=None, display_name=None
        )
        assert user.has_permission(Permission.ORG_DELETE) is True

    def test_owner_can_delete_any_memory(self):
        user = CurrentUser(
            id=uuid4(), organization_id=None, role="owner", email=None, display_name=None
        )
        assert user.has_permission(Permission.MEMORY_DELETE_ANY) is True

    def test_member_cannot_delete_any_memory(self):
        user = CurrentUser(
            id=uuid4(), organization_id=None, role="member", email=None, display_name=None
        )
        assert user.has_permission(Permission.MEMORY_DELETE_ANY) is False

    def test_member_can_view_own_audit(self):
        user = CurrentUser(
            id=uuid4(), organization_id=None, role="member", email=None, display_name=None
        )
        assert user.has_permission(Permission.AUDIT_VIEW_OWN) is True

    def test_member_cannot_view_org_audit(self):
        user = CurrentUser(
            id=uuid4(), organization_id=None, role="member", email=None, display_name=None
        )
        assert user.has_permission(Permission.AUDIT_VIEW_ORG) is False

    def test_admin_can_manage_users(self):
        user = CurrentUser(
            id=uuid4(), organization_id=None, role="admin", email=None, display_name=None
        )
        assert user.has_permission(Permission.USERS_MANAGE) is True


# =============================================================================
# CurrentUser.require_permission
# =============================================================================


class TestCurrentUserRequirePermission:
    """Tests for require_permission raising HTTPException."""

    def test_no_exception_when_allowed(self):
        user = CurrentUser(
            id=uuid4(), organization_id=None, role="admin", email=None, display_name=None
        )
        # Should not raise
        user.require_permission(Permission.MEMORY_READ_ALL)

    def test_raises_403_when_denied(self):
        user = CurrentUser(
            id=uuid4(), organization_id=None, role="member", email=None, display_name=None
        )
        with pytest.raises(HTTPException) as exc_info:
            user.require_permission(Permission.MEMORY_READ_ALL)
        assert exc_info.value.status_code == 403
        assert "memory.read.all" in exc_info.value.detail

    def test_member_denied_users_manage(self):
        user = CurrentUser(
            id=uuid4(), organization_id=None, role="member", email=None, display_name=None
        )
        with pytest.raises(HTTPException) as exc_info:
            user.require_permission(Permission.USERS_MANAGE)
        assert exc_info.value.status_code == 403

    def test_owner_allowed_org_transfer(self):
        user = CurrentUser(
            id=uuid4(), organization_id=None, role="owner", email=None, display_name=None
        )
        user.require_permission(Permission.ORG_TRANSFER)  # Should not raise


# =============================================================================
# CurrentUser.has_scope
# =============================================================================


class TestCurrentUserHasScope:
    """Tests for API key scope checking."""

    def test_default_scopes_grant_full_access(self):
        """Default read+write scopes should grant access to everything."""
        user = CurrentUser(
            id=uuid4(), organization_id=None, role="member", email=None, display_name=None
        )
        assert user.has_scope("daemon-tasks") is True
        assert user.has_scope("anything") is True
        assert user.has_scope("read") is True
        assert user.has_scope("write") is True

    def test_explicit_read_write_grants_full_access(self):
        user = CurrentUser(
            id=uuid4(),
            organization_id=None,
            role="member",
            email=None,
            display_name=None,
            api_key_scopes=["read", "write"],
        )
        assert user.has_scope("daemon-tasks") is True
        assert user.has_scope("custom-scope") is True

    def test_scoped_key_only_matches_exact_scope(self):
        user = CurrentUser(
            id=uuid4(),
            organization_id=None,
            role="member",
            email=None,
            display_name=None,
            api_key_scopes=["daemon-tasks"],
        )
        assert user.has_scope("daemon-tasks") is True
        assert user.has_scope("read") is False
        assert user.has_scope("write") is False
        assert user.has_scope("other") is False

    def test_multiple_limited_scopes(self):
        user = CurrentUser(
            id=uuid4(),
            organization_id=None,
            role="member",
            email=None,
            display_name=None,
            api_key_scopes=["daemon-tasks", "export"],
        )
        assert user.has_scope("daemon-tasks") is True
        assert user.has_scope("export") is True
        assert user.has_scope("import") is False

    def test_read_only_scope(self):
        """A key with only 'read' (no 'write') should NOT get full access."""
        user = CurrentUser(
            id=uuid4(),
            organization_id=None,
            role="member",
            email=None,
            display_name=None,
            api_key_scopes=["read"],
        )
        assert user.has_scope("read") is True
        assert user.has_scope("write") is False
        assert user.has_scope("daemon-tasks") is False

    def test_write_only_scope(self):
        user = CurrentUser(
            id=uuid4(),
            organization_id=None,
            role="member",
            email=None,
            display_name=None,
            api_key_scopes=["write"],
        )
        assert user.has_scope("write") is True
        assert user.has_scope("read") is False
        assert user.has_scope("daemon-tasks") is False

    def test_empty_scopes_default_to_full_access(self):
        """Empty list is falsy, so it defaults to ['read', 'write'] (full access)."""
        user = CurrentUser(
            id=uuid4(),
            organization_id=None,
            role="member",
            email=None,
            display_name=None,
            api_key_scopes=[],
        )
        # Empty list triggers `or` default to ["read", "write"]
        assert user.api_key_scopes == ["read", "write"]
        assert user.has_scope("anything") is True


# =============================================================================
# CurrentUser.require_scope
# =============================================================================


class TestCurrentUserRequireScope:
    """Tests for require_scope raising HTTPException."""

    def test_no_exception_when_scope_present(self):
        user = CurrentUser(
            id=uuid4(),
            organization_id=None,
            role="member",
            email=None,
            display_name=None,
            api_key_scopes=["daemon-tasks"],
        )
        user.require_scope("daemon-tasks")  # Should not raise

    def test_no_exception_with_full_access(self):
        user = CurrentUser(
            id=uuid4(), organization_id=None, role="member", email=None, display_name=None
        )
        user.require_scope("daemon-tasks")  # Default read+write = full access

    def test_raises_403_when_scope_missing(self):
        user = CurrentUser(
            id=uuid4(),
            organization_id=None,
            role="member",
            email=None,
            display_name=None,
            api_key_scopes=["read"],
        )
        with pytest.raises(HTTPException) as exc_info:
            user.require_scope("daemon-tasks")
        assert exc_info.value.status_code == 403
        assert "daemon-tasks" in exc_info.value.detail

    def test_empty_scopes_default_to_full_access(self):
        """Empty list is falsy, so it defaults to ['read', 'write'] (full access)."""
        user = CurrentUser(
            id=uuid4(),
            organization_id=None,
            role="member",
            email=None,
            display_name=None,
            api_key_scopes=[],
        )
        # Should not raise - empty list defaults to full access
        user.require_scope("read")
        user.require_scope("anything")


# =============================================================================
# CurrentUser.is_impersonated
# =============================================================================


class TestCurrentUserImpersonation:
    """Tests for impersonation detection."""

    def test_not_impersonated_by_default(self):
        user = CurrentUser(
            id=uuid4(), organization_id=None, role="member", email=None, display_name=None
        )
        assert user.is_impersonated is False

    def test_impersonated_when_impersonator_set(self):
        user = CurrentUser(
            id=uuid4(),
            organization_id=None,
            role="member",
            email=None,
            display_name=None,
            impersonator_id=uuid4(),
            impersonator_display_name="Admin User",
        )
        assert user.is_impersonated is True

    def test_impersonator_id_preserved(self):
        imp_id = uuid4()
        user = CurrentUser(
            id=uuid4(),
            organization_id=None,
            role="member",
            email=None,
            display_name=None,
            impersonator_id=imp_id,
            impersonator_display_name="Admin",
        )
        assert user.impersonator_id == imp_id
        assert user.impersonator_display_name == "Admin"


# =============================================================================
# CurrentUser.get_audit_context
# =============================================================================


class TestCurrentUserAuditContext:
    """Tests for audit context generation."""

    def test_basic_session_context(self):
        user = CurrentUser(
            id=uuid4(), organization_id=None, role="member", email=None, display_name=None
        )
        ctx = user.get_audit_context()
        assert ctx["auth_method"] == "session"
        assert "api_key_id" not in ctx
        assert "impersonator_id" not in ctx

    def test_api_key_context(self):
        key_id = uuid4()
        user = CurrentUser(
            id=uuid4(),
            organization_id=None,
            role="member",
            email=None,
            display_name=None,
            auth_method="api_key",
            api_key_id=key_id,
            api_key_scopes=["daemon-tasks"],
        )
        ctx = user.get_audit_context()
        assert ctx["auth_method"] == "api_key"
        assert ctx["api_key_id"] == str(key_id)
        assert ctx["api_key_scopes"] == ["daemon-tasks"]

    def test_impersonation_context(self):
        imp_id = uuid4()
        user = CurrentUser(
            id=uuid4(),
            organization_id=None,
            role="member",
            email=None,
            display_name=None,
            impersonator_id=imp_id,
            impersonator_display_name="Admin User",
        )
        ctx = user.get_audit_context()
        assert ctx["impersonator_id"] == str(imp_id)
        assert ctx["impersonator_display_name"] == "Admin User"
        assert ctx["is_impersonated"] is True

    def test_api_key_plus_impersonation_context(self):
        key_id = uuid4()
        imp_id = uuid4()
        user = CurrentUser(
            id=uuid4(),
            organization_id=None,
            role="admin",
            email=None,
            display_name=None,
            auth_method="api_key",
            api_key_id=key_id,
            api_key_scopes=["read", "write"],
            impersonator_id=imp_id,
            impersonator_display_name="Owner",
        )
        ctx = user.get_audit_context()
        assert ctx["auth_method"] == "api_key"
        assert ctx["api_key_id"] == str(key_id)
        assert ctx["impersonator_id"] == str(imp_id)
        assert ctx["is_impersonated"] is True

    def test_no_api_key_id_means_no_key_in_context(self):
        """If api_key_id is None, no key info should appear in context."""
        user = CurrentUser(
            id=uuid4(),
            organization_id=None,
            role="member",
            email=None,
            display_name=None,
            auth_method="session",
        )
        ctx = user.get_audit_context()
        assert "api_key_id" not in ctx
        assert "api_key_scopes" not in ctx

    def test_oauth_auth_method(self):
        user = CurrentUser(
            id=uuid4(),
            organization_id=None,
            role="member",
            email=None,
            display_name=None,
            auth_method="oauth",
        )
        ctx = user.get_audit_context()
        assert ctx["auth_method"] == "oauth"


# =============================================================================
# Scope/Permission Interaction
# =============================================================================


class TestScopePermissionInteraction:
    """Tests that scope and permission are independent checks."""

    def test_high_role_low_scope(self):
        """An owner with restricted scopes still has owner permissions but limited scope."""
        user = CurrentUser(
            id=uuid4(),
            organization_id=None,
            role="owner",
            email=None,
            display_name=None,
            api_key_scopes=["read"],
        )
        # Permission check (role-based) passes
        assert user.has_permission(Permission.ORG_DELETE) is True
        # Scope check (key-based) fails for non-read scopes
        assert user.has_scope("write") is False
        assert user.has_scope("daemon-tasks") is False

    def test_low_role_full_scope(self):
        """A member with full scopes can't bypass role permissions."""
        user = CurrentUser(
            id=uuid4(),
            organization_id=None,
            role="member",
            email=None,
            display_name=None,
            api_key_scopes=["read", "write"],
        )
        # Full scope access
        assert user.has_scope("anything") is True
        # But role restricts permissions
        assert user.has_permission(Permission.MEMORY_DELETE_ANY) is False
        assert user.has_permission(Permission.USERS_MANAGE) is False


# =============================================================================
# Edge Cases
# =============================================================================


class TestCurrentUserEdgeCases:
    """Edge case tests for CurrentUser."""

    def test_all_none_optional_fields(self):
        user = CurrentUser(
            id=uuid4(),
            organization_id=None,
            role="member",
            email=None,
            display_name=None,
        )
        assert user.email is None
        assert user.display_name is None
        assert user.organization_id is None

    def test_different_user_ids_are_distinct(self):
        id1 = uuid4()
        id2 = uuid4()
        u1 = CurrentUser(id=id1, organization_id=None, role="member", email=None, display_name=None)
        u2 = CurrentUser(id=id2, organization_id=None, role="member", email=None, display_name=None)
        assert u1.id != u2.id

    def test_role_case_insensitive(self):
        """Role.from_string lowercases, so 'Admin' should work."""
        user = CurrentUser(
            id=uuid4(), organization_id=None, role="Admin", email=None, display_name=None
        )
        assert user.role == Role.ADMIN

    def test_role_owner_uppercase(self):
        user = CurrentUser(
            id=uuid4(), organization_id=None, role="OWNER", email=None, display_name=None
        )
        assert user.role == Role.OWNER

    def test_empty_string_role_defaults_to_member(self):
        """Empty string should default to member via Role.from_string."""
        user = CurrentUser(id=uuid4(), organization_id=None, role="", email=None, display_name=None)
        assert user.role == Role.MEMBER

    def test_api_key_scopes_none_defaults_to_read_write(self):
        """Passing None for api_key_scopes should default to ['read', 'write']."""
        user = CurrentUser(
            id=uuid4(),
            organization_id=None,
            role="member",
            email=None,
            display_name=None,
            api_key_scopes=None,
        )
        assert user.api_key_scopes == ["read", "write"]
        assert user.has_scope("anything") is True
