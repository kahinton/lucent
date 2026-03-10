"""Tests for RBAC (role-based access control) module."""

from uuid import uuid4

import pytest

from lucent.rbac import (
    Permission,
    RBACPermissionError,
    Role,
    can_assign_role,
    can_manage_user,
    check_memory_access,
    get_user_permissions,
    has_permission,
    require_permission,
)


class TestRole:
    """Tests for Role enum and comparisons."""

    def test_from_string_valid(self):
        assert Role.from_string("member") == Role.MEMBER
        assert Role.from_string("admin") == Role.ADMIN
        assert Role.from_string("owner") == Role.OWNER

    def test_from_string_case_insensitive(self):
        assert Role.from_string("ADMIN") == Role.ADMIN
        assert Role.from_string("Owner") == Role.OWNER

    def test_from_string_invalid_defaults_to_member(self):
        assert Role.from_string("superuser") == Role.MEMBER
        assert Role.from_string("") == Role.MEMBER

    def test_hierarchy_ge(self):
        assert Role.OWNER >= Role.OWNER
        assert Role.OWNER >= Role.ADMIN
        assert Role.OWNER >= Role.MEMBER
        assert Role.ADMIN >= Role.ADMIN
        assert Role.ADMIN >= Role.MEMBER
        assert Role.MEMBER >= Role.MEMBER
        assert not (Role.MEMBER >= Role.ADMIN)

    def test_hierarchy_gt(self):
        assert Role.OWNER > Role.ADMIN
        assert Role.OWNER > Role.MEMBER
        assert Role.ADMIN > Role.MEMBER
        assert not (Role.ADMIN > Role.ADMIN)
        assert not (Role.MEMBER > Role.ADMIN)

    def test_hierarchy_le(self):
        assert Role.MEMBER <= Role.MEMBER
        assert Role.MEMBER <= Role.ADMIN
        assert Role.MEMBER <= Role.OWNER
        assert not (Role.OWNER <= Role.ADMIN)

    def test_hierarchy_lt(self):
        assert Role.MEMBER < Role.ADMIN
        assert Role.MEMBER < Role.OWNER
        assert Role.ADMIN < Role.OWNER
        assert not (Role.ADMIN < Role.ADMIN)

    def test_cross_type_comparison_returns_not_implemented(self):
        # Should not crash — returns NotImplemented, Python falls back to str comparison
        result = Role.ADMIN >= "not_a_role"
        assert isinstance(result, bool)  # Python resolves it via str fallback


class TestPermissions:
    """Tests for permission checks."""

    def test_member_has_basic_permissions(self):
        assert has_permission(Role.MEMBER, Permission.MEMORY_CREATE)
        assert has_permission(Role.MEMBER, Permission.MEMORY_READ_OWN)
        assert has_permission(Role.MEMBER, Permission.MEMORY_UPDATE_OWN)
        assert has_permission(Role.MEMBER, Permission.MEMORY_DELETE_OWN)

    def test_member_lacks_admin_permissions(self):
        assert not has_permission(Role.MEMBER, Permission.MEMORY_READ_ALL)
        assert not has_permission(Role.MEMBER, Permission.MEMORY_DELETE_ANY)
        assert not has_permission(Role.MEMBER, Permission.USERS_MANAGE)
        assert not has_permission(Role.MEMBER, Permission.AUDIT_VIEW_ORG)

    def test_admin_has_management_permissions(self):
        assert has_permission(Role.ADMIN, Permission.MEMORY_READ_ALL)
        assert has_permission(Role.ADMIN, Permission.USERS_MANAGE)
        assert has_permission(Role.ADMIN, Permission.AUDIT_VIEW_ORG)
        assert has_permission(Role.ADMIN, Permission.ACCESS_VIEW_ORG)

    def test_admin_lacks_owner_permissions(self):
        assert not has_permission(Role.ADMIN, Permission.ORG_DELETE)
        assert not has_permission(Role.ADMIN, Permission.ORG_TRANSFER)

    def test_owner_has_all_permissions(self):
        assert has_permission(Role.OWNER, Permission.ORG_DELETE)
        assert has_permission(Role.OWNER, Permission.ORG_TRANSFER)
        assert has_permission(Role.OWNER, Permission.MEMORY_DELETE_ANY)
        assert has_permission(Role.OWNER, Permission.USERS_MANAGE)

    def test_string_role_input(self):
        assert has_permission("admin", Permission.AUDIT_VIEW_ORG)
        assert not has_permission("member", Permission.AUDIT_VIEW_ORG)

    def test_owner_permissions_superset_of_admin(self):
        admin_perms = get_user_permissions(Role.ADMIN)
        owner_perms = get_user_permissions(Role.OWNER)
        assert admin_perms.issubset(owner_perms)

    def test_admin_permissions_superset_of_member(self):
        member_perms = get_user_permissions(Role.MEMBER)
        admin_perms = get_user_permissions(Role.ADMIN)
        assert member_perms.issubset(admin_perms)

    def test_get_user_permissions_returns_copy(self):
        perms1 = get_user_permissions(Role.MEMBER)
        perms2 = get_user_permissions(Role.MEMBER)
        perms1.add(Permission.ORG_DELETE)
        assert Permission.ORG_DELETE not in perms2


class TestCanManageUser:
    """Tests for can_manage_user."""

    def test_owner_can_manage_everyone(self):
        assert can_manage_user("owner", "member")
        assert can_manage_user("owner", "admin")

    def test_admin_can_manage_members(self):
        assert can_manage_user("admin", "member")

    def test_admin_cannot_manage_admin_or_owner(self):
        assert not can_manage_user("admin", "admin")
        assert not can_manage_user("admin", "owner")

    def test_member_cannot_manage_anyone(self):
        assert not can_manage_user("member", "member")
        assert not can_manage_user("member", "admin")
        assert not can_manage_user("member", "owner")


class TestCanAssignRole:
    """Tests for can_assign_role."""

    def test_owner_can_assign_any_role(self):
        assert can_assign_role("owner", "member")
        assert can_assign_role("owner", "admin")
        assert can_assign_role("owner", "owner")

    def test_admin_can_assign_member_only(self):
        assert can_assign_role("admin", "member")
        assert not can_assign_role("admin", "admin")
        assert not can_assign_role("admin", "owner")

    def test_member_cannot_assign_any_role(self):
        assert not can_assign_role("member", "member")


class TestRequirePermission:
    """Tests for require_permission."""

    def test_does_not_raise_when_allowed(self):
        require_permission(Role.ADMIN, Permission.AUDIT_VIEW_ORG)

    def test_raises_when_denied(self):
        with pytest.raises(RBACPermissionError) as exc_info:
            require_permission(Role.MEMBER, Permission.AUDIT_VIEW_ORG)
        assert "member" in str(exc_info.value).lower()
        assert "audit" in str(exc_info.value).lower()


class TestCheckMemoryAccess:
    """Tests for check_memory_access."""

    def test_owner_of_memory_has_access(self):
        user_id = uuid4()
        assert check_memory_access(
            user_id=user_id,
            user_role=Role.MEMBER,
            user_org_id=uuid4(),
            memory_owner_id=user_id,
            memory_org_id=uuid4(),
            memory_shared=False,
        )

    def test_shared_memory_same_org(self):
        org_id = uuid4()
        assert check_memory_access(
            user_id=uuid4(),
            user_role=Role.MEMBER,
            user_org_id=org_id,
            memory_owner_id=uuid4(),
            memory_org_id=org_id,
            memory_shared=True,
        )

    def test_shared_memory_different_org_denied(self):
        assert not check_memory_access(
            user_id=uuid4(),
            user_role=Role.MEMBER,
            user_org_id=uuid4(),
            memory_owner_id=uuid4(),
            memory_org_id=uuid4(),
            memory_shared=True,
        )

    def test_unshared_memory_same_org_member_denied(self):
        org_id = uuid4()
        assert not check_memory_access(
            user_id=uuid4(),
            user_role=Role.MEMBER,
            user_org_id=org_id,
            memory_owner_id=uuid4(),
            memory_org_id=org_id,
            memory_shared=False,
        )

    def test_unshared_memory_same_org_admin_has_access(self):
        org_id = uuid4()
        assert check_memory_access(
            user_id=uuid4(),
            user_role=Role.ADMIN,
            user_org_id=org_id,
            memory_owner_id=uuid4(),
            memory_org_id=org_id,
            memory_shared=False,
        )
