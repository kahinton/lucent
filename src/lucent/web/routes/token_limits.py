"""Token limit administration (web UI).

Server-rendered like the rest of ``/settings``. The page has two layers:

* **Policies** — a budget definition: which model (or all models), a rolling
  window, three optional limits, and how the pool is shared.
* **Assignments** — which principals the policy applies to (org, group, person).
  A policy can carry several, which is how "everyone gets 5M/day, but
  engineering gets 50M/day" is expressed: two policies, both applicable, and the
  tightest one wins per request.

Every write invalidates the enforcement cache. The daemon runs in a separate
process, so it picks changes up when its TTL expires; the page says so rather
than implying an immediate global effect.
"""

from __future__ import annotations

import logging
from urllib.parse import quote_plus
from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from lucent.db import AdminAuditRepository, GroupRepository, UserRepository, get_pool
from lucent.db import admin_audit as audit_actions
from lucent.db.models import ModelRepository
from lucent.db.token_quota import (
    ALL_MODELS,
    PRINCIPAL_GROUP,
    PRINCIPAL_ORG,
    PRINCIPAL_TYPES,
    PRINCIPAL_USER,
    SCOPE_MODES,
    TokenQuotaRepository,
)
from lucent.rbac import is_admin_role

from ._shared import _check_csrf, get_user_context, templates

logger = logging.getLogger(__name__)

router = APIRouter()

# Preset windows offered in the UI. Anything else can be set through the API.
WINDOW_PRESETS: tuple[tuple[int, str], ...] = (
    (3600, "Hour"),
    (21600, "6 hours"),
    (86400, "Day"),
    (604800, "Week"),
    (2592000, "30 days"),
)

MAX_NAME_LENGTH = 128
MAX_DESCRIPTION_LENGTH = 1000
# A policy row with every dimension unbounded is rejected by the schema; refuse
# it here too so the admin gets a message instead of a database error.
MIN_WINDOW_SECONDS = 60
MAX_WINDOW_SECONDS = 365 * 24 * 3600


async def _require_admin(request: Request):
    user = await get_user_context(request)
    role_val = user.role.value if hasattr(user.role, "value") else str(user.role)
    if not is_admin_role(role_val):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def _redirect(
    success: str | None = None,
    error: str | None = None,
    *,
    anchor: str | None = None,
) -> RedirectResponse:
    params: list[str] = []
    if success:
        params.append(f"success={quote_plus(success)}")
    if error:
        params.append(f"error={quote_plus(error)}")
    suffix = f"?{'&'.join(params)}" if params else ""
    fragment = f"#{anchor}" if anchor else ""
    return RedirectResponse(
        url=f"/settings/token-limits{suffix}{fragment}", status_code=303
    )


def _parse_limit(raw: str | None, label: str) -> tuple[int | None, str | None]:
    """Parse one limit field.

    An empty value means *unbounded* — the deliberate third state beyond a
    number — so it must not collapse to zero. Accepts thousands separators
    because the UI shows them.
    """
    text = str(raw or "").strip().replace(",", "").replace("_", "")
    if not text:
        return None, None
    try:
        value = int(text)
    except ValueError:
        return None, f"{label} must be a whole number of tokens, or blank for no limit"
    if value < 0:
        return None, f"{label} cannot be negative"
    return value, None


def _parse_window(raw: str | None) -> tuple[int | None, str | None]:
    text = str(raw or "").strip()
    if not text:
        return None, "A window is required"
    try:
        value = int(text)
    except ValueError:
        return None, "Pick a window from the list"
    if not MIN_WINDOW_SECONDS <= value <= MAX_WINDOW_SECONDS:
        return None, "Window must be between 1 minute and 365 days"
    return value, None


def _parse_model_ids(
    raw_values: list[str] | None, *, known_model_ids: set[str]
) -> tuple[list[str], list[str]]:
    """Normalize the submitted model selection; returns (model_ids, errors).

    The form offers a checklist plus an "all models" option. Picking nothing is
    a mistake rather than "all", so it is reported instead of silently
    expanding to the widest possible scope. Unknown ids are rejected: a policy
    scoped to a typo would look configured while matching nothing.
    """
    values = [str(value).strip() for value in (raw_values or []) if str(value).strip()]
    if not values:
        return [], ["Choose at least one model"]
    if ALL_MODELS in values:
        # "All models" supersedes any individual picks; the checkbox group sends
        # only this value, so this also repairs a stale client posting both.
        return [ALL_MODELS], []
    deduped: list[str] = []
    for value in values:
        if value not in deduped:
            deduped.append(value)
    unknown = [value for value in deduped if value not in known_model_ids]
    if unknown:
        return [], [f"Unknown model: {unknown[0]}"]
    return deduped, []


def _validate_policy_form(
    *,
    name: str,
    model_ids: list[str] | None,
    known_model_ids: set[str],
    scope_mode: str,
    window_seconds: str | None,
    raw_input: str | None,
    cached_input: str | None,
    output: str | None,
) -> tuple[dict[str, object], list[str]]:
    """Normalize and validate a policy form; returns (fields, errors)."""
    errors: list[str] = []
    cleaned_name = str(name or "").strip()
    if not cleaned_name:
        errors.append("Name is required")
    elif len(cleaned_name) > MAX_NAME_LENGTH:
        errors.append(f"Name must be {MAX_NAME_LENGTH} characters or fewer")

    cleaned_models, model_errors = _parse_model_ids(
        model_ids, known_model_ids=known_model_ids
    )
    errors.extend(model_errors)

    cleaned_scope = str(scope_mode or "").strip()
    if cleaned_scope not in SCOPE_MODES:
        errors.append("Choose whether the limit is shared or per person")

    window, window_error = _parse_window(window_seconds)
    if window_error:
        errors.append(window_error)

    raw_value, raw_error = _parse_limit(raw_input, "Input token limit")
    cached_value, cached_error = _parse_limit(cached_input, "Cached input token limit")
    output_value, output_error = _parse_limit(output, "Output token limit")
    for error in (raw_error, cached_error, output_error):
        if error:
            errors.append(error)

    if (
        raw_error is None
        and cached_error is None
        and output_error is None
        and raw_value is None
        and cached_value is None
        and output_value is None
    ):
        errors.append(
            "Set at least one limit — a policy with no limits would not do anything"
        )

    return (
        {
            "name": cleaned_name,
            "model_ids": cleaned_models,
            "scope_mode": cleaned_scope,
            "window_seconds": window,
            "raw_input_tokens_limit": raw_value,
            "cached_input_tokens_limit": cached_value,
            "output_tokens_limit": output_value,
        },
        errors,
    )


def _model_scope_label(model_ids: list[str] | tuple[str, ...], model_names: dict[str, str]) -> str:
    """Human summary of a policy's model scope, for the policy cards."""
    models = [str(model) for model in (model_ids or [])]
    if ALL_MODELS in models:
        return "All models"
    if not models:
        return "No models"
    if len(models) == 1:
        return model_names.get(models[0], models[0])
    if len(models) <= 3:
        return ", ".join(model_names.get(model, model) for model in models)
    names = ", ".join(model_names.get(model, model) for model in models[:2])
    return f"{names} +{len(models) - 2} more"


async def _known_model_ids(pool, org_id: str) -> set[str]:
    """Every model id this organization can see.

    Used to reject a policy scoped to a model that does not exist — a typo would
    otherwise produce a policy that looks configured but matches nothing.
    """
    models = (await ModelRepository(pool).list_models(limit=500, org_id=org_id))["items"]
    return {str(model["id"]) for model in models}


async def _models_and_principals(pool, org_id: str) -> dict[str, object]:
    """Everything the page needs to render its pickers."""
    models = (
        await ModelRepository(pool).list_models(limit=500, org_id=org_id)
    )["items"]
    groups = (await GroupRepository(pool).list_groups(org_id, limit=1000))["items"]
    users = await UserRepository(pool).get_by_organization(UUID(org_id))
    return {
        "models": sorted(
            models,
            key=lambda m: (str(m.get("provider") or ""), str(m.get("name") or "")),
        ),
        "groups": groups,
        "users": users,
    }


@router.get("/token-limits", response_class=HTMLResponse)
async def token_limits_page(request: Request):
    """The token limits settings page: policies, assignments, consumption."""
    user = await _require_admin(request)
    pool = await get_pool()
    org_id = str(user.organization_id)
    repo = TokenQuotaRepository(pool)

    policies = await repo.list_policies(org_id)
    lookups = await _models_and_principals(pool, org_id)
    model_names = {str(m["id"]): m.get("name") or m["id"] for m in lookups["models"]}

    try:
        consumption = await repo.consumption_report(org_id, limit=40)
    except Exception:
        # Consumption is reporting only; a failure must not hide the policies.
        logger.warning("Token limit consumption report failed", exc_info=True)
        consumption = []

    from lucent.settings import token_limits_cache_ttl_seconds, token_limits_enabled

    for policy in policies:
        policy["model_label"] = _model_scope_label(policy["model_ids"], model_names)

    # Label the consumption rows here rather than calling the helper from Jinja:
    # template globals are registered once at import, and this keeps the label
    # logic in one Python place.
    for row in consumption:
        row["model_label"] = _model_scope_label(row.get("model_ids") or [], model_names)

    return templates.TemplateResponse(
        request,
        "token_limits.html",
        {
            "user": user,
            "policies": policies,
            "consumption": consumption,
            "models": lookups["models"],
            "groups": lookups["groups"],
            "users": lookups["users"],
            "window_presets": WINDOW_PRESETS,
            "all_models": ALL_MODELS,
            "model_names": model_names,
            "success": request.query_params.get("success"),
            "error": request.query_params.get("error"),
            "enforcement_enabled": token_limits_enabled(organization_id=org_id),
            "cache_ttl_seconds": token_limits_cache_ttl_seconds(organization_id=org_id),
            "csrf_token": request.cookies.get("lucent_csrf", ""),
        },
    )


@router.post("/token-limits/create")
async def create_policy(
    request: Request,
    name: str = Form(...),
    model_id: list[str] = Form(default=[ALL_MODELS]),
    scope_mode: str = Form("individual"),
    window_seconds: str = Form("86400"),
    raw_input_tokens_limit: str = Form(""),
    cached_input_tokens_limit: str = Form(""),
    output_tokens_limit: str = Form(""),
    description: str = Form(""),
    assign_to: str = Form("org"),
    assign_principal_id: str = Form(""),
):
    """Create a policy and, in the same submit, apply it to a principal.

    Creating and assigning in one step is deliberate: a policy with no
    assignment enforces nothing, and leaving admins to do it in two passes would
    reliably produce dead policies.
    """
    await _check_csrf(request)
    user = await _require_admin(request)
    pool = await get_pool()
    org_id = str(user.organization_id)

    fields, errors = _validate_policy_form(
        name=name,
        model_ids=model_id,
        known_model_ids=set(await _known_model_ids(pool, org_id)),
        scope_mode=scope_mode,
        window_seconds=window_seconds,
        raw_input=raw_input_tokens_limit,
        cached_input=cached_input_tokens_limit,
        output=output_tokens_limit,
    )
    if errors:
        return _redirect(error="; ".join(errors))

    principal_type, principal_id, principal_error = await _resolve_principal(
        pool, org_id, assign_to, assign_principal_id
    )
    if principal_error:
        return _redirect(error=principal_error)

    repo = TokenQuotaRepository(pool)
    try:
        policy = await repo.create_policy(
            org_id,
            model_ids=list(fields["model_ids"]),  # type: ignore[arg-type]
            name=str(fields["name"]),
            description=str(description or "").strip()[:MAX_DESCRIPTION_LENGTH],
            window_seconds=int(fields["window_seconds"] or 86400),
            scope_mode=str(fields["scope_mode"]),
            raw_input_tokens_limit=fields["raw_input_tokens_limit"],  # type: ignore[arg-type]
            cached_input_tokens_limit=fields["cached_input_tokens_limit"],  # type: ignore[arg-type]
            output_tokens_limit=fields["output_tokens_limit"],  # type: ignore[arg-type]
            created_by=user.id,
        )
    except Exception as exc:
        logger.warning("Token limit policy creation failed", exc_info=True)
        if "unique" in str(exc).lower():
            return _redirect(
                error=f"A policy named '{fields['name']}' already exists for this model"
            )
        return _redirect(error="Could not create the policy")

    if principal_type:
        await repo.assign(
            policy["id"],
            organization_id=org_id,
            principal_type=principal_type,
            principal_id=principal_id,
        )

    await _invalidate_cache(org_id)
    await AdminAuditRepository(pool).log_for_user(
        user,
        request,
        action=audit_actions.TOKEN_LIMIT_CREATE,
        entity_type="token_quota_policy",
        entity_id=policy["id"],
        entity_label=str(fields["name"]),
        new_values={
            "model_ids": fields["model_ids"],
            "scope_mode": fields["scope_mode"],
            "window_seconds": fields["window_seconds"],
            "raw_input_tokens_limit": fields["raw_input_tokens_limit"],
            "cached_input_tokens_limit": fields["cached_input_tokens_limit"],
            "output_tokens_limit": fields["output_tokens_limit"],
            "assign_to": principal_type,
        },
    )
    return _redirect(success=f"Policy '{fields['name']}' created")


async def _resolve_principal(
    pool, org_id: str, assign_to: str, assign_principal_id: str
) -> tuple[str | None, str | None, str | None]:
    """Validate the submitted principal against this organization.

    Re-reads the row rather than trusting the form: an id from another
    organization must never be assignable, and the check is cheap.
    """
    principal_type = str(assign_to or "").strip().lower()
    if not principal_type or principal_type == "none":
        return None, None, None
    if principal_type not in PRINCIPAL_TYPES:
        return None, None, "Choose a valid target for the policy"

    if principal_type == PRINCIPAL_ORG:
        return PRINCIPAL_ORG, None, None

    raw_id = str(assign_principal_id or "").strip()
    if not raw_id:
        return None, None, "Pick a group or person for the policy"
    try:
        principal_uuid = UUID(raw_id)
    except ValueError:
        return None, None, "Pick a group or person for the policy"

    if principal_type == PRINCIPAL_GROUP:
        group = await GroupRepository(pool).get_group(str(principal_uuid), org_id)
        if not group:
            return None, None, "That group is not in this organization"
        return PRINCIPAL_GROUP, str(principal_uuid), None

    users = await UserRepository(pool).get_by_organization(UUID(org_id))
    if not any(str(u["id"]) == str(principal_uuid) for u in users):
        return None, None, "That person is not in this organization"
    return PRINCIPAL_USER, str(principal_uuid), None


@router.post("/token-limits/{policy_id}/update")
async def update_policy(
    request: Request,
    policy_id: str,
    name: str = Form(...),
    model_id: list[str] = Form(default=[ALL_MODELS]),
    scope_mode: str = Form("individual"),
    window_seconds: str = Form("86400"),
    raw_input_tokens_limit: str = Form(""),
    cached_input_tokens_limit: str = Form(""),
    output_tokens_limit: str = Form(""),
    description: str = Form(""),
    is_enabled: str = Form("on"),
):
    await _check_csrf(request)
    user = await _require_admin(request)
    pool = await get_pool()
    org_id = str(user.organization_id)

    fields, errors = _validate_policy_form(
        name=name,
        model_ids=model_id,
        known_model_ids=set(await _known_model_ids(pool, org_id)),
        scope_mode=scope_mode,
        window_seconds=window_seconds,
        raw_input=raw_input_tokens_limit,
        cached_input=cached_input_tokens_limit,
        output=output_tokens_limit,
    )
    if errors:
        return _redirect(error="; ".join(errors))

    repo = TokenQuotaRepository(pool)
    existing = await repo.get_policy(policy_id, organization_id=org_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Policy not found")

    updated = await repo.update_policy(
        policy_id,
        organization_id=org_id,
        name=fields["name"],
        description=str(description or "").strip()[:MAX_DESCRIPTION_LENGTH],
        model_ids=fields["model_ids"],
        window_seconds=fields["window_seconds"],
        scope_mode=fields["scope_mode"],
        raw_input_tokens_limit=fields["raw_input_tokens_limit"],
        cached_input_tokens_limit=fields["cached_input_tokens_limit"],
        output_tokens_limit=fields["output_tokens_limit"],
        is_enabled=str(is_enabled).strip().lower() in ("on", "true", "1", "yes"),
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Policy not found")

    await _invalidate_cache(org_id)
    await AdminAuditRepository(pool).log_for_user(
        user,
        request,
        action=audit_actions.TOKEN_LIMIT_UPDATE,
        entity_type="token_quota_policy",
        entity_id=policy_id,
        entity_label=str(fields["name"]),
        old_values={
            "name": existing["name"],
            "model_ids": existing["model_ids"],
            "scope_mode": existing["scope_mode"],
            "window_seconds": existing["window_seconds"],
            "raw_input_tokens_limit": existing["raw_input_tokens_limit"],
            "cached_input_tokens_limit": existing["cached_input_tokens_limit"],
            "output_tokens_limit": existing["output_tokens_limit"],
            "is_enabled": existing["is_enabled"],
        },
        new_values={**updated, "is_enabled": updated["is_enabled"]},
    )
    return _redirect(success=f"Policy '{fields['name']}' saved")


@router.post("/token-limits/{policy_id}/toggle")
async def toggle_policy(request: Request, policy_id: str):
    await _check_csrf(request)
    user = await _require_admin(request)
    pool = await get_pool()
    org_id = str(user.organization_id)
    repo = TokenQuotaRepository(pool)
    existing = await repo.get_policy(policy_id, organization_id=org_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Policy not found")
    updated = await repo.update_policy(
        policy_id, organization_id=org_id, is_enabled=not existing["is_enabled"]
    )
    await _invalidate_cache(org_id)
    await AdminAuditRepository(pool).log_for_user(
        user,
        request,
        action=audit_actions.TOKEN_LIMIT_UPDATE,
        entity_type="token_quota_policy",
        entity_id=policy_id,
        entity_label=existing["name"],
        changed_fields=["is_enabled"],
        old_values={"is_enabled": existing["is_enabled"]},
        new_values={"is_enabled": (updated or {}).get("is_enabled")},
    )
    state = "enabled" if (updated or {}).get("is_enabled") else "disabled"
    return _redirect(success=f"Policy '{existing['name']}' {state}")


@router.post("/token-limits/{policy_id}/delete")
async def delete_policy(request: Request, policy_id: str):
    await _check_csrf(request)
    user = await _require_admin(request)
    pool = await get_pool()
    org_id = str(user.organization_id)
    repo = TokenQuotaRepository(pool)
    existing = await repo.get_policy(policy_id, organization_id=org_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Policy not found")
    if not await repo.delete_policy(policy_id, organization_id=org_id):
        raise HTTPException(status_code=404, detail="Policy not found")
    await _invalidate_cache(org_id)
    await AdminAuditRepository(pool).log_for_user(
        user,
        request,
        action=audit_actions.TOKEN_LIMIT_DELETE,
        entity_type="token_quota_policy",
        entity_id=policy_id,
        entity_label=existing["name"],
        old_values=existing,
    )
    return _redirect(success=f"Policy '{existing['name']}' deleted")


@router.post("/token-limits/{policy_id}/assign")
async def assign_policy(
    request: Request,
    policy_id: str,
    principal_type: str = Form(...),
    principal_id: str = Form(""),
):
    await _check_csrf(request)
    user = await _require_admin(request)
    pool = await get_pool()
    org_id = str(user.organization_id)
    repo = TokenQuotaRepository(pool)
    policy = await repo.get_policy(policy_id, organization_id=org_id)
    if not policy:
        raise HTTPException(status_code=404, detail="Policy not found")

    resolved_type, resolved_id, error = await _resolve_principal(
        pool, org_id, principal_type, principal_id
    )
    if error or not resolved_type:
        return _redirect(error=error or "Choose a valid target")

    await repo.assign(
        policy_id,
        organization_id=org_id,
        principal_type=resolved_type,
        principal_id=resolved_id,
    )
    await _invalidate_cache(org_id)
    await AdminAuditRepository(pool).log_for_user(
        user,
        request,
        action=audit_actions.TOKEN_LIMIT_ASSIGN,
        entity_type="token_quota_policy",
        entity_id=policy_id,
        entity_label=policy["name"],
        changed_fields=["assignments"],
        new_values={"principal_type": resolved_type, "principal_id": resolved_id},
    )
    return _redirect(success=f"Applied '{policy['name']}' to the selected target")


@router.post("/token-limits/{policy_id}/assignments/{assignment_id}/remove")
async def unassign_policy(request: Request, policy_id: str, assignment_id: str):
    await _check_csrf(request)
    user = await _require_admin(request)
    pool = await get_pool()
    org_id = str(user.organization_id)
    repo = TokenQuotaRepository(pool)
    policy = await repo.get_policy(policy_id, organization_id=org_id)
    if not policy:
        raise HTTPException(status_code=404, detail="Policy not found")
    if not await repo.unassign(assignment_id, organization_id=org_id):
        return _redirect(error="That assignment no longer exists")
    await _invalidate_cache(org_id)
    await AdminAuditRepository(pool).log_for_user(
        user,
        request,
        action=audit_actions.TOKEN_LIMIT_UNASSIGN,
        entity_type="token_quota_policy",
        entity_id=policy_id,
        entity_label=policy["name"],
        changed_fields=["assignments"],
        old_values={"assignment_id": assignment_id},
    )
    return _redirect(success="Target removed from the policy")


async def _invalidate_cache(org_id: str) -> None:
    """Drop the enforcement cache for this organization after a write.

    In-process only: the daemon is a separate process and picks a change up when
    the policy cache TTL expires, which the page states explicitly.
    """
    try:
        from lucent.llm.token_quota import get_token_limit_service

        service = await get_token_limit_service()
        service.invalidate(org_id)
    except Exception:  # noqa: BLE001 - a cache miss must never fail the write
        logger.debug("Token limit cache invalidation failed", exc_info=True)
