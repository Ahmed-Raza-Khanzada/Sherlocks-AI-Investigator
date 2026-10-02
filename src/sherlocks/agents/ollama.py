"""Local LLM client.

Everything the agents send goes to an Ollama server on the local network. No subject
data leaves the deployment.

Two things about this client matter more than the code:

1. **Structured output is enforced, not requested.** Calls go to ``/api/generate``
   with a JSON Schema in ``format``. On this Ollama build (0.23.4) the ``/api/chat``
   endpoint silently ignores the schema and returns fenced markdown with commentary,
   so ``/api/generate`` is the only endpoint used here. Do not "simplify" this back
   to ``/api/chat``.

2. **The model is small and it does hallucinate.** Asked to parse a Karachi address,
   ``qwen3.5:4b`` confidently placed "Khi" in Sargodha District on one run and called
   it "Khyber" on the next. Schema validation catches malformed output; it cannot
   catch confident nonsense. Callers are responsible for validating *values* against
   an authority (a gazetteer, a provider record) before trusting them.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol, Self, TypeVar

import requests
from pydantic import BaseModel, ValidationError

from sherlocks.settings import OllamaSettings

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class OllamaError(RuntimeError):
    """Base class for local-LLM failures."""


class OllamaUnavailable(OllamaError):
    """The server could not be reached, or the model is not present."""


class StructuredOutputFailed(OllamaError):
    """The model never produced output matching the requested schema."""


class LlmCacheBackend(Protocol):
    """Anything that can memoise a structured generation.

    Kept a protocol so the client has no database import and stays usable in tests
    and in the CLI with no Postgres running.
    """

    def get(self, input_hash: str) -> dict[str, Any] | None: ...

    def set(
        self, input_hash: str, kind: str, model: str, prompt_version: str,
        input_text: str, output: dict[str, Any],
    ) -> None: ...


@dataclass(slots=True)
class LlmResult:
    """One structured generation, plus how much it cost to get it."""

    data: dict[str, Any]
    model: str
    attempts: int
    duration_ms: int
    cached: bool = False


def _extract_json(text: str) -> Any:
    """Pull a JSON value out of model output.

    ``format`` normally guarantees bare JSON, but a small model under retry sometimes
    wraps it in a code fence or bolts an explanation onto the end. Try the cheap
    parse, then the fence, then the outermost brace span.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("empty response")

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    fenced = _FENCE_RE.search(text)
    if fenced:
        try:
            return json.loads(fenced.group(1).strip())
        except json.JSONDecodeError:
            pass

    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start : end + 1])

    raise ValueError(f"no JSON object found in response: {text[:200]!r}")


class OllamaClient:
    def __init__(self, settings: OllamaSettings, cache: LlmCacheBackend | None = None) -> None:
        self.settings = settings
        self.cache = cache
        self._session = requests.Session()
        # Ollama is on the local network; a proxy env var would break it.
        self._session.trust_env = False
        # Set once the server rejects `think`, so older builds cost one 400 and not one per call.
        self._supports_think = True

    # -- server introspection -----------------------------------------------------

    def health(self) -> bool:
        try:
            response = self._session.get(f"{self.settings.base_url}/api/version", timeout=5)
            return response.ok
        except requests.RequestException:
            return False

    def available_models(self) -> list[str]:
        try:
            response = self._session.get(f"{self.settings.base_url}/api/tags", timeout=10)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise OllamaUnavailable(f"Cannot reach Ollama at {self.settings.base_url}: {exc}") from exc
        return [m.get("name", "") for m in response.json().get("models", [])]

    def ensure_model(self, model: str) -> None:
        """Fail early and legibly when a configured model was never pulled."""
        names = self.available_models()
        if model in names:
            return
        # `qwen3.5:4b` should satisfy a request for `qwen3.5`.
        if any(name.split(":")[0] == model.split(":")[0] for name in names):
            return
        raise OllamaUnavailable(
            f"Model {model!r} is not available on {self.settings.base_url}. "
            f"Present: {', '.join(names) or 'none'}. Run: ollama pull {model}"
        )

    # -- generation ---------------------------------------------------------------

    def _post_generate(self, payload: dict[str, Any]) -> str:
        url = f"{self.settings.base_url}/api/generate"
        try:
            response = self._session.post(url, json=payload, timeout=self.settings.timeout_seconds)
        except requests.Timeout as exc:
            raise OllamaUnavailable(
                f"Ollama timed out after {self.settings.timeout_seconds}s. "
                "A cold model load can take ~40s on this hardware; raise "
                "SHERLOCKS_OLLAMA_TIMEOUT if this recurs."
            ) from exc
        except requests.RequestException as exc:
            raise OllamaUnavailable(f"Ollama request failed: {exc}") from exc

        if response.status_code == 400 and self._supports_think and "think" in payload:
            # Older servers reject the `think` flag. Drop it permanently and retry once.
            logger.debug("Ollama rejected `think`; disabling it for this client")
            self._supports_think = False
            payload.pop("think", None)
            return self._post_generate(payload)

        if not response.ok:
            raise OllamaUnavailable(f"Ollama returned HTTP {response.status_code}: {response.text[:300]}")

        return response.json().get("response", "")

    def generate_structured(
        self,
        *,
        prompt: str,
        schema: type[T],
        system: str | None = None,
        model: str | None = None,
        cache_kind: str | None = None,
        prompt_version: str = "v1",
    ) -> tuple[T, LlmResult]:
        """Generate output conforming to ``schema``.

        Returns the validated model and the call metadata. Raises
        ``StructuredOutputFailed`` if every attempt fails validation - the caller
        should then fall back to a deterministic path rather than guessing.
        """
        if not self.settings.ready:
            raise OllamaUnavailable("Ollama is disabled in configuration")

        model = model or self.settings.reasoning_model
        json_schema = schema.model_json_schema()
        started = time.monotonic()

        cache_key = None
        if self.cache is not None and cache_kind:
            cache_key = hashlib.sha256(
                "\x00".join([cache_kind, model, prompt_version, system or "", prompt]).encode("utf-8")
            ).hexdigest()
            cached = self.cache.get(cache_key)
            if cached is not None:
                try:
                    return schema.model_validate(cached), LlmResult(
                        data=cached, model=model, attempts=0, duration_ms=0, cached=True
                    )
                except ValidationError:
                    # A prompt_version bump normally handles this; tolerate a stale row.
                    logger.debug("Discarding cached LLM output that no longer validates")

        errors: list[str] = []
        for attempt in range(1, self.settings.max_attempts + 1):
            payload: dict[str, Any] = {
                "model": model,
                "prompt": prompt if attempt == 1 else self._repair_prompt(prompt, errors[-1]),
                "stream": False,
                "format": json_schema,
                "options": {
                    "temperature": self.settings.temperature,
                    "num_ctx": self.settings.num_ctx,
                },
            }
            if system:
                payload["system"] = system
            if self._supports_think:
                # Reasoning traces are noise here and cost tokens we do not have.
                payload["think"] = False

            raw = self._post_generate(payload)
            try:
                parsed = _extract_json(raw)
                validated = schema.model_validate(parsed)
            except (ValueError, ValidationError) as exc:
                message = str(exc).replace("\n", " ")[:400]
                errors.append(message)
                logger.warning(
                    "Structured generation attempt %d/%d failed: %s",
                    attempt, self.settings.max_attempts, message,
                )
                continue

            duration_ms = int((time.monotonic() - started) * 1000)
            data = validated.model_dump(mode="json")
            if self.cache is not None and cache_key:
                try:
                    self.cache.set(cache_key, cache_kind or "", model, prompt_version, prompt, data)
                except Exception:
                    # A cache write must never fail the call it was meant to speed up.
                    logger.exception("Failed to write LLM cache entry")
            return validated, LlmResult(
                data=data, model=model, attempts=attempt, duration_ms=duration_ms
            )

        raise StructuredOutputFailed(
            f"{model} produced no output matching {schema.__name__} in "
            f"{self.settings.max_attempts} attempts. Last error: {errors[-1] if errors else 'unknown'}"
        )

    @staticmethod
    def _repair_prompt(original: str, error: str) -> str:
        return (
            f"{original}\n\n"
            f"Your previous answer was rejected: {error}\n"
            "Return ONLY a JSON object matching the schema. No prose, no code fence, "
            "no explanation. Use an empty string for any field you cannot determine."
        )

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
