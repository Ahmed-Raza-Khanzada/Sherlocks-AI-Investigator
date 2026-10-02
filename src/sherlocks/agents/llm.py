"""Pick the LLM client for this deployment.

``llm.provider: openai`` with a base URL and model -> :class:`OpenAICompatClient`
(the LAN vLLM server). Otherwise the local Ollama, if enabled. Otherwise ``None``, and
every caller falls back to its deterministic path.
"""

from __future__ import annotations

from typing import Any

from sherlocks.agents.ollama import LlmCacheBackend, OllamaClient
from sherlocks.settings import Settings


def build_llm(settings: Settings, cache: LlmCacheBackend | None = None) -> Any:
    if settings.llm.provider == "openai" and settings.llm.ready:
        from sherlocks.agents.openai_compat import OpenAICompatClient

        return OpenAICompatClient(settings.llm, cache=cache)
    if settings.ollama.ready and (settings.llm.provider == "ollama" or not settings.llm.ready):
        return OllamaClient(settings.ollama, cache=cache)
    return None


def llm_label(settings: Settings) -> str | None:
    """Human-readable name of the model the AI steps will use, or ``None``."""
    if settings.llm.provider == "openai" and settings.llm.ready:
        return settings.llm.model
    if settings.ollama.ready:
        return f"{settings.ollama.reasoning_model} (Ollama)"
    return None
