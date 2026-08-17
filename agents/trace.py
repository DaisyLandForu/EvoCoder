"""Event-level JSONL tracing for a single agent run.

Prompt bodies are hashed by default. API keys, Authorization headers, and
raw environment values are never written. Optional bodies are redacted first.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterable, Mapping

EVENT_TYPES = frozenset(
    {
        "run_start",
        "model_request",
        "model_response",
        "tool_requested",
        "policy_decision",
        "tool_started",
        "tool_finished",
        "memory_retrieved",
        "context_compacted",
        "skill_retrieved",
        "skill_candidate_created",
        "skill_evaluated",
        "subagent_started",
        "subagent_finished",
        "run_error",
        "run_finished",
    }
)

REQUIRED_FIELDS = (
    "timestamp",
    "run_id",
    "session_id",
    "step_id",
    "parent_step_id",
    "event_type",
    "model",
    "provider",
    "prompt_hash",
    "tool_call_id",
    "tool_name",
    "skill_name",
    "skill_version",
    "permission_decision",
    "latency_ms",
    "input_tokens",
    "output_tokens",
    "estimated_cost",
    "exit_code",
    "artifact_path",
    "error_type",
)

_SECRET_KEY_RE = re.compile(
    r"(^|_)(api[_-]?key|authorization|passwd|password|secret|token|bearer)$",
    re.IGNORECASE,
)
_SECRET_VALUE_RE = re.compile(
    r"(?i)(api[_-]?key|authorization|bearer|secret|token)\s*[:=]\s*\S+"
    r"|sk-[A-Za-z0-9\-_]{8,}"
    r"|Bearer\s+\S+"
)

_active_tracer: ContextVar[TraceRecorder | None] = ContextVar("bear_tracer", default=None)
_active_parent_step: ContextVar[str | None] = ContextVar("bear_parent_step", default=None)


def new_run_id() -> str:
    return f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:10]}"


def new_step_id() -> str:
    return uuid.uuid4().hex[:12]


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.", time.gmtime()) + f"{int((time.time() % 1) * 1000):03d}Z"


def hash_prompt(value: Any) -> str:
    payload = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def is_secret_key(key: str) -> bool:
    """Match API key / Authorization names across case and -/_ variants."""
    text = str(key)
    if text in REQUIRED_FIELDS:
        return False
    normalized = text.lower().replace("-", "_")
    return bool(_SECRET_KEY_RE.search(normalized))


def redact_secrets(value: Any) -> Any:
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text in REQUIRED_FIELDS:
                out[key_text] = redact_secrets(item) if isinstance(item, (Mapping, list, tuple, str)) else item
            elif is_secret_key(key_text):
                out[key_text] = "[REDACTED]"
            else:
                out[key_text] = redact_secrets(item)
        return out
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, tuple):
        return [redact_secrets(item) for item in value]
    if isinstance(value, str):
        return _SECRET_VALUE_RE.sub("[REDACTED]", value)
    return value


def parse_model_usage(usage: Any) -> tuple[int | None, int | None, bool]:
    """Return (input_tokens, output_tokens, known). Missing usage is unknown, never zero-filled."""
    if usage is None:
        return None, None, False
    if isinstance(usage, Mapping):
        raw_in = usage.get("input_tokens", usage.get("prompt_tokens"))
        raw_out = usage.get("output_tokens", usage.get("completion_tokens"))
    else:
        raw_in = getattr(usage, "input_tokens", None)
        if raw_in is None:
            raw_in = getattr(usage, "prompt_tokens", None)
        raw_out = getattr(usage, "output_tokens", None)
        if raw_out is None:
            raw_out = getattr(usage, "completion_tokens", None)
    if raw_in is None or raw_out is None:
        return None, None, False
    try:
        return int(raw_in), int(raw_out), True
    except (TypeError, ValueError):
        return None, None, False


def estimate_cost_usd(
    input_tokens: int | None,
    output_tokens: int | None,
    *,
    input_usd_per_mtok: float = 3.0,
    output_usd_per_mtok: float = 15.0,
) -> float | None:
    if input_tokens is None or output_tokens is None:
        return None
    return (input_tokens / 1_000_000) * input_usd_per_mtok + (output_tokens / 1_000_000) * output_usd_per_mtok


def default_trace_dir(workspace: str | Path | None = None) -> Path:
    root = Path(workspace or Path.cwd()).expanduser().resolve()
    return root / ".bear" / "traces"


def resolve_trace_path(path: str | Path | None, run_id: str, *, workspace: str | Path | None = None) -> Path:
    if path is None:
        target = default_trace_dir(workspace) / f"{run_id}.jsonl"
    else:
        target = Path(path).expanduser()
        if target.suffix.lower() in {".jsonl", ".json"}:
            pass
        else:
            target = target / f"{run_id}.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


class TraceRecorder:
    """Append-only JSONL writer for one run. Parent and child agents share one recorder."""

    def __init__(
        self,
        *,
        run_id: str,
        session_id: str,
        path: str | Path,
        model: str = "",
        provider: str = "",
        include_bodies: bool = False,
    ) -> None:
        self.run_id = run_id
        self.session_id = session_id
        self.path = Path(path)
        self.model = model
        self.provider = provider
        self.include_bodies = include_bodies
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("", encoding="utf-8")
        self._lock = threading.Lock()

    def emit(self, event_type: str, **fields: Any) -> str:
        if event_type not in EVENT_TYPES:
            raise ValueError(f"unknown trace event_type: {event_type}")
        step_id = str(fields.pop("step_id", None) or new_step_id())
        parent = fields.pop("parent_step_id", _active_parent_step.get())
        if event_type == "run_start":
            parent = None
        event = {name: None for name in REQUIRED_FIELDS}
        event.update(
            {
                "timestamp": utc_timestamp(),
                "run_id": self.run_id,
                "session_id": self.session_id,
                "step_id": step_id,
                "parent_step_id": parent,
                "event_type": event_type,
                "model": fields.pop("model", self.model) or None,
                "provider": fields.pop("provider", self.provider) or None,
            }
        )
        for key, value in fields.items():
            event[key] = value
        if not self.include_bodies:
            event.pop("prompt_text", None)
            event.pop("response_text", None)
            event.pop("tool_arguments", None)
        else:
            for body_key in ("prompt_text", "response_text", "tool_arguments"):
                if body_key in event:
                    event[body_key] = redact_secrets(event[body_key])
        event = redact_secrets(event)
        line = json.dumps(event, ensure_ascii=False, default=str)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        return step_id

    def bind(self, parent_step_id: str | None = None):
        tracer_token = _active_tracer.set(self)
        parent_token = _active_parent_step.set(parent_step_id)
        return tracer_token, parent_token

    @staticmethod
    def unbind(tracer_token, parent_token) -> None:
        if tracer_token is not None:
            _active_tracer.reset(tracer_token)
        if parent_token is not None:
            _active_parent_step.reset(parent_token)


def get_active_tracer() -> TraceRecorder | None:
    return _active_tracer.get()


def get_active_parent_step() -> str | None:
    return _active_parent_step.get()


def emit_trace(event_type: str, **fields: Any) -> str | None:
    tracer = get_active_tracer()
    if tracer is None:
        return None
    return tracer.emit(event_type, **fields)


def read_trace(path: str | Path) -> list[dict[str, Any]]:
    target = Path(path)
    if not target.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events


def validate_event(event: Mapping[str, Any]) -> list[str]:
    missing = [name for name in REQUIRED_FIELDS if name not in event]
    event_type = event.get("event_type")
    if event_type not in EVENT_TYPES:
        missing.append(f"invalid event_type:{event_type}")
    return missing


def validate_trace_graph(events: Iterable[Mapping[str, Any]]) -> list[str]:
    """Reject multiple roots, dangling parents, and events after run_finished."""
    items = list(events)
    errors: list[str] = []
    by_run: dict[str, list[tuple[int, Mapping[str, Any]]]] = {}
    for index, event in enumerate(items):
        run_id = str(event.get("run_id") or "")
        by_run.setdefault(run_id, []).append((index, event))
    for run_id, group in by_run.items():
        starts = [event for _, event in group if event.get("event_type") == "run_start"]
        finishes = [event for _, event in group if event.get("event_type") == "run_finished"]
        if len(starts) != 1:
            errors.append(f"run {run_id}: expected 1 run_start, got {len(starts)}")
        if len(finishes) != 1:
            errors.append(f"run {run_id}: expected 1 run_finished, got {len(finishes)}")
        if group and group[0][1].get("event_type") != "run_start":
            errors.append(f"run {run_id}: first event is not run_start")
        if group and group[-1][1].get("event_type") != "run_finished":
            errors.append(f"run {run_id}: last event is not run_finished")
        seen_steps: dict[str, Mapping[str, Any]] = {}
        finished = False
        for index, event in group:
            if finished:
                errors.append(f"event[{index}]: appears after run_finished")
            step_id = str(event.get("step_id") or "")
            if not step_id:
                errors.append(f"event[{index}]: missing step_id")
            elif step_id in seen_steps:
                errors.append(f"event[{index}]: duplicate step_id {step_id}")
            parent = event.get("parent_step_id")
            event_type = event.get("event_type")
            if event_type == "run_start":
                if parent is not None:
                    errors.append(f"event[{index}]: run_start parent_step_id must be null")
            elif parent is None:
                errors.append(f"event[{index}]: missing parent_step_id")
            elif str(parent) not in seen_steps:
                errors.append(f"event[{index}]: dangling parent_step_id {parent}")
            if step_id:
                seen_steps[step_id] = event
            if event_type == "run_finished":
                finished = True
    return errors


def validate_trace(events: Iterable[Mapping[str, Any]]) -> list[str]:
    items = list(events)
    errors: list[str] = []
    for index, event in enumerate(items):
        for problem in validate_event(event):
            errors.append(f"event[{index}]: {problem}")
    errors.extend(validate_trace_graph(items))
    return errors
