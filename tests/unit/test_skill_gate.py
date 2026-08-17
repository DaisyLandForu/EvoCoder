from __future__ import annotations

from agents.skill_gate import (
    DECISION_INCUBATING,
    DECISION_PROMOTE,
    DECISION_REJECT,
    GateConfig,
    apply_gate,
    compute_paired_metrics,
)


def _pairs(
    *,
    both_pass: int = 0,
    champion_only: int = 0,
    candidate_only: int = 0,
    both_fail: int = 0,
    champion_tokens: int = 100,
    candidate_tokens: int = 100,
    candidate_hard_failures: int = 0,
) -> list[dict]:
    pairs: list[dict] = []

    def _add(champion_pass: bool, candidate_pass: bool, count: int):
        for index in range(count):
            pairs.append(
                {
                    "sample_id": f"s-{len(pairs)}-{index}",
                    "champion_pass": champion_pass,
                    "candidate_pass": candidate_pass,
                    "no_skill_pass": False,
                    "champion_tokens": champion_tokens,
                    "candidate_tokens": candidate_tokens,
                    "candidate_hard_failed": False,
                }
            )

    _add(True, True, both_pass)
    _add(True, False, champion_only)
    _add(False, True, candidate_only)
    _add(False, False, both_fail)
    for index in range(candidate_hard_failures):
        pairs[index]["candidate_hard_failed"] = True
    return pairs


def _permissive(**overrides) -> GateConfig:
    base = {
        "min_paired_samples": 1,
        "allow_non_independent_judge_promotion": True,
        "require_holdout": False,
    }
    base.update(overrides)
    return GateConfig(**base)


# ---------------------------------------------------------------------- metrics


def test_negative_transfer_rate_is_losses_over_champion_passes():
    metrics = compute_paired_metrics(_pairs(both_pass=18, champion_only=2, candidate_only=5))

    assert metrics["champion_pass"] == 20
    assert metrics["losses"] == 2
    assert metrics["negative_transfer_rate"] == 0.1
    assert metrics["paired_gain"] == 3


def test_negative_transfer_rate_is_zero_when_the_champion_never_passes():
    metrics = compute_paired_metrics(_pairs(candidate_only=4, both_fail=6))

    assert metrics["champion_pass"] == 0
    assert metrics["negative_transfer_rate"] == 0.0


def test_cost_growth_uses_total_tokens():
    metrics = compute_paired_metrics(
        _pairs(both_pass=10, champion_tokens=100, candidate_tokens=120)
    )

    assert metrics["cost_growth"] == 0.2


# ------------------------------------------------------------------- gate rules


def test_default_thresholds_match_the_specification():
    config = GateConfig()

    assert config.min_paired_samples == 20
    assert config.max_negative_transfer_rate == 0.05
    assert config.max_cost_growth == 0.15
    assert config.allow_hard_failures == 0
    assert config.allow_non_independent_judge_promotion is False


def test_a_clean_win_promotes():
    metrics = compute_paired_metrics(_pairs(both_pass=20, candidate_only=5))
    decision = apply_gate(metrics, config=GateConfig(), independent_judge=True)

    assert decision["decision"] == DECISION_PROMOTE
    assert not decision["blocking_reasons"]


def test_excess_negative_transfer_blocks_promotion():
    metrics = compute_paired_metrics(_pairs(both_pass=18, champion_only=2, candidate_only=6))
    decision = apply_gate(metrics, config=GateConfig(), independent_judge=True)

    assert decision["decision"] == DECISION_REJECT
    assert any("negative transfer" in reason for reason in decision["blocking_reasons"])


def test_negative_transfer_at_the_threshold_is_allowed():
    metrics = compute_paired_metrics(_pairs(both_pass=19, champion_only=1, candidate_only=5))

    assert metrics["negative_transfer_rate"] == 0.05
    assert apply_gate(metrics, config=GateConfig(), independent_judge=True)["decision"] == DECISION_PROMOTE


def test_zero_gain_blocks_promotion():
    metrics = compute_paired_metrics(_pairs(both_pass=25))
    decision = apply_gate(metrics, config=GateConfig(), independent_judge=True)

    assert decision["decision"] == DECISION_REJECT
    assert any("paired gain" in reason for reason in decision["blocking_reasons"])


def test_cost_growth_above_the_threshold_blocks_promotion():
    metrics = compute_paired_metrics(
        _pairs(both_pass=20, candidate_only=5, champion_tokens=100, candidate_tokens=130)
    )
    decision = apply_gate(metrics, config=GateConfig(), independent_judge=True)

    assert decision["decision"] == DECISION_REJECT
    assert any("cost growth" in reason for reason in decision["blocking_reasons"])


def test_a_hard_failure_blocks_even_a_strong_win():
    metrics = compute_paired_metrics(_pairs(both_pass=20, candidate_only=10, candidate_hard_failures=1))
    decision = apply_gate(metrics, config=GateConfig(), independent_judge=True)

    assert decision["decision"] == DECISION_REJECT
    assert any("hard safety/format" in reason for reason in decision["blocking_reasons"])


def test_too_few_samples_becomes_incubating():
    metrics = compute_paired_metrics(_pairs(both_pass=5, candidate_only=3))
    decision = apply_gate(metrics, config=GateConfig(), independent_judge=True)

    assert decision["decision"] == DECISION_INCUBATING
    assert any("valid paired samples" in reason for reason in decision["incubating_reasons"])


def test_a_non_independent_judge_cannot_trigger_promotion():
    metrics = compute_paired_metrics(_pairs(both_pass=20, candidate_only=5))
    decision = apply_gate(metrics, config=GateConfig(), independent_judge=False)

    assert decision["decision"] == DECISION_INCUBATING
    assert any("not independent" in reason for reason in decision["incubating_reasons"])


def test_missing_holdout_becomes_incubating():
    metrics = compute_paired_metrics(_pairs(both_pass=20, candidate_only=5))
    decision = apply_gate(
        metrics, config=GateConfig(), independent_judge=True, holdout_available=False
    )

    assert decision["decision"] == DECISION_INCUBATING
    assert any("holdout" in reason for reason in decision["incubating_reasons"])


def test_hard_failure_outranks_insufficient_samples():
    metrics = compute_paired_metrics(_pairs(both_pass=2, candidate_only=1, candidate_hard_failures=1))
    decision = apply_gate(metrics, config=GateConfig(), independent_judge=True)

    assert decision["decision"] == DECISION_REJECT


# --------------------------------------------------------------- configurability


def test_thresholds_are_configurable_and_recorded():
    config = _permissive(max_negative_transfer_rate=0.5, max_cost_growth=1.0)
    metrics = compute_paired_metrics(_pairs(both_pass=8, champion_only=2, candidate_only=5))
    decision = apply_gate(metrics, config=config, independent_judge=False)

    assert decision["decision"] == DECISION_PROMOTE
    assert decision["thresholds"]["max_negative_transfer_rate"] == 0.5
    assert decision["thresholds"]["min_paired_samples"] == 1
    assert decision["metrics"]["negative_transfer_rate"] == 0.2


def test_environment_overrides_are_picked_up(monkeypatch):
    monkeypatch.setenv("BEAR_GATE_MIN_PAIRS", "5")
    monkeypatch.setenv("BEAR_GATE_MAX_NTR", "0.2")
    monkeypatch.setenv("BEAR_GATE_MAX_COST_GROWTH", "0.5")
    monkeypatch.setenv("BEAR_GATE_REQUIRE_HOLDOUT", "0")

    config = GateConfig.from_env()

    assert config.min_paired_samples == 5
    assert config.max_negative_transfer_rate == 0.2
    assert config.max_cost_growth == 0.5
    assert config.require_holdout is False


def test_malformed_environment_overrides_fall_back_to_defaults(monkeypatch):
    monkeypatch.setenv("BEAR_GATE_MIN_PAIRS", "not-a-number")

    assert GateConfig.from_env().min_paired_samples == 20
