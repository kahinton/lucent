"""Shared builders for Lucent's internal MCP server configuration.

Both the API chat runtime and the daemon connect to Lucent's own MCP endpoint.
Keeping the wire shape here prevents security-relevant fields such as the
``internal`` marker and scoped-key retry headers from drifting between them.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from lucent.memory_scope import build_memory_scope_headers


def _unique_tools(tools: Iterable[str]) -> list[str]:
    """Return non-empty tool names in stable order without duplicates."""
    return list(dict.fromkeys(tool for tool in tools if tool))


def build_internal_mcp_server(
    *,
    url: str,
    bearer_token: str,
    tools: Iterable[str],
    extra_headers: Mapping[str, str] | None = None,
) -> dict:
    """Build a trusted connection to Lucent's own MCP server.

    ``internal`` only bypasses outbound SSRF validation for this application-owned
    endpoint. Authorization remains the supplied bearer credential.
    """
    headers = {"Authorization": f"Bearer {bearer_token}"}
    if extra_headers:
        headers.update({key: value for key, value in extra_headers.items() if value})
    return {
        "type": "http",
        "url": url,
        "headers": headers,
        "tools": _unique_tools(tools),
        "internal": True,
    }


def build_scoped_internal_mcp_server(
    *,
    url: str,
    bearer_token: str,
    memory_scope: str,
    organization_id: str,
    memory_scope_user_id: str | None,
    tools: Iterable[str],
    extra_headers: Mapping[str, str] | None = None,
) -> dict:
    """Build an internal MCP configuration with scoped-key retry metadata."""
    scope_headers = build_memory_scope_headers(
        memory_scope,
        org_id=organization_id,
        memory_scope_user_id=memory_scope_user_id,
    )
    if extra_headers:
        scope_headers.update(extra_headers)
    return build_internal_mcp_server(
        url=url,
        bearer_token=bearer_token,
        tools=tools,
        extra_headers=scope_headers,
    )
