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
    """Load active assets granted to an agent and visible to the requester."""
    try:
        from lucent.db import get_pool

        pool = await get_pool()
    except Exception:
        return []

    from lucent.access_control import AccessControlService
    from lucent.db.definitions import DefinitionRepository

    repository = DefinitionRepository(pool)
    method = getattr(repository, f"get_agent_{asset_type}s")
    assets = await method(agent_id)
    access_control = AccessControlService(pool)
    accessible_ids = set(
        await access_control.list_accessible(requester_user_id, asset_type, org_id)
    )
    return [
        asset
        for asset in assets
        if str(asset["id"]) in accessible_ids and asset.get("status") == "active"
    ]


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
