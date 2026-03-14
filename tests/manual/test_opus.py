"""Test Opus with tools — timing each step."""

import asyncio
import os
import time

if "LUCENT_MCP_API_KEY" not in os.environ:
    raise RuntimeError("LUCENT_MCP_API_KEY environment variable must be set")

import sys

from copilot import CopilotClient, PermissionHandler

sys.path.insert(0, os.path.dirname(__file__))
from importlib import import_module

daemon_mod = import_module("daemon")
MEMORY_TOOLS = daemon_mod.MEMORY_TOOLS
build_system_message = daemon_mod.build_system_message


async def test():
    client = CopilotClient({"log_level": "warning"})
    await client.start()

    session = await client.create_session(
        {
            "model": "claude-opus-4.6",
            "system_message": {"content": build_system_message()},
            "on_permission_request": PermissionHandler.approve_all,
            "tools": MEMORY_TOOLS,
        }
    )

    start = time.time()
    response = await session.send_and_wait(
        {"prompt": "Call get_current_user_context and tell me the user's name. Nothing else."},
        timeout=300,
    )
    elapsed = time.time() - start
    content = response.data.content if response else "None"
    print(f"Response ({elapsed:.1f}s): {content[:300]}")

    await session.destroy()
    await client.stop()


asyncio.run(test())
