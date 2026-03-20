"""MCP tool discovery service with short-lived DB caching."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from lucent.db import DefinitionRepository
from lucent.llm.mcp_bridge import MCPToolBridge
from lucent.secrets import SecretRegistry, resolve_env_vars

DISCOVERY_TIMEOUT_SECONDS = 10


class MCPDiscoveryError(RuntimeError):
    """Raised when MCP tool discovery fails."""



def _coerce_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return value if isinstance(value, dict) else {}



def _coerce_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return []



def _normalize_tool_from_langchain(tool: dict[str, Any]) -> dict[str, Any]:
    fn = tool.get("function") if isinstance(tool, dict) else None
    if not isinstance(fn, dict):
        raise MCPDiscoveryError("Invalid response from MCP server")
    name = fn.get("name")
    if not isinstance(name, str) or not name:
        raise MCPDiscoveryError("Invalid response from MCP server")
    description = fn.get("description") if isinstance(fn.get("description"), str) else ""
    schema = fn.get("parameters") if isinstance(fn.get("parameters"), dict) else {}
    return {
        "name": name,
        "description": description,
        "input_schema": schema,
    }



def _normalize_tool_obj(tool: Any) -> dict[str, Any]:
    if hasattr(tool, "model_dump"):
        data = tool.model_dump(by_alias=True)
    elif isinstance(tool, dict):
        data = tool
    else:
        data = {
            "name": getattr(tool, "name", None),
            "description": getattr(tool, "description", ""),
            "inputSchema": getattr(tool, "inputSchema", None)
            or getattr(tool, "input_schema", None),
        }

    name = data.get("name")
    if not isinstance(name, str) or not name:
        raise MCPDiscoveryError("Invalid response from MCP server")

    description = data.get("description") if isinstance(data.get("description"), str) else ""
    schema = data.get("inputSchema")
    if not isinstance(schema, dict):
        schema = data.get("input_schema") if isinstance(data.get("input_schema"), dict) else {}

    return {
        "name": name,
        "description": description,
        "input_schema": schema,
    }


async def _discover_http(server_config: dict[str, Any]) -> list[dict[str, Any]]:
    url = server_config.get("url")
    if not isinstance(url, str) or not url:
        raise MCPDiscoveryError("Missing URL for HTTP MCP server")

    headers = _coerce_mapping(server_config.get("headers"))
    bridge = MCPToolBridge(mcp_url=url, headers={str(k): str(v) for k, v in headers.items()})
    bridge._client.timeout = DISCOVERY_TIMEOUT_SECONDS  # type: ignore[attr-defined]

    try:
        discovered = await bridge.discover_tools()
        if not isinstance(discovered, list):
            raise MCPDiscoveryError("Invalid response from MCP server")
        return [_normalize_tool_from_langchain(tool) for tool in discovered]
    except MCPDiscoveryError:
        raise
    except Exception as exc:
        raise MCPDiscoveryError(str(exc)) from exc
    finally:
        await bridge.close()


async def _discover_stdio(server_config: dict[str, Any]) -> list[dict[str, Any]]:
    command = server_config.get("command")
    if not isinstance(command, str) or not command:
        raise MCPDiscoveryError("Missing command for stdio MCP server")

    args = _coerce_list(server_config.get("args"))

    env_vars = _coerce_mapping(server_config.get("env_vars"))
    env: dict[str, str] = os.environ.copy()
    if env_vars:
        try:
            provider = SecretRegistry.get()
            resolved = await resolve_env_vars({str(k): str(v) for k, v in env_vars.items()}, provider)
        except Exception as exc:
            raise MCPDiscoveryError(f"Failed to resolve env vars: {exc}") from exc
        env.update(resolved)

    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except Exception as exc:
        raise MCPDiscoveryError(f"MCP stdio client unavailable: {exc}") from exc

    server_params = StdioServerParameters(command=command, args=args, env=env)

    try:
        import asyncio

        async with asyncio.timeout(DISCOVERY_TIMEOUT_SECONDS):
            async with stdio_client(server_params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
    except MCPDiscoveryError:
        raise
    except TimeoutError as exc:
        raise MCPDiscoveryError("Connection timed out") from exc
    except OSError as exc:
        raise MCPDiscoveryError(f"Failed to start process: {exc}") from exc
    except Exception as exc:
        raise MCPDiscoveryError(str(exc)) from exc

    tools = getattr(listed, "tools", None)
    if not isinstance(tools, list):
        raise MCPDiscoveryError("Invalid response from MCP server")

    return [_normalize_tool_obj(tool) for tool in tools]


async def discover_mcp_tools(server_config: dict, db_pool) -> list[dict]:
    """Discover tools from an MCP server and update discovery cache."""
    server_type = (server_config.get("server_type") or "http").lower()

    if server_type == "http":
        tools = await _discover_http(server_config)
    elif server_type == "stdio":
        tools = await _discover_stdio(server_config)
    else:
        raise MCPDiscoveryError(f"Unsupported MCP server type: {server_type}")

    server_id = server_config.get("id")
    org_id = server_config.get("organization_id")
    if server_id and org_id:
        repo = DefinitionRepository(db_pool)
        await repo.save_discovered_tools(str(server_id), tools, str(org_id))

    return tools


async def get_tools_cached(
    server_id,
    org_id,
    db_pool,
    max_age_seconds: int = 60,
) -> tuple[list[dict], bool]:
    """Return discovered tools from cache when fresh, otherwise refresh."""
    repo = DefinitionRepository(db_pool)

    cached = await repo.get_discovered_tools(str(server_id), str(org_id))
    if cached and isinstance(cached.get("discovered_tools"), list):
        discovered_at = cached.get("tools_discovered_at")
        if isinstance(discovered_at, datetime):
            age = (datetime.now(timezone.utc) - discovered_at).total_seconds()
            if age <= max_age_seconds:
                return cached["discovered_tools"], True

    server_config = await repo.get_mcp_server(str(server_id), str(org_id))
    if not server_config:
        raise MCPDiscoveryError("MCP server not found")

    tools = await discover_mcp_tools(server_config, db_pool)
    return tools, False
