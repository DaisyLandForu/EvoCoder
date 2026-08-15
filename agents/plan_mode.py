"""Explicit Plan Mode state machine."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal


class PlanPhase(str, Enum):
    DEFAULT = "default"
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"


ApprovalChoice = Literal["clear-and-execute", "execute", "manual-execute", "keep-planning"]


@dataclass
class PlanApproval:
    choice: ApprovalChoice
    feedback: str | None = None


ApproveFn = Callable[[str], Awaitable[dict[str, Any] | PlanApproval]]


class PlanModeController:
    """DEFAULT → PLANNING → AWAITING_APPROVAL → EXECUTING → DEFAULT."""

    def __init__(self, *, previous_mode: str = "default") -> None:
        self.phase = PlanPhase.DEFAULT
        self.previous_mode = previous_mode
        self.execution_mode = previous_mode
        self.plan_file_path: str | None = None

    @property
    def active(self) -> bool:
        return self.phase in {PlanPhase.PLANNING, PlanPhase.AWAITING_APPROVAL}

    @property
    def readonly(self) -> bool:
        return self.phase in {PlanPhase.PLANNING, PlanPhase.AWAITING_APPROVAL}

    def enter(self, plan_file_path: str, *, previous_mode: str | None = None) -> PlanPhase:
        if previous_mode:
            self.previous_mode = previous_mode
        self.plan_file_path = plan_file_path
        self.phase = PlanPhase.PLANNING
        return self.phase

    def read_plan_content(self) -> str:
        if not self.plan_file_path:
            return ""
        path = Path(self.plan_file_path)
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")

    async def request_approval(self, approve_fn: ApproveFn | None) -> PlanApproval:
        self.phase = PlanPhase.AWAITING_APPROVAL
        content = self.read_plan_content()
        if approve_fn is None:
            return PlanApproval(choice="manual-execute")
        raw = await approve_fn(content)
        if isinstance(raw, PlanApproval):
            return raw
        choice = str((raw or {}).get("choice") or "manual-execute")
        if choice not in {"clear-and-execute", "execute", "manual-execute", "keep-planning"}:
            choice = "manual-execute"
        return PlanApproval(choice=choice, feedback=(raw or {}).get("feedback"))  # type: ignore[arg-type]

    def apply_approval(self, approval: PlanApproval) -> PlanPhase:
        if approval.choice == "keep-planning":
            self.phase = PlanPhase.PLANNING
            return self.phase
        if approval.choice in {"clear-and-execute", "execute"}:
            self.execution_mode = "acceptEdits"
        else:
            self.execution_mode = self.previous_mode or "default"
        self.phase = PlanPhase.EXECUTING
        return self.phase

    def exit_to_default(self, *, restore_mode: str | None = None) -> str:
        mode = restore_mode or self.previous_mode or "default"
        self.phase = PlanPhase.DEFAULT
        self.plan_file_path = None
        self.execution_mode = mode
        return mode

    def permission_mode(self, fallback: str) -> str:
        if self.readonly:
            return "plan"
        if self.phase == PlanPhase.EXECUTING:
            return self.execution_mode
        return fallback
