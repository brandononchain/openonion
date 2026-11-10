"""The plan is a first-class, visible artifact: the model maintains it through this
tool and every revision lands in the event log (and the UI's plan panel)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..events import PLAN_UPDATED
from .base import Tool, ToolContext, ToolResult


class PlanStep(BaseModel):
    id: int
    description: str
    status: Literal["pending", "in_progress", "done", "failed", "skipped"] = "pending"


class UpdatePlan(Tool):
    name = "update_plan"
    description = ("Create or revise your step-by-step plan. Call it first with the full plan, then again whenever "
                   "a step's status changes or the plan needs to change. Always send the complete list.")
    risk = "meta"

    class Args(BaseModel):
        steps: list[PlanStep] = Field(description="The complete current plan.")
        note: str = Field("", description="Optional one-line reason for this revision.")

    async def run(self, args: Args, ctx: ToolContext) -> ToolResult:
        steps = [s.model_dump() for s in args.steps]
        await ctx.emit(PLAN_UPDATED, {"steps": steps, "note": args.note})
        done = sum(1 for s in args.steps if s.status == "done")
        return ToolResult(ok=True, content=f"Plan recorded ({done}/{len(steps)} steps done).")
