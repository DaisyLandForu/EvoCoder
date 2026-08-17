from __future__ import annotations

from pathlib import Path

import pytest
from agents.agent import Agent
from agents.runtime_config import RuntimeConfig
from agents.trace import read_trace, validate_trace


def _agent(workspace: Path, trace_path: Path) -> Agent:
    config = RuntimeConfig(
        provider="openai",
        model="gpt-4o-mini",
        api_key="test-key",
        base_url="https://example.invalid/v1",
        workspace=workspace,
        permission_mode="bypassPermissions",
        trace_enabled=True,
        trace_path=trace_path,
        max_turns=4,
    )
    agent = Agent(config=config, is_sub_agent=True)
    agent._build_side_query = lambda **_kwargs: None
    return agent


def _openai_tool_then_text() -> list[dict]:
    return [
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-read",
                                "type": "function",
                                "function": {"name": "read_file", "arguments": '{"file_path": "src/app.py"}'},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 12, "completion_tokens": 4},
        },
        {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "app prints ok", "tool_calls": None},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 16, "completion_tokens": 5},
        },
    ]


@pytest.mark.asyncio
async def test_agent_trace_links_model_tool_and_policy(workspace: Path, tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.jsonl"
    agent = _agent(workspace, trace_path)
    steps = _openai_tool_then_text()

    async def fake_stream() -> dict:
        return steps.pop(0)

    agent._call_openai_stream = fake_stream
    await agent.chat("read the app")

    events = read_trace(trace_path)
    assert validate_trace(events) == []
    types = [event["event_type"] for event in events]
    assert types[0] == "run_start"
    assert "model_request" in types
    assert "model_response" in types
    assert "tool_requested" in types
    assert "policy_decision" in types
    assert "tool_started" in types
    assert "tool_finished" in types
    assert types[-1] == "run_finished"
    run_id = events[0]["run_id"]
    assert run_id
    assert all(event["run_id"] == run_id for event in events)
    request = next(event for event in events if event["event_type"] == "tool_requested")
    finished = next(event for event in events if event["event_type"] == "tool_finished")
    assert request["tool_name"] == "read_file"
    assert finished["parent_step_id"]
    assert agent.total_input_tokens == 28
    assert agent.total_output_tokens == 9


@pytest.mark.asyncio
async def test_missing_model_usage_is_unknown_not_zero(workspace: Path, tmp_path: Path) -> None:
    trace_path = tmp_path / "unknown.jsonl"
    agent = _agent(workspace, trace_path)

    async def fake_stream() -> dict:
        return {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "hi", "tool_calls": None},
                    "finish_reason": "stop",
                }
            ],
            "usage": None,
        }

    agent._call_openai_stream = fake_stream
    await agent.chat("hello")
    assert agent.total_input_tokens == 0
    assert agent._budget.unknown_usage_count == 1
    response = next(event for event in read_trace(trace_path) if event["event_type"] == "model_response")
    assert response["input_tokens"] is None
    assert response["output_tokens"] is None
    assert response["usage_status"] == "unknown"
