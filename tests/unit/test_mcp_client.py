from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from agents.mcp_client import McpConnection, McpError, McpManager
from agents.policy import write_mcp_trust

FAKE = Path(__file__).resolve().parents[1] / "fakes" / "mcp_stdio_server.py"


def _server(mode: str) -> tuple[str, list[str]]:
    return sys.executable, [str(FAKE), mode]


@pytest.mark.asyncio
async def test_initialize_and_list_tools() -> None:
    command, args = _server("echo")
    conn = McpConnection("echo", command, args)
    await conn.connect()
    await conn.initialize()
    tools = await conn.list_tools()
    await conn.close()
    assert tools[0]["name"] == "echo"


@pytest.mark.asyncio
async def test_fast_response_is_not_lost() -> None:
    command, args = _server("fast")
    conn = McpConnection("fast", command, args)
    await conn.connect()
    await conn.initialize()
    result = await conn.call_tool("echo", {"text": "ping"})
    await conn.close()
    assert "ping" in result


@pytest.mark.asyncio
async def test_call_timeout_clears_pending_future() -> None:
    command, args = _server("hang")
    conn = McpConnection("hang", command, args, call_timeout_s=0.2)
    await conn.connect()
    await conn.initialize()
    await conn.list_tools()
    with pytest.raises(asyncio.TimeoutError):
        await conn.call_tool("hang", {})
    assert conn._pending == {}
    await conn.close()


@pytest.mark.asyncio
async def test_server_crash_surfaces_error() -> None:
    command, args = _server("crash")
    conn = McpConnection("crash", command, args, call_timeout_s=2.0)
    await conn.connect()
    await conn.initialize()
    await conn.list_tools()
    with pytest.raises(McpError):
        await conn.call_tool("boom", {})
    await conn.close()


@pytest.mark.asyncio
async def test_stderr_flood_does_not_deadlock() -> None:
    command, args = _server("stderr")
    conn = McpConnection("stderr", command, args)
    await conn.connect()
    await asyncio.wait_for(conn.initialize(), timeout=5)
    await asyncio.wait_for(conn.list_tools(), timeout=5)
    result = await asyncio.wait_for(conn.call_tool("ok", {}), timeout=5)
    await conn.close()
    assert "ok" in result


@pytest.mark.asyncio
async def test_one_server_failure_does_not_break_others(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    write_mcp_trust(tmp_path)
    command, echo_args = _server("echo")
    _, fail_args = _server("failboot")
    (tmp_path / ".mcp.json").write_text(
        '{"mcpServers": {"good": {"command": "%s", "args": %s}, "bad": {"command": "%s", "args": %s}}}'
        % (command, echo_args, command, fail_args),
        encoding="utf-8",
    )
    # JSON above used python list repr with single quotes - rewrite properly
    import json

    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "good": {"command": command, "args": echo_args},
                    "bad": {"command": command, "args": fail_args},
                }
            }
        ),
        encoding="utf-8",
    )
    write_mcp_trust(tmp_path)
    manager = McpManager(workspace=tmp_path, trusted=True)
    await manager.load_and_connect()
    assert "mcp__good__echo" in {item["name"] for item in manager.get_tool_definitions()}
    result = await manager.call_tool("mcp__good__echo", {"text": "still-works"})
    await manager.disconnect_all()
    assert "still-works" in result
