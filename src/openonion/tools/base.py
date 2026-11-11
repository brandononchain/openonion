"""Tool interface. Tools are typed (pydantic args), described to the model via JSON
schema, gated by policy, and every call is logged as events."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pydantic import BaseModel

if TYPE_CHECKING:
    from ..config import Settings
    from ..policy import SessionPolicy
    from ..runtime.services import SessionServices

Risk = Literal["read", "write", "network", "exec", "meta"]


class ToolError(Exception):
    """Expected failure the model should see and recover from."""


@dataclass
class OutputFile:
    kind: str
    filename: str
    data: bytes
    mime: str
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResult:
    ok: bool
    content: str
    data: dict[str, Any] = field(default_factory=dict)
    files: list[OutputFile] = field(default_factory=list)

    @classmethod
    def error(cls, message: str) -> "ToolResult":
        return cls(ok=False, content=f"ERROR: {message}")


@dataclass
class ToolContext:
    session_id: str
    workspace: Path
    settings: "Settings"
    policy: "SessionPolicy"
    services: "SessionServices"
    emit: Any  # async (type, payload) -> Event
    toolbox: Any = None  # the run's Toolbox (used by find_tools)


class Tool(ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    Args: ClassVar[type[BaseModel]]
    risk: ClassVar[Risk] = "read"

    def spec(self) -> dict[str, Any]:
        schema = self.Args.model_json_schema()
        schema.pop("title", None)
        return {"name": self.name, "description": self.description, "parameters": schema}

    @abstractmethod
    async def run(self, args: Any, ctx: ToolContext) -> ToolResult: ...
