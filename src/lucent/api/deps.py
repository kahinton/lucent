"""Authentication and authorization dependencies for API endpoints."""

import re
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Request, status

from lucent.access_control import AccessControlService
from lucent.auth import (
    set_current_user,
)
from lucent.auth_providers import organization_allows_access
from lucent.db import ApiKeyRepository, UserRepository, get_pool
from lucent.db.api_key import ADMIN_ORGS_SCOPE, API_KEY_PREFIXES
from lucent.log_context import set_user_id
from lucent.rbac import Permission, Role, has_permission


class CurrentUser:
    """Dependency that provides the current authenticated user."""

    def __init__(
        self,
        id: UUID,
        organization_id: UUID | None,
        role: str,
        email: str | None,
        display_name: str | None,
        auth_method: str = "session",  # "session", "api_key", "oauth"
        api_key_id: UUID | None = None,  # Set when authenticated via API key
        api_key_scopes: list[str] | None = None,  # Scopes from API key
        impersonator_id: UUID | None = None,  # Set when being impersonated
        impersonator_display_name: str | None = None,  # For UI display
        external_id: str | None = None,  # External provider ID (e.g. "daemon-service")
        memory_scope_user_id: UUID | None = None,  # Restricts memory ops to this user
        memory_scope: str | None = None,  # 'user' or 'org_shared_only'
        api_key_name: str | None = None,
        sandbox_task_id: str | None = None,
        remote_agent_id: str | None = None,
        remote_session_task_id: str | None = None,
    ):
        self.id = id
        self.organization_id = organization_id
        self.role = Role.from_string(role)
        self.email = email
        self.display_name = display_name
        self.auth_method = auth_method
        self.api_key_id = api_key_id
        self.api_key_scopes = api_key_scopes or ["read", "write"]
        self.impersonator_id = impersonator_id
        self.impersonator_display_name = impersonator_display_name
        self.external_id = external_id
        self.memory_scope_user_id = memory_scope_user_id
        self.memory_scope = memory_scope
        self.api_key_name = api_key_name
        self.sandbox_task_id = sandbox_task_id
        self.remote_agent_id = remote_agent_id
        self.remote_session_task_id = remote_session_task_id

    @property
    def effective_memory_user_id(self) -> UUID:
        """The user ID to use for memory operations. Returns scoped user if set, else self."""
        if self.memory_scope == 'user' and self.memory_scope_user_id is not None:
            return self.memory_scope_user_id
        return self.id

    @property
    def is_memory_scoped(self) -> bool:
        """Whether this session has restricted memory scope."""
        return self.memory_scope is not None

    @property
    def is_impersonated(self) -> bool:
        """Check if this user is being impersonated by another user."""
        return self.impersonator_id is not None

    @property
    def is_daemon_service(self) -> bool:
        """Whether this user is a daemon-service system account.

        Daemon-service users are provisioned per organization with an
        ``external_id`` of ``daemon-service:{org_id}``. The bare legacy
        ``daemon-service`` id (migration 017) has been retired; it is no longer
        recognized anywhere and its account has been removed.
        """
        ext = self.external_id or ""
        return ext.startswith("daemon-service:")

    def has_permission(self, permission: Permission) -> bool:
        """Check if this user has a specific permission."""
        return has_permission(self.role, permission)

    def require_permission(self, permission: Permission) -> None:
        """Raise HTTPException if user doesn't have permission."""
        if not self.has_permission(permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission denied: {permission.value}",
            )

    def has_scope(self, scope: str) -> bool:
        """Check if the API key has a specific scope.

        Keys with 'read' + 'write' scopes (the default) have full access.
        Scoped keys (e.g., 'daemon-tasks') are restricted to those operations.
        """
        if "read" in self.api_key_scopes and "write" in self.api_key_scopes:
            return True
        return scope in self.api_key_scopes

    def require_scope(self, scope: str) -> None:
        """Raise HTTPException if the API key doesn't have the required scope."""
        if not self.has_scope(scope):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"API key does not have required scope: {scope}",
            )

    def get_audit_context(self) -> dict[str, Any]:
        """Get context dict for audit logging."""
        ctx = {"auth_method": self.auth_method}
        if self.api_key_id:
            ctx["api_key_id"] = str(self.api_key_id)
            ctx["api_key_scopes"] = self.api_key_scopes
        if self.impersonator_id:
            ctx["impersonator_id"] = str(self.impersonator_id)
            ctx["impersonator_display_name"] = self.impersonator_display_name
            ctx["is_impersonated"] = True
        return ctx


async def _authenticate_with_api_key(api_key: str) -> CurrentUser | None:
    """Authenticate using an API key.

    Args:
        api_key: The API key (with or without 'Bearer ' prefix).

    Returns:
        CurrentUser if valid, None otherwise.
    """
    # Strip 'Bearer ' prefix if present
    if api_key.startswith("Bearer "):
        api_key = api_key[7:]

    if not api_key.startswith(API_KEY_PREFIXES):
        return None

    pool = await get_pool()
    api_key_repo = ApiKeyRepository(pool)

    key_info = await api_key_repo.verify(api_key)
    if not key_info:
        return None

    # Get the full user record
    user_repo = UserRepository(pool)
    user = await user_repo.get_by_id(key_info["user_id"])
    if not user:
        return None

    if not await organization_allows_access(pool, user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Organization is suspended",
        )

    # Thread memory scope from API key into the context dict
    user["memory_scope_user_id"] = key_info.get("memory_scope_user_id")
    user["memory_scope"] = key_info.get("memory_scope")
    set_current_user(user)

    # Parse memory_scope_user_id to UUID if present
    raw_scope_uid = key_info.get("memory_scope_user_id")
    scope_user_id = UUID(str(raw_scope_uid)) if raw_scope_uid else None

    key_name = key_info.get("name")
    sandbox_task_id = None
    if isinstance(key_name, str):
        match = re.match(r"^sandbox-task-([0-9a-fA-F-]{36})-[0-9a-fA-F]+$", key_name)
        if match:
            sandbox_task_id = match.group(1)

    # Remote engine sessions mint a key per task, named
    # ``remote-engine-{task_id}-{hex}``. Parsing the name binds the key to
    # exactly one task so a compromised remote cannot report on, or link
    # memory to, work that is not its own.
    remote_session_task_id = None
    if isinstance(key_name, str):
        session_match = re.match(
            r"^remote-engine-([0-9a-fA-F-]{36})-[0-9a-fA-F]+$", key_name
        )
        if session_match:
            remote_session_task_id = session_match.group(1)

    # Remote agent keys are named ``remote-agent-{remote_id}``. Parsing the
    # name (rather than trusting a request parameter) binds the key to exactly
    # one remote: the path ``/api/remotes/{remote_id}/...`` must match it.
    remote_agent_id = None
    if isinstance(key_name, str):
        remote_match = re.match(
            r"^remote-agent-([0-9a-fA-F-]{36})$", key_name
        )
        if remote_match:
            remote_agent_id = remote_match.group(1)
    return CurrentUser(
        id=user["id"],
        organization_id=user.get("organization_id"),
        role=user.get("role", "member"),
        email=user.get("email"),
        display_name=user.get("display_name"),
        auth_method="api_key",
        api_key_id=key_info["id"],  # Include the API key ID for auditing
        api_key_scopes=key_info.get("scopes", ["read", "write"]),
        external_id=user.get("external_id"),
        memory_scope_user_id=scope_user_id,
        memory_scope=key_info.get("memory_scope"),
        api_key_name=key_name,
        sandbox_task_id=sandbox_task_id,
        remote_agent_id=remote_agent_id,
        remote_session_task_id=remote_session_task_id,
    )


# ── Remote agent (Agent Control Protocol) authorization ──────────────────
#
# A remote agent key may only touch its own remote's protocol endpoints and
# the task transitions of tasks it holds a claim on. The allowlist is
# deliberately an allowlist (not a denylist) and lives here, next to the
# sandbox equivalent, so both narrow-scope key families are auditable in one
# place.

REMOTE_AGENT_ALLOWED_ROUTES: tuple[tuple[str, str], ...] = (
    ("POST", r"^/api/remotes/[^/]+/claim$"),
    ("POST", r"^/api/remotes/[^/]+/heartbeat$"),
    ("POST", r"^/api/requests/tasks/[^/]+/events$"),
    ("POST", r"^/api/requests/tasks/[^/]+/complete$"),
    ("POST", r"^/api/requests/tasks/[^/]+/fail$"),
)

_REMOTE_PATH_RE = re.compile(r"^/api/remotes/([^/]+)/")
_REMOTE_TASK_PATH_RE = re.compile(r"^/api/requests/tasks/([^/]+)/")
_UUID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")


def _enforce_remote_agent_allowlist(request: Request, user: CurrentUser) -> None:
    """Enforce the remote-agent scope allowlist and identity bindings.

    Raises:
        HTTPException: 403 for a disallowed route, a path/identity mismatch,
            or a key that cannot be bound to a remote instance.
    """
    method = request.method.upper()
    path = request.url.path

    if user.remote_agent_id is None:
        # A remote-agent-scoped key that is not named for a remote cannot be
        # safely bound to anything — fail closed rather than guess.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Remote agent key is not bound to a remote instance",
        )

    if not any(
        method == allowed_method and re.match(pattern, path)
        for allowed_method, pattern in REMOTE_AGENT_ALLOWED_ROUTES
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API key scope does not allow this endpoint",
        )

    # Binding 1: the remote id in the URL must be the key's own remote.
    remote_match = _REMOTE_PATH_RE.match(path)
    if remote_match and remote_match.group(1) != user.remote_agent_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Remote agent key is scoped to a different remote instance",
        )

    # Binding 2: task transitions must name a well-formed task id. Ownership of
    # that specific task is verified transactionally by the transition
    # repository call; validating shape here prevents path-traversal-style
    # attempts before we ever hit the database.
    task_match = _REMOTE_TASK_PATH_RE.match(path)
    if task_match and not _UUID_RE.match(task_match.group(1)):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed task id",
        )


# ── Instance control-plane (admin organization) keys ─────────────────────
#
# A key minted with only the ``admin-orgs`` scope lets an external control
# plane provision tenants. Its owning user must be a hyperadmin (the routes
# require Permission.ORG_MANAGE_INSTANCE), which would otherwise make every
# other REST route reachable as an instance operator. This allowlist is what
# confines the *credential* to tenant administration. The reachable surface
# is the committed organizations router (``lucent.api.app`` mounts it at
# ``/api/organizations``) — provision, list, read, rename, per-tenant
# settings/setting-locks, credential reads, and model grants — and nothing
# of the tenant data plane.

#: The segment that stands for an organization id in the patterns below.
#: It must never swallow a literal segment, because ``current`` (the
#: tenant-scoped routes), ``instance`` (instance-wide routes), and
#: ``provision`` are real words on this router: a negative lookahead keeps
#: ``GET /api/organizations/current`` and friends out of the scoped key's
#: reach even though ``[^/?]+`` would otherwise match them.
_ORG_ID_SEGMENT = r"(?!current(?:/|$)|instance(?:/|$)|provision(?:/?$))[^/?]+"

ADMIN_ORGS_ALLOWED_ROUTES: tuple[tuple[str, str], ...] = (
    # The only create path this scope may reach. There is no bare-create
    # pattern: a shell organization with no member is not what a control
    # plane wants, and POST /provision is the audited, complete path.
    ("POST", r"^/api/organizations/provision/?$"),
    # Reconciliation reads: the paginated list and one organization's detail.
    ("GET", r"^/api/organizations/?$"),
    ("GET", "^/api/organizations/" + _ORG_ID_SEGMENT + "/?$"),
    ("PATCH", "^/api/organizations/" + _ORG_ID_SEGMENT + "/?$"),
    # Per-tenant tier controls and their reads (``features.sandboxes_enabled``
    # and friends). Reads of settings and the lock set, writes of a single
    # setting, and the whole lock-management family stay reachable so a tier
    # application can pin ``features.sandboxes_enabled`` remotely.
    ("GET", "^/api/organizations/" + _ORG_ID_SEGMENT + r"/settings/?$"),
    ("PUT", "^/api/organizations/" + _ORG_ID_SEGMENT + r"/settings/.+$"),
    ("GET", "^/api/organizations/" + _ORG_ID_SEGMENT + r"/setting-locks/?$"),
    ("PUT", "^/api/organizations/" + _ORG_ID_SEGMENT + r"/setting-locks/?$"),
    ("PUT", "^/api/organizations/" + _ORG_ID_SEGMENT + r"/setting-locks/.+$"),
    ("DELETE", "^/api/organizations/" + _ORG_ID_SEGMENT + r"/setting-locks/.+$"),
    ("POST", "^/api/organizations/" + _ORG_ID_SEGMENT + r"/setting-locks/release/?$"),
    # Instance-level account lookup by email (signup pre-flight, below).
    ("GET", r"^/api/organizations/instance/users/by-email/?$"),
    # Instance-level account lookup by login. Accounts whose email is NULL
    # log in with their display name alone and are invisible to the by-email
    # read, so the control plane needs this sibling lookup to resolve them.
    # Read-only, literal path (never a parametric org id), and gated on the
    # same ORG_MANAGE_INSTANCE permission inside the handler.
    ("GET", r"^/api/organizations/instance/users/by-login/?$"),
    # Provider credential read — values are never returned — and the grant
    # that makes one shared model usable by the tenant.
    ("GET", "^/api/organizations/" + _ORG_ID_SEGMENT + r"/credentials/?$"),
    ("POST", "^/api/organizations/" + _ORG_ID_SEGMENT + r"/models/.+/grant/?$"),
    # Member creation (POST /{id}/users): the second write path a signup
    # site needs. An invited signup must create the invitee's account in
    # Lucent — with the password the invitee chose — exactly as the owner
    # account is created by /provision; without this reach the invitee
    # would keep a password the site itself stores, defeating the
    # single-source-of-truth design. The route itself still enforces
    # ORG_MANAGE_INSTANCE and Lucent's own role rules (`can_assign_role`),
    # and the handler's 409 covers the identity collision.
    ("POST", "^/api/organizations/" + _ORG_ID_SEGMENT + r"/users/?$"),
    # Member and consumption reads: a billing/control-plane reconciliation
    # needs to see who is in a tenant and what it consumes, and no more.
    # These are GET-only; every other member sub-route (role changes,
    # reset-password, per-user api-keys, member deletion) stays unreachable.
    # The negative lookahead above keeps the literal segments distinct: the
    # parametric segment can never swallow ``current``/``instance``, so
    # ``/current/users`` and instance-wide routes stay outside the reach.
    ("GET", "^/api/organizations/" + _ORG_ID_SEGMENT + r"/users/?$"),
    ("GET", "^/api/organizations/" + _ORG_ID_SEGMENT + r"/usage/resources/?$"),
)


def _enforce_admin_orgs_allowlist(request: Request, user: CurrentUser) -> None:
    """Confine an ``admin-orgs`` key to the instance organization endpoints.

    Raises:
        HTTPException: 403 for any route outside the allowlist.
    """
    method = request.method.upper()
    path = request.url.path

    if not any(
        method == allowed_method and re.match(pattern, path)
        for allowed_method, pattern in ADMIN_ORGS_ALLOWED_ROUTES
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API key scope does not allow this endpoint",
        )


async def get_current_user(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> CurrentUser:
    """Get the current authenticated user for API routes.

    API key authentication is always required.

    Headers:
        Authorization: Bearer hs_... (API key required)
    """
    # Try API key authentication
    if authorization:
        user = await _authenticate_with_api_key(authorization)
        if user:
            # Sandbox bridge scoped keys are restricted to specific API endpoints.
            has_sandbox_scope = any(
                scope.startswith("sandbox-") for scope in user.api_key_scopes
            )
            if user.auth_method == "api_key" and has_sandbox_scope:
                allowed_method_path_patterns = (
                    ("POST", r"^/api/memories$"),
                    ("PATCH", r"^/api/memories/[^/]+$"),
                    ("POST", r"^/api/search(?:$|/full$)"),
                    ("POST", r"^/api/requests/tasks/[^/]+/(?:events|memories)$"),
                )
                path = request.url.path
                method = request.method.upper()
                if not any(
                    method == allowed_method and re.match(pattern, path)
                    for allowed_method, pattern in allowed_method_path_patterns
                ):
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="API key scope does not allow this endpoint",
                    )
                task_match = re.match(
                    r"^/api/requests/tasks/([^/]+)/(?:events|memories)$", path
                )
                if (
                    task_match
                    and user.sandbox_task_id
                    and task_match.group(1) != user.sandbox_task_id
                ):
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Sandbox API key is scoped to a different task",
                    )

            # Remote-agent keys (Agent Control Protocol) are the narrowest
            # scope in the system: one remote may only claim/heartbeat for
            # *itself* and report on tasks *it* claimed. Two independent
            # bindings are enforced here rather than in the route handlers, so
            # a future route added to this router cannot accidentally inherit
            # remote access:
            #   1. path → key identity (remote_id in the URL must equal the id
            #      embedded in the key name);
            #   2. task → claim identity (task transitions must target a task
            #      whose claimed_by is this remote's instance id).
            if (
                user.auth_method == "api_key"
                and "remote-agent" in user.api_key_scopes
            ):
                _enforce_remote_agent_allowlist(request, user)

            # Control-plane keys are the narrowest *instance-level* scope: a
            # key minted for tenant provisioning may reach the admin
            # organization endpoints and nothing else, even though its owner
            # is a hyperadmin. Checked on scope membership (not `has_scope`)
            # so a key that also carries read/write stays confined.
            if (
                user.auth_method == "api_key"
                and ADMIN_ORGS_SCOPE in user.api_key_scopes
            ):
                _enforce_admin_orgs_allowlist(request, user)

            # Remote *engine session* keys are minted per task and ship to a
            # host Lucent does not control, so they must be useless off the MCP
            # endpoint. They exist for one reason: to let the agent running on
            # that host call Lucent's own tools. MCP has its own auth
            # middleware, so denying every REST route here is the whole
            # restriction — and it is what stops a leaked session key from
            # being usable as a daemon-role REST credential.
            if (
                user.auth_method == "api_key"
                and "remote-session" in user.api_key_scopes
            ):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=(
                        "A remote engine session key is only valid on the MCP "
                        "endpoint"
                    ),
                )

            set_user_id(str(user.id))
            return user

        # Invalid API key
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired API key",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # No authorization header - always reject for API routes
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="API key required. Use Authorization: Bearer hs_your_key_here",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_optional_user(
    authorization: Annotated[str | None, Header()] = None,
) -> CurrentUser | None:
    """Get the current user if authenticated, or None."""
    try:
        return await get_current_user(authorization)
    except HTTPException:
        return None


def require_role(minimum_role: Role):
    """Dependency factory that requires a minimum role level.

    Usage:
        @router.get("/admin-only")
        async def admin_endpoint(user: CurrentUser = Depends(require_role(Role.ADMIN))):
            ...
    """

    async def check_role(
        user: CurrentUser = Depends(get_current_user),
    ) -> CurrentUser:
        if user.role < minimum_role:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires {minimum_role.value} role or higher",
            )
        return user

    return check_role


def require_permission_dep(permission: Permission):
    """Dependency factory that requires a specific permission.

    Usage:
        @router.get("/audit")
        async def audit_endpoint(
            user: CurrentUser = Depends(require_permission_dep(Permission.AUDIT_VIEW_ORG)),
        ):
            ...
    """

    async def check_permission(
        user: CurrentUser = Depends(get_current_user),
    ) -> CurrentUser:
        if not user.has_permission(permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission denied: {permission.value}",
            )
        return user

    return check_permission


def require_scope_dep(scope: str):
    """Dependency factory that requires a specific API key scope.

    Usage:
        @router.post("/tasks")
        async def create_task(user: CurrentUser = Depends(require_scope_dep("daemon-tasks"))):
            ...
    """

    async def check_scope(
        user: CurrentUser = Depends(get_current_user),
    ) -> CurrentUser:
        user.require_scope(scope)
        return user

    return check_scope


async def get_access_control() -> AccessControlService:
    """Get access control service for resource ACL checks."""
    pool = await get_pool()
    return AccessControlService(pool)


# Type alias for dependency injection
AuthenticatedUser = Annotated[CurrentUser, Depends(get_current_user)]
OptionalUser = Annotated[CurrentUser | None, Depends(get_optional_user)]
AdminUser = Annotated[CurrentUser, Depends(require_role(Role.ADMIN))]
OwnerUser = Annotated[CurrentUser, Depends(require_role(Role.OWNER))]
HyperadminUser = Annotated[CurrentUser, Depends(require_role(Role.HYPERADMIN))]
DaemonTaskUser = Annotated[CurrentUser, Depends(require_scope_dep("daemon-tasks"))]
