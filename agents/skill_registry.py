"""Candidate / Champion / Active isolation and the skill lifecycle state machine.

User feedback may only ever produce a *candidate*. Candidates live under
`.bear/skill-evolution/candidates/`, gated winners are copied to `champions/`,
and only an explicit promote writes the file the agent actually loads.
Every activation snapshots the previous active file first and replaces the
target through `os.replace`, so an interrupted write can never leave a
half-written `SKILL.md` behind.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .frontmatter import format_frontmatter, parse_frontmatter
from .skill_evolution import _safe_skill_slug, _utc_now, get_evolution_dir

REGISTRY_FILENAME = "registry.json"
REGISTRY_VERSION = 1

STATUS_CANDIDATE = "candidate"
STATUS_SHADOW = "shadow"
STATUS_CHAMPION = "champion"
STATUS_ACTIVE = "active"
STATUS_RETIRED = "retired"
STATUS_REJECTED = "rejected"

LIFECYCLE_STATES = (
    STATUS_CANDIDATE,
    STATUS_SHADOW,
    STATUS_CHAMPION,
    STATUS_ACTIVE,
    STATUS_RETIRED,
    STATUS_REJECTED,
)

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    STATUS_CANDIDATE: {STATUS_SHADOW, STATUS_REJECTED, STATUS_ACTIVE},
    STATUS_SHADOW: {STATUS_CHAMPION, STATUS_REJECTED, STATUS_CANDIDATE, STATUS_ACTIVE},
    STATUS_CHAMPION: {STATUS_ACTIVE, STATUS_RETIRED, STATUS_REJECTED},
    STATUS_ACTIVE: {STATUS_RETIRED, STATUS_REJECTED},
    STATUS_RETIRED: {STATUS_ACTIVE, STATUS_REJECTED},
    STATUS_REJECTED: {STATUS_CANDIDATE},
}

# `candidate -> active` and `shadow -> active` skip the gate and are only reachable
# through an explicit forced promote, which is recorded in the version history.
FORCED_TRANSITIONS = {(STATUS_CANDIDATE, STATUS_ACTIVE), (STATUS_SHADOW, STATUS_ACTIVE)}

_RE_URL = re.compile(r"\b(?:https?://|www\.)\S+")
_RE_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_RE_ISO_DATE = re.compile(r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:[T ]\d{2}:\d{2}(?::\d{2})?Z?)?\b")
_RE_CN_DATE = re.compile(r"\b\d{4}\s*年\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?")
_RE_SECRET = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{12,}|[A-Za-z0-9_-]{32,})\b"
)
_RE_ABS_PATH = re.compile(r"(?<![\w.])(?:/(?:home|Users|root|var|tmp|data)/[^\s,;:'\"`)]+)")
_RE_LONG_DIGITS = re.compile(r"\b\d{7,}\b")
_RE_AT_HANDLE = re.compile(r"(?<![\w@])@[A-Za-z0-9_]{3,}\b")

_SANITIZE_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("email", _RE_EMAIL, "<redacted:email>"),
    ("secret", _RE_SECRET, "<redacted:secret>"),
    ("url", _RE_URL, "<redacted:url>"),
    ("abs_path", _RE_ABS_PATH, "<redacted:path>"),
    ("date", _RE_ISO_DATE, "<redacted:date>"),
    ("date", _RE_CN_DATE, "<redacted:date>"),
    ("account", _RE_AT_HANDLE, "<redacted:account>"),
    ("identifier", _RE_LONG_DIGITS, "<redacted:number>"),
)


class SkillRegistryError(RuntimeError):
    """Raised for invalid lifecycle operations."""


def sanitize_skill_text(text: str) -> tuple[str, list[str]]:
    """Strip one-off facts (URLs, accounts, secrets, exact dates) from skill content."""
    out = str(text or "")
    hits: list[str] = []
    for label, pattern, replacement in _SANITIZE_RULES:
        out, count = pattern.subn(replacement, out)
        if count:
            hits.append(f"{label}x{count}")
    return out, hits


def sanitize_skill_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    cleaned = dict(payload)
    hits: list[str] = []
    for key in ("description", "when_to_use", "instructions", "evidence"):
        value = cleaned.get(key)
        if not value:
            continue
        new_value, found = sanitize_skill_text(str(value))
        cleaned[key] = new_value
        hits.extend(found)
    tags = cleaned.get("tags")
    if isinstance(tags, list):
        cleaned_tags = []
        for tag in tags:
            new_tag, found = sanitize_skill_text(str(tag))
            hits.extend(found)
            cleaned_tags.append(new_tag)
        cleaned["tags"] = cleaned_tags
    return cleaned, hits


# --------------------------------------------------------------------------- paths


def registry_root() -> Path:
    return get_evolution_dir()


def registry_path() -> Path:
    return registry_root() / REGISTRY_FILENAME


def candidates_root() -> Path:
    return registry_root() / "candidates"


def champions_root() -> Path:
    return registry_root() / "champions"


def snapshots_root() -> Path:
    return registry_root() / "snapshots"


def evaluations_root() -> Path:
    return registry_root() / "evaluations"


def active_skills_root() -> Path:
    return Path.cwd() / ".bear" / "skills"


def candidate_dir(skill_name: str, version: str) -> Path:
    return candidates_root() / _safe_skill_slug(skill_name) / _safe_version(version)


def champion_dir(skill_name: str) -> Path:
    return champions_root() / _safe_skill_slug(skill_name)


def active_skill_file(skill_name: str) -> Path:
    return active_skills_root() / _safe_skill_slug(skill_name) / "SKILL.md"


def _safe_version(version: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.\-]+", "-", str(version or "").strip()).strip("-.")
    return slug[:40] or "0.0.0"


# ------------------------------------------------------------------- atomic writes


def atomic_write_text(path: Path, text: str) -> None:
    """Write via a sibling temp file + os.replace so readers never see a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


# ---------------------------------------------------------------------- rendering


@dataclass
class SkillContent:
    name: str
    description: str = ""
    when_to_use: str = ""
    instructions: str = ""
    tags: list[str] = field(default_factory=list)
    context: str = "inline"
    user_invocable: bool = False
    allowed_tools: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "when_to_use": self.when_to_use,
            "instructions": self.instructions,
            "tags": list(self.tags),
            "context": self.context,
            "user_invocable": self.user_invocable,
            "allowed_tools": self.allowed_tools,
        }


def render_skill_markdown(content: SkillContent, *, version: str, created_at: str) -> str:
    meta: dict[str, Any] = {
        "name": content.name,
        "description": re.sub(r"\s+", " ", str(content.description or "").strip()),
        "version": version,
        "created-at": created_at,
        "user-invocable": "true" if content.user_invocable else "false",
        "context": "fork" if str(content.context).strip().lower() == "fork" else "inline",
    }
    if content.when_to_use:
        meta["when-to-use"] = re.sub(r"\s+", " ", str(content.when_to_use).strip())
    tags = [re.sub(r"\s+", "-", str(tag).strip()) for tag in content.tags if str(tag).strip()]
    if tags:
        meta["tags"] = ",".join(tags)
    if content.allowed_tools:
        meta["allowed-tools"] = content.allowed_tools

    body = str(content.instructions or "").strip()
    if not body:
        body = "# Skill Instructions\n\nApply this reusable skill when the trigger matches.\n"
    if not body.lstrip().startswith("#"):
        body = "# Skill Instructions\n\n" + body
    return format_frontmatter(meta, body.rstrip() + "\n")


def parse_skill_markdown(text: str) -> dict[str, Any]:
    parsed = parse_frontmatter(str(text or ""))
    meta, body = parsed.meta, parsed.body
    tags_raw = str(meta.get("tags") or "")
    return {
        "name": str(meta.get("name") or "").strip(),
        "description": str(meta.get("description") or "").strip(),
        "when_to_use": str(meta.get("when-to-use") or "").strip(),
        "instructions": body.strip(),
        "tags": [tag.strip() for tag in tags_raw.split(",") if tag.strip()],
        "version": str(meta.get("version") or "").strip(),
        "context": str(meta.get("context") or "inline").strip(),
        "user_invocable": str(meta.get("user-invocable") or "false").strip().lower() == "true",
        "allowed_tools": str(meta.get("allowed-tools") or "").strip(),
    }


def content_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def bump_version(version: str | None) -> str:
    parts = str(version or "").strip().split(".")
    numbers = []
    for part in parts[:3]:
        try:
            numbers.append(int(part))
        except ValueError:
            numbers.append(0)
    while len(numbers) < 3:
        numbers.append(0)
    if not str(version or "").strip():
        return "0.1.0"
    numbers[2] += 1
    return ".".join(str(number) for number in numbers)


# ----------------------------------------------------------------------- registry


def load_registry() -> dict[str, Any]:
    data = _read_json(registry_path(), None)
    if not isinstance(data, dict) or not isinstance(data.get("skills"), dict):
        return {"version": REGISTRY_VERSION, "skills": {}}
    data.setdefault("version", REGISTRY_VERSION)
    return data


def save_registry(registry: dict[str, Any]) -> None:
    _atomic_write_json(registry_path(), registry)


def _skill_node(registry: dict[str, Any], skill_name: str) -> dict[str, Any]:
    skills = registry.setdefault("skills", {})
    node = skills.get(skill_name)
    if not isinstance(node, dict):
        node = {
            "skill_name": skill_name,
            "slug": _safe_skill_slug(skill_name),
            "active_version": "",
            "champion_version": "",
            "versions": {},
        }
        skills[skill_name] = node
    node.setdefault("versions", {})
    return node


def _version_entry(registry: dict[str, Any], skill_name: str, version: str) -> dict[str, Any] | None:
    node = registry.get("skills", {}).get(skill_name)
    if not isinstance(node, dict):
        return None
    entry = node.get("versions", {}).get(version)
    return entry if isinstance(entry, dict) else None


def _record_history(entry: dict[str, Any], *, from_status: str, to_status: str, reason: str, actor: str) -> None:
    history = entry.setdefault("history", [])
    history.append(
        {
            "time": _utc_now(),
            "from": from_status,
            "to": to_status,
            "reason": str(reason or "")[:400],
            "actor": str(actor or "system")[:80],
        }
    )


# ---------------------------------------------------------------------- candidates


def create_candidate(
    *,
    skill_name: str,
    description: str,
    instructions: str,
    when_to_use: str = "",
    tags: list[str] | None = None,
    source_provenance: dict[str, Any] | None = None,
    actor: str = "online-extractor",
    origin: str = "add",
    context: str = "inline",
    user_invocable: bool = False,
    allowed_tools: str = "",
) -> dict[str, Any]:
    """Create an isolated candidate version. Never touches the active skill."""
    name = str(skill_name or "").strip()
    if not name:
        return {"ok": False, "error": "skill name is required"}
    if not str(description or "").strip():
        return {"ok": False, "error": "description is required"}

    payload, redactions = sanitize_skill_payload(
        {
            "description": description,
            "when_to_use": when_to_use,
            "instructions": instructions,
            "tags": list(tags or []),
        }
    )
    content = SkillContent(
        name=name,
        description=payload["description"],
        when_to_use=payload["when_to_use"],
        instructions=payload["instructions"],
        tags=list(payload.get("tags") or []),
        context=context,
        user_invocable=user_invocable,
        allowed_tools=allowed_tools,
    )

    registry = load_registry()
    node = _skill_node(registry, name)
    parent_version = str(node.get("champion_version") or node.get("active_version") or "").strip()
    version = bump_version(parent_version)
    while version in node.get("versions", {}) or candidate_dir(name, version).exists():
        version = bump_version(version)

    created_at = _utc_now()
    markdown = render_skill_markdown(content, version=version, created_at=created_at)
    digest = content_hash(markdown)

    target_dir = candidate_dir(name, version)
    target_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target_dir / "SKILL.md", markdown)

    provenance = dict(source_provenance or {})
    note = provenance.get("note")
    if note:
        provenance["note"], note_hits = sanitize_skill_text(str(note))
        redactions.extend(note_hits)

    entry = {
        "skill_name": name,
        "version": version,
        "parent_version": parent_version,
        "created_at": created_at,
        "source_provenance": provenance,
        "content_hash": digest,
        "status": STATUS_CANDIDATE,
        "origin": origin,
        "redactions": redactions,
        "path": str(target_dir / "SKILL.md"),
        "history": [],
    }
    _record_history(entry, from_status="", to_status=STATUS_CANDIDATE, reason=origin, actor=actor)
    node["versions"][version] = entry
    _atomic_write_json(target_dir / "meta.json", entry)
    save_registry(registry)

    return {
        "ok": True,
        "skill": name,
        "version": version,
        "parent_version": parent_version,
        "status": STATUS_CANDIDATE,
        "file": str(target_dir / "SKILL.md"),
        "content_hash": digest,
        "redactions": redactions,
    }


def read_candidate(skill_name: str, version: str) -> dict[str, Any] | None:
    path = candidate_dir(skill_name, version) / "SKILL.md"
    if not path.is_file():
        return None
    parsed = parse_skill_markdown(path.read_text(encoding="utf-8"))
    parsed["version"] = parsed.get("version") or version
    parsed["path"] = str(path)
    return parsed


def read_champion(skill_name: str) -> dict[str, Any] | None:
    path = champion_dir(skill_name) / "SKILL.md"
    if not path.is_file():
        return None
    parsed = parse_skill_markdown(path.read_text(encoding="utf-8"))
    parsed["path"] = str(path)
    return parsed


def read_active(skill_name: str) -> dict[str, Any] | None:
    path = active_skill_file(skill_name)
    if not path.is_file():
        return None
    parsed = parse_skill_markdown(path.read_text(encoding="utf-8"))
    parsed["path"] = str(path)
    return parsed


def latest_candidate(skill_name: str) -> dict[str, Any] | None:
    registry = load_registry()
    node = registry.get("skills", {}).get(skill_name)
    if not isinstance(node, dict):
        return None
    entries = [
        entry
        for entry in node.get("versions", {}).values()
        if isinstance(entry, dict) and entry.get("status") in {STATUS_CANDIDATE, STATUS_SHADOW}
    ]
    if not entries:
        return None
    entries.sort(key=lambda item: str(item.get("created_at") or ""))
    return entries[-1]


def list_skill_names() -> list[str]:
    return sorted(load_registry().get("skills", {}).keys())


# ------------------------------------------------------------------- transitions


def transition(
    skill_name: str,
    version: str,
    to_status: str,
    *,
    reason: str = "",
    actor: str = "system",
    force: bool = False,
) -> dict[str, Any]:
    if to_status not in LIFECYCLE_STATES:
        return {"ok": False, "error": f"unknown lifecycle state: {to_status}"}
    registry = load_registry()
    entry = _version_entry(registry, skill_name, version)
    if entry is None:
        return {"ok": False, "error": f"unknown version: {skill_name}@{version}"}

    current = str(entry.get("status") or STATUS_CANDIDATE)
    if current == to_status:
        return {"ok": True, "skill": skill_name, "version": version, "status": to_status, "noop": True}
    if to_status not in ALLOWED_TRANSITIONS.get(current, set()):
        return {"ok": False, "error": f"illegal transition {current} -> {to_status}"}
    if (current, to_status) in FORCED_TRANSITIONS and not force:
        return {
            "ok": False,
            "error": f"transition {current} -> {to_status} requires an explicit forced promote",
        }

    entry["status"] = to_status
    entry["updated_at"] = _utc_now()
    _record_history(entry, from_status=current, to_status=to_status, reason=reason, actor=actor)

    node = _skill_node(registry, skill_name)
    if to_status == STATUS_CHAMPION:
        node["champion_version"] = version
    save_registry(registry)
    _sync_meta(skill_name, version, entry)
    return {"ok": True, "skill": skill_name, "version": version, "status": to_status, "from": current}


def _sync_meta(skill_name: str, version: str, entry: dict[str, Any]) -> None:
    target = candidate_dir(skill_name, version) / "meta.json"
    if target.parent.is_dir():
        _atomic_write_json(target, entry)


def mark_champion(skill_name: str, version: str, *, reason: str, actor: str = "gate") -> dict[str, Any]:
    """Copy a gated winner into champions/ and record the state change."""
    source = candidate_dir(skill_name, version) / "SKILL.md"
    if not source.is_file():
        return {"ok": False, "error": f"candidate file missing: {source}"}
    entry = _version_entry(load_registry(), skill_name, version)
    if entry is not None and str(entry.get("status") or "") == STATUS_CANDIDATE:
        # A gate decision implies the version was evaluated, so record the shadow leg.
        transition(skill_name, version, STATUS_SHADOW, reason=reason, actor=actor)
    result = transition(skill_name, version, STATUS_CHAMPION, reason=reason, actor=actor)
    if not result.get("ok"):
        return result
    target_dir = champion_dir(skill_name)
    target_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target_dir / "SKILL.md", source.read_text(encoding="utf-8"))
    _atomic_write_json(
        target_dir / "meta.json",
        {"skill_name": skill_name, "version": version, "promoted_at": _utc_now(), "reason": reason},
    )
    return {**result, "champion_file": str(target_dir / "SKILL.md")}


# ------------------------------------------------------------- promote / rollback


def _snapshot_active(skill_name: str, *, reason: str) -> dict[str, Any] | None:
    path = active_skill_file(skill_name)
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    parsed = parse_skill_markdown(text)
    version = parsed.get("version") or "unknown"
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    target_dir = snapshots_root() / _safe_skill_slug(skill_name) / f"{stamp}-{_safe_version(version)}"
    suffix = 1
    while target_dir.exists():
        suffix += 1
        target_dir = target_dir.with_name(f"{target_dir.name}-{suffix}")
    target_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target_dir / "SKILL.md", text)
    meta = {
        "skill_name": skill_name,
        "version": version,
        "captured_at": _utc_now(),
        "reason": reason,
        "content_hash": content_hash(text),
        "source_path": str(path),
    }
    _atomic_write_json(target_dir / "meta.json", meta)
    return {**meta, "dir": str(target_dir)}


def list_snapshots(skill_name: str) -> list[dict[str, Any]]:
    base = snapshots_root() / _safe_skill_slug(skill_name)
    if not base.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for entry in sorted(base.iterdir()):
        if not entry.is_dir() or not (entry / "SKILL.md").is_file():
            continue
        meta = _read_json(entry / "meta.json", {})
        out.append({**meta, "dir": str(entry), "name": entry.name})
    return out


def promote(
    skill_name: str,
    version: str,
    *,
    actor: str = "user",
    reason: str = "manual promote",
    force: bool = False,
) -> dict[str, Any]:
    """Atomically activate a version, snapshotting whatever was active before."""
    registry = load_registry()
    entry = _version_entry(registry, skill_name, version)
    if entry is None:
        return {"ok": False, "error": f"unknown version: {skill_name}@{version}"}
    status = str(entry.get("status") or "")
    if status == STATUS_ACTIVE:
        return {"ok": False, "error": f"{skill_name}@{version} is already active"}
    if status != STATUS_CHAMPION and not force:
        return {
            "ok": False,
            "error": (
                f"{skill_name}@{version} is '{status}'; only a gated champion can be promoted. "
                "Run /skill-eval first, or pass --force to override explicitly."
            ),
        }

    source = champion_dir(skill_name) / "SKILL.md"
    if not source.is_file():
        source = candidate_dir(skill_name, version) / "SKILL.md"
    if not source.is_file():
        return {"ok": False, "error": f"no skill file found for {skill_name}@{version}"}
    payload = source.read_text(encoding="utf-8")

    snapshot = _snapshot_active(skill_name, reason=f"pre-promote {version}")
    target = active_skill_file(skill_name)
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target, payload)

    return _finalize_activation(
        skill_name,
        version,
        actor=actor,
        reason=reason if not force else f"{reason} (forced)",
        force=force,
        snapshot=snapshot,
        target=target,
        payload=payload,
    )


def _finalize_activation(
    skill_name: str,
    version: str,
    *,
    actor: str,
    reason: str,
    force: bool,
    snapshot: dict[str, Any] | None,
    target: Path,
    payload: str,
) -> dict[str, Any]:
    registry = load_registry()
    node = _skill_node(registry, skill_name)
    previous = str(node.get("active_version") or "")
    if previous and previous != version:
        previous_entry = node.get("versions", {}).get(previous)
        if isinstance(previous_entry, dict) and previous_entry.get("status") == STATUS_ACTIVE:
            previous_entry["status"] = STATUS_RETIRED
            previous_entry["updated_at"] = _utc_now()
            _record_history(
                previous_entry,
                from_status=STATUS_ACTIVE,
                to_status=STATUS_RETIRED,
                reason=f"replaced by {version}",
                actor=actor,
            )

    entry = node.setdefault("versions", {}).setdefault(
        version,
        {
            "skill_name": skill_name,
            "version": version,
            "parent_version": previous,
            "created_at": _utc_now(),
            "source_provenance": {"kind": "restored_snapshot"},
            "content_hash": content_hash(payload),
            "status": STATUS_RETIRED,
            "history": [],
        },
    )
    current_status = str(entry.get("status") or STATUS_CANDIDATE)
    if current_status != STATUS_ACTIVE:
        if (
            STATUS_ACTIVE not in ALLOWED_TRANSITIONS.get(current_status, set())
            or ((current_status, STATUS_ACTIVE) in FORCED_TRANSITIONS and not force)
        ):
            return {"ok": False, "error": f"illegal transition {current_status} -> {STATUS_ACTIVE}"}
        entry["status"] = STATUS_ACTIVE
        entry["updated_at"] = _utc_now()
        _record_history(
            entry, from_status=current_status, to_status=STATUS_ACTIVE, reason=reason, actor=actor
        )
    entry["content_hash"] = content_hash(payload)
    entry["active_path"] = str(target)
    node["active_version"] = version
    node["active_path"] = str(target)
    save_registry(registry)
    _sync_meta(skill_name, version, entry)

    from .skills import reset_skill_cache

    reset_skill_cache()
    return {
        "ok": True,
        "skill": skill_name,
        "version": version,
        "status": STATUS_ACTIVE,
        "file": str(target),
        "previous_version": previous,
        "snapshot": (snapshot or {}).get("dir", ""),
        "forced": bool(force),
    }


def rollback(
    skill_name: str,
    version: str = "",
    *,
    actor: str = "user",
    reason: str = "manual rollback",
) -> dict[str, Any]:
    """Restore a snapshot: the named version, or the newest healthy earlier one."""
    snapshots = list_snapshots(skill_name)
    if not snapshots:
        return {"ok": False, "error": f"no snapshots recorded for {skill_name}"}

    registry = load_registry()
    node = registry.get("skills", {}).get(skill_name, {})
    current = str(node.get("active_version") or "")

    chosen: dict[str, Any] | None = None
    if version:
        for snapshot in reversed(snapshots):
            if str(snapshot.get("version") or "") == version:
                chosen = snapshot
                break
        if chosen is None:
            return {"ok": False, "error": f"no snapshot for {skill_name}@{version}"}
    else:
        for snapshot in reversed(snapshots):
            snapshot_version = str(snapshot.get("version") or "")
            if snapshot_version and snapshot_version != current:
                chosen = snapshot
                break
        if chosen is None:
            return {"ok": False, "error": f"no earlier healthy version to roll back to for {skill_name}"}

    payload = (Path(chosen["dir"]) / "SKILL.md").read_text(encoding="utf-8")
    restored_version = str(chosen.get("version") or "") or "0.0.0"

    pre = _snapshot_active(skill_name, reason=f"pre-rollback to {restored_version}")
    target = active_skill_file(skill_name)
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target, payload)

    result = _finalize_activation(
        skill_name,
        restored_version,
        actor=actor,
        reason=f"{reason} from snapshot {chosen.get('name', '')}",
        force=True,
        snapshot=pre,
        target=target,
        payload=payload,
    )
    return {**result, "restored_from": chosen.get("dir", "")}


# ------------------------------------------------------------------- consistency


def verify_consistency() -> dict[str, Any]:
    """Check that registry, active files and champion copies agree."""
    registry = load_registry()
    problems: list[str] = []
    for skill_name, node in registry.get("skills", {}).items():
        if not isinstance(node, dict):
            problems.append(f"{skill_name}: malformed registry node")
            continue
        active_version = str(node.get("active_version") or "")
        path = active_skill_file(skill_name)
        if active_version:
            if not path.is_file():
                problems.append(f"{skill_name}: active version {active_version} has no file at {path}")
            else:
                entry = node.get("versions", {}).get(active_version)
                digest = content_hash(path.read_text(encoding="utf-8"))
                if isinstance(entry, dict) and entry.get("content_hash") not in {None, "", digest}:
                    problems.append(f"{skill_name}: active file hash differs from registry record")
                if isinstance(entry, dict) and entry.get("status") != STATUS_ACTIVE:
                    problems.append(
                        f"{skill_name}: active_version {active_version} has status {entry.get('status')}"
                    )
        actives = [
            version
            for version, entry in node.get("versions", {}).items()
            if isinstance(entry, dict) and entry.get("status") == STATUS_ACTIVE
        ]
        if len(actives) > 1:
            problems.append(f"{skill_name}: {len(actives)} versions marked active")
        champion_version = str(node.get("champion_version") or "")
        if champion_version and not (champion_dir(skill_name) / "SKILL.md").is_file():
            problems.append(f"{skill_name}: champion {champion_version} has no stored file")
    return {"ok": not problems, "problems": problems}


# ---------------------------------------------------------------------- reporting


def skill_status_report() -> str:
    registry = load_registry()
    skills = registry.get("skills", {})
    if not skills:
        return "No governed skills yet. Candidates appear here after online extraction."

    lines = ["Skill lifecycle status", ""]
    for skill_name in sorted(skills):
        node = skills[skill_name]
        active = str(node.get("active_version") or "-")
        champion = str(node.get("champion_version") or "-")
        lines.append(f"- {skill_name}  active={active}  champion={champion}")
        versions = node.get("versions", {})
        for version in sorted(versions, key=lambda item: str(versions[item].get("created_at") or "")):
            entry = versions[version]
            if not isinstance(entry, dict):
                continue
            provenance = entry.get("source_provenance") or {}
            kind = str(provenance.get("kind") or provenance.get("source") or "unknown")
            lines.append(
                f"    {version:<10} {str(entry.get('status') or ''):<10} "
                f"parent={str(entry.get('parent_version') or '-'):<8} "
                f"created={entry.get('created_at', '')} src={kind}"
            )
    check = verify_consistency()
    if not check["ok"]:
        lines.append("")
        lines.append("Consistency problems:")
        lines.extend(f"  ! {problem}" for problem in check["problems"])
    return "\n".join(lines)
