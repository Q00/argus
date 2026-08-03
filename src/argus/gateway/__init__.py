"""Model gateway package: one interface, many backends.

Exports:
    Gateway, LLMAdapter, ModelSpec, RawCompletion, build_adapter.
"""

from __future__ import annotations

from argus.core import ArgusError
from argus.gateway.anthropic import AnthropicAdapter
from argus.gateway.base import Gateway, LLMAdapter, ModelSpec, RawCompletion
from argus.gateway.cli import ClaudeCliAdapter, CodexCliAdapter
from argus.gateway.openai_compat import OpenAICompatAdapter

PROVIDERS = ("openai-compat", "anthropic", "codex-cli", "claude-cli")


def build_adapter(provider: str) -> LLMAdapter:
    """Resolve a provider name to its adapter instance.

    Raises:
        ArgusError: unknown provider name.
    """
    if provider == "openai-compat":
        return OpenAICompatAdapter()
    if provider == "anthropic":
        return AnthropicAdapter()
    if provider == "codex-cli":
        return CodexCliAdapter()
    if provider == "claude-cli":
        return ClaudeCliAdapter()
    raise ArgusError(f"unknown provider: {provider}", {"known": list(PROVIDERS)})


__all__ = [
    "AnthropicAdapter",
    "ClaudeCliAdapter",
    "CodexCliAdapter",
    "Gateway",
    "LLMAdapter",
    "ModelSpec",
    "OpenAICompatAdapter",
    "PROVIDERS",
    "RawCompletion",
    "build_adapter",
]
