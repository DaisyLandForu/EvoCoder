from __future__ import annotations

import asyncio

import pytest
from agents.policy import PolicyDecision
from agents.tool_scheduler import NormalizedToolCall, ScheduledToolResult, ToolCallScheduler


async def _allow(_call: NormalizedToolCall) -> PolicyDecision:
    return PolicyDecision(action="allow")


@pytest.mark.asyncio
async def test_two_distinct_calls_each_execute_once() -> None:
    seen: list[str] = []

    async def execute(call: NormalizedToolCall) -> str:
        seen.append(call.tool_call_id)
        return f"ok:{call.name}"

    scheduler = ToolCallScheduler()
    calls = [
        NormalizedToolCall("a", "read_file", {"file_path": "one"}, 0),
        NormalizedToolCall("b", "read_file", {"file_path": "two"}, 1),
    ]
    results = await scheduler.run_round(calls, authorize=_allow, execute=execute)
    assert [item.tool_call_id for item in results] == ["a", "b"]
    assert seen == ["a", "b"]


@pytest.mark.asyncio
async def test_duplicate_tool_call_id_executes_once() -> None:
    seen: list[str] = []

    async def execute(call: NormalizedToolCall) -> str:
        seen.append(call.tool_call_id)
        return "ok"

    scheduler = ToolCallScheduler()
    calls = [
        NormalizedToolCall("dup", "read_file", {}, 0),
        NormalizedToolCall("dup", "read_file", {}, 1),
    ]
    results = await scheduler.run_round(calls, authorize=_allow, execute=execute)
    assert seen == ["dup"]
    assert results[0].status == "success"
    assert results[1].status == "skipped"


@pytest.mark.asyncio
async def test_concurrent_readonly_results_keep_source_order() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    order: list[str] = []

    async def execute(call: NormalizedToolCall) -> str:
        order.append(f"start:{call.tool_call_id}")
        if call.tool_call_id == "slow":
            started.set()
            await release.wait()
        elif call.tool_call_id == "fast":
            await started.wait()
            release.set()
        order.append(f"end:{call.tool_call_id}")
        return call.tool_call_id

    scheduler = ToolCallScheduler()
    calls = [
        NormalizedToolCall("slow", "read_file", {}, 0),
        NormalizedToolCall("mid", "list_files", {"pattern": "*"}, 1),
        NormalizedToolCall("fast", "grep_search", {"pattern": "x"}, 2),
    ]
    results = await scheduler.run_round(calls, authorize=_allow, execute=execute)
    assert [item.tool_call_id for item in results] == ["slow", "mid", "fast"]
    assert [item.content for item in results] == ["slow", "mid", "fast"]


@pytest.mark.asyncio
async def test_one_failure_does_not_drop_independent_results() -> None:
    async def execute(call: NormalizedToolCall) -> ScheduledToolResult:
        if call.name == "boom":
            raise RuntimeError("explode")
        return ScheduledToolResult(call.tool_call_id, call.name, "success", f"ok:{call.name}")

    scheduler = ToolCallScheduler()
    calls = [
        NormalizedToolCall("1", "read_file", {}, 0),
        NormalizedToolCall("2", "boom", {}, 1),
        NormalizedToolCall("3", "list_files", {"pattern": "*"}, 2),
    ]
    results = await scheduler.run_round(calls, authorize=_allow, execute=execute)
    assert results[0].status == "success"
    assert results[1].status == "error"
    assert results[2].status == "success"
    assert "ok:list_files" in results[2].content


@pytest.mark.asyncio
async def test_openai_and_anthropic_share_scheduler() -> None:
    scheduler = ToolCallScheduler()
    openai_calls = scheduler.normalize_openai(
        [{"id": "o1", "type": "function", "function": {"name": "read_file", "arguments": '{"file_path":"a"}'}}]
    )
    anthropic_calls = scheduler.normalize_anthropic(
        [{"id": "a1", "name": "read_file", "input": {"file_path": "b"}}]
    )
    assert openai_calls[0].name == anthropic_calls[0].name == "read_file"
    executed: list[str] = []

    async def execute(call: NormalizedToolCall) -> str:
        executed.append(call.tool_call_id)
        return "ok"

    await scheduler.run_round(openai_calls + anthropic_calls, authorize=_allow, execute=execute)
    assert executed == ["o1", "a1"]


@pytest.mark.asyncio
async def test_write_tools_are_not_unconstrained_concurrent() -> None:
    current = 0
    max_current = 0

    async def execute(call: NormalizedToolCall) -> str:
        nonlocal current, max_current
        current += 1
        max_current = max(max_current, current)
        await asyncio.sleep(0.02)
        current -= 1
        return "ok"

    scheduler = ToolCallScheduler()
    calls = [
        NormalizedToolCall("w1", "write_file", {"file_path": "a", "content": "1"}, 0),
        NormalizedToolCall("w2", "write_file", {"file_path": "b", "content": "2"}, 1),
        NormalizedToolCall("s1", "run_shell", {"command": "echo hi"}, 2),
    ]
    await scheduler.run_round(calls, authorize=_allow, execute=execute)
    assert max_current == 1
