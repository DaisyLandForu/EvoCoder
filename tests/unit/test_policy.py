from __future__ import annotations

from pathlib import Path

from agents.policy import PolicyEngine, write_mcp_trust


def test_rejects_outside_workspace_file(workspace: Path) -> None:
    engine = PolicyEngine(workspace=workspace, permission_mode="default")
    decision = engine.authorize("read_file", {"file_path": "/etc/passwd"})
    assert decision.action == "deny"
    assert "escapes workspace" in decision.message


def test_rejects_relative_escape(workspace: Path) -> None:
    engine = PolicyEngine(workspace=workspace, permission_mode="default")
    decision = engine.authorize("read_file", {"file_path": "../outside.txt"})
    assert decision.action == "deny"


def test_rejects_symlink_escape(workspace: Path, tmp_path: Path) -> None:
    outside_dir = tmp_path.parent / f"{tmp_path.name}-outside"
    outside_dir.mkdir(exist_ok=True)
    outside = outside_dir / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    link = workspace / "leak"
    link.symlink_to(outside)
    engine = PolicyEngine(workspace=workspace, permission_mode="default")
    decision = engine.authorize("read_file", {"file_path": str(link)})
    assert decision.action == "deny"
    assert "symlink" in decision.message or "escapes" in decision.message


def test_plan_mode_blocks_unknown_mcp_delete(workspace: Path) -> None:
    engine = PolicyEngine(workspace=workspace, permission_mode="plan", mcp_trusted=True)
    decision = engine.authorize("mcp__files__delete_file", {"path": "a"})
    assert decision.action == "deny"
    assert decision.reason == "plan_mcp_side_effect"


def test_child_cannot_bypass_parent_policy(workspace: Path) -> None:
    parent = PolicyEngine(workspace=workspace, permission_mode="plan")
    child = parent.child(permission_mode="bypassPermissions")
    decision = child.authorize("write_file", {"file_path": str(workspace / "a.txt"), "content": "x"})
    assert decision.action == "deny"


def test_untrusted_mcp_json_is_denied(workspace: Path) -> None:
    (workspace / ".mcp.json").write_text('{"mcpServers": {"x": {"command": "true"}}}', encoding="utf-8")
    engine = PolicyEngine(workspace=workspace, permission_mode="default", mcp_trusted=False)
    decision = engine.authorize("mcp__x__echo", {})
    assert decision.action == "deny"
    assert decision.reason == "mcp_untrusted"
    write_mcp_trust(workspace)
    trusted = PolicyEngine(workspace=workspace, permission_mode="default", mcp_trusted=False)
    assert trusted.authorize("mcp__x__echo", {}).action == "allow"
