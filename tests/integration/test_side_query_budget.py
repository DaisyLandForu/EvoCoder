from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from agents.agent import Agent
from agents.runtime_config import RuntimeConfig


@pytest.mark.asyncio
async def test_side_query_usage_counts_toward_parent_budget(workspace: Path) -> None:
    config = RuntimeConfig(
        provider="openai",
        model="gpt-4o-mini",
        api_key="test-key",
        base_url="https://example.invalid/v1",
        workspace=workspace,
        max_cost_usd=1.0,
    )
    agent = Agent(config=config, is_sub_agent=True)
    agent._record_usage(100, 20, known=True)

    async def fake_call():
        return SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7),
            choices=[SimpleNamespace(message=SimpleNamespace(content="memory hit"))],
        )

    await agent._trace_side_query_call(fake_call, system="sys", user_message="user")
    assert agent.total_input_tokens == 111
    assert agent.total_output_tokens == 27
    assert agent._budget.input_tokens == 111
    assert agent._budget.unknown_usage_count == 0


@pytest.mark.asyncio
async def test_child_and_side_query_share_one_budget(workspace: Path) -> None:
    parent = RuntimeConfig(
        provider="openai",
        model="gpt-4o-mini",
        api_key="test-key",
        workspace=workspace,
        max_cost_usd=0.01,
    )
    parent_agent = Agent(config=parent, is_sub_agent=True)
    child_agent = Agent(config=parent.derive_child(agent_type="explore"), parent_agent=parent_agent, is_sub_agent=True)
    child_agent._record_usage(50, 10, known=True)
    parent_agent._record_usage(25, 5, known=True)
    parent_agent._absorb_child_usage()
    assert parent_agent.total_input_tokens == 75
    assert parent_agent.total_output_tokens == 15
    assert parent.budget is child_agent.config.budget
