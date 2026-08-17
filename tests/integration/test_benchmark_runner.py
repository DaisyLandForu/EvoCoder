from __future__ import annotations

from pathlib import Path

import pytest
from agents.trace import read_trace, validate_trace
from benchmarks.runner.run import run_benchmark


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
