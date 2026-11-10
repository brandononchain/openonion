"""Deterministic provider for tests, demos and evals: replays a fixed script."""

from __future__ import annotations

from typing import Any, Callable, Union

from .base import Message, ModelResponse, ProviderError

Step = Union[ModelResponse, Callable[[list[Message]], ModelResponse]]


class ScriptedProvider:
    name = "scripted"
    model = "scripted"

    def __init__(self, steps: list[Step]) -> None:
        self.steps = list(steps)
        self.calls: list[list[Message]] = []

    async def complete(self, messages: list[Message], tools: list[dict[str, Any]]) -> ModelResponse:
        self.calls.append(list(messages))
        if not self.steps:
            raise ProviderError("scripted provider ran out of steps")
        step = self.steps.pop(0)
        return step(messages) if callable(step) else step

    async def aclose(self) -> None:
        return None
