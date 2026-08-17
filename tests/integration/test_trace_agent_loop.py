from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from agents.agent import Agent
from agents.memory import MemoryPrefetch
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
    snapshot = agent._budget.snapshot()
    assert snapshot["estimated_cost_usd"] is None
    assert snapshot["usage_status"] == "unknown"
    assert agent._budget.exceeded()["exceeded"] is False
    events = read_trace(trace_path)
    response = next(event for event in events if event["event_type"] == "model_response")
    assert response["input_tokens"] is None
    assert response["output_tokens"] is None
    assert response["usage_status"] == "unknown"
    finished = next(event for event in events if event["event_type"] == "run_finished")
    assert finished["estimated_cost"] is None
    assert finished["input_tokens"] is None
    assert finished["output_tokens"] is None


@pytest.mark.asyncio
async def test_unknown_usage_with_max_cost_cannot_bypass_budget(workspace: Path, tmp_path: Path) -> None:
    config = RuntimeConfig(
        provider="openai",
        model="gpt-4o-mini",
        api_key="test-key",
        base_url="https://example.invalid/v1",
        workspace=workspace,
        permission_mode="bypassPermissions",
        trace_enabled=True,
        trace_path=tmp_path / "cost.jsonl",
        max_cost_usd=0.01,
        max_turns=4,
    )
    agent = Agent(config=config, is_sub_agent=True)
    agent._build_side_query = lambda **_kwargs: None

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
    snapshot = agent._budget.snapshot()
    assert snapshot["estimated_cost_usd"] is None
    assert snapshot["unknown_usage_count"] == 1
    decision = agent._budget.exceeded()
    assert decision["exceeded"] is True
    assert "incomplete" in decision["reason"].lower()


@pytest.mark.asyncio
async def test_second_chat_uses_a_new_run_id(workspace: Path, tmp_path: Path) -> None:
    trace_dir = tmp_path / "traces"
    agent = _agent(workspace, trace_dir)

    async def fake_stream() -> dict:
        return {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "ok", "tool_calls": None},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        }

    agent._call_openai_stream = fake_stream
    await agent.chat("first")
    first_id = agent._run_id
    await agent.chat("second")
    second_id = agent._run_id
    assert first_id != second_id
    first_events = read_trace(trace_dir / f"{first_id}.jsonl")
    second_events = read_trace(trace_dir / f"{second_id}.jsonl")
    assert validate_trace(first_events) == []
    assert validate_trace(second_events) == []
    assert [event["event_type"] for event in first_events][0] == "run_start"
    assert [event["event_type"] for event in first_events][-1] == "run_finished"
    assert sum(1 for event in first_events if event["event_type"] == "run_start") == 1
    assert sum(1 for event in second_events if event["event_type"] == "run_start") == 1


@pytest.mark.asyncio
async def test_child_model_events_link_to_subagent_step(workspace: Path, tmp_path: Path, monkeypatch) -> None:
    trace_path = tmp_path / "child.jsonl"
    agent = _agent(workspace, trace_path)
    steps = [
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-agent",
                                "type": "function",
                                "function": {
                                    "name": "agent",
                                    "arguments": '{"type": "general", "description": "child", "prompt": "say hi"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 8, "completion_tokens": 3},
        },
        {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "child hi", "tool_calls": None},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2},
        },
        {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "parent done", "tool_calls": None},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 6, "completion_tokens": 2},
        },
    ]

    async def fake_stream(self) -> dict:
        return steps.pop(0)

    monkeypatch.setattr(Agent, "_call_openai_stream", fake_stream)
    await agent.chat("use a subagent")
    events = read_trace(trace_path)
    assert validate_trace(events) == []
    started = next(event for event in events if event["event_type"] == "subagent_started")
    child_requests = [
        event
        for event in events
        if event["event_type"] == "model_request" and event.get("parent_step_id") == started["step_id"]
    ]
    assert child_requests, [event["event_type"] for event in events]


@pytest.mark.asyncio
async def test_skill_candidate_is_emitted_before_run_finished(
    workspace: Path, tmp_path: Path, monkeypatch
) -> None:
    trace_dir = tmp_path / "skill-traces"
    config = RuntimeConfig(
        provider="openai",
        model="gpt-4o-mini",
        api_key="test-key",
        base_url="https://example.invalid/v1",
        workspace=workspace,
        permission_mode="bypassPermissions",
        trace_enabled=True,
        trace_path=trace_dir,
        max_turns=4,
        memory_enabled=False,
    )
    agent = Agent(config=config, is_sub_agent=False)

    async def dummy_side_query(_system: str, _user: str) -> str:
        return "{}"

    agent._build_side_query = lambda **_kwargs: dummy_side_query

    async def fake_stream() -> dict:
        agent._emit_text("done")
        return {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "done", "tool_calls": None},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1},
        }

    async def fake_ingest(**_kwargs):
        from agents.trace import emit_trace

        emit_trace("skill_candidate_created", skill_name="demo", skill_version="0.1.1")
        return {"ok": True, "action": "add", "skill": "demo", "version": "0.1.1"}

    agent._call_openai_stream = fake_stream
    monkeypatch.setattr("agents.online_skill_evolution.online_ingest", fake_ingest)
    await agent.chat("first turn")
    await agent.chat("second turn")
    events = read_trace(trace_dir / f"{agent._run_id}.jsonl")
    types = [event["event_type"] for event in events]
    assert "skill_candidate_created" in types
    assert types[-1] == "run_finished"
    assert types.index("skill_candidate_created") < types.index("run_finished")
    assert validate_trace(events) == []


@pytest.mark.asyncio
async def test_memory_prefetch_cannot_emit_after_run_finished(
    workspace: Path, tmp_path: Path, monkeypatch
) -> None:
    trace_path = tmp_path / "memory.jsonl"
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
    agent = Agent(config=config, is_sub_agent=False)
    hold = asyncio.Event()
    monkeypatch.setenv("BEAR_AUTO_SKILL_EVOLUTION", "0")

    async def blocked_side_query(_system: str, _user: str) -> str:
        async def _call():
            await hold.wait()
            return SimpleNamespace(
                usage=SimpleNamespace(prompt_tokens=7, completion_tokens=2),
                choices=[SimpleNamespace(message=SimpleNamespace(content="late memory"))],
            )

        await agent._trace_side_query_call(_call, system="sys", user_message="user")
        return "[]"

    monkeypatch.setattr(
        "agents.agent.start_memory_prefetch",
        lambda *_args, **_kwargs: MemoryPrefetch(asyncio.create_task(blocked_side_query("sys", "user"))),
    )

    async def fake_stream() -> dict:
        agent._emit_text("fast")
        return {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "fast", "tool_calls": None},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1},
        }

    agent._call_openai_stream = fake_stream
    await agent.chat("please recall the project background")
    settled = read_trace(trace_path)
    hold.set()
    await asyncio.sleep(0.05)

    events = read_trace(trace_path)
    assert [event["event_type"] for event in events] == [
        event["event_type"] for event in settled
    ]
    assert validate_trace(events) == []
    assert events[-1]["event_type"] == "run_finished"
    cancelled = [event for event in events if event.get("memory_status") == "cancelled"]
    assert cancelled
    assert agent.total_input_tokens == 3
    assert agent.total_output_tokens == 1


@pytest.mark.asyncio
async def test_run_finished_reports_this_run_not_session_totals(
    workspace: Path, tmp_path: Path
) -> None:
    trace_dir = tmp_path / "per-run"
    agent = _agent(workspace, trace_dir)

    async def fake_stream() -> dict:
        return {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "ok", "tool_calls": None},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        }

    agent._call_openai_stream = fake_stream
    await agent.chat("first")
    first_id = agent._run_id
    await agent.chat("second")
    second_id = agent._run_id

    first = next(
        event for event in read_trace(trace_dir / f"{first_id}.jsonl")
        if event["event_type"] == "run_finished"
    )
    second = next(
        event for event in read_trace(trace_dir / f"{second_id}.jsonl")
        if event["event_type"] == "run_finished"
    )
    assert (first["input_tokens"], first["output_tokens"]) == (2, 1)
    assert (second["input_tokens"], second["output_tokens"]) == (2, 1)
    assert (second["session_input_tokens"], second["session_output_tokens"]) == (4, 2)
    assert second["estimated_cost"] < second["session_estimated_cost"]
