#!/usr/bin/env python3
"""Project-scoped session persistence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .runtime_config import normalize_workspace, workspace_id


def get_session_root(workspace: str | Path | None = None) -> Path:
    """Sessions are isolated by the normalized workspace path."""
    project = workspace_id(workspace)
    root = Path.home() / ".bear-code" / "projects" / project / "sessions"
    root.mkdir(parents=True, exist_ok=True)
    return root


def get_project_session_dir(workspace: str | Path | None = None) -> Path:
    d = normalize_workspace(workspace) / ".bear" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_session(session_id: str, data: dict[str, Any], *, workspace: str | Path | None = None) -> None:
    root = get_session_root(workspace)
    payload = dict(data)
    metadata = dict(payload.get("metadata") or {})
    metadata["workspace"] = str(normalize_workspace(workspace))
    metadata["workspaceId"] = workspace_id(workspace)
    payload["metadata"] = metadata
    (root / f"{session_id}.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def save_folded_session_memory(
    session_id: str,
    record: dict[str, Any],
    *,
    workspace: str | Path | None = None,
) -> None:
    d = get_project_session_dir(workspace)
    line = json.dumps(record, ensure_ascii=False, default=str)
    with (d / f"{session_id}.folded-memory.jsonl").open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    (d / f"{session_id}.folded-memory.latest.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


def load_session(session_id: str, *, workspace: str | Path | None = None) -> dict[str, Any] | None:
    path = get_session_root(workspace) / f"{session_id}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    expected = workspace_id(workspace)
    actual = str((data.get("metadata") or {}).get("workspaceId") or "")
    if actual and actual != expected:
        return None
    return data


def list_sessions(*, workspace: str | Path | None = None) -> list[dict[str, Any]]:
    root = get_session_root(workspace)
    results: list[dict[str, Any]] = []
    expected = workspace_id(workspace)
    for path in root.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        metadata = data.get("metadata")
        if not isinstance(metadata, dict):
            continue
        if str(metadata.get("workspaceId") or expected) != expected:
            continue
        results.append(metadata)
    return results


def get_latest_session_id(*, workspace: str | Path | None = None) -> str | None:
    sessions = list_sessions(workspace=workspace)
    if not sessions:
        return None
    sessions.sort(key=lambda item: str(item.get("startTime") or ""), reverse=True)
    return sessions[0].get("id")
