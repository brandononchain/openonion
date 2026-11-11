"""Deterministic provider for tests, demos and evals: replays a fixed script."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, Union

from .base import Message, ModelResponse, ProviderError, TextSink

Step = Union[ModelResponse, Callable[[list[Message]], ModelResponse]]


class ScriptedProvider:
    name = "scripted"
    model = "scripted"

    def __init__(self, steps: list[Step]) -> None:
        self.steps = list(steps)
        self.calls: list[list[Message]] = []

    async def complete(self, messages: list[Message], tools: list[dict[str, Any]],
                       on_text: TextSink = None) -> ModelResponse:
        self.calls.append(list(messages))
        if not self.steps:
            raise ProviderError("scripted provider ran out of steps")
        step = self.steps.pop(0)
        resp = step(messages) if callable(step) else step
        if on_text and resp.text:
            # Stream word by word, like a real model, so live rendering gets exercised.
            for piece in re.findall(r"\S+\s*|\s+", resp.text):
                await on_text(piece)
        return resp

    async def aclose(self) -> None:
        return None
