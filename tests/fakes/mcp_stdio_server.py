"""Minimal stdio JSON-RPC MCP servers used by P0 tests."""

from __future__ import annotations

import json
import sys
import time


def _read() -> dict | None:
    line = sys.stdin.readline()
    if not line:
        return None
    return json.loads(line)


def _write(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def _handle_initialize(msg: dict) -> None:
    _write({"jsonrpc": "2.0", "id": msg["id"], "result": {"protocolVersion": "2024-11-05", "capabilities": {}}})


def _handle_list(msg: dict, tools: list[dict]) -> None:
    _write({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": tools}})


def run_echo_server() -> None:
    tools = [
        {
            "name": "echo",
            "description": "Echo text",
            "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
        }
    ]
    while True:
        msg = _read()
        if msg is None:
            return
        method = msg.get("method")
        if method == "initialize":
            _handle_initialize(msg)
        elif method == "tools/list":
            _handle_list(msg, tools)
        elif method == "tools/call":
            text = str((msg.get("params") or {}).get("arguments", {}).get("text") or "")
            _write(
                {
                    "jsonrpc": "2.0",
                    "id": msg["id"],
                    "result": {
                        "isError": False,
                        "content": [{"type": "text", "text": text}],
                        "structuredContent": {"echo": text},
                    },
                }
            )


def run_fast_server() -> None:
    run_echo_server()


def run_hang_call_server() -> None:
    tools = [{"name": "hang", "description": "Never returns", "inputSchema": {"type": "object", "properties": {}}}]
    while True:
        msg = _read()
        if msg is None:
            return
        method = msg.get("method")
        if method == "initialize":
            _handle_initialize(msg)
        elif method == "tools/list":
            _handle_list(msg, tools)
        elif method == "tools/call":
            time.sleep(30)


def run_crash_server() -> None:
    tools = [{"name": "boom", "description": "Crashes", "inputSchema": {"type": "object", "properties": {}}}]
    while True:
        msg = _read()
        if msg is None:
            return
        method = msg.get("method")
        if method == "initialize":
            _handle_initialize(msg)
        elif method == "tools/list":
            _handle_list(msg, tools)
        elif method == "tools/call":
            sys.exit(1)


def run_stderr_flood_server() -> None:
    tools = [{"name": "ok", "description": "ok", "inputSchema": {"type": "object", "properties": {}}}]
    for _ in range(2000):
        sys.stderr.write("x" * 1024 + "\n")
        sys.stderr.flush()
    while True:
        msg = _read()
        if msg is None:
            return
        method = msg.get("method")
        if method == "initialize":
            _handle_initialize(msg)
        elif method == "tools/list":
            _handle_list(msg, tools)
        elif method == "tools/call":
            _write(
                {
                    "jsonrpc": "2.0",
                    "id": msg["id"],
                    "result": {"isError": False, "content": [{"type": "text", "text": "ok"}]},
                }
            )


def run_fail_boot_server() -> None:
    sys.exit(2)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "echo"
    {
        "echo": run_echo_server,
        "fast": run_fast_server,
        "hang": run_hang_call_server,
        "crash": run_crash_server,
        "stderr": run_stderr_flood_server,
        "failboot": run_fail_boot_server,
    }[mode]()
