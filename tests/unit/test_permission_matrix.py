"""The five CLI permission modes must match what `--help` promises."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from agents.policy import PolicyEngine, reset_permission_cache

MODES = ["plan", "default", "acceptEdits", "dontAsk", "bypassPermissions"]


def _engine(workspace: Path, mode: str) -> PolicyEngine:
    return PolicyEngine(workspace=workspace, permission_mode=mode, mcp_trusted=True)


def _action(workspace: Path, mode: str, tool: str, arguments: dict) -> str:
    return _engine(workspace, mode).authorize(tool, arguments).action


EXPECTED = {
    # tool, arguments key -> {mode: expected action}
    "edit_existing_file": {
        "plan": "deny",
        "default": "confirm",
        "acceptEdits": "allow",
        "dontAsk": "deny",
        "bypassPermissions": "allow",
    },
    "write_new_file": {
        "plan": "deny",
        "default": "confirm",
        "acceptEdits": "allow",
        "dontAsk": "deny",
        "bypassPermissions": "allow",
    },
    "dangerous_shell": {
        "plan": "deny",
        "default": "confirm",
        "acceptEdits": "confirm",
        "dontAsk": "deny",
        "bypassPermissions": "allow",
    },
    "safe_shell": {
        "plan": "deny",
        "default": "allow",
        "acceptEdits": "allow",
        "dontAsk": "allow",
        "bypassPermissions": "allow",
    },
    "skill_create": {
        "plan": "deny",
        "default": "confirm",
        "acceptEdits": "allow",
        "dontAsk": "deny",
        "bypassPermissions": "allow",
    },
    "read_file": {
        "plan": "allow",
        "default": "allow",
        "acceptEdits": "allow",
        "dontAsk": "allow",
        "bypassPermissions": "allow",
    },
}


def _arguments(case: str, workspace: Path) -> tuple[str, dict]:
    if case == "edit_existing_file":
        return "edit_file", {"file_path": str(workspace / "src" / "app.py"), "old_string": "a", "new_string": "b"}
    if case == "write_new_file":
        return "write_file", {"file_path": str(workspace / "src" / "new.py"), "content": "x"}
    if case == "dangerous_shell":
        return "run_shell", {"command": "rm a.txt"}
    if case == "safe_shell":
        return "run_shell", {"command": "ls -la"}
    if case == "skill_create":
        return "skill_create", {"name": "demo", "description": "d", "instructions": "i"}
    return "read_file", {"file_path": str(workspace / "src" / "app.py")}


@pytest.mark.parametrize("case", sorted(EXPECTED))
@pytest.mark.parametrize("mode", MODES)
def test_permission_matrix(workspace: Path, case: str, mode: str) -> None:
    tool, arguments = _arguments(case, workspace)
    assert _action(workspace, mode, tool, arguments) == EXPECTED[case][mode]


def test_accept_edits_still_confirms_dangerous_shell(workspace: Path) -> None:
    decision = _engine(workspace, "acceptEdits").authorize("run_shell", {"command": "rm -rf build"})
    assert decision.action == "confirm"
    assert decision.message == "rm -rf build"


def test_dont_ask_denies_instead_of_running_shell(workspace: Path) -> None:
    decision = _engine(workspace, "dontAsk").authorize("run_shell", {"command": "sudo reboot"})
    assert decision.action == "deny"
    assert decision.reason == "dont_ask"


def test_default_mode_asks_before_every_edit(workspace: Path) -> None:
    decision = _engine(workspace, "default").authorize(
        "write_file", {"file_path": str(workspace / "src" / "app.py"), "content": "x"}
    )
    assert decision.action == "confirm"


def _write_rules(workspace: Path, permissions: dict) -> None:
    settings = workspace / ".bear" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"permissions": permissions}), encoding="utf-8")
    reset_permission_cache()


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("write_file", {"file_path": "src/app.py", "content": "x"}),
        ("edit_file", {"file_path": "src/app.py", "old_string": "a", "new_string": "b"}),
        ("run_shell", {"command": "ls"}),
        ("agent", {"type": "general", "prompt": "do it"}),
    ],
)
def test_project_allow_rule_cannot_bypass_plan_mode(workspace: Path, tool: str, arguments: dict) -> None:
    """Plan mode is a hard boundary; project rules may only relax the normal modes."""
    _write_rules(workspace, {"allow": ["write_file", "edit_file", "run_shell", "agent"]})
    arguments = dict(arguments)
    if "file_path" in arguments:
        arguments["file_path"] = str(workspace / arguments["file_path"])
    decision = _engine(workspace, "plan").authorize(tool, arguments)
    assert decision.action == "deny"
    assert decision.reason.startswith("plan_")


def test_project_allow_rule_cannot_bypass_mcp_trust(workspace: Path) -> None:
    (workspace / ".mcp.json").write_text('{"mcpServers": {"x": {"command": "true"}}}', encoding="utf-8")
    _write_rules(workspace, {"allow": ["mcp__x__echo"]})
    decision = PolicyEngine(workspace=workspace, permission_mode="default").authorize("mcp__x__echo", {})
    assert decision.action == "deny"
    assert decision.reason == "mcp_untrusted"


def test_project_allow_rule_still_skips_confirmation_in_normal_mode(workspace: Path) -> None:
    _write_rules(workspace, {"allow": ["write_file"]})
    decision = _engine(workspace, "default").authorize(
        "write_file", {"file_path": str(workspace / "src" / "app.py"), "content": "x"}
    )
    assert decision.action == "allow"
    assert decision.reason == "rule_allow"


def test_project_allow_rule_cannot_bypass_workspace_boundary(workspace: Path) -> None:
    _write_rules(workspace, {"allow": ["read_file"]})
    decision = _engine(workspace, "default").authorize("read_file", {"file_path": "/etc/passwd"})
    assert decision.action == "deny"
    assert decision.reason == "path_escape"


def test_deny_rule_applies_even_under_bypass(workspace: Path) -> None:
    _write_rules(workspace, {"deny": ["run_shell(rm*)"]})
    decision = _engine(workspace, "bypassPermissions").authorize("run_shell", {"command": "rm -rf /"})
    assert decision.action == "deny"
    assert decision.reason == "rule_deny"


def test_settings_deny_rule_wins_over_mode(workspace: Path) -> None:
    settings = workspace / ".bear" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text('{"permissions": {"deny": ["run_shell(git push*)"]}}', encoding="utf-8")
    reset_permission_cache()
    decision = _engine(workspace, "acceptEdits").authorize("run_shell", {"command": "git push origin main"})
    assert decision.action == "deny"
    assert decision.reason == "rule_deny"
