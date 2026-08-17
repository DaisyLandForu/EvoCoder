from __future__ import annotations

import json
from pathlib import Path

import pytest
from agents.trace import read_trace, validate_trace
from benchmarks.runner.run import run_benchmark, tasks_digest, validate_manifest


@pytest.mark.asyncio
async def test_benchmark_runner_writes_frozen_artifacts(tmp_path: Path, skill_workspace: Path) -> None:
    result = await run_benchmark(output_root=tmp_path, run_id="ci-mini")
    run_dir = Path(result["run_dir"])
    assert (run_dir / "config.json").is_file()
    assert (run_dir / "manifest.json").is_file()
    assert (run_dir / "results.jsonl").is_file()
    assert (run_dir / "summary.json").is_file()
    rows = result["results"]
    assert [row["task_id"] for row in rows] == [
        "se-write-hello",
        "se-edit-app",
        "se-read-app",
        "se-wrong-output",
    ]
    by_id = {row["task_id"]: row for row in rows}
    assert by_id["se-write-hello"]["success"] is True
    assert by_id["se-edit-app"]["success"] is True
    assert by_id["se-read-app"]["success"] is True
    assert by_id["se-wrong-output"]["success"] is False
    assert "se-wrong-output" in result["summary"]["metrics"]["failed_task_ids"]
    for row in rows:
        events = read_trace(row["trace_path"])
        assert events, row["task_id"]
        assert validate_trace(events) == []
        assert events[0]["event_type"] == "run_start"
        assert events[-1]["event_type"] == "run_finished"
        assert row["run_id"]
    frozen = (run_dir / "config.json").read_text(encoding="utf-8")
    assert "software-engineering-mini-v1" in frozen
    assert "git_sha" in frozen
    metrics = result["summary"]["metrics"]
    assert metrics["task_count"] == 4
    assert metrics["hard_failures"] == 0
    assert metrics["ntr"] is not None
    assert result["summary"]["skill_eval"]["candidate_version"]
    frozen = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    assert frozen["task_digest"]
    assert frozen["memory_enabled"] is False
    assert frozen["tools"] == ["read_file", "write_file", "edit_file", "list_files"]


def test_task_digest_covers_seeded_task_files() -> None:
    base = [
        {
            "task_id": "se-edit-app",
            "prompt": "Change src/app.py",
            "success_check": {"type": "file_contains", "path": "src/app.py", "text": "ready"},
            "files": {"src/app.py": "A"},
        }
    ]
    changed = [{**base[0], "files": {"src/app.py": "B"}}]
    assert tasks_digest(base) != tasks_digest(changed)

    manifest = {"task_ids": ["se-edit-app"], "task_digest": tasks_digest(base)}
    assert validate_manifest(manifest, base) == tasks_digest(base)
    with pytest.raises(ValueError, match="task_digest"):
        validate_manifest(manifest, changed)


@pytest.mark.asyncio
async def test_benchmark_rerun_same_run_id_is_reproducible(tmp_path: Path, skill_workspace: Path) -> None:
    first = await run_benchmark(output_root=tmp_path, run_id="review-rerun")
    second = await run_benchmark(output_root=tmp_path, run_id="review-rerun")
    first_metrics = first["summary"]["metrics"]
    second_metrics = second["summary"]["metrics"]
    for key in (
        "task_success_rate",
        "average_tool_steps",
        "hard_failures",
        "ntr",
        "paired_gain",
        "failed_task_ids",
        "unknown_usage_events",
    ):
        assert first_metrics[key] == second_metrics[key], key
    assert first["summary"]["skill_eval"]["candidate_version"] == second["summary"]["skill_eval"]["candidate_version"]
    assert first["summary"]["skill_eval"]["champion_version"] == second["summary"]["skill_eval"]["champion_version"]
    first_rows = {row["task_id"]: row["tool_steps"] for row in first["results"]}
    second_rows = {row["task_id"]: row["tool_steps"] for row in second["results"]}
    assert first_rows == second_rows
    trace_lines = 0
    for row in second["results"]:
        events = read_trace(row["trace_path"])
        assert validate_trace(events) == []
        trace_lines += len(events)
        assert sum(1 for event in events if event["event_type"] == "run_start") == 1
    assert trace_lines == sum(len(read_trace(row["trace_path"])) for row in first["results"])
