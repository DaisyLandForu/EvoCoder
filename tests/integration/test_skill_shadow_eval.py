from __future__ import annotations

import json
from pathlib import Path

from agents import skill_registry as reg
from agents.skill_gate import DECISION_INCUBATING, DECISION_PROMOTE, DECISION_REJECT, GateConfig
from agents.skill_shadow_eval import (
    ARM_CANDIDATE,
    ARM_CHAMPION,
    ARM_NO_SKILL,
    ORIGIN_HOLDOUT,
    ORIGIN_REPLAY,
    EvalSample,
    GenerationConfig,
    GenerationRequest,
    GenerationResult,
    JudgeConfig,
    build_sample_pool,
    holdout_root,
    load_holdout_samples,
    load_replay_samples,
    run_paired_evaluation,
    sample_id_for,
)

SKILL = "cite-sources"


def _seed_skill(*, champion_instructions: str, candidate_instructions: str) -> str:
    """Create a champion and a fresh candidate for the same skill lineage."""
    first = reg.create_candidate(
        skill_name=SKILL,
        description="Answer with cited sources",
        when_to_use="Whenever the user asks a factual question",
        instructions=champion_instructions,
        source_provenance={"kind": "user_feedback", "sample_ids": ["s-seed"]},
    )
    reg.mark_champion(SKILL, first["version"], reason="seed champion")
    second = reg.create_candidate(
        skill_name=SKILL,
        description="Answer with cited sources",
        when_to_use="Whenever the user asks a factual question",
        instructions=candidate_instructions,
        source_provenance={"kind": "user_feedback", "sample_ids": ["s-generated"]},
    )
    return second["version"]


def _samples(count: int, *, origin: str = ORIGIN_HOLDOUT, prefix: str = "h") -> list[EvalSample]:
    return [
        EvalSample(sample_id=f"{prefix}-{index}", user_message=f"question {prefix} {index}", origin=origin)
        for index in range(count)
    ]


def _arm_generator(texts: dict[str, str], tokens: dict[str, int] | None = None, record: list | None = None):
    """Reply depends only on the arm; token counts are fixed unless a test varies them."""

    async def _generate(request: GenerationRequest) -> GenerationResult:
        if record is not None:
            record.append(request)
        return GenerationResult(
            text=texts[request.arm],
            input_tokens=10,
            output_tokens=(tokens or {}).get(request.arm, 20),
        )

    return _generate


def _config() -> GenerationConfig:
    return GenerationConfig(model="gen-model", provider="test", temperature=0.0, seed=42, timeout_s=5.0)


def _judge_config(*, independent: bool) -> JudgeConfig:
    return JudgeConfig(model="judge-model" if independent else "gen-model", provider="test")


def _permissive_gate(**overrides) -> GateConfig:
    base = {"min_paired_samples": 1, "allow_non_independent_judge_promotion": True}
    base.update(overrides)
    return GateConfig(**base)


# ------------------------------------------------------ P1.3 three regenerated arms


async def test_all_three_arms_are_regenerated_for_every_sample(skill_workspace: Path):
    version = _seed_skill(champion_instructions="Cite sources.", candidate_instructions="Always cite sources.")
    record: list[GenerationRequest] = []
    samples = _samples(3)

    artifact = await run_paired_evaluation(
        skill_name=SKILL,
        candidate_version=version,
        generate=_arm_generator(
            {ARM_NO_SKILL: "no source", ARM_CHAMPION: "see https://a", ARM_CANDIDATE: "see https://b"},
            record=record,
        ),
        generation_config=_config(),
        judge_config=_judge_config(independent=True),
        gate_config=_permissive_gate(),
        samples=samples,
    )

    assert artifact["ok"], artifact
    assert len(record) == 9
    assert {item.arm for item in record} == {ARM_NO_SKILL, ARM_CHAMPION, ARM_CANDIDATE}
    for sample in artifact["samples"]:
        assert set(sample["arms"]) == {ARM_NO_SKILL, ARM_CHAMPION, ARM_CANDIDATE}
        for arm in sample["arms"].values():
            assert arm["response"], "every arm must produce a freshly generated response"


async def test_every_arm_shares_one_frozen_generation_config(skill_workspace: Path):
    version = _seed_skill(champion_instructions="Cite sources.", candidate_instructions="Always cite sources.")
    record: list[GenerationRequest] = []

    artifact = await run_paired_evaluation(
        skill_name=SKILL,
        candidate_version=version,
        generate=_arm_generator(
            {ARM_NO_SKILL: "a https://x", ARM_CHAMPION: "b https://x", ARM_CANDIDATE: "c https://x"},
            record=record,
        ),
        generation_config=_config(),
        judge_config=_judge_config(independent=True),
        gate_config=_permissive_gate(),
        samples=_samples(2),
    )

    configs = {(item.config.model, item.config.temperature, item.config.seed, item.config.timeout_s) for item in record}
    assert len(configs) == 1
    recorded = artifact["generation_config"]
    assert recorded["model"] == "gen-model"
    assert recorded["temperature"] == 0.0
    assert recorded["seed"] == 42
    assert recorded["timeout_s"] == 5.0
    assert recorded["max_output_tokens"] == 1024
    assert recorded["tools"] == []


async def test_only_the_skill_block_differs_between_arms(skill_workspace: Path):
    version = _seed_skill(champion_instructions="Cite sources.", candidate_instructions="Always cite sources.")
    record: list[GenerationRequest] = []

    await run_paired_evaluation(
        skill_name=SKILL,
        candidate_version=version,
        generate=_arm_generator(
            {ARM_NO_SKILL: "a https://x", ARM_CHAMPION: "b https://x", ARM_CANDIDATE: "c https://x"},
            record=record,
        ),
        generation_config=_config(),
        judge_config=_judge_config(independent=True),
        gate_config=_permissive_gate(),
        samples=_samples(1),
    )

    by_arm = {item.arm: item.system_prompt for item in record}
    assert by_arm[ARM_NO_SKILL] == _config().base_system_prompt
    assert "Cite sources." in by_arm[ARM_CHAMPION]
    assert "Always cite sources." in by_arm[ARM_CANDIDATE]
    assert by_arm[ARM_CHAMPION] != by_arm[ARM_CANDIDATE]
    assert {item.user_message for item in record} == {"question h 0"}


async def test_the_same_rule_set_grades_all_arms(skill_workspace: Path):
    version = _seed_skill(champion_instructions="Cite sources.", candidate_instructions="Always cite sources.")

    artifact = await run_paired_evaluation(
        skill_name=SKILL,
        candidate_version=version,
        generate=_arm_generator(
            {ARM_NO_SKILL: "plain", ARM_CHAMPION: "see https://a", ARM_CANDIDATE: "see https://b"}
        ),
        generation_config=_config(),
        judge_config=_judge_config(independent=True),
        gate_config=_permissive_gate(),
        samples=_samples(1),
    )

    arms = artifact["samples"][0]["arms"]
    rule_ids = [{rule["rule_id"] for rule in arm["rules"]} for arm in arms.values()]
    assert rule_ids[0] == rule_ids[1] == rule_ids[2]
    assert artifact["rules_source"] == "champion"
    assert arms[ARM_NO_SKILL]["passed"] is False
    assert arms[ARM_CANDIDATE]["passed"] is True


async def test_paired_differences_are_reported_per_sample(skill_workspace: Path):
    version = _seed_skill(champion_instructions="Cite sources.", candidate_instructions="Always cite sources.")

    artifact = await run_paired_evaluation(
        skill_name=SKILL,
        candidate_version=version,
        generate=_arm_generator(
            {ARM_NO_SKILL: "plain", ARM_CHAMPION: "plain", ARM_CANDIDATE: "see https://b"}
        ),
        generation_config=_config(),
        judge_config=_judge_config(independent=True),
        gate_config=_permissive_gate(),
        samples=_samples(3),
    )

    assert [sample["pair_delta"] for sample in artifact["samples"]] == [1, 1, 1]
    assert artifact["metrics"]["gate"]["paired_gain"] == 3


async def test_a_generation_failure_counts_as_a_hard_failure(skill_workspace: Path):
    version = _seed_skill(champion_instructions="Cite sources.", candidate_instructions="Always cite sources.")

    async def _generate(request: GenerationRequest) -> GenerationResult:
        if request.arm == ARM_CANDIDATE:
            raise RuntimeError("model exploded")
        return GenerationResult(text="see https://a", input_tokens=10, output_tokens=10)

    artifact = await run_paired_evaluation(
        skill_name=SKILL,
        candidate_version=version,
        generate=_generate,
        generation_config=_config(),
        judge_config=_judge_config(independent=True),
        gate_config=_permissive_gate(),
        samples=_samples(2),
    )

    assert artifact["metrics"]["gate"]["candidate_hard_failures"] == 2
    assert artifact["gate"]["decision"] == DECISION_REJECT


# ------------------------------------------------- P1.4 replay / holdout isolation


def test_generation_samples_cannot_serve_as_holdout():
    shared = EvalSample(sample_id="s-shared", user_message="reused question", origin=ORIGIN_HOLDOUT)
    clean = EvalSample(sample_id="s-clean", user_message="fresh question", origin=ORIGIN_HOLDOUT)

    pool = build_sample_pool(replay=[], holdout=[shared, clean], generation_sample_ids={"s-shared"})

    assert [item.sample_id for item in pool["holdout"]] == ["s-clean"]
    assert [item.sample_id for item in pool["replay"]] == ["s-shared"]
    assert pool["contaminated_holdout_ids"] == ["s-shared"]


def test_replay_samples_are_never_relabelled_as_holdout():
    replay = EvalSample(sample_id="s-1", user_message="q", origin=ORIGIN_HOLDOUT)

    pool = build_sample_pool(replay=[replay], holdout=[], generation_sample_ids=set())

    assert pool["replay"][0].origin == ORIGIN_REPLAY
    assert pool["holdout"] == []


def test_replay_pool_is_built_from_candidate_provenance(skill_workspace: Path):
    from agents.skill_evolution import ONLINE_PROVENANCE_LOG, get_evolution_dir

    log = get_evolution_dir() / ONLINE_PROVENANCE_LOG
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        json.dumps(
            {
                "skill": SKILL,
                "time": "2026-01-01T00:00:00Z",
                "action": "add",
                "messages": [{"role": "user", "content": "how do I cite?"}],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    replay = load_replay_samples(SKILL)

    assert [item.origin for item in replay] == [ORIGIN_REPLAY]
    assert replay[0].sample_id == sample_id_for("how do I cite?")


def test_holdout_file_samples_are_marked_holdout(skill_workspace: Path):
    holdout_root().mkdir(parents=True, exist_ok=True)
    (holdout_root() / "shared.jsonl").write_text(
        json.dumps({"sample_id": "hp-1", "user_message": "independent question"}) + "\n",
        encoding="utf-8",
    )

    samples = load_holdout_samples(SKILL)

    assert [item.sample_id for item in samples] == ["hp-1"]
    assert samples[0].origin == ORIGIN_HOLDOUT


async def test_the_gate_reads_holdout_only_and_reports_both_pools(skill_workspace: Path):
    version = _seed_skill(champion_instructions="Cite sources.", candidate_instructions="Always cite sources.")
    samples = _samples(2, origin=ORIGIN_REPLAY, prefix="r") + _samples(3, origin=ORIGIN_HOLDOUT, prefix="h")

    artifact = await run_paired_evaluation(
        skill_name=SKILL,
        candidate_version=version,
        generate=_arm_generator(
            {ARM_NO_SKILL: "plain", ARM_CHAMPION: "plain", ARM_CANDIDATE: "see https://b"}
        ),
        generation_config=_config(),
        judge_config=_judge_config(independent=True),
        gate_config=_permissive_gate(),
        samples=samples,
    )

    assert artifact["sample_pool"]["replay_count"] == 2
    assert artifact["sample_pool"]["holdout_count"] == 3
    assert artifact["metrics"]["gate"]["paired_samples"] == 3
    assert artifact["metrics"]["replay"]["paired_samples"] == 2
    assert artifact["metrics"]["holdout"]["paired_samples"] == 3


async def test_without_holdout_the_run_can_only_incubate(skill_workspace: Path):
    version = _seed_skill(champion_instructions="Cite sources.", candidate_instructions="Always cite sources.")

    artifact = await run_paired_evaluation(
        skill_name=SKILL,
        candidate_version=version,
        generate=_arm_generator(
            {ARM_NO_SKILL: "plain", ARM_CHAMPION: "plain", ARM_CANDIDATE: "see https://b"}
        ),
        generation_config=_config(),
        judge_config=_judge_config(independent=True),
        gate_config=GateConfig(min_paired_samples=1),
        samples=_samples(4, origin=ORIGIN_REPLAY, prefix="r"),
    )

    assert artifact["gate"]["decision"] == DECISION_INCUBATING
    assert any("holdout" in reason for reason in artifact["gate"]["incubating_reasons"])
    assert reg.load_registry()["skills"][SKILL]["versions"][version]["status"] == reg.STATUS_SHADOW


async def test_a_shared_generator_and_judge_are_marked_non_independent(skill_workspace: Path):
    version = _seed_skill(champion_instructions="Cite sources.", candidate_instructions="Always cite sources.")

    async def _judge(system: str, user: str) -> str:
        return json.dumps({"pass": True, "reason": "ok"})

    artifact = await run_paired_evaluation(
        skill_name=SKILL,
        candidate_version=version,
        generate=_arm_generator(
            {ARM_NO_SKILL: "plain", ARM_CHAMPION: "plain", ARM_CANDIDATE: "see https://b"}
        ),
        judge=_judge,
        generation_config=_config(),
        judge_config=_judge_config(independent=False),
        gate_config=GateConfig(min_paired_samples=1, require_holdout=False),
        samples=_samples(3),
    )

    assert artifact["judge_independent"] is False
    assert artifact["judge_role"] == "non_independent"
    assert artifact["gate"]["decision"] == DECISION_INCUBATING
    assert not (reg.champion_dir(SKILL) / "meta.json").read_text(encoding="utf-8").count(version)


async def test_the_judge_never_sees_which_arm_it_is_scoring(skill_workspace: Path):
    version = _seed_skill(champion_instructions="Cite sources.", candidate_instructions="Always cite sources.")
    seen: list[str] = []

    async def _judge(system: str, user: str) -> str:
        seen.append(system + user)
        return json.dumps({"pass": True, "reason": "ok"})

    await run_paired_evaluation(
        skill_name=SKILL,
        candidate_version=version,
        generate=_arm_generator(
            {ARM_NO_SKILL: "plain", ARM_CHAMPION: "plain", ARM_CANDIDATE: "see https://b"}
        ),
        judge=_judge,
        generation_config=_config(),
        judge_config=_judge_config(independent=True),
        gate_config=_permissive_gate(),
        samples=_samples(1),
    )

    assert seen
    for payload in seen:
        assert "current_champion" not in payload
        assert "no_skill" not in payload
        assert '"arm"' not in payload


# ------------------------------------------------------------ P1.5 gated outcomes


async def test_a_gated_win_becomes_champion_but_not_active(skill_workspace: Path):
    version = _seed_skill(champion_instructions="Cite sources.", candidate_instructions="Always cite sources.")

    artifact = await run_paired_evaluation(
        skill_name=SKILL,
        candidate_version=version,
        generate=_arm_generator(
            {ARM_NO_SKILL: "plain", ARM_CHAMPION: "plain", ARM_CANDIDATE: "see https://b"}
        ),
        generation_config=_config(),
        judge_config=_judge_config(independent=True),
        gate_config=_permissive_gate(),
        samples=_samples(3),
    )

    assert artifact["gate"]["decision"] == DECISION_PROMOTE
    assert reg.load_registry()["skills"][SKILL]["versions"][version]["status"] == reg.STATUS_CHAMPION
    assert not reg.active_skill_file(SKILL).exists()


async def test_a_regression_is_rejected_and_recorded(skill_workspace: Path):
    version = _seed_skill(champion_instructions="Cite sources.", candidate_instructions="Skip the sources.")

    artifact = await run_paired_evaluation(
        skill_name=SKILL,
        candidate_version=version,
        generate=_arm_generator(
            {ARM_NO_SKILL: "plain", ARM_CHAMPION: "see https://a", ARM_CANDIDATE: "plain"}
        ),
        generation_config=_config(),
        judge_config=_judge_config(independent=True),
        gate_config=_permissive_gate(),
        samples=_samples(4),
    )

    assert artifact["gate"]["decision"] == DECISION_REJECT
    assert artifact["metrics"]["gate"]["negative_transfer_rate"] == 1.0
    entry = reg.load_registry()["skills"][SKILL]["versions"][version]
    assert entry["status"] == reg.STATUS_REJECTED
    assert entry["history"][-1]["reason"]


async def test_the_artifact_records_thresholds_and_config(skill_workspace: Path):
    version = _seed_skill(champion_instructions="Cite sources.", candidate_instructions="Always cite sources.")

    artifact = await run_paired_evaluation(
        skill_name=SKILL,
        candidate_version=version,
        generate=_arm_generator(
            {ARM_NO_SKILL: "plain", ARM_CHAMPION: "plain", ARM_CANDIDATE: "see https://b"}
        ),
        generation_config=_config(),
        judge_config=_judge_config(independent=True),
        gate_config=_permissive_gate(max_negative_transfer_rate=0.07),
        samples=_samples(2),
    )

    stored = json.loads(Path(artifact["artifact_path"]).read_text(encoding="utf-8"))
    assert stored["gate"]["thresholds"]["max_negative_transfer_rate"] == 0.07
    assert stored["generation_config"]["seed"] == 42
    assert stored["judge_config"]["model"] == "judge-model"
    assert stored["arms"] == [ARM_NO_SKILL, ARM_CHAMPION, ARM_CANDIDATE]
    assert stored["candidate_version"] == version


async def test_an_expensive_candidate_is_rejected_on_cost(skill_workspace: Path):
    version = _seed_skill(champion_instructions="Cite sources.", candidate_instructions="Always cite sources.")

    artifact = await run_paired_evaluation(
        skill_name=SKILL,
        candidate_version=version,
        generate=_arm_generator(
            {ARM_NO_SKILL: "plain", ARM_CHAMPION: "plain", ARM_CANDIDATE: "see https://b"},
            tokens={ARM_NO_SKILL: 20, ARM_CHAMPION: 20, ARM_CANDIDATE: 60},
        ),
        generation_config=_config(),
        judge_config=_judge_config(independent=True),
        gate_config=_permissive_gate(),
        samples=_samples(3),
    )

    assert artifact["gate"]["decision"] == DECISION_REJECT
    assert any("cost growth" in reason for reason in artifact["gate"]["blocking_reasons"])
    assert artifact["metrics"]["gate"]["paired_gain"] == 3


async def test_evaluation_without_a_candidate_fails_cleanly(skill_workspace: Path):
    artifact = await run_paired_evaluation(
        skill_name="nothing-here",
        generate=_arm_generator({ARM_NO_SKILL: "a", ARM_CHAMPION: "b", ARM_CANDIDATE: "c"}),
        generation_config=_config(),
    )

    assert not artifact["ok"]
    assert "no candidate" in artifact["error"]


async def test_full_lifecycle_from_candidate_to_rollback(skill_workspace: Path):
    version = _seed_skill(champion_instructions="Cite sources.", candidate_instructions="Always cite sources.")
    champion_version = reg.load_registry()["skills"][SKILL]["champion_version"]
    reg.promote(SKILL, champion_version)

    artifact = await run_paired_evaluation(
        skill_name=SKILL,
        candidate_version=version,
        generate=_arm_generator(
            {ARM_NO_SKILL: "plain", ARM_CHAMPION: "plain", ARM_CANDIDATE: "see https://b"}
        ),
        generation_config=_config(),
        judge_config=_judge_config(independent=True),
        gate_config=_permissive_gate(),
        samples=_samples(3),
    )
    assert artifact["gate"]["decision"] == DECISION_PROMOTE

    promoted = reg.promote(SKILL, version)
    assert promoted["ok"], promoted
    assert "Always cite sources." in reg.active_skill_file(SKILL).read_text(encoding="utf-8")

    rolled_back = reg.rollback(SKILL)
    assert rolled_back["ok"], rolled_back
    assert "Always cite sources." not in reg.active_skill_file(SKILL).read_text(encoding="utf-8")
    assert reg.verify_consistency()["ok"]
