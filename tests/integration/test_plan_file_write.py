"""Plan mode must be able to write its plan file through the real tool chain."""

from __future__ import annotations

from pathlib import Path

import pytest
from agents.policy import PolicyEngine
from agents.tools import execute_tool


def _plan_engine(workspace: Path, plan_file: Path) -> PolicyEngine:
    return PolicyEngine(
        workspace=workspace,
        permission_mode="plan",
        plan_file_path=str(plan_file),
    )


@pytest.mark.asyncio
async def test_plan_file_outside_workspace_is_authorized_and_written(workspace: Path, tmp_path: Path) -> None:
    plan_home = tmp_path.parent / f"{tmp_path.name}-home" / ".bear" / "plans"
    plan_home.mkdir(parents=True, exist_ok=True)
    plan_file = plan_home / "plan-abc123.md"
    engine = _plan_engine(workspace, plan_file)

    arguments = {"file_path": str(plan_file), "content": "# Plan\n1. step one\n"}
    decision = engine.authorize("write_file", arguments)
    assert decision.action == "allow"
    assert decision.reason == "plan_file"

    result = await execute_tool("write_file", arguments, {}, policy=engine)
    assert "Successfully wrote" in result
    assert plan_file.read_text(encoding="utf-8") == "# Plan\n1. step one\n"


@pytest.mark.asyncio
async def test_plan_mode_still_blocks_other_files(workspace: Path, tmp_path: Path) -> None:
    plan_file = tmp_path.parent / f"{tmp_path.name}-home" / "plan.md"
    plan_file.parent.mkdir(parents=True, exist_ok=True)
    engine = _plan_engine(workspace, plan_file)

    target = workspace / "src" / "app.py"
    assert engine.authorize("write_file", {"file_path": str(target), "content": "x"}).action == "deny"
    assert engine.authorize("run_shell", {"command": "ls"}).action == "deny"


@pytest.mark.asyncio
async def test_allowing_the_plan_file_does_not_open_its_directory(workspace: Path, tmp_path: Path) -> None:
    plan_dir = tmp_path.parent / f"{tmp_path.name}-home" / ".bear" / "plans"
    plan_dir.mkdir(parents=True, exist_ok=True)
    plan_file = plan_dir / "plan-abc123.md"
    engine = _plan_engine(workspace, plan_file)

    sibling = plan_dir / "other.md"
    assert engine.authorize("write_file", {"file_path": str(sibling), "content": "x"}).action == "deny"
    denied = await execute_tool("write_file", {"file_path": str(sibling), "content": "x"}, {}, policy=engine)
    assert "Action denied" in denied
    assert not sibling.exists()
