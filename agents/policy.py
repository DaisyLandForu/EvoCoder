"""Unified policy boundary for tools, MCP, skills, sub-agents, and shell."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Protocol

from .runtime_config import normalize_workspace

PolicyAction = Literal["allow", "deny", "confirm"]

READ_TOOLS = frozenset({"read_file", "list_files", "grep_search", "compact_context"})
WRITE_TOOLS = frozenset({"write_file", "edit_file", "skill_evolve", "skill_create"})
SIDE_EFFECT_TOOLS = WRITE_TOOLS | frozenset({"run_shell", "agent", "skill"})
CONCURRENCY_SAFE_TOOLS = frozenset({"read_file", "list_files", "grep_search"})

MCP_TRUST_FILENAME = "mcp.trusted"
MCP_TRUST_ENV = "BEAR_MCP_TRUSTED"


@dataclass(frozen=True)
class PolicyDecision:
    action: PolicyAction
    message: str = ""
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.action == "allow"


@dataclass(frozen=True)
class ShellResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def format(self) -> str:
        if self.timed_out:
            return f"Command timed out after workspace-limited execution.\nStdout: {self.stdout}\nStderr: {self.stderr}"
        if self.exit_code != 0:
            stdout = f"\nStdout: {self.stdout}" if self.stdout else ""
            stderr = f"\nStderr: {self.stderr}" if self.stderr else ""
            return f"Command failed (exit code {self.exit_code}){stdout}{stderr}"
        return self.stdout or "(no output)"


class ShellBackend(Protocol):
    """Replaceable shell execution backend. P0 uses a local restricted process."""

    def run(
        self,
        command: str,
        *,
        cwd: Path,
        timeout_s: float,
        env: Mapping[str, str],
    ) -> ShellResult: ...


class LocalProcessShellBackend:
    """Run a command in a workspace-bound process with a filtered environment."""

    def run(
        self,
        command: str,
        *,
        cwd: Path,
        timeout_s: float,
        env: Mapping[str, str],
    ) -> ShellResult:
        try:
            completed = subprocess.run(
                command,
                shell=True,
                cwd=str(cwd),
                env=dict(env),
                capture_output=True,
                text=True,
                timeout=max(0.1, float(timeout_s)),
            )
            return ShellResult(
                exit_code=int(completed.returncode),
                stdout=completed.stdout or "",
                stderr=completed.stderr or "",
            )
        except subprocess.TimeoutExpired as exc:
            return ShellResult(
                exit_code=124,
                stdout=exc.stdout or "" if isinstance(exc.stdout, str) else "",
                stderr=exc.stderr or "" if isinstance(exc.stderr, str) else "",
                timed_out=True,
            )


def restricted_shell_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Pass only a small allow-list of environment variables into shell commands."""
    allowed = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR", "TZ")
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    if extra:
        env.update({str(k): str(v) for k, v in extra.items() if v is not None})
    return env


def mcp_config_hash(workspace: Path) -> str:
    import hashlib

    path = Path(workspace) / ".mcp.json"
    if not path.is_file():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def is_mcp_config_trusted(workspace: Path) -> bool:
    """Project `.mcp.json` must be explicitly trusted before servers are started."""
    raw = os.environ.get(MCP_TRUST_ENV, "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    workspace = normalize_workspace(workspace)
    mcp_path = workspace / ".mcp.json"
    if not mcp_path.is_file():
        return True
    trust_path = workspace / ".bear" / MCP_TRUST_FILENAME
    if not trust_path.is_file():
        return False
    try:
        recorded = trust_path.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return recorded == mcp_config_hash(workspace)


def write_mcp_trust(workspace: Path) -> Path:
    workspace = normalize_workspace(workspace)
    trust_path = workspace / ".bear" / MCP_TRUST_FILENAME
    trust_path.parent.mkdir(parents=True, exist_ok=True)
    trust_path.write_text(mcp_config_hash(workspace) + "\n", encoding="utf-8")
    return trust_path


class PathEscapeError(ValueError):
    """Raised when a file path leaves the current workspace."""


class PolicyEngine:
    """Authorize every runtime action against workspace, plan mode, and trust rules."""

    def __init__(
        self,
        *,
        workspace: Path,
        permission_mode: str = "default",
        plan_file_path: str | None = None,
        mcp_trusted: bool = False,
        tool_timeout_s: float = 30.0,
        shell_backend: ShellBackend | None = None,
        parent_engine: PolicyEngine | None = None,
    ) -> None:
        self.workspace = normalize_workspace(workspace)
        self.permission_mode = permission_mode
        self.plan_file_path = plan_file_path
        self.mcp_trusted = mcp_trusted
        self.tool_timeout_s = tool_timeout_s
        self.shell_backend = shell_backend or LocalProcessShellBackend()
        self.parent_engine = parent_engine

    def child(self, *, permission_mode: str | None = None, readonly: bool = False) -> PolicyEngine:
        from .runtime_config import clamp_permission

        mode = clamp_permission(self.permission_mode, permission_mode, readonly=readonly)
        return PolicyEngine(
            workspace=self.workspace,
            permission_mode=mode,
            plan_file_path=self.plan_file_path,
            mcp_trusted=self.mcp_trusted,
            tool_timeout_s=self.tool_timeout_s,
            shell_backend=self.shell_backend,
            parent_engine=self,
        )

    def resolve_workspace_path(self, raw_path: str, *, must_exist: bool = False) -> Path:
        """Resolve a path and reject workspace escapes, including symlink hops."""
        if not raw_path:
            raise PathEscapeError("empty path is not allowed")
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        try:
            resolved = candidate.resolve(strict=must_exist)
        except FileNotFoundError as exc:
            if must_exist:
                raise PathEscapeError(f"path does not exist: {raw_path}") from exc
            resolved = candidate.expanduser().resolve()
        workspace_root = self.workspace
        try:
            resolved.relative_to(workspace_root)
        except ValueError as exc:
            raise PathEscapeError(f"path escapes workspace: {raw_path}") from exc
        if candidate.exists() or resolved.exists():
            real = resolved.resolve()
            try:
                real.relative_to(workspace_root)
            except ValueError as exc:
                raise PathEscapeError(f"symlink escapes workspace: {raw_path}") from exc
            if candidate.is_symlink():
                link_target = Path(os.path.realpath(candidate))
                try:
                    link_target.relative_to(workspace_root)
                except ValueError as exc:
                    raise PathEscapeError(f"symlink escapes workspace: {raw_path}") from exc
        return resolved

    def authorize(self, tool_name: str, arguments: dict[str, Any] | None = None) -> PolicyDecision:
        inp = dict(arguments or {})
        if self.parent_engine is not None:
            parent_decision = self.parent_engine.authorize(tool_name, inp)
            if parent_decision.action == "deny":
                return PolicyDecision(
                    action="deny",
                    message=parent_decision.message,
                    reason="parent_policy_denied",
                )

        if self.permission_mode == "bypassPermissions":
            path_decision = self._authorize_paths(tool_name, inp)
            if path_decision.action == "deny":
                return path_decision
            return PolicyDecision(action="allow", reason="bypass")

        path_decision = self._authorize_paths(tool_name, inp)
        if path_decision.action != "allow":
            return path_decision

        if tool_name.startswith("mcp__"):
            return self._authorize_mcp(tool_name)

        if tool_name in READ_TOOLS:
            return PolicyDecision(action="allow", reason="read_tool")

        if self.permission_mode == "plan":
            if tool_name in WRITE_TOOLS:
                file_path = str(inp.get("file_path") or inp.get("path") or "")
                plan_match = (
                    self.plan_file_path
                    and file_path
                    and Path(file_path).resolve() == Path(self.plan_file_path).resolve()
                )
                if plan_match:
                    return PolicyDecision(action="allow", reason="plan_file")
                return PolicyDecision(
                    action="deny",
                    message=f"Blocked in plan mode: {tool_name}",
                    reason="plan_write",
                )
            if tool_name == "run_shell":
                return PolicyDecision(action="deny", message="Shell commands blocked in plan mode", reason="plan_shell")
            if tool_name in {"agent", "skill", "skill_create", "skill_evolve"}:
                return PolicyDecision(
                    action="deny",
                    message=f"Blocked in plan mode: {tool_name}",
                    reason="plan_side_effect",
                )

        if tool_name in {"enter_plan_mode", "exit_plan_mode", "tool_search", "compact_context"}:
            return PolicyDecision(action="allow", reason="control_tool")

        if self.permission_mode == "acceptEdits" and tool_name in WRITE_TOOLS:
            return PolicyDecision(action="allow", reason="accept_edits")

        if tool_name == "run_shell":
            return PolicyDecision(
                action="confirm" if self.permission_mode == "default" else "allow",
                message=str(inp.get("command") or ""),
                reason="shell",
            )
        if tool_name in {"skill_evolve", "skill_create"} and self.permission_mode == "default":
            return PolicyDecision(action="confirm", message=f"{tool_name}: {inp}", reason="skill_write")
        if self.permission_mode == "dontAsk" and tool_name in SIDE_EFFECT_TOOLS:
            return PolicyDecision(action="deny", message=f"Auto-denied (dontAsk mode): {tool_name}", reason="dont_ask")
        return PolicyDecision(action="allow", reason="default_allow")

    def _authorize_paths(self, tool_name: str, inp: dict[str, Any]) -> PolicyDecision:
        raw_paths: list[str] = []
        if "file_path" in inp and inp.get("file_path"):
            raw_paths.append(str(inp["file_path"]))
        if tool_name in {"list_files", "grep_search"} and inp.get("path"):
            raw_paths.append(str(inp["path"]))
        if self.plan_file_path:
            plan_resolved = Path(self.plan_file_path).expanduser().resolve()
            raw_paths = [
                raw
                for raw in raw_paths
                if Path(raw).expanduser().resolve() != plan_resolved
            ]
        for raw in raw_paths:
            try:
                self.resolve_workspace_path(raw, must_exist=False)
            except PathEscapeError as exc:
                return PolicyDecision(action="deny", message=str(exc), reason="path_escape")
        return PolicyDecision(action="allow", reason="path_ok")

    def _authorize_mcp(self, tool_name: str) -> PolicyDecision:
        if not self.mcp_trusted and not is_mcp_config_trusted(self.workspace):
            return PolicyDecision(
                action="deny",
                message="Project .mcp.json is not trusted. Trust it before starting MCP servers.",
                reason="mcp_untrusted",
            )
        if self.permission_mode == "plan":
            return PolicyDecision(
                action="deny",
                message=f"Blocked in plan mode: unknown MCP tool {tool_name} is treated as side-effecting",
                reason="plan_mcp_side_effect",
            )
        return PolicyDecision(action="allow", reason="mcp_allow")

    def run_shell(self, command: str, *, timeout_s: float | None = None) -> ShellResult:
        decision = self.authorize("run_shell", {"command": command})
        if decision.action == "deny":
            return ShellResult(exit_code=126, stdout="", stderr=decision.message)
        return self.shell_backend.run(
            command,
            cwd=self.workspace,
            timeout_s=timeout_s or self.tool_timeout_s,
            env=restricted_shell_env(),
        )
