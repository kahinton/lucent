"""Tests for the shipped Copilot plugin memory lookup hook."""

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK_SCRIPT = ROOT / ".github" / "hooks" / "file-memory-lookup.py"
HOOK_CONFIG = ROOT / ".github" / "hooks" / "file-memory-lookup.json"
PLUGIN_MANIFEST = ROOT / ".github" / "plugin" / "plugin.json"


def _load_hook_module():
    spec = importlib.util.spec_from_file_location("lucent_file_memory_lookup_hook", HOOK_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_plugin_manifest_ships_hooks_directory():
    manifest = json.loads(PLUGIN_MANIFEST.read_text())

    assert manifest["hooks"] == "../hooks/"


def test_hook_config_invokes_memory_lookup_script():
    config = json.loads(HOOK_CONFIG.read_text())
    pre_tool_hooks = config["hooks"]["PreToolUse"]

    assert pre_tool_hooks[0]["type"] == "command"
    assert ".github/hooks/file-memory-lookup.py" in pre_tool_hooks[0]["command"]
    assert HOOK_SCRIPT.exists()


def test_hook_extracts_file_references_from_vscode_tool_event():
    hook = _load_hook_module()
    tool_name, arguments = hook._extract_tool_call(
        {
            "toolName": "read_file",
            "toolInput": {
                "filePath": "/Users/example/project/src/lucent/llm/hooks.py",
                "startLine": 1,
            },
        }
    )

    refs = hook.extract_file_references(tool_name, arguments)

    assert "/Users/example/project/src/lucent/llm/hooks.py" in refs


def test_hook_ignores_non_file_events():
    hook = _load_hook_module()

    refs = hook.extract_file_references("ask_user", {"question": "What should I do next?"})

    assert refs == []


def test_hook_formats_system_message_with_memory_context():
    hook = _load_hook_module()

    message = hook.format_system_message(
        ["src/lucent/llm/hooks.py"],
        [
            {
                "id": "12345678-1234-5678-1234-567812345678",
                "tags": ["lucent", "hooks"],
                "content": "Use the file-memory hook to inject relevant technical memories.",
            }
        ],
    )

    assert "Lucent memory hook context" in message
    assert "src/lucent/llm/hooks.py" in message
    assert "12345678 [lucent, hooks]" in message


def test_hook_discovers_base_url_from_mcp_url():
    hook = _load_hook_module()

    assert hook._base_url_from_mcp_url("http://localhost:8766/mcp") == "http://localhost:8766"
