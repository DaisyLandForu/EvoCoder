from __future__ import annotations

import json
from pathlib import Path

import pytest
from agents.agent import Agent
from agents.mcp_client import McpManager
from agents.runtime_config import RuntimeConfig
from agents.subagent import get_sub_agent_config
from agents.tool_scheduler import ToolCallScheduler


@pytest.mark.asyncio
async def test_subagent_inherits_custom_openai_base_url(workspace: Path) -> None:
    parent = Agent(
        config=RuntimeConfig(
            provider="openai",
            model="gpt-4o-mini",
            api_key="sk-test",
            base_url="https://gateway.example/v1",
            workspace=workspace,
            permission_mode="default",
            max_cost_usd=1.0,
            max_turns=4,
        )
    )
    child_config, _ = parent._child_runtime(agent_type="general")
    child = Agent(config=child_config, parent_agent=parent, is_sub_agent=True)
    assert str(child._openai_client.base_url).rstrip("/") == "https://gateway.example/v1"
    assert child.permission_mode != "bypassPermissions"
    child._record_usage(1000, 2000)
    parent._absorb_child_usage()
    assert parent.total_input_tokens == 1000
    assert parent.total_output_tokens == 2000


@pytest.mark.asyncio
async def test_plan_subagent_cannot_write_or_shell(workspace: Path) -> None:
    parent = Agent(
        config=RuntimeConfig(provider="anthropic", api_key="k", workspace=workspace, permission_mode="default")
    )
    child_config, child_policy = parent._child_runtime(agent_type="plan")
    assert child_config.permission_mode == "plan"
    assert child_policy.authorize("write_file", {"file_path": str(workspace / "x.py"), "content": "a"}).action == "deny"
    assert child_policy.authorize("run_shell", {"command": "echo hi"}).action == "deny"
    tools = {item["name"] for item in get_sub_agent_config("plan")["tools"]}
    assert "write_file" not in tools
    assert "run_shell" not in tools


@pytest.mark.asyncio
async def test_untrusted_mcp_json_does_not_start_servers(workspace: Path) -> None:
    (workspace / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"echo": {"command": "python3", "args": ["-c", "raise SystemExit(0)"]}}}),
        encoding="utf-8",
    )
    manager = McpManager(workspace=workspace, trusted=False)
    await manager.load_and_connect()
    assert manager.get_tool_definitions() == []
    assert manager._connections == {}


@pytest.mark.asyncio
async def test_scheduler_used_by_agent_dispatch(workspace: Path) -> None:
    agent = Agent(
        config=RuntimeConfig(provider="openai", base_url="https://example.test/v1", api_key="k", workspace=workspace)
    )
    assert isinstance(agent._scheduler, ToolCallScheduler)
    openai_calls = agent._scheduler.normalize_openai(
        [{"id": "1", "function": {"name": "read_file", "arguments": '{"file_path":"src/app.py"}'}}]
    )
    anthropic_calls = agent._scheduler.normalize_anthropic(
        [{"id": "2", "name": "read_file", "input": {"file_path": "src/app.py"}}]
    )
    assert openai_calls[0].name == anthropic_calls[0].name
