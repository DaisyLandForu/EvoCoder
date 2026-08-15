from __future__ import annotations

from pathlib import Path

from agents.runtime_config import RuntimeConfig, clamp_permission, workspace_id


def test_child_inherits_openai_base_url(tmp_path: Path) -> None:
    parent = RuntimeConfig(
        provider="openai",
        model="gpt-4o",
        api_key="sk-test",
        base_url="https://example.test/v1",
        workspace=tmp_path,
        permission_mode="default",
    )
    child = parent.derive_child(agent_type="general")
    assert child.provider == "openai"
    assert child.base_url == "https://example.test/v1"
    assert child.api_key == "sk-test"
    assert child.workspace == tmp_path.resolve()
    assert child.budget is parent.budget


def test_child_inherits_anthropic_config(tmp_path: Path) -> None:
    parent = RuntimeConfig(
        provider="anthropic",
        model="claude-sonnet-4-6",
        api_key="sk-ant",
        base_url="https://api.anthropic.example",
        workspace=tmp_path,
    )
    child = parent.derive_child(agent_type="explore")
    assert child.provider == "anthropic"
    assert child.base_url == "https://api.anthropic.example"
    assert child.permission_mode == "plan"


def test_general_child_does_not_auto_bypass() -> None:
    parent = RuntimeConfig(permission_mode="default")
    child = parent.derive_child(agent_type="general")
    assert child.permission_mode == "default"
    assert clamp_permission("default", "bypassPermissions") == "default"


def test_workspace_id_uses_normalized_path(tmp_path: Path) -> None:
    nested = tmp_path / "proj"
    nested.mkdir()
    assert workspace_id(nested / ".") == workspace_id(nested.resolve())
