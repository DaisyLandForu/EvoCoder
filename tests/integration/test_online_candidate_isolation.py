from __future__ import annotations

import json
from pathlib import Path

from agents import skill_registry as reg
from agents.online_skill_evolution import online_ingest
from agents.skill_shadow_eval import sample_id_for

MESSAGES = [
    {"role": "user", "content": "Always answer with the conclusion first, then the reasoning."},
    {"role": "assistant", "content": "Understood."},
    {"role": "user", "content": "Yes, keep doing that from now on."},
]

EXTRACTED = {
    "skills": [
        {
            "name": "conclusion-first",
            "description": "Lead every answer with the conclusion",
            "when_to_use": "Any open-ended question",
            "instructions": "State the conclusion in the first sentence, then justify it.",
            "evidence": "User asked twice for conclusion-first answers.",
            "tags": ["style"],
        }
    ]
}


def _side_query(*, action: str = "add", target: str = ""):
    """Answer the extractor with a candidate and the maintainer with a decision."""

    async def _query(system: str, user: str) -> str:
        if "Skill Extractor" in system:
            return json.dumps(EXTRACTED)
        if "Skill Set Manager" in system:
            return json.dumps(
                {
                    "action": action,
                    "target_skill": target,
                    "reason": "durable style preference",
                    "merged_instructions": "Merged: state the conclusion first.",
                    "merged_description": "Lead every answer with the conclusion",
                    "merged_when_to_use": "Any open-ended question",
                }
            )
        return "{}"

    return _query


async def test_user_feedback_only_produces_a_candidate(skill_workspace: Path):
    result = await online_ingest(messages=MESSAGES, side_query=_side_query())

    assert result["ok"], result
    assert result["action"] == "add"
    assert result["status"] == reg.STATUS_CANDIDATE
    assert reg.candidates_root() in Path(result["file"]).parents
    assert not reg.active_skill_file("conclusion-first").exists()
    assert not (reg.champion_dir("conclusion-first") / "SKILL.md").exists()


async def test_feedback_cannot_overwrite_an_existing_active_skill(skill_workspace: Path):
    seed = reg.create_candidate(
        skill_name="conclusion-first",
        description="Lead every answer with the conclusion",
        instructions="Original active guidance.",
        source_provenance={"kind": "manual"},
    )
    reg.mark_champion("conclusion-first", seed["version"], reason="seed")
    reg.promote("conclusion-first", seed["version"])
    active = reg.active_skill_file("conclusion-first")
    before = active.read_text(encoding="utf-8")

    result = await online_ingest(
        messages=MESSAGES, side_query=_side_query(action="merge", target="conclusion-first")
    )

    assert result["ok"], result
    assert result["action"] == "merge"
    assert active.read_text(encoding="utf-8") == before
    assert result["status"] == reg.STATUS_CANDIDATE
    assert result["parent_version"] == seed["version"]


async def test_the_candidate_records_the_samples_that_produced_it(skill_workspace: Path):
    result = await online_ingest(messages=MESSAGES, side_query=_side_query(), hint="style")

    provenance = reg.load_registry()["skills"]["conclusion-first"]["versions"][result["version"]][
        "source_provenance"
    ]
    assert provenance["kind"] == "user_feedback"
    assert provenance["sample_ids"] == [
        sample_id_for(MESSAGES[0]["content"]),
        sample_id_for(MESSAGES[2]["content"]),
    ]
    assert provenance["hint"] == "style"


async def test_one_off_facts_from_feedback_are_not_persisted(skill_workspace: Path):
    leaky = {
        "skills": [
            {
                "name": "deploy-notes",
                "description": "Deploy checklist",
                "instructions": "Ping ops@example.com and read https://wiki.example.com/deploy on 2025-06-01.",
                "evidence": "user request",
                "tags": [],
            }
        ]
    }

    async def _query(system: str, user: str) -> str:
        if "Skill Extractor" in system:
            return json.dumps(leaky)
        return json.dumps({"action": "add", "reason": "durable"})

    result = await online_ingest(messages=MESSAGES, side_query=_query)

    body = Path(result["file"]).read_text(encoding="utf-8")
    assert "ops@example.com" not in body
    assert "wiki.example.com" not in body
    assert "2025-06-01" not in body
    assert result["redactions"]


async def test_a_discarded_candidate_writes_nothing(skill_workspace: Path):
    async def _query(system: str, user: str) -> str:
        if "Skill Extractor" in system:
            return json.dumps(EXTRACTED)
        return json.dumps({"action": "discard", "reason": "duplicate"})

    result = await online_ingest(messages=MESSAGES, side_query=_query)

    assert result["action"] == "discard"
    assert not reg.candidates_root().exists()
    assert "conclusion-first" not in reg.load_registry()["skills"]


async def test_repeated_feedback_stacks_candidates_without_activating(skill_workspace: Path):
    first = await online_ingest(messages=MESSAGES, side_query=_side_query())
    second = await online_ingest(
        messages=MESSAGES, side_query=_side_query(action="merge", target="conclusion-first")
    )

    node = reg.load_registry()["skills"]["conclusion-first"]
    assert {first["version"], second["version"]} <= set(node["versions"])
    assert node["active_version"] == ""
    assert not reg.active_skill_file("conclusion-first").exists()


async def test_a_denied_write_produces_no_candidate(skill_workspace: Path):
    async def _deny(summary: str) -> bool:
        assert "not activated" in summary
        return False

    result = await online_ingest(messages=MESSAGES, side_query=_side_query(), confirm_write=_deny)

    assert not result["ok"]
    assert result["action"] == "add_denied"
    assert not reg.candidates_root().exists()


def test_auto_archive_requires_explicit_opt_in(skill_workspace: Path, monkeypatch):
    from agents.skill_evolution import _maybe_prune_stale_skill

    skill_dir = Path.cwd() / ".bear" / "skills" / "stale"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: stale\n---\n\nbody\n", encoding="utf-8")
    stats = {"source": "user", "retrieved": 999, "used": 0, "skill_dir": str(skill_dir)}

    assert _maybe_prune_stale_skill("stale", stats) is False
    assert skill_dir.is_dir()

    monkeypatch.setenv("BEAR_SKILL_AUTO_ARCHIVE", "1")
    assert _maybe_prune_stale_skill("stale", dict(stats)) is True
    assert not skill_dir.exists()
