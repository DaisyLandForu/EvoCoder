from __future__ import annotations

from pathlib import Path

import pytest
from agents import skill_registry as reg


def _candidate(skill_workspace: Path, *, instructions: str = "Always answer with the conclusion first.") -> dict:
    result = reg.create_candidate(
        skill_name="answer-style",
        description="Answer with the conclusion first",
        when_to_use="When the user asks an open question",
        instructions=instructions,
        source_provenance={"kind": "user_feedback", "sample_ids": ["s-1"]},
    )
    assert result["ok"], result
    return result


# ------------------------------------------------------- P1.1 candidate isolation


def test_candidate_creation_never_writes_the_active_skill(skill_workspace: Path):
    result = _candidate(skill_workspace)

    assert Path(result["file"]).is_file()
    assert reg.candidates_root() in Path(result["file"]).parents
    assert not reg.active_skill_file("answer-style").exists()
    assert not (reg.champion_dir("answer-style") / "SKILL.md").exists()


def test_candidate_records_the_required_provenance_fields(skill_workspace: Path):
    result = _candidate(skill_workspace)
    entry = reg.load_registry()["skills"]["answer-style"]["versions"][result["version"]]

    for field in ("skill_name", "version", "parent_version", "created_at", "source_provenance", "content_hash", "status"):
        assert field in entry, field
    assert entry["status"] == reg.STATUS_CANDIDATE
    assert entry["content_hash"].startswith("sha256:")
    assert entry["source_provenance"]["sample_ids"] == ["s-1"]


def test_second_candidate_chains_to_the_previous_version(skill_workspace: Path):
    first = _candidate(skill_workspace)
    reg.mark_champion("answer-style", first["version"], reason="test")
    second = _candidate(skill_workspace, instructions="Answer first, then justify.")

    assert second["parent_version"] == first["version"]
    assert second["version"] != first["version"]


def test_one_off_facts_are_stripped_from_candidates(skill_workspace: Path):
    result = reg.create_candidate(
        skill_name="leaky",
        description="Ping the deploy box",
        instructions=(
            "Email ops@example.com and open https://internal.example.com/runbook on 2024-03-11.\n"
            "Ticket 1234567 was filed by @alice with token sk-abcdef0123456789abcd.\n"
            "The log is at /home/alice/logs/deploy.log.\n"
            "Also check /etc/shadow, /opt/deploy/run.sh, C:\\Users\\alice\\secret.txt and \\\\nas\\share\\runbook on 11/03/2024.\n"
        ),
        source_provenance={"kind": "user_feedback"},
    )
    assert result["ok"], result

    body = Path(result["file"]).read_text(encoding="utf-8")
    for leak in (
        "ops@example.com",
        "internal.example.com",
        "2024-03-11",
        "1234567",
        "sk-abcdef0123456789abcd",
        "/home/alice/logs",
        "@alice",
        "/etc/shadow",
        "/opt/deploy/run.sh",
        r"C:\Users\alice\secret.txt",
        r"\\nas\share\runbook",
        "11/03/2024",
    ):
        assert leak not in body, leak
    assert "<redacted:" in body
    assert result["redactions"]


# ------------------------------------------------------------- P1.2 state machine


def test_illegal_transitions_are_refused(skill_workspace: Path):
    result = _candidate(skill_workspace)
    outcome = reg.transition("answer-style", result["version"], reg.STATUS_RETIRED, reason="nope")

    assert not outcome["ok"]
    assert "illegal transition" in outcome["error"]


def test_every_transition_is_recorded_with_provenance(skill_workspace: Path):
    result = _candidate(skill_workspace)
    version = result["version"]
    reg.transition("answer-style", version, reg.STATUS_SHADOW, reason="eval started", actor="evaluator")
    reg.mark_champion("answer-style", version, reason="gate passed", actor="gate")
    reg.promote("answer-style", version, actor="user")

    history = reg.load_registry()["skills"]["answer-style"]["versions"][version]["history"]
    assert [item["to"] for item in history] == [
        reg.STATUS_CANDIDATE,
        reg.STATUS_SHADOW,
        reg.STATUS_CHAMPION,
        reg.STATUS_ACTIVE,
    ]
    assert [item["from"] for item in history] == ["", reg.STATUS_CANDIDATE, reg.STATUS_SHADOW, reg.STATUS_CHAMPION]
    assert all(item["time"] and item["actor"] for item in history)


def test_promote_requires_a_gated_champion(skill_workspace: Path):
    result = _candidate(skill_workspace)
    outcome = reg.promote("answer-style", result["version"])

    assert not outcome["ok"]
    assert "champion" in outcome["error"]
    assert not reg.active_skill_file("answer-style").exists()


def test_forced_promote_is_allowed_and_marked(skill_workspace: Path):
    result = _candidate(skill_workspace)
    outcome = reg.promote("answer-style", result["version"], force=True)

    assert outcome["ok"], outcome
    assert outcome["forced"] is True
    entry = reg.load_registry()["skills"]["answer-style"]["versions"][result["version"]]
    assert "forced" in entry["history"][-1]["reason"]


# ---------------------------------------------------- P1.2 promote / rollback IO


def test_promote_snapshots_the_previous_active_version(skill_workspace: Path):
    first = _candidate(skill_workspace, instructions="Version one guidance.")
    reg.mark_champion("answer-style", first["version"], reason="test")
    reg.promote("answer-style", first["version"])

    second = _candidate(skill_workspace, instructions="Version two guidance.")
    reg.mark_champion("answer-style", second["version"], reason="test")
    outcome = reg.promote("answer-style", second["version"])

    assert outcome["ok"], outcome
    assert "Version two guidance." in reg.active_skill_file("answer-style").read_text(encoding="utf-8")
    snapshots = reg.list_snapshots("answer-style")
    assert [item["version"] for item in snapshots] == [first["version"]]
    assert "Version one guidance." in (Path(snapshots[0]["dir"]) / "SKILL.md").read_text(encoding="utf-8")


def test_rollback_restores_the_last_healthy_version(skill_workspace: Path):
    first = _candidate(skill_workspace, instructions="Good guidance.")
    reg.mark_champion("answer-style", first["version"], reason="test")
    reg.promote("answer-style", first["version"])
    second = _candidate(skill_workspace, instructions="Regressive guidance.")
    reg.mark_champion("answer-style", second["version"], reason="test")
    reg.promote("answer-style", second["version"])

    outcome = reg.rollback("answer-style")

    assert outcome["ok"], outcome
    assert outcome["version"] == first["version"]
    assert "Good guidance." in reg.active_skill_file("answer-style").read_text(encoding="utf-8")
    node = reg.load_registry()["skills"]["answer-style"]
    assert node["active_version"] == first["version"]
    assert node["versions"][second["version"]]["status"] == reg.STATUS_RETIRED


def test_rollback_to_an_explicit_version(skill_workspace: Path):
    versions = []
    for text in ("v1 guidance.", "v2 guidance.", "v3 guidance."):
        created = _candidate(skill_workspace, instructions=text)
        reg.mark_champion("answer-style", created["version"], reason="test")
        reg.promote("answer-style", created["version"])
        versions.append(created["version"])

    outcome = reg.rollback("answer-style", versions[0])

    assert outcome["ok"], outcome
    assert "v1 guidance." in reg.active_skill_file("answer-style").read_text(encoding="utf-8")


def test_rollback_without_any_snapshot_fails_cleanly(skill_workspace: Path):
    _candidate(skill_workspace)
    outcome = reg.rollback("answer-style")

    assert not outcome["ok"]
    assert "no snapshots" in outcome["error"]


def test_interrupted_activation_leaves_the_active_skill_intact(skill_workspace: Path, monkeypatch):
    first = _candidate(skill_workspace, instructions="Stable guidance.")
    reg.mark_champion("answer-style", first["version"], reason="test")
    reg.promote("answer-style", first["version"])
    active = reg.active_skill_file("answer-style")
    before = active.read_text(encoding="utf-8")

    second = _candidate(skill_workspace, instructions="Half written guidance.")
    reg.mark_champion("answer-style", second["version"], reason="test")

    real_replace = reg.os.replace

    def _explode(src, dst):
        if str(dst) == str(active):
            raise OSError("simulated crash mid-activation")
        return real_replace(src, dst)

    monkeypatch.setattr(reg.os, "replace", _explode)
    with pytest.raises(OSError):
        reg.promote("answer-style", second["version"])
    monkeypatch.setattr(reg.os, "replace", real_replace)

    assert active.read_text(encoding="utf-8") == before
    assert "Half written" not in active.read_text(encoding="utf-8")
    assert not [item for item in active.parent.iterdir() if item.name.endswith(".tmp")]
    assert reg.load_registry()["skills"]["answer-style"]["active_version"] == first["version"]


def test_registry_active_and_snapshots_stay_consistent(skill_workspace: Path):
    for text in ("v1 guidance.", "v2 guidance."):
        created = _candidate(skill_workspace, instructions=text)
        reg.mark_champion("answer-style", created["version"], reason="test")
        reg.promote("answer-style", created["version"])
    reg.rollback("answer-style")

    check = reg.verify_consistency()
    assert check["ok"], check["problems"]


def test_consistency_check_reports_a_tampered_active_file(skill_workspace: Path):
    created = _candidate(skill_workspace)
    reg.mark_champion("answer-style", created["version"], reason="test")
    reg.promote("answer-style", created["version"])
    reg.active_skill_file("answer-style").write_text("tampered", encoding="utf-8")

    check = reg.verify_consistency()
    assert not check["ok"]
    assert any("hash differs" in problem for problem in check["problems"])


def test_only_one_version_can_be_active(skill_workspace: Path):
    for text in ("v1 guidance.", "v2 guidance."):
        created = _candidate(skill_workspace, instructions=text)
        reg.mark_champion("answer-style", created["version"], reason="test")
        reg.promote("answer-style", created["version"])

    node = reg.load_registry()["skills"]["answer-style"]
    actives = [item for item in node["versions"].values() if item["status"] == reg.STATUS_ACTIVE]
    assert len(actives) == 1


def test_promoted_skill_becomes_discoverable(skill_workspace: Path):
    from agents.skills import discover_skills

    created = _candidate(skill_workspace)
    reg.mark_champion("answer-style", created["version"], reason="test")
    reg.promote("answer-style", created["version"])

    assert "answer-style" in {skill.name for skill in discover_skills()}


def test_status_report_lists_lifecycle_state(skill_workspace: Path):
    created = _candidate(skill_workspace)
    reg.mark_champion("answer-style", created["version"], reason="test")

    report = reg.skill_status_report()
    assert "answer-style" in report
    assert reg.STATUS_CHAMPION in report


def test_promote_loads_the_requested_version_not_the_current_pointer(skill_workspace: Path):
    first = _candidate(skill_workspace, instructions="Version one guidance.")
    reg.mark_champion("answer-style", first["version"], reason="test")
    second = _candidate(skill_workspace, instructions="Version two guidance.")
    pointer = reg.champion_dir("answer-style") / "SKILL.md"
    pointer.write_text(
        (reg.candidate_dir("answer-style", second["version"]) / "SKILL.md").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    outcome = reg.promote("answer-style", first["version"])

    assert outcome["ok"], outcome
    active = reg.active_skill_file("answer-style").read_text(encoding="utf-8")
    parsed = reg.parse_skill_markdown(active)
    assert parsed["version"] == first["version"]
    assert "Version one guidance." in active
    assert "Version two guidance." not in active


def test_new_champion_retires_the_previous_champion(skill_workspace: Path):
    first = _candidate(skill_workspace, instructions="First champion.")
    reg.mark_champion("answer-style", first["version"], reason="first")
    second = _candidate(skill_workspace, instructions="Second champion.")
    reg.mark_champion("answer-style", second["version"], reason="second")

    node = reg.load_registry()["skills"]["answer-style"]
    assert node["champion_version"] == second["version"]
    assert node["versions"][first["version"]]["status"] == reg.STATUS_RETIRED
    assert node["versions"][second["version"]]["status"] == reg.STATUS_CHAMPION
    assert "Second champion." in (reg.champion_dir("answer-style") / "SKILL.md").read_text(encoding="utf-8")
    assert "Second champion." in reg.champion_version_file("answer-style", second["version"]).read_text(
        encoding="utf-8"
    )


def test_rejected_force_promote_does_not_write_active(skill_workspace: Path):
    created = _candidate(skill_workspace, instructions="Rejected guidance.")
    rejected = reg.transition("answer-style", created["version"], reg.STATUS_REJECTED, reason="gate")
    assert rejected["ok"], rejected

    outcome = reg.promote("answer-style", created["version"], force=True)

    assert not outcome["ok"]
    assert "rejected" in outcome["error"]
    assert "--force" in outcome["error"] or "even with" in outcome["error"]
    assert not reg.active_skill_file("answer-style").exists()
    assert reg.load_registry()["skills"]["answer-style"]["active_version"] == ""
    assert reg.verify_consistency()["ok"], reg.verify_consistency()["problems"]


def test_consistency_reports_active_file_without_registry_record(skill_workspace: Path):
    path = reg.active_skill_file("orphan")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\nname: orphan\nversion: 0.1.0\n---\n\nbody\n", encoding="utf-8")

    check = reg.verify_consistency()
    assert not check["ok"]
    assert any("no registry record" in problem for problem in check["problems"])


def test_consistency_reports_champion_pointer_version_mismatch(skill_workspace: Path):
    first = _candidate(skill_workspace, instructions="Champion one.")
    reg.mark_champion("answer-style", first["version"], reason="test")
    second = _candidate(skill_workspace, instructions="Champion two.")
    (reg.champion_dir("answer-style") / "SKILL.md").write_text(
        (reg.candidate_dir("answer-style", second["version"]) / "SKILL.md").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    check = reg.verify_consistency()
    assert not check["ok"]
    assert any("frontmatter version" in problem for problem in check["problems"])
