"""SCIM 2.0 provisioning API.

SCIM is how an enterprise identity provider (Okta, Entra ID, OneLogin) keeps a
directory in sync: it creates users when someone is hired, deactivates them when
they leave, and updates names and addresses in between. Getting this wrong is
worse than not having it — a broken SCIM integration is how departed employees
keep their access — so the implementation is deliberately conservative about
what it accepts.

Design decisions worth knowing:

* **Separate credential family.** SCIM uses its own ``sc_`` bearer tokens
  (``scim_tokens``), not user API keys. A SCIM token can create and delete
  people; no user's API key should imply that, and rotating the directory-sync
  credential must not disturb normal API access.
* **Organization comes from the token, never the payload.** Every query is
  scoped to the token's tenant. There is no cross-tenant path here at all.
* **``externalId`` is the IdP's key, not our login identity.** A SCIM-managed
  user is found by ``scim_external_id`` within the tenant. Their *login*
  identity — password or federated subject — is a separate fact and is left
  alone. Deleting through SCIM therefore does not orphan a password.
* **Deactivation is ``active: false``, not deletion.** SCIM's DELETE is
  honoured as a deactivate for users who have done work: their memories, tasks,
  and audit trail reference them, and hard-deleting would either cascade that
  history away or be blocked by a foreign key. ``DELETE`` does a real delete
     only when the user has never signed in, which keeps "undo a mistaken
  provisioning" working.
* **``hyperadmin`` is unreachable.** A directory sync must never be able to
  mint an instance operator, so ``role`` is only read through a fixed map that
  caps at ``admin``.
* **Pagination and filtering are minimal but honest.** ``startIndex``/
  ``count`` work; ``filter`` supports the equality form IdPs actually send
  (``userName eq "..."``, ``externalId eq "..."``). Anything else is a 400
  rather than a silently wrong full list.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, status
from fastapi.responses import JSONResponse, Response

from lucent.db import (
    AdminAuditRepository,
    ScimTokenRepository,
    UserRepository,
    get_pool,
)
from lucent.db import admin_audit as audit_actions

logger = logging.getLogger(__name__)

router = APIRouter()

#: SCIM schema URNs. These are fixed by RFC 7643 and must be exact.
SCIM_USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
SCIM_LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
SCIM_ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
SCIM_PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
SCIM_SERVICE_CONFIG_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"

_CONTENT_TYPE = "application/scim+json"

#: Roles a directory may assign. ``owner`` is deliberately absent: an IdP group
#: membership should not be able to take over a tenant's ownership, and the
#: unique-owner index would make a second owner a hard failure anyway.
_SCIM_ASSIGNABLE_ROLES = frozenset({"member", "admin"})


def _scim_error(status_code: int, detail: str, scim_type: str | None = None) -> JSONResponse:
    body: dict[str, Any] = {
        "schemas": [SCIM_ERROR_SCHEMA],
        "detail": detail,
        "status": str(status_code),
    }
    if scim_type:
        body["scimType"] = scim_type
    return JSONResponse(status_code=status_code, content=body, media_type=_CONTENT_TYPE)


class ScimAuthError(Exception):
    """Authentication failure, carrying the two challenge headers SCIM requires.

    Raised rather than returned so every endpoint (including the discovery
    endpoints) fails identically without each one repeating the check. The app
    registers a handler that renders it as a SCIM error document with a
    ``WWW-Authenticate`` challenge, which is what RFC 7644 §2 requires and what
    an identity provider expects to see.
    """

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)

    def to_response(self) -> JSONResponse:
        response = _scim_error(401, self.detail)
        response.headers["WWW-Authenticate"] = 'Bearer realm="lucent-scim"'
        return response


async def _authenticate(authorization: str | None) -> dict[str, Any]:
    """Resolve the bearer token to its tenant, or raise :class:`ScimAuthError`."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise ScimAuthError("Missing SCIM bearer token")
    token = authorization[7:].strip()
    pool = await get_pool()
    resolved = await ScimTokenRepository(pool).verify(token)
    if resolved is None:
        # One message for every failure mode (unknown, revoked, expired, wrong
        # prefix) so a caller cannot probe which tokens once existed.
        logger.warning("SCIM request with an invalid or revoked token")
        raise ScimAuthError("Invalid SCIM bearer token")
    return resolved


def _user_to_scim(user: dict[str, Any], *, base_url: str) -> dict[str, Any]:
    """Render a Lucent user as a SCIM User resource.

    Only fields Lucent actually has are returned. SCIM allows a response to omit
    attributes it does not support; inventing plausible-looking values (a
    ``title``, a ``department``) would make the IdP believe it successfully
    synced data that went nowhere.
    """
    user_id = str(user["id"])
    email = user.get("email")
    display = user.get("display_name") or email or user.get("external_id")
    resource: dict[str, Any] = {
        "schemas": [SCIM_USER_SCHEMA],
        "id": user_id,
        "userName": email or user.get("external_id"),
        "displayName": display,
        "active": bool(user.get("is_active", True)),
        "meta": {
            "resourceType": "User",
            "created": user["created_at"].isoformat() if user.get("created_at") else None,
            "lastModified": user["updated_at"].isoformat() if user.get("updated_at") else None,
            "location": f"{base_url}/scim/v2/Users/{user_id}",
        },
    }
    if user.get("scim_external_id"):
        resource["externalId"] = user["scim_external_id"]
    if email:
        resource["emails"] = [{"value": email, "primary": True, "type": "work"}]
    return {key: value for key, value in resource.items() if value is not None}


def _primary_email(payload: dict[str, Any]) -> str | None:
    """Extract the primary email from a SCIM User payload.

    ``userName`` is the fallback: many IdPs send the address as the user name
    and no ``emails`` array at all, and that is legal SCIM.
    """
    emails = payload.get("emails") or []
    if isinstance(emails, list):
        for entry in emails:
            if isinstance(entry, dict) and entry.get("primary") and entry.get("value"):
                return str(entry["value"]).strip().lower()
        for entry in emails:
            if isinstance(entry, dict) and entry.get("value"):
                return str(entry["value"]).strip().lower()
    user_name = payload.get("userName")
    if isinstance(user_name, str) and "@" in user_name:
        return user_name.strip().lower()
    return None


def _parse_filter(value: str | None) -> tuple[str, str] | None:
    """Parse the one filter form identity providers actually emit.

    Supports ``attr eq "value"`` (and the unquoted variant) for the attributes
    we can honour. Anything else raises so a caller gets a clear 400 instead of
    a full list they will misinterpret as a filter result.
    """
    if not value:
        return None
    text = value.strip()
    parts = text.split(None, 2)
    if len(parts) != 3 or parts[1].lower() != "eq":
        raise ValueError(
            "Only 'attribute eq \"value\"' filters are supported (attributes: "
            "userName, externalId, emails.value)."
        )
    attribute = parts[0].strip().lower()
    raw = parts[2].strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'"):
        raw = raw[1:-1]
    attribute = {
        "username": "userName",
        "externalid": "externalId",
        "emails.value": "emails.value",
    }.get(attribute, parts[0].strip())
    if attribute not in ("userName", "externalId", "emails.value"):
        raise ValueError(f"Filtering on {attribute!r} is not supported.")
    return attribute, raw


# =============================================================================
# Discovery endpoints
# =============================================================================


@router.get("/scim/v2/ServiceProviderConfig")
async def service_provider_config(authorization: str | None = Header(default=None)):
    """Advertise what this SCIM implementation supports.

    Okta and Entra read this before syncing. Reporting support we do not have
    (bulk operations, ETags) would cause them to call endpoints that 404.
    """
    await _authenticate(authorization)
    return JSONResponse(
        content={
            "schemas": [SCIM_SERVICE_CONFIG_SCHEMA],
            "documentationUri": "/docs/scim",
            "patch": {"supported": True},
            "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
            "filter": {"supported": True, "maxResults": 200},
            "changePassword": {"supported": False},
            "sort": {"supported": False},
            "etag": {"supported": False},
            "authenticationSchemes": [
                {
                    "type": "oauthbearertoken",
                    "name": "OAuth Bearer Token",
                    "description": "Lucent SCIM token, created in Settings > Identity & access.",
                    "primary": True,
                }
            ],
        },
        media_type=_CONTENT_TYPE,
    )


@router.get("/scim/v2/ResourceTypes")
async def resource_types(authorization: str | None = Header(default=None)):
    await _authenticate(authorization)
    return JSONResponse(
        content={
            "schemas": [SCIM_LIST_SCHEMA],
            "totalResults": 1,
            "Resources": [
                {
                    "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
                    "id": "User",
                    "name": "User",
                    "endpoint": "/Users",
                    "schema": SCIM_USER_SCHEMA,
                }
            ],
        },
        media_type=_CONTENT_TYPE,
    )


@router.get("/scim/v2/Schemas")
async def schemas(authorization: str | None = Header(default=None)):
    await _authenticate(authorization)
    return JSONResponse(
        content={
            "schemas": [SCIM_LIST_SCHEMA],
            "totalResults": 0,
            "Resources": [],
        },
        media_type=_CONTENT_TYPE,
    )


# =============================================================================
# Users
# =============================================================================


@router.get("/scim/v2/Users")
async def list_users(
    request: Request,
    authorization: str | None = Header(default=None),
    filter: str | None = Query(default=None),
    start_index: int = Query(default=1, ge=1, alias="startIndex"),
    count: int = Query(default=100, ge=0, le=200),
):
    """List members of the token's tenant, optionally filtered."""
    token = await _authenticate(authorization)
    pool = await get_pool()
    repo = UserRepository(pool)
    organization_id = token["organization_id"]

    base_url = str(request.base_url).rstrip("/")

    try:
        parsed_filter = _parse_filter(filter)
    except ValueError as exc:
        return _scim_error(400, str(exc), "invalidFilter")
    if parsed_filter is not None:
        attribute, value = parsed_filter
        if attribute in ("userName", "emails.value"):
            user = await repo.get_by_email_in_organization(value, organization_id)
        else:
            user = await repo.get_by_scim_external_id(value, organization_id)
        resources = [user] if user else []
        total = len(resources)
    else:
        # SCIM lists exclude service identities; a directory sync must not be
        # able to see or touch the daemon account.
        users = await repo.get_by_organization(UUID(str(organization_id)))
        resources = [u for u in users if u.get("role") != "daemon"]
        total = len(resources)
        resources = resources[start_index - 1 : start_index - 1 + count]

    return JSONResponse(
        content={
            "schemas": [SCIM_LIST_SCHEMA],
            "totalResults": total,
            "startIndex": start_index,
            "itemsPerPage": len(resources),
            "Resources": [
                _user_to_scim(user, base_url=base_url) for user in resources
            ],
        },
        media_type=_CONTENT_TYPE,
    )


@router.get("/scim/v2/Users/{user_id}")
async def get_user(
    request: Request,
    user_id: str,
    authorization: str | None = Header(default=None),
):
    token = await _authenticate(authorization)
    pool = await get_pool()
    user = await _load_scoped_user(pool, user_id, token["organization_id"])
    if user is None:
        return _scim_error(404, "User not found")
    return JSONResponse(
        content=_user_to_scim(user, base_url=str(request.base_url).rstrip("/")),
        media_type=_CONTENT_TYPE,
    )


@router.post("/scim/v2/Users", status_code=status.HTTP_201_CREATED)
async def create_user(
    request: Request,
    payload: dict[str, Any],
    authorization: str | None = Header(default=None),
):
    """Provision a member from the directory.

    The created account has **no password**: SCIM proves who someone is to the
    IdP, not to Lucent. They sign in through SSO (if a connection exists) or
    through an admin-issued reset. Creating a password here would mean inventing
    a credential to hand back in a SCIM response, which no one should do.
    """
    token = await _authenticate(authorization)
    pool = await get_pool()
    repo = UserRepository(pool)
    organization_id = token["organization_id"]

    external_id = payload.get("externalId")
    external_id = str(external_id).strip() if external_id else None
    email = _primary_email(payload)
    display_name = payload.get("displayName") or payload.get("userName")

    if not email and not external_id:
        return _scim_error(
            400, "A userName or externalId is required to provision a user.", "invalidValue"
        )

    if external_id:
        existing = await repo.get_by_scim_external_id(external_id, organization_id)
        if existing is not None:
            return _scim_error(409, "A user with this externalId already exists.", "uniqueness")

    if email:
        existing_email = await repo.get_by_email_in_organization(email, organization_id)
        if existing_email is not None:
            # Idempotent by design: Okta retries a create it is unsure about, and
            # a 409 on the retry makes it mark the sync as failed. Return the
            # existing resource with its id so the IdP adopts it instead.
            if external_id and not existing_email.get("scim_external_id"):
                await _set_scim_external_id(pool, existing_email["id"], external_id)
                existing_email["scim_external_id"] = external_id
            return JSONResponse(
                content=_user_to_scim(
                    existing_email, base_url=str(request.base_url).rstrip("/")
                ),
                status_code=status.HTTP_200_OK,
                media_type=_CONTENT_TYPE,
            )

    role = (payload.get("role") or "member").strip().lower()
    if role not in _SCIM_ASSIGNABLE_ROLES:
        role = "member"

    active = payload.get("active", True)
    if not isinstance(active, bool):
        active = True

    created = await repo.create(
        external_id=f"scim_{secrets.token_hex(8)}",
        provider="local",
        organization_id=organization_id,
        email=email,
        display_name=str(display_name) if display_name else email,
        role=role,
        # The IdP asserting the address is the verification. No separate email
        # round trip is possible or useful for a directory-provisioned account.
        email_verified=bool(email),
        provisioned_by="scim",
    )
    if external_id:
        await _set_scim_external_id(pool, created["id"], external_id)
        created["scim_external_id"] = external_id
    if not active:
        await repo.update(created["id"], is_active=False)
        created["is_active"] = False

    await _audit(
        pool,
        token=token,
        action=audit_actions.SCIM_USER_CREATE,
        user_id=created["id"],
        label=email,
        new_values={"email": email, "role": role, "external_id": external_id},
    )

    logger.info(
        "SCIM provisioned user %s in org %s", created["id"], organization_id
    )
    return JSONResponse(
        content=_user_to_scim(created, base_url=str(request.base_url).rstrip("/")),
        status_code=status.HTTP_201_CREATED,
        media_type=_CONTENT_TYPE,
    )


@router.put("/scim/v2/Users/{user_id}")
async def replace_user(
    request: Request,
    user_id: str,
    payload: dict[str, Any],
    authorization: str | None = Header(default=None),
):
    """Full replace. SCIM PUT is a replace, not a merge — absent attributes reset."""
    token = await _authenticate(authorization)
    pool = await get_pool()
    user = await _load_scoped_user(pool, user_id, token["organization_id"])
    if user is None:
        return _scim_error(404, "User not found")

    updates: dict[str, Any] = {}
    display_name = payload.get("displayName")
    if display_name:
        updates["display_name"] = str(display_name)

    email = _primary_email(payload)
    if email and email != (user.get("email") or "").lower():
        # Changing an address can collide with another member; the unique index
        # would raise. Fail as a clean SCIM conflict instead of a 500.
        repo = UserRepository(pool)
        clash = await repo.get_by_email_in_organization(email, token["organization_id"])
        if clash is not None and str(clash["id"]) != str(user["id"]):
            return _scim_error(409, "That email belongs to another member.", "uniqueness")
        updates["email"] = email

    active = payload.get("active")
    if isinstance(active, bool):
        updates["active"] = active

    if updates:
        await _apply_updates(pool, user_id, updates, token["organization_id"])

    refreshed = await _load_scoped_user(pool, user_id, token["organization_id"])
    await _audit(
        pool,
        token=token,
        action=audit_actions.SCIM_USER_UPDATE,
        user_id=user_id,
        label=(refreshed or user).get("email"),
        new_values={k: str(v) for k, v in updates.items()},
    )
    return JSONResponse(
        content=_user_to_scim(refreshed or user, base_url=str(request.base_url).rstrip("/")),
        media_type=_CONTENT_TYPE,
    )


@router.patch("/scim/v2/Users/{user_id}")
async def patch_user(
    request: Request,
    user_id: str,
    payload: dict[str, Any],
    authorization: str | None = Header(default=None),
):
    """Partial update. Supports ``replace``/``add`` with ``path`` or a value map.

    ``remove`` is accepted only for attributes we can actually remove (there are
    none today except optional display data), so it is reported as unsupported
    rather than silently ignored — an IdP that thinks it removed an attribute
    will not try again.
    """
    token = await _authenticate(authorization)
    pool = await get_pool()
    user = await _load_scoped_user(pool, user_id, token["organization_id"])
    if user is None:
        return _scim_error(404, "User not found")

    operations = payload.get("Operations")
    if not isinstance(operations, list) or not operations:
        # Some IdPs send only the bare attributes without an Operations array.
        operations = [{"op": "replace", "value": payload}]

    updates: dict[str, Any] = {}
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        op = str(operation.get("op", "")).strip().lower()
        if op == "remove":
            return _scim_error(
                400,
                "Removing user attributes is not supported; send active=false to "
                "deactivate the account.",
                "invalidSyntax",
            )
        if op not in ("add", "replace"):
            return _scim_error(400, f"Unsupported patch operation: {op!r}", "invalidSyntax")

        path = operation.get("path")
        value = operation.get("value")
        attributes: dict[str, Any] = {}
        if isinstance(path, str) and path.strip():
            attributes[path.strip().lower()] = value
        elif isinstance(value, dict):
            attributes = {str(k).lower(): v for k, v in value.items()}

        for key, raw in attributes.items():
            if key == "active":
                if isinstance(raw, bool):
                    updates["active"] = raw
                elif isinstance(raw, str) and raw.lower() in ("true", "false"):
                    updates["active"] = raw.lower() == "true"
            elif key == "displayname":
                if isinstance(raw, str) and raw.strip():
                    updates["display_name"] = raw.strip()
            elif key in ("username", "emails", "emails.value"):
                email = _primary_email(raw if isinstance(raw, list) else {"emails": raw})
                if not email and isinstance(raw, str):
                    email = raw.strip().lower()
                if email:
                    repo = UserRepository(pool)
                    clash = await repo.get_by_email_in_organization(
                        email, token["organization_id"]
                    )
                    if clash is not None and str(clash["id"]) != str(user["id"]):
                        return _scim_error(
                            409, "That email belongs to another member.", "uniqueness"
                        )
                    updates["email"] = email
            elif key == "externalid":
                if isinstance(raw, str) and raw.strip():
                    await _set_scim_external_id(pool, user_id, raw.strip())

    if updates:
        await _apply_updates(pool, user_id, updates, token["organization_id"])

    refreshed = await _load_scoped_user(pool, user_id, token["organization_id"])
    await _audit(
        pool,
        token=token,
        action=audit_actions.SCIM_USER_UPDATE,
        user_id=user_id,
        label=(refreshed or user).get("email"),
        new_values={k: str(v) for k, v in updates.items()},
    )
    return JSONResponse(
        content=_user_to_scim(refreshed or user, base_url=str(request.base_url).rstrip("/")),
        media_type=_CONTENT_TYPE,
    )


@router.delete("/scim/v2/Users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: str,
    authorization: str | None = Header(default=None),
):
    """Remove a member.

    A user who has never signed in is deleted outright, so a mistaken
    provisioning can be undone cleanly. Anyone who has actually used the
    workspace is **deactivated instead of deleted**: their memories, tasks, and
    audit rows reference them, and hard-deleting would either destroy that
    history by cascade or fail on a foreign key. The IdP gets a 204 either way,
    which is the correct answer for "this person should no longer have access".
    """
    token = await _authenticate(authorization)
    pool = await get_pool()
    user = await _load_scoped_user(pool, user_id, token["organization_id"])
    if user is None:
        return _scim_error(404, "User not found")

    if user.get("role") in ("owner", "hyperadmin"):
        return _scim_error(
            400,
            "An owner cannot be removed by directory sync. Transfer ownership first.",
            "mutability",
        )

    repo = UserRepository(pool)
    never_used = user.get("last_login_at") is None
    if never_used:
        await repo.delete(user["id"])
    else:
        await repo.update(user["id"], is_active=False)
        # Kill any live session immediately: deactivating the row is not enough
        # on its own, because an existing session keeps working until it expires.
        from lucent.auth_providers import destroy_all_user_sessions

        await destroy_all_user_sessions(pool, user["id"])

    await _audit(
        pool,
        token=token,
        action=audit_actions.SCIM_USER_DELETE,
        user_id=user_id,
        label=user.get("email"),
        new_values={"deleted": never_used, "deactivated": not never_used},
    )

    logger.info(
        "SCIM %s user %s in org %s",
        "deleted" if never_used else "deactivated",
        user_id,
        token["organization_id"],
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# =============================================================================
# Helpers
# =============================================================================


async def _load_scoped_user(pool, user_id: str, organization_id: Any) -> dict[str, Any] | None:
    """Load a user only if it belongs to the token's tenant.

    A malformed id returns ``None`` (a 404 to the caller) rather than a 500, so a
    broken sync produces a readable error instead of a stack trace.
    """
    try:
        parsed = UUID(str(user_id))
    except (ValueError, TypeError):
        return None
    user = await UserRepository(pool).get_by_id(parsed)
    if user is None:
        return None
    if str(user.get("organization_id")) != str(organization_id):
        return None
    if user.get("role") == "daemon":
        return None
    return user


async def _apply_updates(pool, user_id: str, updates: dict[str, Any], organization_id) -> None:
    """Apply a validated update dict through the user repository.

    Kept separate so both PUT and PATCH funnel through one write path with one
    notion of which fields are writable.
    """
    repo = UserRepository(pool)
    user_uuid = UUID(str(user_id))
    if "active" in updates:
        await repo.update(user_uuid, is_active=bool(updates["active"]))
        if not updates["active"]:
            from lucent.auth_providers import destroy_all_user_sessions

            await destroy_all_user_sessions(pool, user_uuid)
    field_updates = {
        key: value
        for key, value in updates.items()
        if key in ("email", "display_name") and value is not None
    }
    if field_updates:
        await repo.update(user_uuid, **field_updates)


async def _set_scim_external_id(pool, user_id: Any, external_id: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET scim_external_id = $2, updated_at = NOW() WHERE id = $1",
            str(user_id),
            external_id,
        )


async def _audit(
    pool,
    *,
    token: dict[str, Any],
    action: str,
    user_id: Any,
    label: str | None,
    new_values: dict[str, Any] | None = None,
) -> None:
    """Record a SCIM mutation.

    The actor is the token, not a person: there is no session behind a directory
    sync, and attributing it to a human would be a lie in the audit trail.
    """
    try:
        await AdminAuditRepository(pool).log(
            organization_id=token["organization_id"],
            action=action,
            entity_type="user",
            entity_id=str(user_id),
            entity_label=label,
            new_values=new_values,
            context={
                "scim_token_id": str(token["id"]),
                "scim_token_name": token.get("name"),
            },
            notes="applied by directory sync (SCIM)",
        )
    except Exception:  # noqa: BLE001 — an audit failure must not fail the sync
        logger.warning("Could not write SCIM audit row", exc_info=True)
