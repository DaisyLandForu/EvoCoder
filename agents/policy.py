"""Unified policy boundary for tools, MCP, skills, sub-agents, and shell."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Protocol

from .runtime_config import normalize_workspace, workspace_id

PolicyAction = Literal["allow", "deny", "confirm"]

READ_TOOLS = frozenset({"read_file", "list_files", "grep_search", "compact_context"})
WRITE_TOOLS = frozenset({"write_file", "edit_file", "skill_evolve", "skill_create"})
SIDE_EFFECT_TOOLS = WRITE_TOOLS | frozenset({"run_shell", "agent", "skill"})
CONCURRENCY_SAFE_TOOLS = frozenset({"read_file", "list_files", "grep_search"})
CONTROL_TOOLS = frozenset({"enter_plan_mode", "exit_plan_mode", "tool_search", "compact_context"})

MCP_TRUST_DIRNAME = "mcp-trust"
MCP_TRUST_ENV = "BEAR_MCP_TRUSTED"

#: Repository-level MCP configuration files. All of them are covered by the trust hash,
#: because any of them can start a server process from inside a cloned repository.
REPO_MCP_CONFIG_FILES = (".mcp.json", ".bear/settings.json")

DANGEROUS_PATTERNS = [
    re.compile(r"\brm\s"),
    re.compile(r"\bgit\s+(push|reset|clean|checkout\s+\.)"),
    re.compile(r"\bsudo\b"),
    re.compile(r"\bmkfs\b"),
    re.compile(r"\bdd\s"),
    re.compile(r">\s*/dev/"),
    re.compile(r"\bkill\b"),
    re.compile(r"\bpkill\b"),
    re.compile(r"\breboot\b"),
    re.compile(r"\bshutdown\b"),
    re.compile(r"\bdel\s", re.IGNORECASE),
    re.compile(r"\brmdir\s", re.IGNORECASE),
    re.compile(r"\bformat\s", re.IGNORECASE),
    re.compile(r"\btaskkill\s", re.IGNORECASE),
    re.compile(r"\bRemove-Item\s", re.IGNORECASE),
    re.compile(r"\bStop-Process\s", re.IGNORECASE),
]


def is_dangerous(command: str) -> bool:
    return any(pattern.search(command or "") for pattern in DANGEROUS_PATTERNS)


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


def repo_mcp_config_paths(workspace: Path) -> list[Path]:
    """Every repository-level file that can declare MCP servers."""
    root = normalize_workspace(workspace)
    return [root / relative for relative in REPO_MCP_CONFIG_FILES]


def declares_mcp_servers(path: Path) -> bool:
    """Mirror the loader: a config declares servers when it maps names to commands."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(raw, dict):
        return False
    servers = raw.get("mcpServers", raw)
    if not isinstance(servers, dict):
        return False
    return any(isinstance(cfg, dict) and "command" in cfg for cfg in servers.values())


def repo_mcp_declarations(workspace: Path) -> list[Path]:
    """Repository-level files that actually declare MCP servers."""
    return [path for path in repo_mcp_config_paths(workspace) if declares_mcp_servers(path)]


def mcp_config_hash(workspace: Path) -> str:
    """Hash every repository-level MCP declaration so trusting one file cannot cover another."""
    import hashlib

    declarations = repo_mcp_declarations(workspace)
    if not declarations:
        return ""
    digest = hashlib.sha256()
    for path in declarations:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def mcp_trust_record_path(workspace: Path) -> Path:
    """Trust lives in the user's home, never inside the repository being trusted.

    A record committed to the repository would otherwise arrive pre-trusted after a clone.
    """
    return Path.home() / ".bear" / MCP_TRUST_DIRNAME / f"{workspace_id(workspace)}.json"


def is_mcp_config_trusted(workspace: Path) -> bool:
    """Repository-level MCP config must be explicitly trusted before servers are started."""
    raw = os.environ.get(MCP_TRUST_ENV, "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    workspace = normalize_workspace(workspace)
    if not repo_mcp_declarations(workspace):
        return True
    record = _load_settings(mcp_trust_record_path(workspace))
    if not isinstance(record, dict):
        return False
    if str(record.get("workspace") or "") != str(workspace):
        return False
    return str(record.get("configHash") or "") == mcp_config_hash(workspace)


def write_mcp_trust(workspace: Path) -> Path:
    """Record an explicit user decision to trust this repository's MCP declarations."""
    workspace = normalize_workspace(workspace)
    trust_path = mcp_trust_record_path(workspace)
    trust_path.parent.mkdir(parents=True, exist_ok=True)
    trust_path.write_text(
        json.dumps(
            {
                "workspace": str(workspace),
                "workspaceId": workspace_id(workspace),
                "configHash": mcp_config_hash(workspace),
                "files": [str(path.relative_to(workspace)) for path in repo_mcp_declarations(workspace)],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return trust_path


class PathEscapeError(ValueError):
    """Raised when a file path leaves the current workspace."""


_rules_cache: dict[str, dict[str, list[dict[str, Any]]]] = {}


def _parse_rule(rule: str) -> dict[str, Any]:
    match = re.match(r"^([a-z_]+)\((.+)\)$", rule)
    if match:
        return {"tool": match.group(1), "pattern": match.group(2)}
    return {"tool": rule, "pattern": None}


def _load_settings(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_permission_rules(workspace: Path | str | None = None) -> dict[str, list[dict[str, Any]]]:
    """User and project `settings.json` allow/deny rules, cached per workspace."""
    root = normalize_workspace(workspace)
    cached = _rules_cache.get(str(root))
    if cached is not None:
        return cached
    allow: list[dict[str, Any]] = []
    deny: list[dict[str, Any]] = []
    sources = (Path.home() / ".bear" / "settings.json", root / ".bear" / "settings.json")
    for settings in (_load_settings(path) for path in sources):
        if not settings or not isinstance(settings.get("permissions"), dict):
            continue
        permissions = settings["permissions"]
        allow.extend(_parse_rule(rule) for rule in permissions.get("allow", []) if isinstance(rule, str))
        deny.extend(_parse_rule(rule) for rule in permissions.get("deny", []) if isinstance(rule, str))
    rules = {"allow": allow, "deny": deny}
    _rules_cache[str(root)] = rules
    return rules


def reset_permission_cache() -> None:
    _rules_cache.clear()


def _matches_rule(rule: dict[str, Any], tool_name: str, inp: dict[str, Any]) -> bool:
    if rule["tool"] != tool_name:
        return False
    if rule["pattern"] is None:
        return True
    if tool_name == "run_shell":
        value = str(inp.get("command") or "")
    elif "file_path" in inp:
        value = str(inp["file_path"])
    else:
        return True
    pattern = str(rule["pattern"])
    if pattern.endswith("*"):
        return value.startswith(pattern[:-1])
    return value == pattern


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
        self._plan_file_path: str | None = None
        self._plan_file_resolved: Path | None = None
        self.plan_file_path = plan_file_path
        self.mcp_trusted = mcp_trusted
        self.tool_timeout_s = tool_timeout_s
        self.shell_backend = shell_backend or LocalProcessShellBackend()
        self.parent_engine = parent_engine

    @property
    def plan_file_path(self) -> str | None:
        return self._plan_file_path

    @plan_file_path.setter
    def plan_file_path(self, value: str | None) -> None:
        """The plan file lives outside the workspace, so it is an explicit path allow-list entry."""
        self._plan_file_path = value
        self._plan_file_resolved = Path(value).expanduser().resolve() if value else None

    def _is_plan_file(self, resolved: Path) -> bool:
        return self._plan_file_resolved is not None and resolved == self._plan_file_resolved

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
        if self._is_plan_file(resolved):
            return resolved
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

        path_decision = self._authorize_paths(tool_name, inp)
        if path_decision.action == "deny":
            return path_decision

        rule = self._rule_decision(tool_name, inp)
        if rule == "deny":
            return PolicyDecision(
                action="deny",
                message=f"Denied by permission rule for {tool_name}",
                reason="rule_deny",
            )

        # Plan mode and MCP trust are hard boundaries: a project `allow` rule may only
        # remove confirmation prompts in the normal modes, never lift these limits.
        if self.permission_mode == "plan":
            plan_decision = self._authorize_plan_mode(tool_name, inp)
            if plan_decision is not None:
                return plan_decision

        if tool_name.startswith("mcp__"):
            return self._authorize_mcp(tool_name)

        if rule == "allow":
            return PolicyDecision(action="allow", reason="rule_allow")

        if self.permission_mode == "bypassPermissions":
            return PolicyDecision(action="allow", reason="bypass")

        if tool_name in READ_TOOLS:
            return PolicyDecision(action="allow", reason="read_tool")

        if tool_name in CONTROL_TOOLS:
            return PolicyDecision(action="allow", reason="control_tool")

        message = self._confirmation_message(tool_name, inp)
        if message is None:
            return PolicyDecision(action="allow", reason="default_allow")
        if self.permission_mode == "acceptEdits" and tool_name in WRITE_TOOLS:
            return PolicyDecision(action="allow", reason="accept_edits")
        if self.permission_mode == "dontAsk":
            return PolicyDecision(
                action="deny",
                message=f"Auto-denied (dontAsk mode): {message}",
                reason="dont_ask",
            )
        return PolicyDecision(action="confirm", message=message, reason="needs_confirmation")

    def _authorize_plan_mode(self, tool_name: str, inp: dict[str, Any]) -> PolicyDecision | None:
        if tool_name in READ_TOOLS:
            return None
        if tool_name.startswith("mcp__"):
            return PolicyDecision(
                action="deny",
                message=f"Blocked in plan mode: unknown MCP tool {tool_name} is treated as side-effecting",
                reason="plan_mcp_side_effect",
            )
        if tool_name in WRITE_TOOLS:
            raw_path = str(inp.get("file_path") or inp.get("path") or "")
            if raw_path and self._is_plan_file(Path(raw_path).expanduser().resolve()):
                return PolicyDecision(action="allow", reason="plan_file")
            return PolicyDecision(
                action="deny",
                message=f"Blocked in plan mode: {tool_name}",
                reason="plan_write",
            )
        if tool_name == "run_shell":
            return PolicyDecision(action="deny", message="Shell commands blocked in plan mode", reason="plan_shell")
        if tool_name in {"agent", "skill"}:
            return PolicyDecision(
                action="deny",
                message=f"Blocked in plan mode: {tool_name}",
                reason="plan_side_effect",
            )
        return None

    def _confirmation_message(self, tool_name: str, inp: dict[str, Any]) -> str | None:
        """Return the confirmation prompt for side-effecting actions, or None when free to run."""
        if tool_name == "run_shell":
            command = str(inp.get("command") or "")
            return command if is_dangerous(command) else None
        if tool_name in {"write_file", "edit_file"}:
            return f"{tool_name}: {inp.get('file_path', '')}"
        if tool_name == "skill_evolve":
            return f"evolve skill: {inp.get('skill_name', '')}"
        if tool_name == "skill_create":
            return f"create skill: {inp.get('name', '')}"
        return None

    def _rule_decision(self, tool_name: str, inp: dict[str, Any]) -> str | None:
        rules = load_permission_rules(self.workspace)
        for rule in rules["deny"]:
            if _matches_rule(rule, tool_name, inp):
                return "deny"
        for rule in rules["allow"]:
            if _matches_rule(rule, tool_name, inp):
                return "allow"
        return None

    def _authorize_paths(self, tool_name: str, inp: dict[str, Any]) -> PolicyDecision:
        raw_paths: list[str] = []
        if inp.get("file_path"):
            raw_paths.append(str(inp["file_path"]))
        if tool_name in {"list_files", "grep_search"} and inp.get("path"):
            raw_paths.append(str(inp["path"]))
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
                message="Repository MCP config is not trusted. Trust it before starting MCP servers.",
                reason="mcp_untrusted",
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
