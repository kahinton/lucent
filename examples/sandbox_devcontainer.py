#!/usr/bin/env python3
"""Example: Using sandboxes with devcontainer-enabled repositories.

Demonstrates how Lucent automatically detects and applies devcontainer.json
configs when creating sandboxes from repositories.

Usage:
    export LUCENT_URL="http://localhost:8766"
    export LUCENT_API_KEY="hs_your_api_key"
    python examples/sandbox_devcontainer.py

Prerequisites:
    pip install httpx
    Docker must be running on the Lucent host.
"""

import os
import sys
import time

import httpx

LUCENT_URL = os.environ.get("LUCENT_URL", "http://localhost:8766")
API_KEY = os.environ.get("LUCENT_API_KEY", "")

if not API_KEY:
    print("Error: Set LUCENT_API_KEY environment variable")
    sys.exit(1)

headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}


def create_sandbox_with_devcontainer(repo_url: str, branch: str = "main") -> dict:
    """Create a sandbox from a repo that has a devcontainer.json.

    Lucent will:
    1. Clone the repo into /workspace
    2. Detect .devcontainer/devcontainer.json (or .devcontainer.json)
    3. Apply lifecycle commands (onCreateCommand, postCreateCommand, etc.)
    4. Apply environment variables from containerEnv/remoteEnv
    5. If the devcontainer specifies a different image, rebuild the container
    """
    resp = httpx.post(
        f"{LUCENT_URL}/api/sandboxes",
        headers=headers,
        json={
            "repo_url": repo_url,
            "branch": branch,
            "image": "python:3.12-slim",  # Base image; overridden if devcontainer specifies one
            "memory_limit": "2g",
            "cpu_limit": 2.0,
            "timeout_seconds": 1800,
            "network_mode": "bridge",  # Needed for git clone and package installs
        },
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()


def wait_for_ready(sandbox_id: str, max_wait: int = 120) -> dict:
    """Poll until the sandbox is ready."""
    for _ in range(max_wait // 5):
        resp = httpx.get(f"{LUCENT_URL}/api/sandboxes/{sandbox_id}", headers=headers)
        resp.raise_for_status()
        info = resp.json()
        if info["status"] == "ready":
            return info
        if info["status"] == "failed":
            print(f"✗ Sandbox failed: {info.get('error', 'unknown')}")
            sys.exit(1)
        time.sleep(5)
    print("✗ Sandbox did not become ready in time")
    sys.exit(1)


def exec_in_sandbox(sandbox_id: str, command: str) -> dict:
    """Execute a command in the sandbox."""
    resp = httpx.post(
        f"{LUCENT_URL}/api/sandboxes/{sandbox_id}/exec",
        headers=headers,
        json={"command": command, "timeout": 60},
    )
    resp.raise_for_status()
    return resp.json()


def main():
    print("=== Sandbox with Devcontainer Example ===\n")

    # --- Example 1: Repo with devcontainer.json ---
    # Replace with a repo that has a .devcontainer/devcontainer.json
    repo_url = "https://github.com/your-org/your-repo"

    print(f"1. Creating sandbox from {repo_url}...")
    sandbox = create_sandbox_with_devcontainer(repo_url)
    sandbox_id = sandbox["id"]
    print(f"   Sandbox ID: {sandbox_id}")
    print(f"   Status: {sandbox['status']}")

    print("\n2. Waiting for sandbox to be ready...")
    info = wait_for_ready(sandbox_id)
    print(f"   Status: {info['status']}")

    print("\n3. Running a command in the sandbox...")
    result = exec_in_sandbox(sandbox_id, "ls -la /workspace")
    print(f"   Exit code: {result['exit_code']}")
    print(f"   Output:\n{result['stdout'][:500]}")

    print("\n4. Checking environment (devcontainer env vars applied)...")
    result = exec_in_sandbox(sandbox_id, "env | sort")
    print(f"   Environment variables:\n{result['stdout'][:500]}")

    # --- Example 2: Template with devcontainer repo ---
    print("\n5. Creating a reusable template...")
    resp = httpx.post(
        f"{LUCENT_URL}/api/sandboxes/templates",
        headers=headers,
        json={
            "name": "my-project-dev",
            "description": "Dev environment with devcontainer support",
            "image": "python:3.12-slim",
            "repo_url": repo_url,
            "branch": "main",
            "network_mode": "bridge",
            "memory_limit": "4g",
        },
    )
    resp.raise_for_status()
    template = resp.json()
    print(f"   Template ID: {template['id']}")

    print("\n6. Launching sandbox from template...")
    resp = httpx.post(
        f"{LUCENT_URL}/api/sandboxes/templates/{template['id']}/launch",
        headers=headers,
    )
    resp.raise_for_status()
    sandbox2 = resp.json()
    print(f"   Sandbox ID: {sandbox2['id']}")

    # Cleanup
    print("\n7. Cleaning up...")
    httpx.delete(f"{LUCENT_URL}/api/sandboxes/{sandbox_id}", headers=headers)
    httpx.delete(f"{LUCENT_URL}/api/sandboxes/{sandbox2['id']}", headers=headers)
    httpx.delete(f"{LUCENT_URL}/api/sandboxes/templates/{template['id']}", headers=headers)
    print("   Done!")


if __name__ == "__main__":
    main()
