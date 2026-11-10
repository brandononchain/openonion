"""Shell tool. Disabled unless OPENONION_ENABLE_SHELL=true, and approval-gated by
default policy. It runs inside the API container with the workspace as cwd —
the container (non-root user, internal networks) is the sandbox boundary; for
hostile workloads run it under gVisor or in a per-session VM."""

from __future__ import annotations

import asyncio
import os

from pydantic import BaseModel, Field

from .base import Tool, ToolContext, ToolResult

MAX_OUTPUT = 20_000


class RunShell(Tool):
    name = "run_shell"
    description = "Run a bash command in the session workspace. Returns exit code, stdout and stderr."
    risk = "exec"

    class Args(BaseModel):
        command: str = Field(description="The bash command to run.")
        timeout_s: int = Field(60, ge=1, le=600)

    async def run(self, args: Args, ctx: ToolContext) -> ToolResult:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(ctx.workspace), "LANG": "C.UTF-8"}
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", args.command, cwd=str(ctx.workspace), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), args.timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return ToolResult(ok=False, content=f"ERROR: command timed out after {args.timeout_s}s")
        text = (
            f"exit_code: {proc.returncode}\n"
            f"--- stdout ---\n{out.decode(errors='replace')[:MAX_OUTPUT]}\n"
            f"--- stderr ---\n{err.decode(errors='replace')[:MAX_OUTPUT]}"
        )
        return ToolResult(ok=proc.returncode == 0, content=text, data={"exit_code": proc.returncode})
