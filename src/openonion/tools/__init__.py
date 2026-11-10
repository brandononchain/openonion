from __future__ import annotations

from ..config import Settings
from .base import OutputFile, Tool, ToolContext, ToolError, ToolResult
from .fs import ListFiles, ReadFile, WriteFile
from .plan import UpdatePlan
from .shell import RunShell
from .tor import TorBrowse, TorNewIdentity
from .web import Browse, WebFetch


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for t in tools or []:
            self.register(t)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def specs(self, allowed: list[str] | None = None) -> list[dict]:
        return [t.spec() for n, t in self._tools.items() if allowed is None or n in allowed or n == "update_plan"]


def default_registry(settings: Settings) -> ToolRegistry:
    tools: list[Tool] = [UpdatePlan(), ReadFile(), WriteFile(), ListFiles(), WebFetch(), Browse(),
                         TorBrowse(), TorNewIdentity()]
    if settings.enable_shell:
        tools.append(RunShell())
    return ToolRegistry(tools)


__all__ = ["OutputFile", "Tool", "ToolContext", "ToolError", "ToolRegistry", "ToolResult", "default_registry"]
