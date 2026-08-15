"""Stdio JSON-RPC MCP client with timeouts, pending-future safety, and process cleanup."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from .policy import is_mcp_config_trusted
from .ui import print_error, print_info


class McpError(RuntimeError):
    """Raised when an MCP server returns an error or the connection fails."""


class McpConnection:
    """One stdio MCP server subprocess and its JSON-RPC request map."""

    def __init__(
        self,
        server_name: str,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        *,
        initialize_timeout_s: float = 15.0,
        list_timeout_s: float = 15.0,
        call_timeout_s: float = 30.0,
    ) -> None:
        self.server_name = server_name
        self.command = command
        self.args = args or []
        self.env = env or {}
        self.initialize_timeout_s = initialize_timeout_s
        self.list_timeout_s = list_timeout_s
        self.call_timeout_s = call_timeout_s
        self._process: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._reader_task: asyncio.Task[Any] | None = None
        self._stderr_task: asyncio.Task[Any] | None = None
        self._closed = False

    async def connect(self) -> None:
        merged_env = {**os.environ, **self.env}
        self._process = await asyncio.create_subprocess_exec(
            self.command,
            *self.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=merged_env,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _read_loop(self) -> None:
        assert self._process and self._process.stdout
        try:
            while True:
                line = await self._process.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg_id = msg.get("id")
                if msg_id is None or msg_id not in self._pending:
                    continue
                fut = self._pending.pop(msg_id)
                if fut.done():
                    continue
                if "error" in msg:
                    error = msg["error"] if isinstance(msg["error"], dict) else {"message": str(msg["error"])}
                    fut.set_exception(
                        McpError(f"MCP error {error.get('code')}: {error.get('message')}")
                    )
                else:
                    fut.set_result(msg.get("result"))
        finally:
            self._fail_pending(McpError(f"MCP server '{self.server_name}' closed"))

    async def _drain_stderr(self) -> None:
        assert self._process and self._process.stderr
        try:
            while True:
                chunk = await self._process.stderr.read(4096)
                if not chunk:
                    break
        except Exception:
            return

    async def _send_request(self, method: str, params: dict | None = None, *, timeout_s: float) -> Any:
        if self._closed or not self._process or not self._process.stdin:
            raise McpError(f"MCP server '{self.server_name}' is not connected")
        req_id = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        # Register before writing so a fast response cannot be dropped.
        self._pending[req_id] = fut
        payload = json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})
        try:
            self._process.stdin.write((payload + "\n").encode("utf-8"))
            await self._process.stdin.drain()
            return await asyncio.wait_for(fut, timeout=timeout_s)
        except Exception:
            pending = self._pending.pop(req_id, None)
            if pending is not None and not pending.done():
                pending.cancel()
            raise

    def _send_notification(self, method: str, params: dict | None = None) -> None:
        if not self._process or not self._process.stdin:
            return
        payload = json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}})
        self._process.stdin.write((payload + "\n").encode("utf-8"))

    async def initialize(self) -> None:
        await self._send_request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "mini-claude", "version": "1.0.0"},
            },
            timeout_s=self.initialize_timeout_s,
        )
        self._send_notification("notifications/initialized")

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._send_request("tools/list", timeout_s=self.list_timeout_s)
        if not result or not isinstance(result.get("tools"), list):
            return []
        return [
            {
                "name": t["name"],
                "description": t.get("description", ""),
                "inputSchema": t.get("inputSchema"),
                "serverName": self.server_name,
            }
            for t in result["tools"]
            if isinstance(t, dict) and t.get("name")
        ]

    async def call_tool(self, name: str, args: dict) -> str:
        result = await self._send_request(
            "tools/call",
            {"name": name, "arguments": args},
            timeout_s=self.call_timeout_s,
        )
        return self._format_tool_result(result)

    @staticmethod
    def _format_tool_result(result: Any) -> str:
        if not isinstance(result, dict):
            return json.dumps(result, ensure_ascii=False)
        is_error = bool(result.get("isError"))
        texts: list[str] = []
        content = result.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(str(block.get("text") or ""))
        structured = result.get("structuredContent")
        payload: dict[str, Any] = {
            "isError": is_error,
            "text": "\n".join(part for part in texts if part),
        }
        if structured is not None:
            payload["structuredContent"] = structured
        if not payload["text"] and structured is None:
            payload["raw"] = result
        if is_error and not payload["text"]:
            payload["text"] = "MCP tool returned isError=true"
        return json.dumps(payload, ensure_ascii=False)

    def _fail_pending(self, error: Exception) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(error)
        self._pending.clear()

    async def close(self) -> None:
        self._closed = True
        self._fail_pending(McpError(f"MCP server '{self.server_name}' closed"))
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                task.cancel()
        self._reader_task = None
        self._stderr_task = None
        process = self._process
        self._process = None
        if process is None or process.returncode is not None:
            return
        try:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        except ProcessLookupError:
            return


class McpManager:
    """Load MCP configs, connect servers, and route mcp__server__tool calls."""

    def __init__(self, *, workspace: Path | None = None, trusted: bool = False) -> None:
        self.workspace = Path(workspace).resolve() if workspace else Path.cwd().resolve()
        self.trusted = trusted
        self._connections: dict[str, McpConnection] = {}
        self._tools: list[dict[str, Any]] = []
        self._connected = False

    async def load_and_connect(self) -> None:
        if self._connected:
            return
        self._connected = True
        if not self.trusted and not is_mcp_config_trusted(self.workspace):
            print_info("MCP project config is not trusted; servers were not started.")
            return
        configs = self._load_configs()
        if not configs:
            return
        for name, cfg in configs.items():
            conn = McpConnection(
                name,
                cfg["command"],
                cfg.get("args"),
                cfg.get("env"),
            )
            try:
                await conn.connect()
                await conn.initialize()
                server_tools = await conn.list_tools()
                self._connections[name] = conn
                self._tools.extend(server_tools)
                print_info(f"MCP connected: {name} ({len(server_tools)} tools)")
            except Exception as exc:
                print_error(f"MCP failed to connect: {name}: {exc}")
                await conn.close()

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": f"mcp__{t['serverName']}__{t['name']}",
                "description": t.get("description") or f"MCP tool {t['name']} from {t['serverName']}",
                "input_schema": t.get("inputSchema") or {"type": "object", "properties": {}},
            }
            for t in self._tools
        ]

    def is_mcp_tool(self, name: str) -> bool:
        return name.startswith("mcp__")

    async def call_tool(self, prefixed_name: str, args: dict) -> str:
        parts = prefixed_name.split("__")
        if len(parts) < 3:
            raise ValueError(f"Invalid MCP tool name: {prefixed_name}")
        server_name = parts[1]
        tool_name = "__".join(parts[2:])
        conn = self._connections.get(server_name)
        if not conn:
            raise RuntimeError(f"MCP server '{server_name}' not connected")
        return await conn.call_tool(tool_name, args)

    async def disconnect_all(self) -> None:
        for conn in list(self._connections.values()):
            await conn.close()
        self._connections.clear()
        self._tools.clear()
        self._connected = False

    def _load_configs(self) -> dict[str, dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        self._merge_config_file(Path.home() / ".bear" / "settings.json", merged)
        self._merge_config_file(self.workspace / ".bear" / "settings.json", merged)
        self._merge_config_file(self.workspace / ".mcp.json", merged)
        return merged

    def _merge_config_file(self, path: Path, target: dict[str, dict[str, Any]]) -> None:
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text())
            servers = raw.get("mcpServers", raw)
            for name, config in servers.items():
                if isinstance(config, dict) and "command" in config:
                    sanitized = {
                        "command": config["command"],
                        "args": config.get("args") or [],
                    }
                    if isinstance(config.get("env"), dict):
                        sanitized["env"] = {str(k): str(v) for k, v in config["env"].items()}
                    target[name] = sanitized
        except Exception:
            return
