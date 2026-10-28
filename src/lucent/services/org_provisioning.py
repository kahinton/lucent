"""Organization provisioning for instance operators (hyperadmins).

Creating a tenant is more than an ``INSERT INTO organizations``. An organization
row by itself is unusable: it has no human member, so nobody can log into it,
and it has no entitled model, so even a logged-in member cannot chat. This
module owns the whole sequence so the two callers that need it — the
hyperadmin REST API and the standalone local console — cannot drift:

1. create the organization (and its per-org daemon identity),
2. create the first human member and give them a role,
3. set a password (generated and returned once if the caller did not supply one),
4. mint an API key ("default API keys" for a signup flow),
5. seed the built-in skills / agents / hooks / sandbox templates,
6. optionally ensure the tenant has one enabled, org-cleared model,
7. write an audit trail that survives even if the tenant is later deleted.

Everything here runs as the *operator*, writing into another organization's
rows, so every write passes the target org explicitly to the scoped-connection
helpers rather than inheriting the caller's tenant context.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import asyncpg

from lucent.db.admin_audit import AdminAuditRepository
from lucent.db.api_key import ApiKeyRepository
from lucent.db.models import ModelRepository
from lucent.db.organization import OrganizationRepository
from lucent.db.user import UserRepository
from lucent.logging import get_logger

logger = get_logger(__name__)

#: Roles an operator may give the first member of a new tenant. A tenant with
#: no owner cannot transfer ownership or delete itself, so ``owner`` is the
#: default; ``admin`` exists for signup flows that intend to retain ownership
#: at the instance level.
PROVISIONABLE_FIRST_ROLES = ("owner", "admin")

#: Roles an operator may assign to *any* member of an existing tenant.
#: ``hyperadmin`` is never in this set: instance-wide access is granted by
#: operator tooling, not by adding someone to a customer's organization.
ASSIGNABLE_MEMBER_ROLES = ("member", "admin", "owner")

DEFAULT_API_KEY_NAME = "Default API key"


class ProvisioningError(Exception):
    """Base class for provisioning failures that map to a 4xx response."""


class OrganizationNameTakenError(ProvisioningError):
    """The requested organization name already exists."""


class FirstUserConflictError(ProvisioningError):
    """The requested first-user identity already exists on this instance."""


class InvalidProvisioningRequestError(ProvisioningError):
    """A validation failure (bad role, weak password, unusable email)."""


class OrganizationAlreadyHasOwnerError(ProvisioningError):
    """The tenant already has an owner and the unique-owner index forbids two."""


@dataclass
class ProvisionedOrganization:
    """What a provisioning run produced. The plaintext key is shown once."""

    organization: dict[str, Any]
    user: dict[str, Any]
    api_key: str | None = None
    temporary_password: str | None = None
    seeded: dict[str, int] = field(default_factory=dict)
    model_id: str | None = None
    providers_configured: list[str] = field(default_factory=list)
    backends_created: list[str] = field(default_factory=list)
    models_discovered: int = 0
    settings_applied: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


async def log_instance_event(
    pool: asyncpg.Pool,
    *,
    operator_org_id: UUID | str,
    operator_user_id: UUID | str | None,
    action: str,
    entity_type: str,
    entity_id: UUID | str | None = None,
    entity_label: str | None = None,
    target_org_id: UUID | str | None = None,
    changed_fields: list[str] | None = None,
    old_values: dict[str, Any] | None = None,
    new_values: dict[str, Any] | None = None,
    notes: str | None = None,
) -> None:
    """Record an instance-level action taken against another organization.

    Two rows are written on purpose:

    * **The operator's own org** — always. This is the durable instance audit
      trail: it outlives a deleted tenant (audit rows cascade with the org they
      belong to) and it is the stream the console reads.
    * **The target org** — when it differs and still exists. A tenant admin
      should be able to see that an operator changed their settings. Without
      this, the change is "visible" only to the person who made it, which is the
      opposite of what an operator-managed instance needs.

    Failures are logged and swallowed: an audit write must never undo a
    completed provisioning step.
    """
    repo = AdminAuditRepository(pool)
    context: dict[str, Any] = {"instance_level": True}
    if target_org_id:
        context["target_organization_id"] = str(target_org_id)

    try:
        await repo.log(
            organization_id=UUID(str(operator_org_id)),
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_label=entity_label,
            actor_user_id=UUID(str(operator_user_id)) if operator_user_id else None,
            changed_fields=changed_fields,
            old_values=old_values,
            new_values=new_values,
            context=context,
            notes=notes,
        )
    except Exception:  # noqa: BLE001 — audit must not break the action
        logger.warning("Failed to write instance audit row for %s", action, exc_info=True)

    if not target_org_id or str(target_org_id) == str(operator_org_id):
        return

    try:
        await repo.log(
            organization_id=UUID(str(target_org_id)),
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_label=entity_label,
            actor_user_id=UUID(str(operator_user_id)) if operator_user_id else None,
            changed_fields=changed_fields,
            old_values=old_values,
            new_values=new_values,
            context={"instance_level": True, "performed_by_instance_operator": True},
            notes=notes or "Changed by an instance operator.",
        )
    except Exception:  # noqa: BLE001 — the tenant may be gone; that is fine
        logger.debug(
            "Could not mirror instance audit row into org %s", target_org_id, exc_info=True
        )


async def create_first_user(
    pool: asyncpg.Pool,
    *,
    organization_id: UUID | str,
    role: str = "owner",
    email: str | None = None,
    display_name: str | None = None,
    external_id: str | None = None,
    password: str | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Create the first human member of an organization.

    Args:
        pool: Database pool.
        organization_id: The target organization.
        role: ``owner`` or ``admin`` (see :data:`PROVISIONABLE_FIRST_ROLES`).
        email: Member email. Also used as the login identifier.
        display_name: Member display name.
        external_id: Identity-provider id. Defaults to the email.
        password: Plaintext password. When omitted a strong temporary password
            is generated and returned so the caller can deliver it once.

    Returns:
        ``(user_record, temporary_password_or_None)``.

    Raises:
        InvalidProvisioningRequest: On a bad role, or a password that does not
            meet the instance policy.
        FirstUserConflict: If the identity already exists.
    """
    from lucent.auth_providers import (
        generate_temporary_password,
        set_user_password,
        validate_password_complexity,
    )

    if role not in PROVISIONABLE_FIRST_ROLES:
        raise InvalidProvisioningRequestError(
            f"First user role must be one of: {', '.join(PROVISIONABLE_FIRST_ROLES)}"
        )

    identity = external_id or (email or "").strip() or None
    if not identity:
        raise InvalidProvisioningRequestError("An email or external_id is required.")

    if password is not None:
        complexity_error = validate_password_complexity(password)
        if complexity_error:
            raise InvalidProvisioningRequestError(f"Password rejected: {complexity_error}")

    user_repo = UserRepository(pool)
    existing = await user_repo.get_by_external_id(identity, "local")
    if existing is not None:
        raise FirstUserConflictError(
            f"A user with external_id '{identity}' already exists "
            f"(organization {existing.get('organization_id')})."
        )

    user = await user_repo.create(
        external_id=identity,
        provider="local",
        organization_id=organization_id,
        email=email,
        display_name=display_name or email,
        role=role,
    )

    temporary_password: str | None = None
    if password is None:
        temporary_password = generate_temporary_password()
        await set_user_password(
            pool, user["id"], temporary_password, force_change=True
        )
    else:
        await set_user_password(pool, user["id"], password)

    # ``create`` already stored the role, but re-read so the caller receives the
    # row exactly as persisted (the individual memory sync runs here too).
    refreshed = await user_repo.update_role(user["id"], role)
    return (refreshed or user), temporary_password


async def add_member(
    pool: asyncpg.Pool,
    *,
    organization_id: UUID | str,
    role: str = "member",
    email: str | None = None,
    display_name: str | None = None,
    external_id: str | None = None,
    password: str | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Add a member to an *existing* organization.

    This is :func:`create_first_user` with the full tenant role set, because the
    first-user restriction ("owner or admin only") exists to guarantee a new
    tenant has an administrator — it is meaningless once a tenant exists.

    Raises:
        OrganizationAlreadyHasOwner: If ``role='owner'`` and the tenant already
            has one. ``idx_one_owner_per_org`` allows exactly one, so asking for
            a second is a request error rather than a 500.
    """
    from lucent.auth_providers import (
        generate_temporary_password,
        set_user_password,
        validate_password_complexity,
    )

    if role not in ASSIGNABLE_MEMBER_ROLES:
        raise InvalidProvisioningRequestError(
            f"Role must be one of: {', '.join(ASSIGNABLE_MEMBER_ROLES)}"
        )

    identity = external_id or (email or "").strip() or None
    if not identity:
        raise InvalidProvisioningRequestError("An email or external_id is required.")

    if password is not None:
        complexity_error = validate_password_complexity(password)
        if complexity_error:
            raise InvalidProvisioningRequestError(f"Password rejected: {complexity_error}")

    if role == "owner":
        stats = await OrganizationRepository(pool).get_stats(organization_id)
        if stats["owner_count"] > 0:
            raise OrganizationAlreadyHasOwnerError(
                "This organization already has an owner. Transfer ownership from the "
                "current owner, or add this person as an admin."
            )

    user_repo = UserRepository(pool)
    existing = await user_repo.get_by_external_id(identity, "local")
    if existing is not None:
        raise FirstUserConflictError(
            f"A user with external_id '{identity}' already exists "
            f"(organization {existing.get('organization_id')})."
        )

    user = await user_repo.create(
        external_id=identity,
        provider="local",
        organization_id=organization_id,
        email=email,
        display_name=display_name or email,
        role=role,
    )

    temporary_password: str | None = None
    if password is None:
        temporary_password = generate_temporary_password()
        await set_user_password(pool, user["id"], temporary_password, force_change=True)
    else:
        await set_user_password(pool, user["id"], password)

    refreshed = await user_repo.update_role(user["id"], role)
    return (refreshed or user), temporary_password


async def mint_api_key(
    pool: asyncpg.Pool,
    *,
    user_id: UUID | str,
    organization_id: UUID | str,
    name: str = DEFAULT_API_KEY_NAME,
) -> dict[str, Any]:
    """Mint an API key for a member of any organization.

    Returns ``{"record": ..., "plaintext": ...}``. The plaintext is only
    available here — only its bcrypt hash is stored.
    """
    repo = ApiKeyRepository(pool)
    record, plaintext = await repo.create(
        user_id=UUID(str(user_id)),
        organization_id=UUID(str(organization_id)),
        name=name,
    )
    return {"record": record, "plaintext": plaintext}


async def provision_organization(
    pool: asyncpg.Pool,
    *,
    name: str,
    admin_email: str | None = None,
    admin_display_name: str | None = None,
    admin_password: str | None = None,
    admin_role: str = "owner",
    admin_external_id: str | None = None,
    api_key_name: str | None = DEFAULT_API_KEY_NAME,
    model_id: str | None = None,
    model_provider: str | None = None,
    model_backend: str | None = None,
    providers: list[dict[str, str]] | None = None,
    backends: list[dict[str, Any]] | None = None,
    settings: dict[str, Any] | None = None,
    operator_org_id: UUID | str | None = None,
    operator_user_id: UUID | str | None = None,
) -> ProvisionedOrganization:
    """Create a complete, immediately usable organization.

    Args:
        pool: Database pool.
        name: Unique organization name.
        admin_email: First member's email / login.
        admin_display_name: First member's display name.
        admin_password: Optional password; a temporary one is generated if not.
        admin_role: ``owner`` (default) or ``admin``.
        admin_external_id: Override the identity id (defaults to the email).
        api_key_name: Name for the minted key, or ``None`` to skip minting.
        model_id: Model to enable + grant if the tenant has none usable yet.
        model_provider: Provider for ``model_id`` when it is not already in the
            shared catalogue. Defaults to the first configured provider.
        model_backend: Name of an entry in ``backends`` to bind ``model_id`` to.
            Needed when an instance offers several gateways and the model must
            run through a specific one.
        providers: Provider API keys to store for the tenant, as
            ``[{"provider": "openai", "api_key": "..."}]``. Stored encrypted at
            the canonical provider secret so the engines can use them.
        backends: Engine backends (a gateway, a pinned endpoint) to create for the
            tenant, as
            ``[{"name": ..., "engine": "langchain", "provider": "custom",
            "config": {"base_url": ...}, "secrets": {"api_key": ...}}]``.
            Created **before** models are bound and before discovery runs, since
            both need the backend to exist. A per-tenant gateway key belongs in
            ``secrets.api_key`` here, not in ``providers``: that is what makes
            each organization's traffic carry its own credential.
        settings: Runtime settings to apply, keyed by setting key. An invalid key
            raises ``InvalidProvisioningRequest`` so a tenant is never left
            half-configured.
        operator_org_id: Organization to attribute the audit trail to. When
            omitted the new organization is used (self-service signup).
        operator_user_id: Acting operator, for the audit trail.

    Raises:
        OrganizationNameTaken: If ``name`` already exists.
        InvalidProvisioningRequest: On validation failures.
        FirstUserConflict: If the first member's identity already exists.
    """
    from lucent.db.organization import OrganizationNameConflictError

    cleaned_name = (name or "").strip()
    if not cleaned_name:
        raise InvalidProvisioningRequestError("Organization name is required.")
    if len(cleaned_name) > 255:
        raise InvalidProvisioningRequestError("Organization name must be 255 characters or fewer.")

    org_repo = OrganizationRepository(pool)
    try:
        org = await org_repo.create(cleaned_name)
    except OrganizationNameConflictError as exc:
        raise OrganizationNameTakenError(f"Organization '{cleaned_name}' already exists.") from exc

    org_id = org["id"]
    result = ProvisionedOrganization(organization=org, user={})

    try:
        user, temporary_password = await create_first_user(
            pool,
            organization_id=org_id,
            role=admin_role,
            email=admin_email,
            display_name=admin_display_name,
            external_id=admin_external_id,
            password=admin_password,
        )
    except Exception as exc:
        # Do not leave a headless tenant behind: an org with no human member is
        # invisible to every UI and can only be cleaned up by hand.
        try:
            await org_repo.delete(org_id)
        except Exception:  # noqa: BLE001 — best effort, the original error matters
            logger.warning("Failed to roll back empty org %s", org_id, exc_info=True)
        raise exc from None

    result.user = user
    result.temporary_password = temporary_password

    if api_key_name:
        try:
            minted = await mint_api_key(
                pool,
                user_id=user["id"],
                organization_id=org_id,
                name=api_key_name,
            )
            result.api_key = minted["plaintext"]
        except Exception as exc:  # noqa: BLE001 — a key failure must not orphan the org
            result.warnings.append(f"API key could not be minted: {exc}")
            logger.warning("Failed to mint API key for org %s", org_id, exc_info=True)

    try:
        from lucent.builtin_definitions import sync_built_in_definitions_for_org

        result.seeded = await sync_built_in_definitions_for_org(pool, str(org_id))
    except Exception as exc:  # noqa: BLE001
        result.warnings.append(f"Built-in definitions were not seeded: {exc}")
        logger.warning("Failed to seed built-ins for org %s", org_id, exc_info=True)

    # Backends before credentials, models, and discovery: all three can depend on
    # one. A gateway backend is the credential for its provider, and discovery
    # fans out over enabled ``custom`` backends to learn what the gateway serves —
    # so creating them last would leave a provisioned tenant with a configured
    # gateway and no models behind it.
    backend_ids: dict[str, str] = {}
    for entry in backends or []:
        backend_name = str(entry.get("name") or "").strip()
        if not backend_name:
            result.warnings.append("A backend entry was skipped: name is required.")
            continue
        try:
            from lucent.services import org_providers as provider_service

            created = await provider_service.create_backend(
                pool,
                org_id,
                name=backend_name,
                engine=str(entry.get("engine") or "langchain"),
                provider=str(entry.get("provider") or ""),
                config=dict(entry.get("config") or {}),
                secrets=dict(entry.get("secrets") or {}),
                is_enabled=bool(entry.get("is_enabled", True)),
            )
            backend_ids[backend_name] = created["id"]
            result.backends_created.append(backend_name)
        except Exception as exc:  # noqa: BLE001 — record, do not orphan the org
            result.warnings.append(f"Backend {backend_name} was not created: {exc}")
            logger.warning(
                "Failed to create backend %s for org %s",
                backend_name,
                org_id,
                exc_info=True,
            )

    for entry in providers or []:
        provider_key = str(entry.get("provider") or "").strip()
        api_key = str(entry.get("api_key") or "").strip()
        if not provider_key or not api_key:
            continue
        try:
            from lucent.services import org_providers as provider_service

            await provider_service.set_credential(
                pool, org_id, provider=provider_key, value=api_key
            )
            result.providers_configured.append(provider_key)
        except Exception as exc:  # noqa: BLE001 — record, do not orphan the org
            result.warnings.append(
                f"Provider {provider_key} was not configured: {exc}"
            )
            logger.warning(
                "Failed to store %s credential for org %s", provider_key, org_id, exc_info=True
            )

    # A stored credential is not yet a usable model: discovery is what turns
    # "the tenant has an OpenAI key" into "the tenant can pick gpt-5.4". Without
    # this step a provisioned tenant looks configured while having an empty model
    # list, which is exactly the half-done state the create form exists to avoid.
    #
    # A ``custom`` backend counts as a credential source even though it stores no
    # provider credential: the gateway's key lives on the backend itself, and the
    # catalog it serves is whatever the gateway exposes. Without including it here
    # a provisioned gateway tenant would have zero models despite a working route.
    discovery_providers = list(result.providers_configured)
    if any(
        str(entry.get("provider") or "").strip().lower() == "custom"
        for entry in backends or []
    ):
        discovery_providers.append("custom")
    if discovery_providers:
        try:
            from lucent.services import org_providers as provider_service

            discovered = await provider_service.discover_models(
                pool, org_id, providers=discovery_providers
            )
            # ``upserted_count`` is the number of models actually registered;
            # ``created`` does not exist in the sync summary, so reading it would
            # silently always report zero.
            result.models_discovered = int(
                discovered.get("upserted_count")
                or discovered.get("created")
                or 0
            )
            # Discovery does NOT raise for a per-provider failure — it records the
            # error in the summary and moves on. Treating only exceptions as
            # failure meant an unreachable gateway produced a tenant with no
            # models and a clean-looking result, which is the half-configured
            # state this whole block exists to prevent.
            for failed in discovered.get("errors") or []:
                result.warnings.append(
                    f"Model discovery failed for {failed.get('provider')}: "
                    f"{failed.get('error')}"
                )
        except Exception as exc:  # noqa: BLE001 — credentials are still stored
            result.warnings.append(f"Model discovery did not run: {exc}")
            logger.warning(
                "Failed to discover models for org %s", org_id, exc_info=True
            )

    if settings:
        from lucent.services import org_settings as instance_settings

        for key, value in settings.items():
            try:
                applied = await instance_settings.update_setting(
                    pool, org_id, key, value=value, actor_user_id=user["id"]
                )
                result.settings_applied[key] = applied["value"]
            except Exception as exc:  # noqa: BLE001
                # Settings are validated up-front by the caller, so a failure
                # here is a real problem worth surfacing rather than skipping.
                result.warnings.append(f"Setting {key} was not applied: {exc}")
                logger.warning(
                    "Failed to apply setting %s to org %s", key, org_id, exc_info=True
                )

    if model_id:
        try:
            from lucent.services import org_providers as provider_service

            existing = await ModelRepository(pool).get_model(model_id)
            # ``models.id`` is a global primary key, so the same id can already
            # exist as a *shared* row from a different source (discovery of
            # another provider). Two cases must be kept apart:
            #
            #   * a shared row and no backend requested — clone it into the tenant
            #     (granting alone would leave it instance-wide);
            #   * a backend requested — the caller named the route this model must
            #     run through, so the tenant needs its OWN row bound to it. Cloning
            #     a shared row would silently drop the binding and route the model
            #     wherever the shared backend points.
            already_tenants = (
                existing is not None
                and str(existing.get("organization_id") or "") == str(org_id)
            )
            if existing is None or (model_backend and not already_tenants):
                # The model exists only behind the gateway (its ids are whatever
                # the gateway serves), or it exists as another source's shared
                # row. Define it for this tenant, bound to the requested backend,
                # which is what makes it reachable at all.
                created = await provider_service.create_model(
                    pool,
                    org_id,
                    model_id=model_id,
                    provider=(model_provider or "").strip().lower()
                    or (
                        result.providers_configured[0]
                        if result.providers_configured
                        else "custom"
                    ),
                    name=model_id,
                    engine="langchain",
                    backend_id=backend_ids.get(model_backend) if model_backend else None,
                    granted_by=user["id"],
                    # A gateway model id can collide with an unrelated shared
                    # catalogue row of the same name; give the tenant its own
                    # suffixed row rather than cloning the shared one, which
                    # would drop this binding.
                    scope_id_on_collision=True,
                )
                # create_model returns the model row, whose primary key is ``id``
                # (the model id string), not ``model_id``.
                result.model_id = created.get("id") or created.get("model_id")
            else:
                # Clones a shared registry row into the tenant rather than
                # promoting it: ``models.id`` is the primary key, so promoting
                # would take the model away from every other tenant. The tenant's
                # own owner is used as grantor because an operator is not a
                # member of the tenant.
                granted = await provider_service.grant_model_to_org(
                    pool, org_id, model_id, granted_by=user["id"]
                )
                result.model_id = granted["model_id"]
        except Exception as exc:  # noqa: BLE001
            result.warnings.append(f"Model was not enabled: {exc}")
            logger.warning("Failed to enable model for org %s", org_id, exc_info=True)

        # Enabling and granting a model makes it *usable*; it does not make it
        # the one the tenant starts with. Without this the new workspace would
        # fall back to the instance default model — which the tenant probably
        # has no credential for — so a freshly provisioned org could log in and
        # still fail its first chat. An explicit settings entry wins, since the
        # caller said what it wanted.
        if result.model_id and "models.default_model" not in (settings or {}):
            try:
                from lucent.services import org_settings as instance_settings

                applied = await instance_settings.update_setting(
                    pool, org_id, "models.default_model", value=result.model_id
                )
                result.settings_applied["models.default_model"] = applied["value"]
            except Exception as exc:  # noqa: BLE001 — the model is still usable
                result.warnings.append(f"Default model was not set: {exc}")
                logger.warning(
                    "Failed to set default model for org %s", org_id, exc_info=True
                )

    await log_instance_event(
        pool,
        operator_org_id=operator_org_id or org_id,
        operator_user_id=operator_user_id or user["id"],
        action="org.provision",
        entity_type="organization",
        entity_id=org_id,
        entity_label=cleaned_name,
        target_org_id=org_id,
        changed_fields=["organization", "first_user", "api_key"],
        new_values={
            "name": cleaned_name,
            "first_user_role": admin_role,
            "first_user_email": admin_email,
            "api_key_minted": bool(result.api_key),
            "model_id": result.model_id,
        },
        notes=(
            "Organization provisioned with its first "
            f"{admin_role} and a default API key."
        ),
    )

    logger.info(
        "Provisioned organization %s (%s) with first user %s",
        cleaned_name,
        org_id,
        user["id"],
    )
    return result


async def delete_organization(
    pool: asyncpg.Pool,
    *,
    organization_id: UUID | str,
    operator_org_id: UUID | str,
    operator_user_id: UUID | str | None = None,
    confirm_name: str | None = None,
) -> dict[str, Any]:
    """Permanently delete a tenant, recording the event before it disappears.

    The audit row is written to the operator's organization first, because the
    target's own audit rows cascade away with it.

    Args:
        confirm_name: Must equal the organization's current name. This is the
            guard against deleting the wrong tenant from a stale UI.

    Raises:
        InvalidProvisioningRequest: If ``confirm_name`` does not match.
    """
    org_repo = OrganizationRepository(pool)
    org = await org_repo.get_by_id(UUID(str(organization_id)))
    if org is None:
        return {"deleted": False, "reason": "not_found"}

    if confirm_name is not None and confirm_name != org["name"]:
        raise InvalidProvisioningRequestError(
            "Confirmation name does not match; refusing to delete."
        )

    from lucent.builtin_definitions import SYSTEM_ORG_NAME

    if org["name"] == SYSTEM_ORG_NAME:
        raise InvalidProvisioningRequestError(
            "The internal system organization cannot be deleted."
        )

    stats = await org_repo.get_stats(org["id"])

    await log_instance_event(
        pool,
        operator_org_id=operator_org_id,
        operator_user_id=operator_user_id,
        action="org.delete",
        entity_type="organization",
        entity_id=org["id"],
        entity_label=org["name"],
        # Recorded on the operator's row so the trail still says *which* tenant
        # was destroyed after the tenant's own rows are gone. The mirror write
        # into the tenant succeeds here and is then removed by the cascade —
        # which is exactly the intent: the operator's copy is the durable one.
        target_org_id=org["id"],
        changed_fields=["organization"],
        old_values={"name": org["name"], "status": org["status"], **stats},
        notes="Organization permanently deleted by an instance operator.",
    )

    deleted = await org_repo.delete(org["id"])
    logger.info("Deleted organization %s (%s)", org["name"], org["id"])
    return {"deleted": deleted, "name": org["name"], "stats": stats}
