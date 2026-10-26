"""Unit tests for the RBAC role groups.

``ADMIN_ROLES`` / ``OWNER_ROLES`` exist because ~60 route guards spelled the role
set out as a literal tuple, which silently excluded the instance operator from
every tenant screen. These tests pin the membership so that regression is caught
here rather than in a 403 an operator only discovers by clicking.
"""

from __future__ import annotations

from lucent.rbac import (
    ADMIN_ROLES,
    OWNER_ROLES,
    Role,
    has_permission,
    is_admin_role,
    is_owner_role,
    role_name,
)


class TestRoleGroups:
    def test_admin_group_includes_hyperadmin(self):
        assert "admin" in ADMIN_ROLES
        assert "owner" in ADMIN_ROLES
        assert "hyperadmin" in ADMIN_ROLES

    def test_admin_group_excludes_members_and_daemons(self):
        assert "member" not in ADMIN_ROLES
        assert "daemon" not in ADMIN_ROLES

    def test_owner_group_includes_hyperadmin(self):
        assert OWNER_ROLES == {"owner", "hyperadmin"}

    def test_admin_group_is_subset_of_owner_group_plus_admin(self):
        assert ADMIN_ROLES == OWNER_ROLES | {"admin"}


class TestHelpers:
    def test_accepts_strings_and_enums(self):
        assert is_admin_role("owner")
        assert is_admin_role(Role.HYPERADMIN)
        assert is_owner_role(Role.OWNER)
        assert is_owner_role("hyperadmin")

    def test_rejects_everything_else(self):
        assert not is_admin_role("member")
        assert not is_admin_role("daemon")
        assert not is_admin_role(None)
        assert not is_owner_role("admin")
        assert not is_owner_role("")
        assert not is_owner_role(None)

    def test_role_name_normalizes(self):
        assert role_name(Role.HYPERADMIN) == "hyperadmin"
        assert role_name("admin") == "admin"
        assert role_name(None) == ""


class TestHyperadminPermissions:
    """The role groups must never claim more than the permission matrix grants."""

    def test_hyperadmin_has_every_owner_permission(self):
        from lucent.rbac import ROLE_PERMISSIONS

        assert ROLE_PERMISSIONS[Role.OWNER].issubset(ROLE_PERMISSIONS[Role.HYPERADMIN])

    def test_hyperadmin_only_extra_is_instance_management(self):
        from lucent.rbac import ROLE_PERMISSIONS, Permission

        extra = ROLE_PERMISSIONS[Role.HYPERADMIN] - ROLE_PERMISSIONS[Role.OWNER]
        assert extra == {Permission.ORG_MANAGE_INSTANCE}

    def test_owner_lacks_instance_management(self):
        from lucent.rbac import Permission

        assert not has_permission(Role.OWNER, Permission.ORG_MANAGE_INSTANCE)
        assert has_permission(Role.HYPERADMIN, Permission.ORG_MANAGE_INSTANCE)

    def test_owner_cannot_assign_hyperadmin(self):
        from lucent.rbac import can_assign_role

        assert not can_assign_role(Role.OWNER, Role.HYPERADMIN)
        assert can_assign_role(Role.HYPERADMIN, Role.HYPERADMIN)
