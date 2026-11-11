from __future__ import annotations

from ..config import Settings
from .base import Message, ModelProvider, ModelResponse, ProviderError, ToolCall, Usage


def build_provider(settings: Settings) -> ModelProvider:
    kind = settings.provider.lower()
    if kind in {"openai", "openai-compatible", "ollama", "vllm", "openrouter"}:
        from .openai_compat import OpenAICompatProvider
        return OpenAICompatProvider(settings.base_url, settings.model, settings.api_key, settings.max_output_tokens)
    if kind == "anthropic":
        from .anthropic import AnthropicProvider
        return AnthropicProvider(settings.model, settings.anthropic_api_key or settings.api_key,
                                 settings.max_output_tokens)
    raise ProviderError(f"unknown provider '{settings.provider}' (use 'openai' or 'anthropic')")


__all__ = ["Message", "ModelProvider", "ModelResponse", "ProviderError", "ToolCall", "Usage", "build_provider"]
