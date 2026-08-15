"""Unified tool-call scheduler for OpenAI and Anthropic responses."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal

from .policy import CONCURRENCY_SAFE_TOOLS, PolicyDecision

ToolStatus = Literal["success", "failure", "error", "denied", "skipped"]


@dataclass
class NormalizedToolCall:
    tool_call_id: str
    name: str
    arguments: dict[str, Any]
    source_index: int


@dataclass
class ScheduledToolResult:
    tool_call_id: str
    name: str
    status: ToolStatus
    content: str
    error_type: str | None = None
    source_index: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "success"


AuthorizeFn = Callable[[NormalizedToolCall], Awaitable[PolicyDecision] | PolicyDecision]
ExecuteFn = Callable[[NormalizedToolCall], Awaitable[ScheduledToolResult] | ScheduledToolResult]
ConfirmFn = Callable[[str], Awaitable[bool]]


def _looks_like_failure(name: str, content: str) -> bool:
    text = f"{name}\n{content}".lower()
    return any(marker in text for marker in ("error", "denied", "timed out", "timeout", "failed"))


class ToolCallScheduler:
    """normalize → deduplicate → authorize → execute → commit."""

    def __init__(self, *, concurrency_safe: set[str] | frozenset[str] | None = None) -> None:
        self.concurrency_safe = set(concurrency_safe or CONCURRENCY_SAFE_TOOLS)
        self._seen_ids: set[str] = set()

    def reset_round(self) -> None:
        self._seen_ids.clear()

    def normalize_openai(self, tool_calls: list[dict[str, Any]] | None) -> list[NormalizedToolCall]:
        normalized: list[NormalizedToolCall] = []
        for index, raw in enumerate(tool_calls or []):
            if not isinstance(raw, dict):
                continue
            fn = raw.get("function") if isinstance(raw.get("function"), dict) else {}
            name = str(fn.get("name") or raw.get("name") or "")
            arguments = raw.get("arguments")
            if arguments is None:
                arguments = fn.get("arguments")
            parsed = self._parse_arguments(arguments)
            tool_call_id = str(raw.get("id") or raw.get("tool_call_id") or f"openai-{index}")
            normalized.append(
                NormalizedToolCall(
                    tool_call_id=tool_call_id,
                    name=name,
                    arguments=parsed,
                    source_index=index,
                )
            )
        return normalized

    def normalize_anthropic(self, tool_uses: list[Any] | None) -> list[NormalizedToolCall]:
        normalized: list[NormalizedToolCall] = []
        for index, raw in enumerate(tool_uses or []):
            if isinstance(raw, dict):
                name = str(raw.get("name") or "")
                tool_call_id = str(raw.get("id") or f"anthropic-{index}")
                arguments = self._parse_arguments(raw.get("input") or raw.get("arguments") or {})
            else:
                name = str(getattr(raw, "name", "") or "")
                tool_call_id = str(getattr(raw, "id", "") or f"anthropic-{index}")
                raw_input = getattr(raw, "input", {})
                arguments = self._parse_arguments(dict(raw_input) if hasattr(raw_input, "items") else raw_input)
            normalized.append(
                NormalizedToolCall(
                    tool_call_id=tool_call_id,
                    name=name,
                    arguments=arguments,
                    source_index=index,
                )
            )
        return normalized

    def deduplicate(self, calls: list[NormalizedToolCall]) -> list[NormalizedToolCall]:
        unique: list[NormalizedToolCall] = []
        for call in calls:
            if not call.tool_call_id or call.tool_call_id in self._seen_ids:
                continue
            self._seen_ids.add(call.tool_call_id)
            unique.append(call)
        return unique

    async def run_round(
        self,
        calls: list[NormalizedToolCall],
        *,
        authorize: AuthorizeFn,
        execute: ExecuteFn,
        confirm: ConfirmFn | None = None,
        already_seen: bool = False,
    ) -> list[ScheduledToolResult]:
        unique = []
        original = list(calls)
        for call in original:
            if not call.tool_call_id:
                continue
            if call.tool_call_id in self._seen_ids:
                continue
            self._seen_ids.add(call.tool_call_id)
            unique.append(call)
        results: dict[str, ScheduledToolResult] = {}

        executable: list[NormalizedToolCall] = []
        for call in unique:
            decision = await self._maybe_await(authorize(call))
            if isinstance(decision, PolicyDecision):
                action = decision.action
                message = decision.message
            elif isinstance(decision, dict):
                action = str(decision.get("action") or "deny")
                message = str(decision.get("message") or "")
            else:
                action = "deny"
                message = "invalid policy decision"
            if action == "deny":
                results[call.tool_call_id] = ScheduledToolResult(
                    tool_call_id=call.tool_call_id,
                    name=call.name,
                    status="denied",
                    content=f"Action denied: {message}",
                    error_type="denied",
                    source_index=call.source_index,
                )
                continue
            if action == "confirm":
                allowed = False
                if confirm is not None:
                    allowed = bool(await confirm(message or call.name))
                if not allowed:
                    results[call.tool_call_id] = ScheduledToolResult(
                        tool_call_id=call.tool_call_id,
                        name=call.name,
                        status="denied",
                        content="User denied this action.",
                        error_type="denied",
                        source_index=call.source_index,
                    )
                    continue
            executable.append(call)

        concurrent = [call for call in executable if call.name in self.concurrency_safe]
        serial = [call for call in executable if call.name not in self.concurrency_safe]

        if concurrent:
            gathered = await asyncio.gather(
                *[self._run_one(call, execute) for call in concurrent],
                return_exceptions=True,
            )
            for call, item in zip(concurrent, gathered):
                results[call.tool_call_id] = self._coerce_result(call, item)

        context_break = False
        for call in serial:
            if context_break:
                results[call.tool_call_id] = ScheduledToolResult(
                    tool_call_id=call.tool_call_id,
                    name=call.name,
                    status="skipped",
                    content="Tool execution skipped because the conversation context was reset.",
                    error_type="context_cleared",
                    source_index=call.source_index,
                )
                continue
            item = await self._run_one(call, execute)
            result = self._coerce_result(call, item)
            results[call.tool_call_id] = result
            if result.error_type == "context_cleared":
                context_break = True

        ordered: list[ScheduledToolResult] = []
        emitted: set[str] = set()
        for call in original:
            result = results.get(call.tool_call_id)
            is_first = call.tool_call_id not in emitted
            if is_first:
                emitted.add(call.tool_call_id)
            if result is None:
                ordered.append(
                    ScheduledToolResult(
                        tool_call_id=call.tool_call_id,
                        name=call.name,
                        status="skipped",
                        content="Tool execution skipped.",
                        source_index=call.source_index,
                    )
                )
                continue
            if is_first:
                copy = ScheduledToolResult(
                    tool_call_id=result.tool_call_id,
                    name=result.name,
                    status=result.status,
                    content=result.content,
                    error_type=result.error_type,
                    source_index=call.source_index,
                )
            else:
                copy = ScheduledToolResult(
                    tool_call_id=call.tool_call_id,
                    name=call.name,
                    status="skipped",
                    content="Duplicate tool_call_id skipped after exactly-once execution.",
                    error_type="duplicate",
                    source_index=call.source_index,
                )
            ordered.append(copy)
        ordered.sort(key=lambda item: item.source_index)
        return ordered

    async def _run_one(self, call: NormalizedToolCall, execute: ExecuteFn) -> ScheduledToolResult:
        try:
            raw = await self._maybe_await(execute(call))
            return self._coerce_result(call, raw)
        except Exception as exc:
            return ScheduledToolResult(
                tool_call_id=call.tool_call_id,
                name=call.name,
                status="error",
                content=f"Error executing tool: {exc}",
                error_type=type(exc).__name__,
                source_index=call.source_index,
            )

    def _coerce_result(self, call: NormalizedToolCall, raw: Any) -> ScheduledToolResult:
        if isinstance(raw, ScheduledToolResult):
            raw.source_index = call.source_index
            return raw
        if isinstance(raw, BaseException):
            return ScheduledToolResult(
                tool_call_id=call.tool_call_id,
                name=call.name,
                status="error",
                content=f"Error executing tool: {raw}",
                error_type=type(raw).__name__,
                source_index=call.source_index,
            )
        content = str(raw)
        status: ToolStatus = "failure" if _looks_like_failure(call.name, content) else "success"
        return ScheduledToolResult(
            tool_call_id=call.tool_call_id,
            name=call.name,
            status=status,
            content=content,
            error_type=None if status == "success" else "tool_failure",
            source_index=call.source_index,
        )

    @staticmethod
    def _parse_arguments(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, str) and value.strip():
            import json

            try:
                parsed = json.loads(value)
            except Exception:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}

    @staticmethod
    async def _maybe_await(value: Any) -> Any:
        if asyncio.iscoroutine(value) or isinstance(value, Awaitable):
            return await value
        return value
