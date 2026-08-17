"""Promotion gate: paired gain, negative transfer rate, cost growth, hard failures.

Every threshold is configurable and is copied into the evaluation artifact so a
past decision can be re-read with the thresholds that actually produced it.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Any

DECISION_PROMOTE = "promote"
DECISION_REJECT = "reject"
DECISION_INCUBATING = "incubating"


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class GateConfig:
    """Thresholds for candidate promotion. Defaults follow the P1 specification."""

    min_paired_samples: int = 20
    max_negative_transfer_rate: float = 0.05
    max_cost_growth: float = 0.15
    min_paired_gain: int = 1
    allow_hard_failures: int = 0
    require_holdout: bool = True
    allow_non_independent_judge_promotion: bool = False

    @classmethod
    def from_env(cls) -> "GateConfig":
        base = cls()
        return cls(
            min_paired_samples=_env_int("BEAR_GATE_MIN_PAIRS", base.min_paired_samples),
            max_negative_transfer_rate=_env_float("BEAR_GATE_MAX_NTR", base.max_negative_transfer_rate),
            max_cost_growth=_env_float("BEAR_GATE_MAX_COST_GROWTH", base.max_cost_growth),
            min_paired_gain=_env_int("BEAR_GATE_MIN_GAIN", base.min_paired_gain),
            allow_hard_failures=_env_int("BEAR_GATE_ALLOW_HARD_FAILURES", base.allow_hard_failures),
            require_holdout=_env_bool("BEAR_GATE_REQUIRE_HOLDOUT", base.require_holdout),
            allow_non_independent_judge_promotion=_env_bool(
                "BEAR_GATE_ALLOW_NON_INDEPENDENT_JUDGE",
                base.allow_non_independent_judge_promotion,
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_paired_metrics(pairs: list[dict[str, Any]], *, invalid_pairs: int = 0) -> dict[str, Any]:
    """Aggregate per-sample paired outcomes into the metrics the gate reads.

    NTR = #(champion passes but candidate fails) / #(champion passes).
    `pairs` must already exclude infrastructure failures.
    """
    total = len(pairs)
    champion_pass = sum(1 for pair in pairs if pair.get("champion_pass"))
    candidate_pass = sum(1 for pair in pairs if pair.get("candidate_pass"))
    no_skill_pass = sum(1 for pair in pairs if pair.get("no_skill_pass"))
    wins = sum(1 for pair in pairs if pair.get("candidate_pass") and not pair.get("champion_pass"))
    losses = sum(1 for pair in pairs if pair.get("champion_pass") and not pair.get("candidate_pass"))
    hard_failures = sum(1 for pair in pairs if pair.get("candidate_hard_failed"))
    champion_tokens = sum(int(pair.get("champion_tokens") or 0) for pair in pairs)
    candidate_tokens = sum(int(pair.get("candidate_tokens") or 0) for pair in pairs)

    if champion_tokens > 0:
        cost_growth = (candidate_tokens - champion_tokens) / champion_tokens
    else:
        cost_growth = 0.0 if candidate_tokens == 0 else 1.0

    return {
        "paired_samples": total,
        "invalid_pairs": int(invalid_pairs),
        "champion_pass": champion_pass,
        "candidate_pass": candidate_pass,
        "no_skill_pass": no_skill_pass,
        "wins": wins,
        "losses": losses,
        "paired_gain": candidate_pass - champion_pass,
        "paired_gain_rate": ((candidate_pass - champion_pass) / total) if total else 0.0,
        "negative_transfer_rate": (losses / champion_pass) if champion_pass else 0.0,
        "candidate_hard_failures": hard_failures,
        "champion_tokens": champion_tokens,
        "candidate_tokens": candidate_tokens,
        "cost_growth": cost_growth,
        "champion_pass_rate": (champion_pass / total) if total else 0.0,
        "candidate_pass_rate": (candidate_pass / total) if total else 0.0,
        "no_skill_pass_rate": (no_skill_pass / total) if total else 0.0,
    }


def apply_gate(
    metrics: dict[str, Any],
    *,
    config: GateConfig,
    independent_judge: bool,
    holdout_available: bool = True,
) -> dict[str, Any]:
    """Turn metrics into a decision. Only `promote` may activate a candidate."""
    reasons: list[str] = []
    blocking: list[str] = []
    incubating: list[str] = []

    hard_failures = int(metrics.get("candidate_hard_failures") or 0)
    if hard_failures > config.allow_hard_failures:
        blocking.append(
            f"candidate produced {hard_failures} hard safety/format failures "
            f"(allowed {config.allow_hard_failures})"
        )

    paired_samples = int(metrics.get("paired_samples") or 0)
    if paired_samples < config.min_paired_samples:
        incubating.append(
            f"only {paired_samples} valid paired samples (need {config.min_paired_samples})"
        )
    if config.require_holdout and not holdout_available:
        incubating.append("no holdout samples independent of candidate generation")
    if not independent_judge and not config.allow_non_independent_judge_promotion:
        incubating.append("judge is not independent of the generator; automatic promotion is disabled")
    invalid_pairs = int(metrics.get("invalid_pairs") or 0)
    if invalid_pairs > 0:
        incubating.append(
            f"{invalid_pairs} pairs discarded due to generation/judge infrastructure failure"
        )

    gain = int(metrics.get("paired_gain") or 0)
    if gain < config.min_paired_gain:
        blocking.append(f"paired gain {gain} is below the required {config.min_paired_gain}")
    else:
        reasons.append(f"paired gain {gain}")

    ntr = float(metrics.get("negative_transfer_rate") or 0.0)
    if ntr > config.max_negative_transfer_rate:
        blocking.append(f"negative transfer rate {ntr:.3f} exceeds {config.max_negative_transfer_rate:.3f}")
    else:
        reasons.append(f"NTR {ntr:.3f}")

    cost_growth = float(metrics.get("cost_growth") or 0.0)
    if cost_growth > config.max_cost_growth:
        blocking.append(f"token cost growth {cost_growth:.3f} exceeds {config.max_cost_growth:.3f}")
    else:
        reasons.append(f"cost growth {cost_growth:.3f}")

    if hard_failures > config.allow_hard_failures:
        # A safety or format failure blocks regardless of how much evidence exists.
        decision = DECISION_REJECT
    elif invalid_pairs > 0:
        # Infrastructure failures make the comparison unusable; do not promote or reject.
        decision = DECISION_INCUBATING
        blocking = []
    elif paired_samples <= 0:
        # No usable pairs means no verdict, not a rejection.
        decision = DECISION_INCUBATING
        blocking = [reason for reason in blocking if "hard safety" in reason]
    elif blocking:
        decision = DECISION_REJECT
    elif incubating:
        decision = DECISION_INCUBATING
    else:
        decision = DECISION_PROMOTE

    return {
        "decision": decision,
        "blocking_reasons": blocking,
        "incubating_reasons": incubating,
        "passing_reasons": reasons,
        "thresholds": config.to_dict(),
        "independent_judge": bool(independent_judge),
        "holdout_available": bool(holdout_available),
        "metrics": dict(metrics),
    }
