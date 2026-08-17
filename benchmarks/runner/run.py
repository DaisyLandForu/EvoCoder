"""Frozen benchmark runner. CI uses the scripted model; no live API keys required."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from agents.agent import Agent
from agents.runtime_config import RuntimeConfig
from agents.skill_gate import GateConfig
from agents.skill_registry import create_candidate, mark_champion
from agents.skill_shadow_eval import (
    ORIGIN_HOLDOUT,
    ORIGIN_REPLAY,
    EvalSample,
    GenerationConfig,
    GenerationRequest,
    GenerationResult,
    JudgeConfig,
    run_paired_evaluation,
)
from agents.trace import read_trace, validate_trace

from benchmarks.runner.metrics import summarize_results
from benchmarks.runner.scripted import ScriptedStream, script_for_task

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "benchmarks" / "configs" / "ci.json"
DEFAULT_TASKS = ROOT / "benchmarks" / "tasks" / "software_engineering_mini.jsonl"
DEFAULT_MANIFEST = ROOT / "benchmarks" / "manifests" / "ci.manifest.json"


def git_sha(repo: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def new_run_id() -> str:
    return f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:8]}"


def tasks_digest(tasks: list[dict[str, Any]]) -> str:
    payload = [
        {
            "task_id": task.get("task_id"),
            "prompt": task.get("prompt"),
            "success_check": task.get("success_check"),
        }
        for task in tasks
    ]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def validate_manifest(manifest: dict[str, Any], tasks: list[dict[str, Any]]) -> str:
    task_ids = [str(task["task_id"]) for task in tasks]
    expected_ids = manifest.get("task_ids")
    if expected_ids is not None and [str(item) for item in expected_ids] != task_ids:
        raise ValueError(f"manifest task_ids {list(expected_ids)} do not match task file {task_ids}")
    digest = tasks_digest(tasks)
    expected_digest = manifest.get("task_digest")
    if expected_digest and str(expected_digest) != digest:
        raise ValueError("manifest task_digest does not match task file contents")
    return digest


def check_success(workspace: Path, task: dict[str, Any], output_text: str) -> bool:
    check = task.get("success_check") or {}
    kind = str(check.get("type") or "")
    if kind == "file_contains":
        target = workspace / str(check["path"])
        if not target.exists():
            return False
        return str(check.get("text") or "") in target.read_text(encoding="utf-8")
    if kind == "output_contains":
        return str(check.get("text") or "") in (output_text or "")
    return False


def _prepare_workspace(base: Path, task: dict[str, Any]) -> Path:
    workspace = base / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    src = workspace / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "app.py").write_text("print('ok')\n", encoding="utf-8")
    for relative, content in (task.get("files") or {}).items():
        path = workspace / str(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(content), encoding="utf-8")
    return workspace


async def _run_task(task: dict[str, Any], *, run_dir: Path, config: dict[str, Any]) -> dict[str, Any]:
    task_id = str(task["task_id"])
    task_dir = run_dir / "artifacts" / task_id
    if task_dir.exists():
        shutil.rmtree(task_dir)
    workspace = _prepare_workspace(task_dir, task)
    trace_path = run_dir / "traces" / f"{task_id}.jsonl"
    tools = config.get("tools")
    runtime = RuntimeConfig(
        provider=str(config.get("provider") or "openai"),
        model=str(config.get("model") or "scripted-openai"),
        api_key="benchmark-unused",
        base_url="https://example.invalid/v1",
        workspace=workspace,
        permission_mode="bypassPermissions",
        max_cost_usd=config.get("max_cost_usd"),
        max_turns=config.get("max_turns"),
        temperature=config.get("temperature"),
        seed=config.get("seed"),
        model_timeout_s=float(config.get("timeout_s") or 30),
        tool_timeout_s=float(config.get("tool_timeout_s") or 10),
        trace_enabled=True,
        trace_path=trace_path,
        memory_enabled=bool(config.get("memory_enabled", True)),
        folding_enabled=bool(config.get("folding_enabled", True)),
        allowed_tools=tuple(str(name) for name in tools) if tools is not None else None,
    )
    agent = Agent(config=runtime, is_sub_agent=True)
    agent._build_side_query = lambda **_kwargs: None  # type: ignore[method-assign]
    scripted = ScriptedStream(script_for_task(task_id))
    agent._call_openai_stream = scripted  # type: ignore[method-assign]
    started = time.monotonic()
    hard_failure = False
    error_type = None
    try:
        result = await agent.run_once(str(task["prompt"]))
        text = str(result.get("text") or "").strip()
        if not text:
            for message in reversed(agent._openai_messages):
                if message.get("role") == "assistant" and message.get("content"):
                    text = str(message["content"]).strip()
                    break
    except Exception as exc:
        hard_failure = True
        error_type = type(exc).__name__
        text = ""
        result = {"tokens": {"input": 0, "output": 0}}
    latency_ms = (time.monotonic() - started) * 1000
    events = read_trace(trace_path)
    schema_errors = validate_trace(events)
    tool_steps = sum(1 for event in events if event.get("event_type") == "tool_finished")
    if any(event.get("event_type") == "run_error" for event in events):
        hard_failure = True
    if any(event.get("event_type") == "tool_finished" and event.get("exit_code") not in (0, None) for event in events):
        hard_failure = True
    budget = agent._budget.snapshot() if agent._budget is not None else {}
    success = (not hard_failure) and check_success(workspace, task, text)
    row = {
        "task_id": task_id,
        "success": success,
        "hard_failure": hard_failure,
        "error_type": error_type,
        "latency_ms": round(latency_ms, 3),
        "tool_steps": tool_steps,
        "input_tokens": budget.get("input_tokens", result.get("tokens", {}).get("input")),
        "output_tokens": budget.get("output_tokens", result.get("tokens", {}).get("output")),
        "estimated_cost": budget.get("estimated_cost_usd"),
        "unknown_usage_count": budget.get("unknown_usage_count", 0),
        "usage_status": budget.get("usage_status"),
        "trace_path": str(trace_path),
        "artifact_path": str(task_dir),
        "run_id": agent._run_id,
        "schema_errors": schema_errors,
        "output_text": text,
    }
    return row


async def _run_skill_eval(artifact_dir: Path) -> dict[str, Any]:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    previous = Path.cwd()

    async def generate(request: GenerationRequest) -> GenerationResult:
        if request.arm == "candidate":
            return GenerationResult(text="see https://example.test", input_tokens=12, output_tokens=8)
        if request.arm == "current_champion":
            return GenerationResult(text="see https://example.test", input_tokens=11, output_tokens=7)
        return GenerationResult(text="no citation", input_tokens=9, output_tokens=5)

    async def judge(_system: str, _user: str) -> str:
        return '{"pass": true, "reason": "scripted"}'

    try:
        import os

        os.chdir(artifact_dir)
        first = create_candidate(
            skill_name="cite-sources",
            description="Cite sources",
            instructions="Cite sources.",
            when_to_use="factual questions",
        )
        mark_champion("cite-sources", first["version"], reason="benchmark seed")
        second = create_candidate(
            skill_name="cite-sources",
            description="Cite sources",
            instructions="Always cite sources.",
            when_to_use="factual questions",
        )
        samples = [
            EvalSample(sample_id="replay-1", user_message="What is Python?", origin=ORIGIN_REPLAY),
            EvalSample(sample_id="holdout-1", user_message="What is Git?", origin=ORIGIN_HOLDOUT),
        ]
        return await run_paired_evaluation(
            skill_name="cite-sources",
            candidate_version=second["version"],
            generate=generate,
            judge=judge,
            generation_config=GenerationConfig(model="scripted-openai", provider="openai", seed=7),
            judge_config=JudgeConfig(model="scripted-judge", provider="openai"),
            gate_config=GateConfig(min_paired_samples=1, allow_non_independent_judge_promotion=True),
            samples=samples,
            persist=True,
        )
    finally:
        os.chdir(previous)


async def run_benchmark(
    *,
    config_path: Path = DEFAULT_CONFIG,
    tasks_path: Path = DEFAULT_TASKS,
    manifest_path: Path = DEFAULT_MANIFEST,
    output_root: Path | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    config = load_json(config_path)
    tasks = load_jsonl(tasks_path)
    manifest = load_json(manifest_path)
    digest = validate_manifest(manifest, tasks)
    run_id = run_id or new_run_id()
    output_root = Path(output_root or ROOT / "runs")
    run_dir = output_root / run_id
    if run_dir.exists():
        shutil.rmtree(run_dir)
    (run_dir / "traces").mkdir(parents=True, exist_ok=True)
    (run_dir / "artifacts").mkdir(parents=True, exist_ok=True)

    frozen = {
        **config,
        "run_id": run_id,
        "dataset_version": manifest.get("dataset_version") or config.get("dataset_version"),
        "task_ids": [task["task_id"] for task in tasks],
        "task_digest": digest,
        "git_sha": git_sha(ROOT),
        "config_path": str(config_path),
        "tasks_path": str(tasks_path),
        "manifest_path": str(manifest_path),
    }
    (run_dir / "config.json").write_text(json.dumps(frozen, indent=2) + "\n", encoding="utf-8")
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    rows: list[dict[str, Any]] = []
    with (run_dir / "results.jsonl").open("w", encoding="utf-8") as handle:
        for task in tasks:
            row = await _run_task(task, run_dir=run_dir, config=config)
            rows.append(row)
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    skill_eval = await _run_skill_eval(run_dir / "artifacts" / "skill-eval")
    (run_dir / "artifacts" / "skill_eval.json").write_text(
        json.dumps(skill_eval, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    summary = {
        "run_id": run_id,
        "git_sha": frozen["git_sha"],
        "dataset_version": frozen["dataset_version"],
        "metrics": summarize_results(rows, skill_eval=skill_eval),
        "skill_eval": {
            "run_id": skill_eval.get("run_id"),
            "candidate_version": skill_eval.get("candidate_version"),
            "champion_version": skill_eval.get("champion_version"),
            "decision": (skill_eval.get("gate") or {}).get("decision"),
            "artifact_path": skill_eval.get("artifact_path"),
        },
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return {"run_dir": str(run_dir), "summary": summary, "results": rows}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the frozen EvoCoder benchmark suite")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--tasks", default=str(DEFAULT_TASKS))
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--output-root", default=str(ROOT / "runs"))
    parser.add_argument("--run-id", default=None)
    args = parser.parse_args(argv)
    result = asyncio.run(
        run_benchmark(
            config_path=Path(args.config),
            tasks_path=Path(args.tasks),
            manifest_path=Path(args.manifest),
            output_root=Path(args.output_root),
            run_id=args.run_id,
        )
    )
    print(json.dumps(result["summary"], indent=2))
    return 0
