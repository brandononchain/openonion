"""Filesystem tools, jailed to the session workspace."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from .base import Tool, ToolContext, ToolError, ToolResult

MAX_READ_BYTES = 200_000
MAX_WRITE_BYTES = 5_000_000


def safe_path(workspace: Path, rel: str) -> Path:
    """Resolve `rel` inside `workspace`, rejecting traversal and symlink escapes."""
    root = workspace.resolve()
    candidate = (root / rel.lstrip("/")).resolve()
    if candidate != root and not candidate.is_relative_to(root):
        raise ToolError(f"path '{rel}' escapes the session workspace")
    return candidate


class ReadFile(Tool):
    name = "read_file"
    description = "Read a UTF-8 text file from the session workspace."
    risk = "read"

    class Args(BaseModel):
        path: str = Field(description="Path relative to the workspace root.")

    async def run(self, args: Args, ctx: ToolContext) -> ToolResult:
        p = safe_path(ctx.workspace, args.path)
        if not p.is_file():
            raise ToolError(f"no such file: {args.path}")
        raw = p.read_bytes()
        text = raw[:MAX_READ_BYTES].decode("utf-8", errors="replace")
        suffix = f"\n…[truncated, {len(raw)} bytes total]" if len(raw) > MAX_READ_BYTES else ""
        return ToolResult(ok=True, content=text + suffix, data={"bytes": len(raw)})


class WriteFile(Tool):
    name = "write_file"
    description = "Create or overwrite a UTF-8 text file in the session workspace."
    risk = "write"

    class Args(BaseModel):
        path: str = Field(description="Path relative to the workspace root.")
        content: str = Field(description="Full file contents.")

    async def run(self, args: Args, ctx: ToolContext) -> ToolResult:
        data = args.content.encode("utf-8")
        if len(data) > MAX_WRITE_BYTES:
            raise ToolError("file too large")
        p = safe_path(ctx.workspace, args.path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return ToolResult(ok=True, content=f"wrote {len(data)} bytes to {args.path}",
                          data={"path": args.path, "bytes": len(data)})


class ListFiles(Tool):
    name = "list_files"
    description = "List files in a workspace directory (recursive, max 500 entries)."
    risk = "read"

    class Args(BaseModel):
        path: str = Field(".", description="Directory relative to the workspace root.")

    async def run(self, args: Args, ctx: ToolContext) -> ToolResult:
        d = safe_path(ctx.workspace, args.path)
        if not d.is_dir():
            raise ToolError(f"not a directory: {args.path}")
        root = ctx.workspace.resolve()
        entries = []
        for p in sorted(d.rglob("*")):
            if len(entries) >= 500:
                entries.append("…(truncated)")
                break
            rel = p.relative_to(root)
            entries.append(f"{rel}/" if p.is_dir() else f"{rel}  ({p.stat().st_size} B)")
        return ToolResult(ok=True, content="\n".join(entries) or "(empty)")
