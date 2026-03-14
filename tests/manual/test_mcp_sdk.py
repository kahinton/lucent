"""Test Copilot SDK with MCP server inside Docker."""
import asyncio
import os


async def test():
    from copilot import CopilotClient, PermissionHandler

    from lucent.api.routers.chat import _ensure_chat_api_key

    # Provision a key
    from lucent.db import init_db

    db_url = os.environ.get("DATABASE_URL")
    pool = await init_db(db_url)
    key = await _ensure_chat_api_key(pool)
    print(f"Key provisioned: {key[:15]}...")

    token = os.environ.get("GITHUB_TOKEN", "")
    opts = {"log_level": "info"}
    if token:
        opts["github_token"] = token

    client = CopilotClient(opts)
    await client.start()
    print("Client started")

    mcp_config = {
        "memory-server": {
            "type": "http",
            "url": "http://localhost:8766/mcp",
            "headers": {"Authorization": f"Bearer {key}"},
        },
    }

    session = await client.create_session({
        "model": "claude-opus-4.6",
        "system_message": {"content": "You are a helpful assistant with access to a memory server via MCP tools. Use the search_memories tool to answer questions about the user's memories."},
        "on_permission_request": PermissionHandler.approve_all,
        "mcpServers": mcp_config,
    })
    print("Session created with MCP config")

    # Collect events
    events = []
    def on_event(event):
        etype = event.type.value if hasattr(event.type, "value") else str(event.type)
        print(f"Event: {etype}")
        events.append(event)
        if etype == "assistant.message":
            content = getattr(event.data, "content", None)
            if content:
                print(f"Response: {content[:200]}")

    session.on(on_event)

    response = await session.send_and_wait(
        {"prompt": "Search for memories tagged 'architecture' and list the top 3"},
        timeout=120,
    )

    if response and response.data:
        content = getattr(response.data, "content", None)
        print(f"\nFinal response: {content[:500] if content else 'None'}")
    else:
        print("\nNo response received")

    await session.disconnect()
    await client.stop()
    print("Done")

asyncio.run(test())
