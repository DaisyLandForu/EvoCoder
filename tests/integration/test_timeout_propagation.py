"""RuntimeConfig timeouts must reach side queries and shell execution, not just the main stream."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from agents.agent import Agent
from agents.policy import PolicyEngine, ShellResult
from agents.runtime_config import RuntimeConfig
from agents.tools import execute_tool


class _SlowMessages:
    async def create(self, **_kwargs):
        await asyncio.sleep(0.2)
        raise AssertionError("side query should have timed out")


class _SlowAnthropicClient:
    def __init__(self) -> None:
        self.messages = _SlowMessages()


class _SlowCompletions:
    async def create(self, **_kwargs):
        await asyncio.sleep(0.2)
        raise AssertionError("side query should have timed out")


class _SlowOpenAIClient:
    def __init__(self) -> None:
        self.chat = type("_Chat", (), {"completions": _SlowCompletions()})()


class _RecordingShellBackend:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run(self, command: str, *, cwd: Path, timeout_s: float, env) -> ShellResult:
        self.calls.append({"command": command, "cwd": cwd, "timeout_s": timeout_s})
        return ShellResult(exit_code=0, stdout="ok", stderr="")


def _agent(workspace: Path, *, provider: str, **kwargs) -> Agent:
    config = RuntimeConfig(
        provider=provider,
        model="gpt-4o-mini" if provider == "openai" else "claude-sonnet-4-6",
        api_key="test-key",
        base_url="https://example.invalid/v1" if provider == "openai" else None,
        workspace=workspace,
        **kwargs,
    )
    return Agent(config=config, is_sub_agent=True)


@pytest.mark.asyncio
async def test_anthropic_side_query_uses_model_timeout(workspace: Path) -> None:
    agent = _agent(workspace, provider="anthropic", model_timeout_s=0.05)
    agent._anthropic_client = _SlowAnthropicClient()
    side_query = agent._build_side_query()
    assert side_query is not None
    with pytest.raises(TimeoutError):
        await side_query("system", "user")


@pytest.mark.asyncio
async def test_openai_side_query_uses_model_timeout(workspace: Path) -> None:
    agent = _agent(workspace, provider="openai", model_timeout_s=0.05)
    agent._openai_client = _SlowOpenAIClient()
    side_query = agent._build_side_query()
    assert side_query is not None
    with pytest.raises(TimeoutError):
        await side_query("system", "user")


@pytest.mark.asyncio
async def test_shell_without_explicit_timeout_uses_runtime_tool_timeout(workspace: Path) -> None:
    backend = _RecordingShellBackend()
    engine = PolicyEngine(
        workspace=workspace,
        permission_mode="acceptEdits",
        tool_timeout_s=1.25,
        shell_backend=backend,
    )
    await execute_tool("run_shell", {"command": "echo hi"}, {}, policy=engine)
    assert backend.calls[0]["timeout_s"] == pytest.approx(1.25)
    assert backend.calls[0]["cwd"] == workspace


@pytest.mark.asyncio
async def test_shell_honours_an_explicit_tool_timeout(workspace: Path) -> None:
    backend = _RecordingShellBackend()
    engine = PolicyEngine(
        workspace=workspace,
        permission_mode="acceptEdits",
        tool_timeout_s=1.25,
        shell_backend=backend,
    )
    await execute_tool("run_shell", {"command": "echo hi", "timeout": 4000}, {}, policy=engine)
    assert backend.calls[0]["timeout_s"] == pytest.approx(4.0)


@pytest.mark.asyncio
async def test_agent_policy_receives_configured_tool_timeout(workspace: Path) -> None:
    agent = _agent(workspace, provider="openai", tool_timeout_s=2.5)
    assert agent._policy.tool_timeout_s == pytest.approx(2.5)
    child_config, child_policy = agent._child_runtime(agent_type="general")
    assert child_policy.tool_timeout_s == pytest.approx(2.5)
    assert child_config.model_timeout_s == agent.config.model_timeout_s
