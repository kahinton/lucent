#!/usr/bin/env python3
"""Example: External agent integration with Lucent daemon tasks.

Demonstrates the submit → poll → retrieve flow for an external AI agent
interacting with Lucent's daemon task API.

Usage:
    export LUCENT_URL="http://localhost:8766"
    export LUCENT_API_KEY="mcp_your_daemon_tasks_key"
    python examples/agent_integration.py

Prerequisites:
    pip install httpx
    Create an API key with 'daemon-tasks' scope via the Lucent web UI.
"""

import os
import sys
import time

import httpx

LUCENT_URL = os.environ.get("LUCENT_URL", "http://localhost:8766")
API_KEY = os.environ.get("LUCENT_API_KEY", "")
POLL_INTERVAL = 10  # seconds between polls
MAX_POLL_ATTEMPTS = 60  # give up after 10 minutes

if not API_KEY:
    print("Error: Set LUCENT_API_KEY environment variable")
    sys.exit(1)

headers = {"Authorization": f"Bearer {API_KEY}"}
base = f"{LUCENT_URL}/api/daemon/tasks"


def submit_task(description: str, agent_type: str = "code", priority: str = "medium") -> dict:
    """Submit a new daemon task."""
    resp = httpx.post(
        base,
        headers=headers,
        json={
            "description": description,
            "agent_type": agent_type,
            "priority": priority,
        },
    )
    resp.raise_for_status()
    task = resp.json()
    print(f"✓ Task submitted: {task['id']} (status: {task['status']})")
    return task


def poll_for_completion(task_id: str) -> dict:
    """Poll until the task is completed or max attempts reached."""
    url = f"{base}/{task_id}/result"

    for attempt in range(1, MAX_POLL_ATTEMPTS + 1):
        resp = httpx.get(url, headers=headers)
        resp.raise_for_status()

        task = resp.json()
        status = task["status"]

        if resp.status_code == 200 and status == "completed":
            print(f"✓ Task completed after {attempt} poll(s)")
            return task

        print(f"  Poll {attempt}: status={status} (waiting {POLL_INTERVAL}s...)")
        time.sleep(POLL_INTERVAL)

    print(f"✗ Task did not complete within {MAX_POLL_ATTEMPTS * POLL_INTERVAL}s")
    sys.exit(1)


def list_tasks(status_filter: str | None = None) -> list[dict]:
    """List tasks with optional status filter."""
    params = {}
    if status_filter:
        params["status"] = status_filter

    resp = httpx.get(base, headers=headers, params=params)
    resp.raise_for_status()
    data = resp.json()
    print(f"✓ Found {data['total_count']} task(s)")
    return data["tasks"]


def main():
    print("=== Lucent Agent Integration Example ===\n")

    # 1. Submit a task
    print("1. Submitting task...")
    task = submit_task(
        description="Review the authentication module for security issues. "
        "Check for common vulnerabilities like timing attacks, "
        "missing rate limiting, and improper session handling.",
        agent_type="code",
        priority="medium",
    )
    task_id = task["id"]

    # 2. List pending tasks
    print("\n2. Listing pending tasks...")
    list_tasks("pending")

    # 3. Poll for result
    print(f"\n3. Polling for task {task_id[:8]}... result...")
    result = poll_for_completion(task_id)

    # 4. Display result
    print(f"\n4. Result:\n{'─' * 60}")
    if result.get("result"):
        print(result["result"])
    else:
        print("(No result content — check task description)")
    print(f"{'─' * 60}")

    print("\nDone!")


if __name__ == "__main__":
    main()
