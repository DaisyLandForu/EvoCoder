from __future__ import annotations

from pathlib import Path

import pytest
from agents.memory import _should_prefetch_memory
from agents.session import get_latest_session_id, load_session, save_session
from agents.session_memory import (
    fallback_folded_memory,
    load_folded_memory_or_fallback,
    validate_folded_memory,
)


def test_sessions_are_isolated_by_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    ws_a = tmp_path / "a"
    ws_b = tmp_path / "b"
    ws_a.mkdir()
    ws_b.mkdir()
    save_session("s1", {"metadata": {"id": "s1", "startTime": "2026-01-01T00:00:00Z"}}, workspace=ws_a)
    save_session("s2", {"metadata": {"id": "s2", "startTime": "2026-01-02T00:00:00Z"}}, workspace=ws_b)
    assert get_latest_session_id(workspace=ws_a) == "s1"
    assert get_latest_session_id(workspace=ws_b) == "s2"
    assert load_session("s2", workspace=ws_a) is None


def test_chinese_query_triggers_memory_prefetch() -> None:
    assert _should_prefetch_memory("请回忆这个项目的部署方式") is True
    assert _should_prefetch_memory("部署") is True
    assert _should_prefetch_memory("hi") is False


def test_folded_memory_roundtrip() -> None:
    memory = {
        "episode_memory": {
            "task_description": "Fix the scheduler",
            "key_events": [{"step": "1", "description": "found bug", "outcome": "reproduced"}],
            "current_progress": "tests written",
        },
        "working_memory": {
            "immediate_goal": "implement scheduler",
            "current_challenges": "duplicate batching",
            "next_actions": [{"type": "tool_call", "description": "edit agent.py"}],
        },
        "tool_memory": {
            "tools_used": [{"tool_name": "read_file", "experience": "read first"}],
            "derived_rules": ["do not batch inside the loop"],
        },
    }
    validated = validate_folded_memory(memory)
    restored = load_folded_memory_or_fallback(validated)
    assert restored["episode_memory"]["task_description"] == "Fix the scheduler"
    assert restored["working_memory"]["current_challenges"] == "duplicate batching"
    assert restored["working_memory"]["next_actions"][0]["description"] == "edit agent.py"


def test_corrupt_fold_json_falls_back() -> None:
    restored = load_folded_memory_or_fallback("{not json", transcript="original task")
    assert restored["episode_memory"]["task_description"]
    assert restored["working_memory"]["immediate_goal"]


def test_missing_fields_are_rejected_then_fallback() -> None:
    restored = load_folded_memory_or_fallback({"oops": True}, transcript="keep going")
    assert "keep going" in restored["episode_memory"]["current_progress"] or restored["working_memory"]["immediate_goal"]
    fallback = fallback_folded_memory("task goal")
    assert fallback["episode_memory"]["task_description"]
