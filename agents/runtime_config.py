"""Unified runtime configuration shared by the main agent, sub-agents, and fork skills."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

Provider = Literal["openai", "anthropic"]
PermissionMode = Literal["default", "plan", "acceptEdits", "bypassPermissions", "dontAsk"]

PERMISSION_RANK: dict[str, int] = {
    "plan": 0,
    "dontAsk": 1,
    "default": 2,
    "acceptEdits": 3,
    "bypassPermissions": 4,
}

READONLY_CHILD_TYPES = frozenset({"explore", "plan"})


def normalize_workspace(workspace: str | Path | None = None) -> Path:
    """Return a resolved workspace path used as the project identity."""
    return Path(workspace or Path.cwd()).expanduser().resolve()


def workspace_id(workspace: str | Path | None = None) -> str:
    """Stable project id derived from the normalized workspace path."""
    import hashlib

    resolved = str(normalize_workspace(workspace))
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]


@dataclass
class BudgetTracker:
    """Shared token/cost budget that parent and child agents increment together."""

    input_tokens: int = 0
    output_tokens: int = 0
    max_cost_usd: float | None = None
    max_turns: int | None = None
    current_turns: int = 0
    input_usd_per_mtok: float = 3.0
    output_usd_per_mtok: float = 15.0

    def add_usage(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens += max(0, int(input_tokens or 0))
        self.output_tokens += max(0, int(output_tokens or 0))

    def add_turn(self) -> None:
        self.current_turns += 1

    def cost_usd(self) -> float:
        return (self.input_tokens / 1_000_000) * self.input_usd_per_mtok + (
            self.output_tokens / 1_000_000
        ) * self.output_usd_per_mtok

    def exceeded(self) -> dict[str, Any]:
        if self.max_cost_usd is not None and self.cost_usd() >= self.max_cost_usd:
            return {
                "exceeded": True,
                "reason": f"Cost limit reached (${self.cost_usd():.4f} >= ${self.max_cost_usd})",
            }
        if self.max_turns is not None and self.current_turns >= self.max_turns:
            return {
                "exceeded": True,
                "reason": f"Turn limit reached ({self.current_turns} >= {self.max_turns})",
            }
        return {"exceeded": False}


def clamp_permission(parent_mode: str, requested_mode: str | None = None, *, readonly: bool = False) -> str:
    """Child permission can only stay equal or drop below the parent. Never escalate."""
    if readonly:
        return "plan"
    requested = requested_mode or parent_mode
    parent_rank = PERMISSION_RANK.get(parent_mode, PERMISSION_RANK["default"])
    requested_rank = PERMISSION_RANK.get(requested, PERMISSION_RANK["default"])
    if requested_rank > parent_rank:
        return parent_mode
    return requested


@dataclass
class RuntimeConfig:
    """Single source of runtime settings for every agent instance."""

    provider: Provider = "anthropic"
    model: str = "deepseek-chat"
    api_key: str | None = None
    base_url: str | None = None
    workspace: Path = field(default_factory=lambda: Path.cwd())
    permission_mode: PermissionMode | str = "default"
    model_timeout_s: float = 120.0
    tool_timeout_s: float = 30.0
    max_cost_usd: float | None = None
    max_turns: int | None = None
    trace_enabled: bool = False
    mcp_trusted: bool = False
    thinking: bool = False
    budget: BudgetTracker | None = None

    def __post_init__(self) -> None:
        self.workspace = normalize_workspace(self.workspace)
        if self.budget is None:
            self.budget = BudgetTracker(
                max_cost_usd=self.max_cost_usd,
                max_turns=self.max_turns,
            )
        else:
            if self.max_cost_usd is not None:
                self.budget.max_cost_usd = self.max_cost_usd
            if self.max_turns is not None:
                self.budget.max_turns = self.max_turns

    @property
    def use_openai(self) -> bool:
        return self.provider == "openai"

    @property
    def workspace_id(self) -> str:
        return workspace_id(self.workspace)

    def derive_child(
        self,
        *,
        agent_type: str = "general",
        permission_mode: str | None = None,
        readonly: bool | None = None,
    ) -> RuntimeConfig:
        """Create a child config that inherits model/API/workspace/budget/timeouts."""
        is_readonly = READONLY_CHILD_TYPES.__contains__(agent_type) if readonly is None else readonly
        child_mode = clamp_permission(str(self.permission_mode), permission_mode, readonly=is_readonly)
        return replace(
            self,
            permission_mode=child_mode,
            budget=self.budget,
        )
