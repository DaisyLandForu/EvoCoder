from __future__ import annotations

import json
from pathlib import Path

from agents.runtime_config import BudgetTracker
from agents.trace import (
    REQUIRED_FIELDS,
    TraceRecorder,
    hash_prompt,
    parse_model_usage,
    read_trace,
    redact_secrets,
    validate_trace,
)


def test_trace_event_has_required_fields(tmp_path: Path) -> None:
    recorder = TraceRecorder(
        run_id="run-1",
        session_id="sess-1",
        path=tmp_path / "run.jsonl",
        model="demo",
        provider="openai",
    )
    recorder.emit("run_start", prompt_hash=hash_prompt("hello"))
    recorder.emit("model_request", prompt_hash=hash_prompt("hello"), parent_step_id="abc")
    events = read_trace(tmp_path / "run.jsonl")
    assert events
    assert validate_trace(events) == []
    for event in events:
        for name in REQUIRED_FIELDS:
            assert name in event


def test_prompt_bodies_are_hashed_not_stored_by_default(tmp_path: Path) -> None:
    recorder = TraceRecorder(
        run_id="run-2",
        session_id="sess-2",
        path=tmp_path / "run.jsonl",
        include_bodies=False,
    )
    secret_prompt = "Authorization: Bearer sk-secret-token user question"
    recorder.emit("model_request", prompt_hash=hash_prompt(secret_prompt), prompt_text=secret_prompt)
    event = read_trace(tmp_path / "run.jsonl")[0]
    assert event["prompt_hash"] == hash_prompt(secret_prompt)
    assert "prompt_text" not in event
    assert "sk-secret-token" not in json.dumps(event)


def test_optional_bodies_are_redacted(tmp_path: Path) -> None:
    recorder = TraceRecorder(
        run_id="run-3",
        session_id="sess-3",
        path=tmp_path / "run.jsonl",
        include_bodies=True,
    )
    recorder.emit(
        "model_request",
        prompt_hash="abc",
        prompt_text="API_KEY=sk-live-secret and Bearer tok-1",
        api_key="sk-live-secret",
    )
    raw = (tmp_path / "run.jsonl").read_text(encoding="utf-8")
    assert "sk-live-secret" not in raw
    event = json.loads(raw)
    assert event["prompt_text"]
    assert "[REDACTED]" in event["prompt_text"] or "[REDACTED]" in json.dumps(event)


def test_redact_secrets_covers_env_style_keys() -> None:
    payload = redact_secrets({"Authorization": "Bearer abc", "OPENAI_API_KEY": "sk-1", "note": "ok", "input_tokens": 9})
    assert payload["Authorization"] == "[REDACTED]"
    assert payload["OPENAI_API_KEY"] == "[REDACTED]"
    assert payload["note"] == "ok"
    assert payload["input_tokens"] == 9


def test_missing_usage_is_unknown_not_zero() -> None:
    budget = BudgetTracker()
    budget.add_usage(None, None, known=False)
    assert budget.input_tokens == 0
    assert budget.output_tokens == 0
    assert budget.unknown_usage_count == 1
    assert budget.usage_status() == "unknown"
    assert parse_model_usage(None) == (None, None, False)
    assert parse_model_usage({"prompt_tokens": 4, "completion_tokens": 2}) == (4, 2, True)


def test_known_usage_adds_to_shared_budget() -> None:
    budget = BudgetTracker()
    budget.add_usage(10, 3, known=True)
    budget.add_usage(5, 1, known=True)
    assert budget.input_tokens == 15
    assert budget.output_tokens == 4
    assert budget.unknown_usage_count == 0
