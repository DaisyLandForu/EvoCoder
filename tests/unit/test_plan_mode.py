from __future__ import annotations

from pathlib import Path

import pytest
from agents.agent import Agent
from agents.plan_mode import PlanModeController, PlanPhase
from agents.policy import PolicyEngine
from agents.runtime_config import RuntimeConfig


@pytest.mark.asyncio
async def test_plan_mode_blocks_write_and_shell(workspace: Path) -> None:
    engine = PolicyEngine(workspace=workspace, permission_mode="plan")
    assert engine.authorize("write_file", {"file_path": str(workspace / "a.py"), "content": "x"}).action == "deny"
    assert engine.authorize("run_shell", {"command": "echo hi"}).action == "deny"


@pytest.mark.asyncio
async def test_approval_function_is_awaited(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan = workspace / "plan.md"
    plan.write_text("# Plan\nDo the work\n", encoding="utf-8")
    awaited = {"called": False}

    async def approve(content: str) -> dict:
        awaited["called"] = True
        awaited["content"] = content
        return {"choice": "execute"}

    controller = PlanModeController(previous_mode="default")
    controller.enter(str(plan), previous_mode="default")
    approval = await controller.request_approval(approve)
    assert awaited["called"] is True
    assert "Do the work" in awaited["content"]
    assert approval.choice == "execute"
    controller.apply_approval(approval)
    assert controller.phase == PlanPhase.EXECUTING
    assert controller.execution_mode == "acceptEdits"


@pytest.mark.asyncio
async def test_rejected_plan_stays_readonly(workspace: Path) -> None:
    plan = workspace / "plan.md"
    plan.write_text("stay planning", encoding="utf-8")

    async def reject(content: str) -> dict:
        return {"choice": "keep-planning", "feedback": "more detail"}

    controller = PlanModeController()
    controller.enter(str(plan))
    approval = await controller.request_approval(reject)
    controller.apply_approval(approval)
    assert controller.phase == PlanPhase.PLANNING
    assert controller.readonly is True


@pytest.mark.asyncio
async def test_system_prompt_syncs_for_both_providers(workspace: Path) -> None:
    openai_agent = Agent(
        config=RuntimeConfig(provider="openai", base_url="https://example.test/v1", api_key="k", workspace=workspace),
    )
    anthropic_agent = Agent(
        config=RuntimeConfig(provider="anthropic", api_key="k", workspace=workspace),
    )
    openai_agent.toggle_plan_mode()
    anthropic_agent.toggle_plan_mode()
    assert "Plan Mode Active" in openai_agent._system_prompt
    assert "Plan Mode Active" in anthropic_agent._system_prompt
    assert "Plan Mode Active" in openai_agent._openai_messages[0]["content"]
    openai_agent.toggle_plan_mode()
    anthropic_agent.toggle_plan_mode()
    assert "Plan Mode Active" not in openai_agent._system_prompt
    assert "Plan Mode Active" not in anthropic_agent._system_prompt


@pytest.mark.asyncio
async def test_plan_file_content_is_submitted_for_approval(workspace: Path) -> None:
    agent = Agent(
        config=RuntimeConfig(provider="openai", base_url="https://example.test/v1", api_key="k", workspace=workspace),
    )
    agent.toggle_plan_mode()
    Path(agent._plan_file_path).write_text("IMPLEMENT STEP 1", encoding="utf-8")
    seen: dict[str, str] = {}

    async def approve(content: str) -> dict:
        seen["content"] = content
        return {"choice": "keep-planning", "feedback": "ok"}

    agent.set_plan_approval_fn(approve)
    result = await agent._execute_plan_mode_tool("exit_plan_mode")
    assert seen["content"] == "IMPLEMENT STEP 1"
    assert "keep planning" in result.lower()
    assert agent.permission_mode == "plan"
