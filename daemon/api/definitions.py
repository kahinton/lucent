"""Load approved agent definitions and their access-controlled assets."""

from __future__ import annotations

import asyncio

import httpx


async def load_instance_agent(agent_type: str) -> dict | None:
    """Load an active instance agent definition from the definitions API."""
    from daemon.runtime.module_proxy import runtime

    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(
                    f"{runtime.API_BASE}/definitions/agents",
                    params={"status": "active", "limit": 200},
                    headers=runtime.API_HEADERS,
                )
                if response.status_code == 200:
                    data = response.json()
                    agents = data.get("items", data) if isinstance(data, dict) else data
                    for agent in agents:
                        if agent.get("name") == agent_type:
                            detail_response = await client.get(
                                f"{runtime.API_BASE}/definitions/agents/{agent['id']}",
                                headers=runtime.API_HEADERS,
                            )
                            if detail_response.status_code == 200:
                                return detail_response.json()
            return None
        except Exception as error:
            runtime.log(
                f"Attempt {attempt + 1} failed to load agent definition "
                f"'{agent_type}' via API: {error}",
                "WARN",
            )
            if attempt == 0:
                await asyncio.sleep(1)
    return None


async def load_accessible_agent(
    *,
    org_id: str,
    requester_user_id: str,
    agent_type: str,
    agent_definition_id: str | None = None,
) -> dict | None:
    """Load an active agent definition accessible to the requesting user."""
    from daemon.runtime.module_proxy import runtime

    try:
        from lucent.db import get_pool

        pool = await get_pool()
    except Exception as error:
        runtime.log(
            f"DB pool unavailable for agent resolution, falling back to API: {error}",
            "DEBUG",
        )
        return await runtime.load_instance_agent(agent_type)

    from lucent.access_control import AccessControlService
    from lucent.db.definitions import DefinitionRepository

    access_control = AccessControlService(pool)
    repository = DefinitionRepository(pool)
    if agent_definition_id:
        if not await access_control.can_access(
            requester_user_id, "agent", agent_definition_id, org_id
        ):
            return None
        agent = await repository.get_agent(agent_definition_id, org_id)
        return agent if agent and agent.get("status") == "active" else None

    accessible_ids = set(
        await access_control.list_accessible(requester_user_id, "agent", org_id)
    )
    agents = await repository.list_agents(org_id, status="active", limit=200)
    for agent in agents["items"]:
        if str(agent["id"]) not in accessible_ids or agent.get("name") != agent_type:
            continue
        full_agent = await repository.get_agent(str(agent["id"]), org_id)
        if full_agent and full_agent.get("status") == "active":
            return full_agent
    return None


async def _load_accessible_assets(
    *, org_id: str, requester_user_id: str, agent_id: str, asset_type: str
) -> list[dict]:
    """Load the agent's capabilities that this requester may actually use.

    Delegates to the shared resolver so a daemon task and an interactive chat
    resolve an agent's capability chain identically: attached, readable by the
    requester, and consented (or exempt). Previously this method intersected
    attachments with the legacy flat-OR ``AccessControlService`` predicate,
    which bypassed the ``auth_clearances`` model entirely and had no notion of
    consent.

    Fail-closed: if resolution cannot run, the agent carries no capabilities for
    this dispatch rather than all of them.
    """
    from lucent.agent_capabilities import resolve_agent_capabilities
    from lucent.db.definitions import normalize_capability_type

    try:
        from lucent.db import get_pool

        pool = await get_pool()
    except Exception:
        return []

    try:
        capability_type = normalize_capability_type(asset_type)
    except ValueError:
        from daemon.runtime.module_proxy import runtime

        runtime.log(f"Unknown capability type '{asset_type}'", "WARN")
        return []

    try:
        resolved = await resolve_agent_capabilities(
            pool=pool,
            user={"id": requester_user_id, "organization_id": org_id},
            agent_id=agent_id,
            org_id=org_id,
        )
    except Exception as error:
        from daemon.runtime.module_proxy import runtime

        runtime.log(
            f"Capability resolution failed for agent {agent_id}: {error}", "WARN"
        )
        return []
    return resolved.as_loader_map()[capability_type]


async def load_accessible_skills_for_agent(
    *, org_id: str, requester_user_id: str, agent_id: str
) -> list[dict]:
    return await _load_accessible_assets(
        org_id=org_id,
        requester_user_id=requester_user_id,
        agent_id=agent_id,
        asset_type="skill",
    )


async def load_accessible_mcp_servers_for_agent(
    *, org_id: str, requester_user_id: str, agent_id: str
) -> list[dict]:
    return await _load_accessible_assets(
        org_id=org_id,
        requester_user_id=requester_user_id,
        agent_id=agent_id,
        asset_type="mcp_server",
    )


async def load_accessible_hooks_for_agent(
    *, org_id: str, requester_user_id: str, agent_id: str
) -> list[dict]:
    return await _load_accessible_assets(
        org_id=org_id,
        requester_user_id=requester_user_id,
        agent_id=agent_id,
        asset_type="hook",
    )


async def load_accessible_managed_tools_for_agent(
    *, org_id: str, requester_user_id: str, agent_id: str
) -> list[dict]:
    return await _load_accessible_assets(
        org_id=org_id,
        requester_user_id=requester_user_id,
        agent_id=agent_id,
        asset_type="managed_tool",
    )


async def load_instance_skills_for_agent(agent_id: str) -> list[dict]:
    """Load skills granted to an instance agent from the definitions API."""
    from daemon.runtime.module_proxy import runtime

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                f"{runtime.API_BASE}/definitions/agents/{agent_id}",
                headers=runtime.API_HEADERS,
            )
            if response.status_code == 200:
                return response.json().get("skills", [])
    except Exception:
        runtime.log("Failed to load instance skills for agent", "DEBUG")
    return []
