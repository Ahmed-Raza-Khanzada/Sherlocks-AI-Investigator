"""OpenAI-compatible chat client for a model served on the local network.

Written against vLLM (the office Qwen 27B server) and compatible with LM Studio and
llama.cpp's server. Same contract as :class:`~sherlocks.agents.ollama.OllamaClient` -
``generate_structured(prompt=, schema=, system=, ...) -> (model, LlmResult)`` - so
every caller works with either.

Structured output is enforced with ``response_format: json_schema`` (vLLM's guided
decoding), not merely requested. Qwen3 "thinking" is switched off with
``chat_template_kwargs``; servers that reject either field get it dropped once and
the call retried, and any ``<think>`` block that still arrives is stripped.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from typing import Any, Self, TypeVar

import requests
from pydantic import BaseModel, ValidationError

from sherlocks.agents.ollama import (
    LlmCacheBackend,
    LlmResult,
    OllamaError,
    StructuredOutputFailed,
    _extract_json,
)
from sherlocks.settings import LlmSettings

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


class LlmUnavailable(OllamaError):
    """The server could not be reached or refused the request."""


class OpenAICompatClient:
    def __init__(self, settings: LlmSettings, cache: LlmCacheBackend | None = None) -> None:
        self.settings = settings
        self.cache = cache
        self._session = requests.Session()
        # The model is on the LAN; a proxy environment variable would route around it.
        self._session.trust_env = False
        self._schema_ok = True
        self._template_kwargs_ok = True

    @property
    def model(self) -> str:
        return self.settings.model or ""

    def _url(self, path: str) -> str:
        return f"{(self.settings.base_url or '').rstrip('/')}/{path.lstrip('/')}"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.settings.api_key or 'EMPTY'}", "Content-Type": "application/json"}

    # -- server introspection -----------------------------------------------------

    def health(self) -> bool:
        try:
            return self._session.get(self._url("models"), headers=self._headers(), timeout=5).ok
        except requests.RequestException:
            return False

    def available_models(self) -> list[str]:
        try:
            response = self._session.get(self._url("models"), headers=self._headers(), timeout=10)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise LlmUnavailable(f"Cannot reach {self.settings.base_url}: {exc}") from exc
        return [m.get("id", "") for m in response.json().get("data", [])]

    # -- generation ---------------------------------------------------------------

    def _chat(self, messages: list[dict[str, str]], json_schema: dict[str, Any]) -> str:
        payload: dict[str, Any] = {
            "model": self.settings.model,
            "messages": messages,
            "temperature": self.settings.temperature,
            "max_tokens": self.settings.max_tokens,
        }
        if self._schema_ok:
            payload["response_format"] = {"type": "json_schema", "json_schema": {"name": "result", "schema": json_schema}}
        if self._template_kwargs_ok:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        try:
            response = self._session.post(self._url("chat/completions"), json=payload, headers=self._headers(),
                                          timeout=self.settings.timeout_seconds)
        except requests.Timeout as exc:
            raise LlmUnavailable(f"{self.settings.model} timed out after {self.settings.timeout_seconds}s") from exc
        except requests.RequestException as exc:
            raise LlmUnavailable(f"LLM request failed: {exc}") from exc

        if response.status_code == 400 and (self._template_kwargs_ok or self._schema_ok):
            # An older or different server: drop the optional fields one at a time.
            if self._template_kwargs_ok:
                self._template_kwargs_ok = False
            else:
                self._schema_ok = False
            logger.info("LLM server rejected an optional field; retrying without it")
            return self._chat(messages, json_schema)
        if not response.ok:
            raise LlmUnavailable(f"LLM server returned HTTP {response.status_code}: {response.text[:300]}")
        try:
            content = response.json()["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, ValueError) as exc:
            raise LlmUnavailable(f"Unexpected LLM response: {response.text[:300]}") from exc
        return _THINK_RE.sub("", content).strip()

    def generate_structured(
        self,
        *,
        prompt: str,
        schema: type[T],
        system: str | None = None,
        model: str | None = None,  # accepted for OllamaClient parity; the server serves one model
        cache_kind: str | None = None,
        prompt_version: str = "v1",
    ) -> tuple[T, LlmResult]:
        if not self.settings.ready:
            raise LlmUnavailable("LLM is disabled or not configured")
        started = time.monotonic()
        name = self.model

        cache_key = None
        if self.cache is not None and cache_kind:
            cache_key = hashlib.sha256(
                "\x00".join([cache_kind, name, prompt_version, system or "", prompt]).encode("utf-8")
            ).hexdigest()
            cached = self.cache.get(cache_key)
            if cached is not None:
                try:
                    return schema.model_validate(cached), LlmResult(data=cached, model=name, attempts=0, duration_ms=0, cached=True)
                except ValidationError:
                    logger.debug("Discarding cached LLM output that no longer validates")

        json_schema = schema.model_json_schema()
        errors: list[str] = []
        for attempt in range(1, self.settings.max_attempts + 1):
            user = prompt if attempt == 1 else (
                f"{prompt}\n\nYour previous answer was rejected: {errors[-1]}\n"
                "Return ONLY a JSON object matching the schema."
            )
            messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": user}]
            raw = self._chat(messages, json_schema)
            try:
                validated = schema.model_validate(_extract_json(raw))
            except (ValueError, ValidationError) as exc:
                errors.append(str(exc).replace("\n", " ")[:400])
                logger.warning("Structured generation attempt %d/%d failed: %s", attempt, self.settings.max_attempts, errors[-1])
                continue
            data = validated.model_dump(mode="json")
            if self.cache is not None and cache_key:
                try:
                    self.cache.set(cache_key, cache_kind or "", name, prompt_version, prompt, data)
                except Exception:
                    logger.exception("Failed to write LLM cache entry")
            return validated, LlmResult(data=data, model=name, attempts=attempt,
                                        duration_ms=int((time.monotonic() - started) * 1000))

        raise StructuredOutputFailed(
            f"{name} produced no output matching {schema.__name__} in {self.settings.max_attempts} attempts. "
            f"Last error: {errors[-1] if errors else 'unknown'}"
        )

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
