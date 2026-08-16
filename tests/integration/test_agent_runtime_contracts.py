"""Agent-level contracts: budget message closure, model timeout, and resume state."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from agents.agent import Agent
from agents.runtime_config import RuntimeConfig


def _agent(workspace: Path, *, provider: str = "openai", **kwargs) -> Agent:
    config = RuntimeConfig(
        provider=provider,
        model="gpt-4o-mini" if provider == "openai" else "claude-sonnet-4-6",
        api_key="test-key",
        base_url="https://example.invalid/v1" if provider == "openai" else None,
        workspace=workspace,
        **kwargs,
    )
    return Agent(config=config, is_sub_agent=True)


def _openai_tool_response(*tool_call_ids: str) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": tool_call_id,
                            "type": "function",
                            "function": {"name": "read_file", "arguments": '{"file_path": "src/app.py"}'},
                        }
                        for tool_call_id in tool_call_ids
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


@pytest.mark.asyncio
async def test_openai_budget_stop_closes_every_tool_call(workspace: Path, monkeypatch) -> None:
    agent = _agent(workspace, max_turns=1)

    async def fake_stream() -> dict:
        return _openai_tool_response("call-1", "call-2")

    monkeypatch.setattr(agent, "_call_openai_stream", fake_stream)
    await agent.chat("read the app")

    messages = agent._openai_messages
    assert messages[-3]["role"] == "assistant"
    replies = messages[-2:]
    assert [m["role"] for m in replies] == ["tool", "tool"]
    assert [m["tool_call_id"] for m in replies] == ["call-1", "call-2"]
    assert all("skipped" in m["content"] for m in replies)


@pytest.mark.asyncio
async def test_anthropic_and_openai_agree_on_budget_closure(workspace: Path, monkeypatch) -> None:
    """Both backends must answer every requested tool call before stopping."""
    agent = _agent(workspace, max_turns=1)
    monkeypatch.setattr(agent, "_call_openai_stream", lambda: _async(_openai_tool_response("only-one")))
    await agent.chat("hi")

    pending = [m for m in agent._openai_messages if m.get("role") == "assistant" and m.get("tool_calls")]
    answered = {m["tool_call_id"] for m in agent._openai_messages if m.get("role") == "tool"}
    requested = {tc["id"] for m in pending for tc in m["tool_calls"]}
    assert requested == answered


async def _async(value):
    return value


@pytest.mark.asyncio
async def test_model_timeout_is_enforced(workspace: Path) -> None:
    agent = _agent(workspace, model_timeout_s=0.05)

    async def slow():
        await asyncio.sleep(5)
        return "never"

    with pytest.raises(TimeoutError):
        await agent._call_model_with_timeout(slow)


@pytest.mark.asyncio
async def test_model_timeout_disabled_when_not_positive(workspace: Path) -> None:
    agent = _agent(workspace, model_timeout_s=0)

    async def quick():
        return "done"

    assert await agent._call_model_with_timeout(quick) == "done"


def test_resume_restores_folded_memory_and_session_identity(workspace: Path) -> None:
    agent = _agent(workspace)
    payload = {
        "metadata": {"id": "sess-1234", "startTime": "2026-01-01T00:00:00Z"},
        "openaiMessages": [
            {"role": "system", "content": "old system prompt"},
            {"role": "user", "content": "continue the migration"},
        ],
        "foldedSessionMemories": [
            {
                "episode_memory": {
                    "task_description": "migrate the parser",
                    "key_events": ["read tests"],
                    "current_progress": "lexer half ported",
                },
                "working_memory": {
                    "immediate_goal": "port the lexer",
                    "current_challenges": "ambiguous token",
                    "next_actions": [{"type": "edit", "description": "write the lexer"}],
                },
                "tool_memory": {"tools_used": ["pytest"], "derived_rules": ["pytest -q works"]},
            }
        ],
    }
    agent.restore_session(payload)

    assert agent.session_id == "sess-1234"
    assert agent.session_start_time == "2026-01-01T00:00:00Z"
    assert len(agent._folded_session_memories) == 1
    restored = agent._folded_session_memories[0]
    assert restored["episode_memory"]["task_description"] == "migrate the parser"
    assert restored["working_memory"]["current_challenges"] == "ambiguous token"
    assert restored["working_memory"]["next_actions"][0]["description"] == "write the lexer"
    assert agent._openai_messages[0]["role"] == "system"
    assert agent._openai_messages[0]["content"] == agent._system_prompt
