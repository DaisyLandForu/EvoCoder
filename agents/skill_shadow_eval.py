"""Paired shadow evaluation: regenerate `no_skill`, `current_champion`, `candidate`.

All three arms answer the *same* sample under the *same* frozen generation
config; the only thing that varies is the skill block injected into the system
prompt. Reusing a champion's historical reply is therefore impossible: every arm
is generated inside this run and the run records the config it used.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .skill_evolution import ONLINE_PROVENANCE_LOG, _safe_skill_slug, _utc_now, get_evolution_dir
from .skill_gate import (
    DECISION_INCUBATING,
    DECISION_PROMOTE,
    DECISION_REJECT,
    GateConfig,
    apply_gate,
    compute_paired_metrics,
)
from .skill_registry import (
    STATUS_CANDIDATE,
    STATUS_REJECTED,
    STATUS_SHADOW,
    atomic_write_text,
    evaluations_root,
    latest_candidate,
    load_registry,
    mark_champion,
    read_candidate,
    read_champion,
    registry_root,
    transition,
)
from .skill_rules import compile_eval_rules, score_response

ARM_NO_SKILL = "no_skill"
ARM_CHAMPION = "current_champion"
ARM_CANDIDATE = "candidate"
ARMS = (ARM_NO_SKILL, ARM_CHAMPION, ARM_CANDIDATE)

ORIGIN_REPLAY = "replay"
ORIGIN_HOLDOUT = "holdout"

BASE_SYSTEM_PROMPT = "You are a helpful engineering assistant. Answer the user's message directly and completely."


# ------------------------------------------------------------------------ config


@dataclass(frozen=True)
class GenerationConfig:
    """Frozen per-run generation settings, identical for all three arms."""

    model: str = "eval-model"
    provider: str = "unknown"
    temperature: float = 0.0
    max_output_tokens: int = 1024
    timeout_s: float = 60.0
    seed: int | None = 7
    tools: tuple[str, ...] = ()
    base_system_prompt: str = BASE_SYSTEM_PROMPT
    enforced_by: str = "runner"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["tools"] = list(self.tools)
        return data

    def identity(self) -> str:
        return f"{self.provider}:{self.model}:{self.temperature}:{self.seed}"


@dataclass(frozen=True)
class JudgeConfig:
    """Judge settings, kept separate from the generator so independence is checkable."""

    model: str = "judge-model"
    provider: str = "unknown"
    temperature: float = 0.0
    seed: int | None = 11
    blinded: bool = True
    declared_independent: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def identity(self) -> str:
        return f"{self.provider}:{self.model}"

    def is_independent_of(self, generation: GenerationConfig) -> bool:
        if self.declared_independent is not None:
            return bool(self.declared_independent)
        return self.identity() != f"{generation.provider}:{generation.model}"


@dataclass
class EvalSample:
    sample_id: str
    user_message: str
    origin: str = ORIGIN_HOLDOUT
    tags: list[str] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GenerationRequest:
    arm: str
    sample_id: str
    system_prompt: str
    user_message: str
    config: GenerationConfig


@dataclass
class GenerationResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    error: str = ""

    @property
    def total_tokens(self) -> int:
        return int(self.input_tokens) + int(self.output_tokens)


GenerateFn = Callable[[GenerationRequest], Awaitable[GenerationResult]]
JudgeFn = Callable[[str, str], Awaitable[str]]


def estimate_tokens(text: str) -> int:
    return max(1, len(str(text or "")) // 4)


# ----------------------------------------------------------------------- samples


def sample_id_for(text: str) -> str:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    return "s-" + hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]


def holdout_root() -> Path:
    return registry_root() / "holdout"


def load_replay_samples(skill_name: str, *, limit: int = 200) -> list[EvalSample]:
    """Samples that produced the candidate. Usable for replay only, never as holdout."""
    path = get_evolution_dir() / ONLINE_PROVENANCE_LOG
    if not path.is_file():
        return []
    samples: dict[str, EvalSample] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if str(row.get("skill") or "").strip() != skill_name:
            continue
        messages = row.get("messages") if isinstance(row.get("messages"), list) else []
        user_messages = [
            str(item.get("content") or "").strip()
            for item in messages
            if isinstance(item, dict) and str(item.get("role") or "") == "user"
        ]
        text = next((item for item in reversed(user_messages) if item), "")
        if not text:
            continue
        identifier = sample_id_for(text)
        samples[identifier] = EvalSample(
            sample_id=identifier,
            user_message=text,
            origin=ORIGIN_REPLAY,
            provenance={"kind": "online_provenance", "time": row.get("time"), "action": row.get("action")},
        )
    return list(samples.values())[-limit:]


def load_holdout_samples(skill_name: str) -> list[EvalSample]:
    """Independent samples, from files explicitly marked as not used for generation."""
    out: dict[str, EvalSample] = {}
    for path in (holdout_root() / "shared.jsonl", holdout_root() / f"{_safe_skill_slug(skill_name)}.jsonl"):
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            text = str(row.get("user_message") or row.get("prompt") or "").strip()
            if not text:
                continue
            identifier = str(row.get("sample_id") or "").strip() or sample_id_for(text)
            out[identifier] = EvalSample(
                sample_id=identifier,
                user_message=text,
                origin=ORIGIN_HOLDOUT,
                tags=[str(tag) for tag in (row.get("tags") or [])],
                provenance={"kind": "holdout_file", "file": str(path)},
            )
    return list(out.values())


def build_sample_pool(
    *,
    replay: list[EvalSample],
    holdout: list[EvalSample],
    generation_sample_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Enforce the replay / holdout split: anything used to build the candidate is replay."""
    generation_ids = set(generation_sample_ids or set())
    replay_by_id: dict[str, EvalSample] = {}
    for sample in replay:
        sample.origin = ORIGIN_REPLAY
        replay_by_id[sample.sample_id] = sample
    generation_ids |= set(replay_by_id)

    clean_holdout: dict[str, EvalSample] = {}
    contaminated: list[str] = []
    for sample in holdout:
        if sample.sample_id in generation_ids:
            contaminated.append(sample.sample_id)
            sample.origin = ORIGIN_REPLAY
            replay_by_id.setdefault(sample.sample_id, sample)
            continue
        sample.origin = ORIGIN_HOLDOUT
        clean_holdout[sample.sample_id] = sample

    return {
        "replay": list(replay_by_id.values()),
        "holdout": list(clean_holdout.values()),
        "contaminated_holdout_ids": contaminated,
        "generation_sample_ids": sorted(generation_ids),
    }


# ------------------------------------------------------------------- arm prompts


def build_arm_system_prompt(arm: str, skill: dict[str, Any] | None, config: GenerationConfig) -> str:
    base = config.base_system_prompt
    if arm == ARM_NO_SKILL or not skill:
        return base
    block_lines = ["", "## Active Skill", f"Name: {skill.get('name', '')}"]
    if skill.get("description"):
        block_lines.append(f"Description: {skill['description']}")
    if skill.get("when_to_use"):
        block_lines.append(f"When to use: {skill['when_to_use']}")
    if skill.get("instructions"):
        block_lines.extend(["", str(skill["instructions"]).strip()])
    return base + "\n" + "\n".join(block_lines) + "\n"


def _prompt_hash(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:16]


# -------------------------------------------------------------------- evaluation


async def _run_arm(
    *,
    arm: str,
    sample: EvalSample,
    skill: dict[str, Any] | None,
    config: GenerationConfig,
    generate: GenerateFn,
    rules: list[dict[str, Any]],
    judge: JudgeFn | None,
) -> dict[str, Any]:
    system_prompt = build_arm_system_prompt(arm, skill, config)
    request = GenerationRequest(
        arm=arm,
        sample_id=sample.sample_id,
        system_prompt=system_prompt,
        user_message=sample.user_message,
        config=config,
    )
    started = time.monotonic()
    try:
        result = await asyncio.wait_for(generate(request), timeout=config.timeout_s or None)
    except asyncio.TimeoutError:
        result = GenerationResult(text="", error=f"generation timed out after {config.timeout_s}s")
    except Exception as exc:
        result = GenerationResult(text="", error=str(exc)[:500])

    input_tokens = int(result.input_tokens or 0) or estimate_tokens(system_prompt + sample.user_message)
    output_tokens = int(result.output_tokens or 0) or estimate_tokens(result.text)
    score = await score_response(
        rules=rules, response_text=result.text, user_message=sample.user_message, judge=judge
    )
    return {
        "arm": arm,
        "sample_id": sample.sample_id,
        "system_prompt_hash": _prompt_hash(system_prompt),
        "response": result.text,
        "error": result.error,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "latency_s": round(time.monotonic() - started, 4),
        "passed": bool(score["passed"]) and not result.error,
        "hard_failed": bool(score["hard_failed"]) or bool(result.error),
        "hard_failures": score["hard_failures"],
        "soft_pass_rate": score["soft_pass_rate"],
        "rules": score["rules"],
    }


async def run_paired_evaluation(
    *,
    skill_name: str,
    candidate_version: str = "",
    generate: GenerateFn,
    judge: JudgeFn | None = None,
    generation_config: GenerationConfig | None = None,
    judge_config: JudgeConfig | None = None,
    gate_config: GateConfig | None = None,
    samples: list[EvalSample] | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Regenerate all three arms for every sample, score them pairwise, apply the gate."""
    generation_config = generation_config or GenerationConfig()
    judge_config = judge_config or JudgeConfig()
    gate_config = gate_config or GateConfig.from_env()

    version = candidate_version
    if not version:
        entry = latest_candidate(skill_name)
        if entry is None:
            return {"ok": False, "error": f"no candidate awaiting evaluation for {skill_name}"}
        version = str(entry.get("version") or "")
    candidate = read_candidate(skill_name, version)
    if candidate is None:
        return {"ok": False, "error": f"candidate file missing: {skill_name}@{version}"}
    champion = read_champion(skill_name)

    registry = load_registry()
    entry = registry.get("skills", {}).get(skill_name, {}).get("versions", {}).get(version, {})
    if str(entry.get("status") or "") == STATUS_CANDIDATE:
        transition(skill_name, version, STATUS_SHADOW, reason="shadow evaluation started", actor="evaluator")

    provenance = entry.get("source_provenance") if isinstance(entry.get("source_provenance"), dict) else {}
    generation_sample_ids = {str(item) for item in (provenance.get("sample_ids") or []) if str(item).strip()}

    if samples is None:
        pool = build_sample_pool(
            replay=load_replay_samples(skill_name),
            holdout=load_holdout_samples(skill_name),
            generation_sample_ids=generation_sample_ids,
        )
    else:
        pool = build_sample_pool(
            replay=[item for item in samples if item.origin == ORIGIN_REPLAY],
            holdout=[item for item in samples if item.origin != ORIGIN_REPLAY],
            generation_sample_ids=generation_sample_ids,
        )

    all_samples = pool["replay"] + pool["holdout"]
    if not all_samples:
        return {
            "ok": False,
            "error": f"no evaluation samples available for {skill_name}",
            "skill": skill_name,
            "version": version,
        }

    # One rule set for every arm, so the comparison measures behaviour, not grading.
    rules_source = "champion" if champion else "candidate"
    rules = compile_eval_rules(champion or candidate, include_llm_rules=judge is not None)
    independent_judge = judge is not None and judge_config.is_independent_of(generation_config)

    run_id = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:8]}"
    sample_results: list[dict[str, Any]] = []
    for sample in all_samples:
        arms: dict[str, dict[str, Any]] = {}
        for arm, skill in (
            (ARM_NO_SKILL, None),
            (ARM_CHAMPION, champion),
            (ARM_CANDIDATE, candidate),
        ):
            arms[arm] = await _run_arm(
                arm=arm,
                sample=sample,
                skill=skill,
                config=generation_config,
                generate=generate,
                rules=rules,
                judge=judge,
            )
        champion_arm = arms[ARM_CHAMPION]
        candidate_arm = arms[ARM_CANDIDATE]
        # With no champion yet, the champion arm degenerates to the no-skill baseline.
        champion_pass = bool(champion_arm["passed"]) if champion else bool(arms[ARM_NO_SKILL]["passed"])
        champion_tokens = champion_arm["total_tokens"] if champion else arms[ARM_NO_SKILL]["total_tokens"]
        sample_results.append(
            {
                "sample_id": sample.sample_id,
                "origin": sample.origin,
                "user_message": sample.user_message,
                "arms": arms,
                "champion_pass": champion_pass,
                "candidate_pass": bool(candidate_arm["passed"]),
                "no_skill_pass": bool(arms[ARM_NO_SKILL]["passed"]),
                "candidate_hard_failed": bool(candidate_arm["hard_failed"]),
                "champion_tokens": champion_tokens,
                "candidate_tokens": candidate_arm["total_tokens"],
                "no_skill_tokens": arms[ARM_NO_SKILL]["total_tokens"],
                "pair_delta": int(bool(candidate_arm["passed"])) - int(champion_pass),
            }
        )

    holdout_pairs = [item for item in sample_results if item["origin"] == ORIGIN_HOLDOUT]
    replay_pairs = [item for item in sample_results if item["origin"] == ORIGIN_REPLAY]
    gated_pairs = holdout_pairs if holdout_pairs else []

    metrics_holdout = compute_paired_metrics(holdout_pairs)
    metrics_replay = compute_paired_metrics(replay_pairs)
    metrics_gate = compute_paired_metrics(gated_pairs)
    gate = apply_gate(
        metrics_gate,
        config=gate_config,
        independent_judge=independent_judge,
        holdout_available=bool(holdout_pairs),
    )

    artifact = {
        "run_id": run_id,
        "created_at": _utc_now(),
        "skill": skill_name,
        "candidate_version": version,
        "champion_version": str((champion or {}).get("version") or ""),
        "rules_source": rules_source,
        "rules": rules,
        "generation_config": generation_config.to_dict(),
        "judge_config": judge_config.to_dict(),
        "judge_independent": independent_judge,
        "judge_role": "independent" if independent_judge else "non_independent",
        "arms": list(ARMS),
        "sample_pool": {
            "replay_count": len(replay_pairs),
            "holdout_count": len(holdout_pairs),
            "contaminated_holdout_ids": pool["contaminated_holdout_ids"],
            "generation_sample_ids": pool["generation_sample_ids"],
        },
        "metrics": {"gate": metrics_gate, "holdout": metrics_holdout, "replay": metrics_replay},
        "gate": gate,
        "samples": sample_results,
    }

    decision = gate["decision"]
    if decision == DECISION_PROMOTE:
        outcome = mark_champion(
            skill_name, version, reason=f"gate passed in run {run_id}", actor="gate"
        )
        artifact["lifecycle"] = outcome
    elif decision == DECISION_REJECT:
        artifact["lifecycle"] = transition(
            skill_name,
            version,
            STATUS_REJECTED,
            reason="; ".join(gate["blocking_reasons"])[:400],
            actor="gate",
        )
    else:
        artifact["lifecycle"] = {
            "ok": True,
            "status": STATUS_SHADOW,
            "decision": DECISION_INCUBATING,
            "reasons": gate["incubating_reasons"],
        }

    if persist:
        target = evaluations_root() / _safe_skill_slug(skill_name) / f"{run_id}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(target, json.dumps(artifact, ensure_ascii=False, indent=2) + "\n")
        artifact["artifact_path"] = str(target)

    artifact["ok"] = True
    return artifact


def make_side_query_generator(side_query: JudgeFn) -> GenerateFn:
    """Adapt an agent side query into a generator for all three arms."""

    async def _generate(request: GenerationRequest) -> GenerationResult:
        text = await side_query(request.system_prompt, request.user_message)
        return GenerationResult(text=str(text or ""))

    return _generate


def format_evaluation_summary(artifact: dict[str, Any]) -> str:
    if not artifact.get("ok"):
        return f"Shadow evaluation failed: {artifact.get('error', 'unknown error')}"
    gate = artifact.get("gate", {})
    metrics = gate.get("metrics", {})
    pool = artifact.get("sample_pool", {})
    lines = [
        f"Shadow evaluation {artifact.get('run_id', '')} for {artifact.get('skill', '')}"
        f"@{artifact.get('candidate_version', '')}",
        f"  arms: {', '.join(artifact.get('arms', []))} (all regenerated in this run)",
        f"  generation: {artifact.get('generation_config', {}).get('model', '')} "
        f"temp={artifact.get('generation_config', {}).get('temperature', '')} "
        f"seed={artifact.get('generation_config', {}).get('seed', '')}",
        f"  judge: {artifact.get('judge_config', {}).get('model', '')} "
        f"({artifact.get('judge_role', '')})",
        f"  samples: replay={pool.get('replay_count', 0)} holdout={pool.get('holdout_count', 0)} "
        f"contaminated={len(pool.get('contaminated_holdout_ids', []))}",
        f"  pass rates: no_skill={metrics.get('no_skill_pass_rate', 0):.2f} "
        f"champion={metrics.get('champion_pass_rate', 0):.2f} "
        f"candidate={metrics.get('candidate_pass_rate', 0):.2f}",
        f"  paired gain={metrics.get('paired_gain', 0)} "
        f"NTR={metrics.get('negative_transfer_rate', 0):.3f} "
        f"cost_growth={metrics.get('cost_growth', 0):.3f} "
        f"hard_failures={metrics.get('candidate_hard_failures', 0)}",
        f"  decision: {gate.get('decision', '')}",
    ]
    for reason in gate.get("blocking_reasons", []):
        lines.append(f"    blocked: {reason}")
    for reason in gate.get("incubating_reasons", []):
        lines.append(f"    incubating: {reason}")
    if artifact.get("artifact_path"):
        lines.append(f"  artifact: {artifact['artifact_path']}")
    if gate.get("decision") == DECISION_PROMOTE:
        lines.append(
            f"  next: /skill-promote {artifact.get('skill', '')} {artifact.get('candidate_version', '')}"
        )
    return "\n".join(lines)
