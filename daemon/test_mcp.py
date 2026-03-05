"""Quick test to verify MCP connection from daemon."""
import asyncio
import json
import httpx

async def test_mcp():
    url = "http://localhost:8766/mcp"
    key = "mcp_Vzwb0Lk88Eelu9OlYhZMpzqY-KKJbnRFSDZvlR7EQGo"
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        # Initialize MCP session
        init = {
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "lucent-daemon", "version": "1.0.0"},
            },
            "id": 1,
        }
        resp = await client.post(url, json=init, headers=headers)
        print(f"Init status: {resp.status_code}")
        session_id = resp.headers.get("mcp-session-id")
        print(f"Session ID: {session_id}")

        if not session_id:
            print("No session ID returned!")
            return

        headers["mcp-session-id"] = session_id

        # Call get_current_user_context
        call = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "get_current_user_context", "arguments": {}},
            "id": 2,
        }
        resp = await client.post(url, json=call, headers=headers)
        
        # Handle both JSON and SSE responses
        content_type = resp.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            # Parse SSE: look for data: lines with JSON
            for line in resp.text.split("\n"):
                if line.startswith("data: "):
                    data = json.loads(line[6:])
                    break
            else:
                print(f"No data in SSE response: {resp.text[:300]}")
                return
        else:
            data = resp.json()

        if "result" in data:
            content = json.loads(data["result"]["content"][0]["text"])
            user_name = content.get("user", {}).get("display_name")
            print(f"User: {user_name}")
            print("MCP CONNECTION WORKING!")
        else:
            print(f"Error: {json.dumps(data, indent=2)}")

        # Try creating a memory
        create = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": "create_memory",
                "arguments": {
                    "type": "experience",
                    "content": "First autonomous daemon memory. I exist between conversations now.",
                    "tags": ["daemon", "milestone", "identity"],
                    "importance": 8,
                },
            },
            "id": 3,
        }
        resp = await client.post(url, json=create, headers=headers)
        content_type = resp.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            for line in resp.text.split("\n"):
                if line.startswith("data: "):
                    data = json.loads(line[6:])
                    break
            else:
                print(f"No data in SSE: {resp.text[:300]}")
                return
        else:
            data = resp.json()

        if "result" in data:
            content = json.loads(data["result"]["content"][0]["text"])
            print(f"Created memory: {content.get('id')}")
            print("MEMORY WRITE WORKING!")
        else:
            print(f"Create error: {json.dumps(data, indent=2)}")

asyncio.run(test_mcp())
