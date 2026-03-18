"""Role-based access control (RBAC) utilities.

Provides permission checking for the Lucent memory system.

Roles (hierarchical):
- member: Standard user, can manage own memories
- admin: Can manage org users, view org-wide audit/access logs
- owner: Full control including org settings and deletion

Permission checking is designed to be:
- Simple: 3 roles cover most use cases
- Safe: Default deny, explicit allow
- Fast: Role hierarchy means simple comparisons
"""

from enum import Enum
from uuid import UUID


class Role(str, Enum):
    """User roles in order of increasing privilege."""

    MEMBER = "member"
    ADMIN = "admin"
    OWNER = "owner"

    @classmethod
    def from_string(cls, value: str) -> "Role":
        """Convert string to Role, defaulting to MEMBER for invalid values."""
        try:
            return cls(value.lower())
        except ValueError:
            return cls.MEMBER

    def __ge__(self, other: "Role") -> bool:
        """Check if this role has >= privileges than another."""
        if not isinstance(other, Role):
            return NotImplemented
        order = {Role.MEMBER: 0, Role.ADMIN: 1, Role.OWNER: 2}
        return order[self] >= order[other]

    def __gt__(self, other: "Role") -> bool:
        """Check if this role has > privileges than another."""
        if not isinstance(other, Role):
            return NotImplemented
        order = {Role.MEMBER: 0, Role.ADMIN: 1, Role.OWNER: 2}
        return order[self] > order[other]

    def __le__(self, other: "Role") -> bool:
        """Check if this role has <= privileges than another."""
        if not isinstance(other, Role):
            return NotImplemented
        order = {Role.MEMBER: 0, Role.ADMIN: 1, Role.OWNER: 2}
        return order[self] <= order[other]

    def __lt__(self, other: "Role") -> bool:
        """Check if this role has < privileges than another."""
        if not isinstance(other, Role):
            return NotImplemented
        order = {Role.MEMBER: 0, Role.ADMIN: 1, Role.OWNER: 2}
        return order[self] < order[other]


class Permission(str, Enum):
    """Available permissions in the system."""

    # Memory permissions
    MEMORY_CREATE = "memory.create"
    MEMORY_READ_OWN = "memory.read.own"
    MEMORY_READ_SHARED = "memory.read.shared"
    MEMORY_READ_ALL = "memory.read.all"  # Admin override
    MEMORY_UPDATE_OWN = "memory.update.own"
    MEMORY_DELETE_OWN = "memory.delete.own"
    MEMORY_DELETE_ANY = "memory.delete.any"  # Owner only
    MEMORY_SHARE = "memory.share"

    # Audit & analytics permissions
    AUDIT_VIEW_OWN = "audit.view.own"
    AUDIT_VIEW_ORG = "audit.view.org"
    ACCESS_VIEW_OWN = "access.view.own"
    ACCESS_VIEW_ORG = "access.view.org"

    # User management permissions
    USERS_VIEW = "users.view"
    USERS_INVITE = "users.invite"
    USERS_MANAGE = "users.manage"  # Change roles, remove users

    # Organization permissions
    ORG_VIEW = "org.view"
    ORG_UPDATE = "org.update"
    ORG_DELETE = "org.delete"
    ORG_TRANSFER = "org.transfer"

    # Integration permissions
    MANAGE_INTEGRATIONS = "integrations.manage"


# Permission matrix: which roles have which permissions
ROLE_PERMISSIONS: dict[Role, set[Permission]] = {
    Role.MEMBER: {
        # Memory
        Permission.MEMORY_CREATE,
        Permission.MEMORY_READ_OWN,
        Permission.MEMORY_READ_SHARED,
        Permission.MEMORY_UPDATE_OWN,
        Permission.MEMORY_DELETE_OWN,
        Permission.MEMORY_SHARE,
        # Audit (own only)
        Permission.AUDIT_VIEW_OWN,
        Permission.ACCESS_VIEW_OWN,
        # Users (view only)
        Permission.USERS_VIEW,
        # Org (view only)
        Permission.ORG_VIEW,
    },
    Role.ADMIN: {
        # All member permissions
        Permission.MEMORY_CREATE,
        Permission.MEMORY_READ_OWN,
        Permission.MEMORY_READ_SHARED,
        Permission.MEMORY_READ_ALL,  # Admin can see all org memories
        Permission.MEMORY_UPDATE_OWN,
        Permission.MEMORY_DELETE_OWN,
        Permission.MEMORY_SHARE,
        # Audit (org-wide)
        Permission.AUDIT_VIEW_OWN,
        Permission.AUDIT_VIEW_ORG,
        Permission.ACCESS_VIEW_OWN,
        Permission.ACCESS_VIEW_ORG,
        # Users (can manage)
        Permission.USERS_VIEW,
        Permission.USERS_INVITE,
        Permission.USERS_MANAGE,
        # Org (view only)
        Permission.ORG_VIEW,
        # Integrations
        Permission.MANAGE_INTEGRATIONS,
    },
    Role.OWNER: {
        # All admin permissions plus...
        Permission.MEMORY_CREATE,
        Permission.MEMORY_READ_OWN,
        Permission.MEMORY_READ_SHARED,
        Permission.MEMORY_READ_ALL,
        Permission.MEMORY_UPDATE_OWN,
        Permission.MEMORY_DELETE_OWN,
        Permission.MEMORY_DELETE_ANY,  # Owner can delete any memory
        Permission.MEMORY_SHARE,
        # Audit
        Permission.AUDIT_VIEW_OWN,
        Permission.AUDIT_VIEW_ORG,
        Permission.ACCESS_VIEW_OWN,
        Permission.ACCESS_VIEW_ORG,
        # Users
        Permission.USERS_VIEW,
        Permission.USERS_INVITE,
        Permission.USERS_MANAGE,
        # Org (full control)
        Permission.ORG_VIEW,
        Permission.ORG_UPDATE,
        Permission.ORG_DELETE,
        Permission.ORG_TRANSFER,
        # Integrations
        Permission.MANAGE_INTEGRATIONS,
    },
}


def has_permission(role: Role | str, permission: Permission) -> bool:
    """Check if a role has a specific permission.

    Args:
        role: The user's role (Role enum or string).
        permission: The permission to check.

    Returns:
        True if the role has the permission, False otherwise.
    """
    if isinstance(role, str):
        role = Role.from_string(role)

    return permission in ROLE_PERMISSIONS.get(role, set())


def can_manage_user(manager_role: Role | str, target_role: Role | str) -> bool:
    """Check if a user with manager_role can manage a user with target_role.

    Rules:
    - Owner can manage anyone
    - Admin can manage members but not other admins or owners
    - Member cannot manage anyone

    Args:
        manager_role: Role of the user trying to manage.
        target_role: Role of the user being managed.

    Returns:
        True if management is allowed, False otherwise.
    """
    if isinstance(manager_role, str):
        manager_role = Role.from_string(manager_role)
    if isinstance(target_role, str):
        target_role = Role.from_string(target_role)

    # Must have user management permission
    if not has_permission(manager_role, Permission.USERS_MANAGE):
        return False

    # Owner can manage anyone
    if manager_role == Role.OWNER:
        return True

    # Admin can only manage members
    if manager_role == Role.ADMIN:
        return target_role == Role.MEMBER

    return False


def can_assign_role(assigner_role: Role | str, new_role: Role | str) -> bool:
    """Check if a user can assign a specific role to another user.

    Rules:
    - Owner can assign any role (including promoting to owner for transfer)
    - Admin can only assign member role
    - Member cannot assign roles

    Args:
        assigner_role: Role of the user assigning the role.
        new_role: The role being assigned.

    Returns:
        True if role assignment is allowed, False otherwise.
    """
    if isinstance(assigner_role, str):
        assigner_role = Role.from_string(assigner_role)
    if isinstance(new_role, str):
        new_role = Role.from_string(new_role)

    # Must have user management permission
    if not has_permission(assigner_role, Permission.USERS_MANAGE):
        return False

    # Owner can assign any role
    if assigner_role == Role.OWNER:
        return True

    # Admin can only assign member role (can't promote to admin/owner)
    if assigner_role == Role.ADMIN:
        return new_role == Role.MEMBER

    return False


class RBACPermissionError(Exception):
    """Raised when a user doesn't have permission for an action."""

    def __init__(self, permission: Permission, role: Role | None = None):
        self.permission = permission
        self.role = role
        if role:
            message = f"Role '{role.value}' does not have permission '{permission.value}'"
        else:
            message = f"Permission denied: {permission.value}"
        super().__init__(message)


def require_permission(role: Role | str, permission: Permission) -> None:
    """Raise RBACPermissionError if the role doesn't have the permission.

    Args:
        role: The user's role.
        permission: The required permission.

    Raises:
        RBACPermissionError: If the role doesn't have the permission.
    """
    if isinstance(role, str):
        role = Role.from_string(role)

    if not has_permission(role, permission):
        raise RBACPermissionError(permission, role)


def get_user_permissions(role: Role | str) -> set[Permission]:
    """Get all permissions for a role.

    Args:
        role: The user's role.

    Returns:
        Set of permissions the role has.
    """
    if isinstance(role, str):
        role = Role.from_string(role)

    return ROLE_PERMISSIONS.get(role, set()).copy()


def check_memory_access(
    user_id: UUID,
    user_role: Role | str,
    user_org_id: UUID,
    memory_owner_id: UUID | None,
    memory_org_id: UUID | None,
    memory_shared: bool,
) -> bool:
    """Check if a user can access a specific memory.

    Args:
        user_id: The user trying to access the memory.
        user_role: The user's role.
        user_org_id: The user's organization ID.
        memory_owner_id: The memory owner's user ID.
        memory_org_id: The memory's organization ID.
        memory_shared: Whether the memory is shared with the org.

    Returns:
        True if access is allowed, False otherwise.
    """
    if isinstance(user_role, str):
        user_role = Role.from_string(user_role)

    # User owns the memory
    if memory_owner_id and user_id == memory_owner_id:
        return True

    # Memory is shared and user is in the same org
    if memory_shared and memory_org_id and user_org_id == memory_org_id:
        return True

    # Admin/owner can see all memories in their org
    if has_permission(user_role, Permission.MEMORY_READ_ALL):
        if memory_org_id and user_org_id == memory_org_id:
            return True

    return False
