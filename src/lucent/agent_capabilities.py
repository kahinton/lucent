"""Resolve which capabilities an agent actually carries for one user.

The agent is the structural hub for capability access. A skill, managed tool,
hook, or MCP server reaches a running agent in two steps:

1. it is attached to the agent (the ``agent_*`` junction tables), and
2. the requesting user may use it.

Step 2 is an *intersection*: a capability attached to a shared agent is only
visible to a user who can already read that capability. That is what makes a
shared agent multi-tenant safe — Alice's private "Pep Talk" skill attached to
"Super Duper Coder" never appears for Bob, because Bob cannot read it at all.

The intersection alone is not enough. A user who *can* read a capability could
still be handed it, by somebody else, inside an agent they trust. That is how a
malicious hook or managed tool would be injected into another person's agent.
So there is a third gate: per-user consent (``agent_capability_consents``).

A capability is therefore loaded only when all three hold:

* it is attached to the agent,
* the requester holds read clearance on it, and
* the requester consented — or is exempt.

Consent is not required for built-in capabilities, for the capability's own
owner (user or owning group), or for the user who attached it. Those are
resolved here rather than stored, so transferring a capability to a new owner
needs no consent backfill.

This module is deliberately import-safe from both the server (``lucent.*``) and
the daemon (which runs with ``PYTHONPATH=src``). It owns no policy of its own:
the SQL lives in :class:`lucent.db.definitions.DefinitionRepository`, and the
clearance filter lives in :class:`lucent.db.pool.AuthorizedDatabasePool`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from lucent.db.definitions import (
    CAPABILITY_SPECS,
    CAPABILITY_SPEC_BY_TYPE,
    CapabilitySpec,
    DefinitionRepository,
    get_authorized_definitions_pool,
    normalize_capability_type,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CAPABILITY_SPECS",
    "ResolvedCapabilities",
    "assert_capability_permitted",
    "resolve_agent_capabilities",
]


@dataclass
class ResolvedCapabilities:
    """The effective capability set for one (user, agent) pair.

    ``skills``/``managed_tools``/``hooks``/``mcp_servers`` are what the agent
    may actually use for this user. ``pending`` holds attached-but-undecided
    capabilities (the user should be prompted); ``declined`` holds capabilities
    the user rejected. Both are reported rather than silently dropped, so a
    missing capability is always explainable in the UI.
    """

    agent_id: str | None = None
    skills: list[dict[str, Any]] = field(default_factory=list)
    managed_tools: list[dict[str, Any]] = field(default_factory=list)
    hooks: list[dict[str, Any]] = field(default_factory=list)
    mcp_servers: list[dict[str, Any]] = field(default_factory=list)
    pending: list[dict[str, Any]] = field(default_factory=list)
    declined: list[dict[str, Any]] = field(default_factory=list)

    @property
    def skill_names(self) -> list[str]:
        return [str(item["name"]) for item in self.skills if item.get("name")]

    @property
    def managed_tool_names(self) -> list[str]:
        return [str(item["name"]) for item in self.managed_tools if item.get("name")]

    def as_loader_map(self) -> dict[str, list[dict[str, Any]]]:
        """Per-family lists, keyed by capability type (for generic callers)."""
        return {
            "skill": self.skills,
            "managed_tool": self.managed_tools,
            "hook": self.hooks,
            "mcp_server": self.mcp_servers,
        }


def _group_ids(user: dict[str, Any]) -> set[str]:
    raw = user.get("group_ids") or ()
    if isinstance(raw, str):
        raw = [part for part in raw.split(",") if part]
    return {str(group_id) for group_id in raw}


def _is_exempt(
    *,
    capability: dict[str, Any],
    granted_by: str | None,
    user_id: str,
    group_ids: set[str],
) -> bool:
    """Consent exemptions, evaluated at read time (never stored)."""
    if str(capability.get("scope") or "instance") == "built-in":
        return True
    if granted_by and str(granted_by) == user_id:
        return True
    owner_user_id = capability.get("owner_user_id")
    if owner_user_id and str(owner_user_id) == user_id:
        return True
    owner_group_id = capability.get("owner_group_id")
    if owner_group_id and str(owner_group_id) in group_ids:
        return True
    return False


def _family_getter(repository: DefinitionRepository, spec: CapabilitySpec):
    return {
        "skill": repository.get_agent_skills,
        "managed_tool": repository.get_agent_managed_tools,
        "hook": repository.get_agent_hooks,
        "mcp_server": repository.get_agent_mcp_servers,
    }[spec.capability_type]


def _summarize(
    spec: CapabilitySpec, capability: dict[str, Any], agent_id: str, agent_name: str | None,
) -> dict[str, Any]:
    return {
        "capability_type": spec.capability_type,
        "capability_id": str(capability.get("id")),
        "capability_name": capability.get("name"),
        "capability_description": capability.get("description"),
        "agent_id": agent_id,
        "agent_name": agent_name,
    }


async def resolve_agent_capabilities(
    *,
    pool: Any,
    user: dict[str, Any],
    agent_id: str | None,
    org_id: str,
    user_role: str | None = None,
) -> ResolvedCapabilities:
    """Resolve the capability chain for one user and one agent.

    ``user`` must carry ``id`` and ``organization_id``; ``group_ids`` is used
    for the owner-group consent exemption and is resolved by the authorized
    pool when absent.

    Fail-closed at every step: a missing identity, an unbuildable authorized
    pool, or a read error yields an *empty* capability set rather than an
    unfiltered one. Callers must never fall back to the unfiltered junction
    loaders on the strength of an error here.
    """
    resolved = ResolvedCapabilities(agent_id=agent_id)
    if not agent_id or not org_id:
        return resolved
    user_id = str(user.get("id") or "")
    if not user_id:
        logger.debug("Capability resolution skipped: no user identity")
        return resolved

    try:
        authorized_pool = await get_authorized_definitions_pool(
            pool, {"id": user_id, "organization_id": str(org_id)}
        )
    except Exception:
        logger.warning(
            "Capability resolution: could not build an authorized pool", exc_info=True
        )
        return resolved

    authorized_repo = DefinitionRepository(authorized_pool)
    plain_repo = DefinitionRepository(pool)

    try:
        consent = await plain_repo.list_capability_consents(agent_id, user_id, str(org_id))
    except Exception:
        # Fail closed: without a consent map, nothing outside the exemption set
        # may load. An empty map means "not yet asked", which is the safe read.
        logger.warning("Capability resolution: consent lookup failed", exc_info=True)
        consent = {spec.capability_type: {} for spec in CAPABILITY_SPECS}

    group_ids = _group_ids(user)
    if not group_ids:
        # The authorized pool already resolved the principal's groups; reuse
        # that rather than issuing a second membership query.
        group_ids = {str(group_id) for group_id in authorized_pool.principal.group_ids}

    agent_name: str | None = None
    try:
        agent_row = await authorized_repo.get_usable_agent(agent_id)
        if agent_row:
            agent_name = agent_row.get("name")
    except Exception:
        logger.debug("Capability resolution: agent name unavailable", exc_info=True)

    for spec in CAPABILITY_SPECS:
        try:
            accessible_ids = await authorized_repo.get_accessible_capability_ids(
                spec.capability_type, str(org_id)
            )
        except Exception:
            logger.warning(
                "Capability resolution: clearance lookup failed for %s",
                spec.capability_type,
                exc_info=True,
            )
            continue

        try:
            attached = await _family_getter(plain_repo, spec)(agent_id, str(org_id))
        except Exception:
            logger.warning(
                "Capability resolution: attachment lookup failed for %s",
                spec.capability_type,
                exc_info=True,
            )
            continue

        decisions = consent.get(spec.capability_type, {})
        bucket = resolved.as_loader_map()[spec.capability_type]
        for capability in attached:
            capability_id = str(capability.get("id"))
            # Gate 2: the requester must be able to read it at all.
            if capability_id not in accessible_ids:
                continue
            granted_by = capability.get("granted_by")
            if _is_exempt(
                capability=capability,
                granted_by=str(granted_by) if granted_by else None,
                user_id=user_id,
                group_ids=group_ids,
            ):
                bucket.append(capability)
                continue
            # Gate 3: explicit per-user consent.
            decision = decisions.get(capability_id)
            if decision == "approved":
                bucket.append(capability)
            elif decision == "declined":
                resolved.declined.append(
                    _summarize(spec, capability, agent_id, agent_name)
                )
            else:
                resolved.pending.append(
                    _summarize(spec, capability, agent_id, agent_name)
                )

    return resolved


async def assert_capability_permitted(
    *,
    pool: Any,
    user: dict[str, Any],
    agent_id: str,
    org_id: str,
    capability_type: str,
    capability_id: str,
    user_role: str | None = None,
) -> bool:
    """May this user use this specific capability inside this agent?

    Used on the on-demand paths — ``get_skill_definition`` and friends, and
    ``run_managed_tool`` — where the model names a capability by id instead of
    it being rendered into the prompt. Without this gate the consent model is
    decorative: a model holding an id could fetch a body it was never given.

    Deliberately narrower than :func:`resolve_agent_capabilities`: it inspects
    only the one capability family it was asked about, so a hot on-demand path
    costs one clearance query plus one attachment lookup instead of every
    family's.

    Returns ``False`` for anything not positively permitted. Unknown agent,
    unknown user, and unbuildable pools all deny.
    """
    try:
        spec = CAPABILITY_SPEC_BY_TYPE[normalize_capability_type(capability_type)]
    except ValueError:
        return False
    if not agent_id or not org_id or not capability_id:
        return False

    user_id = str(user.get("id") or "")
    if not user_id:
        return False

    try:
        authorized_pool = await get_authorized_definitions_pool(
            pool, {"id": user_id, "organization_id": str(org_id)}
        )
    except Exception:
        logger.warning(
            "Capability permission check: could not build an authorized pool",
            exc_info=True,
        )
        return False

    authorized_repo = DefinitionRepository(authorized_pool)
    plain_repo = DefinitionRepository(pool)

    # Gate 2: the requester must be able to read the capability at all.
    try:
        readable_ids = await authorized_repo.get_accessible_capability_ids(
            spec.capability_type, str(org_id)
        )
    except Exception:
        logger.warning(
            "Capability permission check: clearance lookup failed", exc_info=True
        )
        return False
    if str(capability_id) not in readable_ids:
        return False

    # Gate 1: it must actually be attached to this agent.
    try:
        attached = await _family_getter(plain_repo, spec)(agent_id, str(org_id))
    except Exception:
        logger.warning(
            "Capability permission check: attachment lookup failed", exc_info=True
        )
        return False
    capability = next(
        (item for item in attached if str(item.get("id")) == str(capability_id)), None
    )
    if capability is None:
        return False

    group_ids = set(_group_ids(user))
    if not group_ids:
        group_ids = {str(group_id) for group_id in authorized_pool.principal.group_ids}
    granted_by = capability.get("granted_by")
    if _is_exempt(
        capability=capability,
        granted_by=str(granted_by) if granted_by else None,
        user_id=user_id,
        group_ids=group_ids,
    ):
        return True

    # Gate 3: explicit per-user consent. A declined capability is a recorded
    # "no", so absence of an approved row denies.
    try:
        decisions = await plain_repo.list_capability_consents(
            agent_id, user_id, str(org_id)
        )
    except Exception:
        logger.warning(
            "Capability permission check: consent lookup failed", exc_info=True
        )
        return False
    return decisions.get(spec.capability_type, {}).get(str(capability_id)) == "approved"
