from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agents import main as cli
from agents import skill_registry as reg
from agents.skill_gate import DECISION_INCUBATING


class _FakeAgent:
    """Minimal stand-in exposing what /skill-eval reads from a live agent."""

    def __init__(self):
        self.config = SimpleNamespace(model="session-model", provider="test", model_timeout_s=45.0)
        self.prompts: list[tuple[str, str]] = []

    def _build_side_query(self, max_tokens: int = 0):
        async def _query(system: str, user: str) -> str:
            self.prompts.append((system, user))
            if "binary evaluator" in system:
                return json.dumps({"pass": True, "reason": "ok"})
            if "always attach a link" in system:
                return "Conclusion: yes. Source: https://example.invalid/detail"
            return "Conclusion: yes. The detail follows without any link."

        return _query


def _candidate(instructions: str = "State the conclusion first and cite sources.") -> str:
    created = reg.create_candidate(
        skill_name="conclusion-first",
        description="Lead with the conclusion",
        when_to_use="Open questions",
        instructions=instructions,
        source_provenance={"kind": "user_feedback", "sample_ids": ["s-1"]},
    )
    return created["version"]


def test_help_text_documents_the_lifecycle_commands():
    for command in ("/skill-status", "/skill-eval", "/skill-promote", "/skill-rollback"):
        assert command in cli.HELP_TEXT


async def test_skill_eval_reports_a_non_independent_judge(skill_workspace: Path):
    champion = _candidate("State the conclusion first and cite sources plainly.")
    reg.mark_champion("conclusion-first", champion, reason="seed")
    version = _candidate("State the conclusion first and cite sources; always attach a link.")
    holdout = reg.registry_root() / "holdout"
    holdout.mkdir(parents=True, exist_ok=True)
    (holdout / "shared.jsonl").write_text(
        "\n".join(
            json.dumps({"sample_id": f"h-{index}", "user_message": f"question {index}"})
            for index in range(3)
        )
        + "\n",
        encoding="utf-8",
    )

    agent = _FakeAgent()
    summary = await cli._run_skill_eval(agent, "conclusion-first", version)

    assert "non_independent" in summary
    assert f"decision: {DECISION_INCUBATING}" in summary
    assert "judge is not independent" in summary
    assert "session-model" in summary
    assert reg.load_registry()["skills"]["conclusion-first"]["versions"][version]["status"] == (
        reg.STATUS_SHADOW
    )
    assert not reg.active_skill_file("conclusion-first").exists()


async def test_skill_eval_without_samples_reports_the_failure(skill_workspace: Path):
    version = _candidate()
    summary = await cli._run_skill_eval(_FakeAgent(), "conclusion-first", version)

    assert "failed" in summary
    assert "no evaluation samples" in summary


def test_skill_status_report_is_reachable_from_the_cli(skill_workspace: Path):
    version = _candidate()

    report = cli.skill_status_report()
    assert "conclusion-first" in report
    assert version in report


def test_promote_and_rollback_are_wired_to_the_registry(skill_workspace: Path):
    first = _candidate()
    reg.mark_champion("conclusion-first", first, reason="test")
    assert cli.promote_skill_version("conclusion-first", first)["ok"]

    second = _candidate()
    reg.mark_champion("conclusion-first", second, reason="test")
    assert cli.promote_skill_version("conclusion-first", second)["ok"]

    rolled_back = cli.rollback_skill_version("conclusion-first")
    assert rolled_back["ok"], rolled_back
    assert rolled_back["version"] == first
