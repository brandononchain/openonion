"""Provider-neutral message and tool-call types.

The orchestrator only ever speaks these types; each adapter translates them to
and from its wire format. Swapping providers never touches agent logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ToolCall":
        return cls(id=d["id"], name=d["name"], arguments=d.get("arguments") or {})


@dataclass
class Message:
    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "tool_calls": [t.to_dict() for t in self.tool_calls],
            "tool_call_id": self.tool_call_id,
            "name": self.name,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Message":
        return cls(
            role=d["role"],
            content=d.get("content") or "",
            tool_calls=[ToolCall.from_dict(t) for t in d.get("tool_calls") or []],
            tool_call_id=d.get("tool_call_id"),
            name=d.get("name"),
        )


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class ModelResponse:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    stop_reason: str | None = None


class ProviderError(RuntimeError):
    pass


class ModelProvider(Protocol):
    name: str
    model: str

    async def complete(self, messages: list[Message], tools: list[dict[str, Any]]) -> ModelResponse:
        """tools: [{"name", "description", "parameters": <JSON schema>}]"""
        ...

    async def aclose(self) -> None: ...
