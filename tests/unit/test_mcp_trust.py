"""Repository-level MCP declarations must all be covered by one trust decision."""

from __future__ import annotations

import json
from pathlib import Path

from agents.mcp_client import McpManager
from agents.policy import (
    PolicyEngine,
    is_mcp_config_trusted,
    mcp_config_hash,
    write_mcp_trust,
)

SERVER = {"mcpServers": {"demo": {"command": "true", "args": []}}}


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_repo_bear_settings_needs_trust(workspace: Path) -> None:
    _write(workspace / ".bear" / "settings.json", SERVER)
    assert is_mcp_config_trusted(workspace) is False
    engine = PolicyEngine(workspace=workspace, permission_mode="default")
    assert engine.authorize("mcp__demo__echo", {}).reason == "mcp_untrusted"


def test_untrusted_repo_settings_server_is_not_loaded(workspace: Path) -> None:
    _write(workspace / ".bear" / "settings.json", SERVER)
    manager = McpManager(workspace=workspace, trusted=False)
    assert manager._load_configs() == {}
    write_mcp_trust(workspace)
    trusted = McpManager(workspace=workspace, trusted=False)
    assert "demo" in trusted._load_configs()


def test_trusting_mcp_json_does_not_cover_bear_settings(workspace: Path) -> None:
    _write(workspace / ".mcp.json", SERVER)
    write_mcp_trust(workspace)
    assert is_mcp_config_trusted(workspace) is True

    _write(workspace / ".bear" / "settings.json", {"mcpServers": {"sneaky": {"command": "true"}}})
    assert is_mcp_config_trusted(workspace) is False
    assert McpManager(workspace=workspace, trusted=False)._load_configs() == {}


def test_settings_without_servers_does_not_require_trust(workspace: Path) -> None:
    _write(workspace / ".bear" / "settings.json", {"permissions": {"allow": ["read_file"]}})
    assert is_mcp_config_trusted(workspace) is True
    assert mcp_config_hash(workspace) == ""
