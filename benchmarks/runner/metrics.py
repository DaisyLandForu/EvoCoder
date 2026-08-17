"""Aggregate metrics for a frozen benchmark run."""

from __future__ import annotations

from typing import Any, Iterable


def percentile(values: Iterable[float], p: float) -> float | None:
    items = [float(item) for item in values]
    if not items:
        return None
    ordered = sorted(items)
    if len(ordered) == 1:
        return ordered[0]
    rank = (p / 100.0) * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    weight = rank - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def summarize_results(rows: list[dict[str, Any]], *, skill_eval: dict[str, Any] | None = None) -> dict[str, Any]:
    total = len(rows)
    successes = [row for row in rows if row.get("success")]
    hard_failures = [row for row in rows if row.get("hard_failure")]
    latencies = [float(row["latency_ms"]) for row in rows if row.get("latency_ms") is not None]
    tool_steps = [int(row.get("tool_steps") or 0) for row in rows]
    unknown = sum(int(row.get("unknown_usage_count") or 0) for row in rows)
    incomplete = unknown > 0 or any(row.get("usage_status") == "unknown" for row in rows)
    tokens = [
        int(row.get("input_tokens") or 0) + int(row.get("output_tokens") or 0)
        for row in rows
        if row.get("input_tokens") is not None and row.get("output_tokens") is not None
    ]
    costs = [float(row["estimated_cost"]) for row in rows if row.get("estimated_cost") is not None]
    if incomplete:
        average_tokens = None
        average_cost_usd = None
    elif total:
        average_tokens = (sum(tokens) / total) if tokens else 0.0
        average_cost_usd = (sum(costs) / total) if costs else 0.0
    else:
        average_tokens = 0.0
        average_cost_usd = 0.0
    gate = (skill_eval or {}).get("gate") or {}
    metrics = (gate.get("metrics") or {}) if isinstance(gate, dict) else {}
    return {
        "task_count": total,
        "task_success_rate": (len(successes) / total) if total else 0.0,
        "average_tool_steps": (sum(tool_steps) / total) if total else 0.0,
        "p95_latency_ms": percentile(latencies, 95),
        "average_tokens": average_tokens,
        "average_cost_usd": average_cost_usd,
        "hard_failures": len(hard_failures),
        "unknown_usage_events": unknown,
        "ntr": metrics.get("negative_transfer_rate"),
        "paired_gain": metrics.get("paired_gain"),
        "no_skill_pass_rate": metrics.get("no_skill_pass_rate"),
        "champion_pass_rate": metrics.get("champion_pass_rate"),
        "candidate_pass_rate": metrics.get("candidate_pass_rate"),
        "failed_task_ids": [row["task_id"] for row in rows if not row.get("success")],
    }
