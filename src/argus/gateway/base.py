"""Provider-agnostic model gateway.

Exports:
    ModelSpec: which backend and model a pipeline stage uses.
    RawCompletion: what an adapter returns.
    LLMAdapter: the only contract an adapter must satisfy.
    Gateway: schema enforcement, retry, and audit logging around any adapter.

Design rules:
    * Adapters ONLY produce text. JSON extraction, jsonschema validation,
      repair retries, and audit records live here, once. (Ouroboros learned
      this the hard way: five drifting copies of response-format handling.)
    * Local validation always runs, even when a backend claims native
      structured output. Never trust the wire.
    * ``isolation`` is recorded per call: API adapters are
      ``payload`` (the model sees only what we send — isolation by
      construction); CLI agent adapters are ``process`` (weaker; the agent
      process could touch the filesystem). The audit trail must not let the
      two be confused.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import re
import time
from typing import Any, Protocol

import jsonschema

from argus.core import ProviderError, Result, SchemaViolation, sha256_text

TRANSIENT_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """One backend+model binding, assignable per pipeline stage.

    Attributes:
        provider: ``openai-compat`` | ``anthropic`` | ``codex-cli`` | ``claude-cli``.
        model: Backend-native model name.
        base_url: OpenAI-compatible endpoint root (vLLM/OpenRouter/Ollama/...).
        api_key_env: Environment variable holding the key. The key value itself
            is never stored in specs, manifests, or logs.
        temperature: Sampling temperature where the backend supports it.
        max_output_tokens: Completion budget.
        reasoning_effort: ``low``/``medium``/``high`` where supported.
    """

    provider: str
    model: str
    base_url: str | None = None
    api_key_env: str | None = None
    temperature: float = 0.2
    max_output_tokens: int = 16384
    reasoning_effort: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "api_key_env": self.api_key_env,
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
            "reasoning_effort": self.reasoning_effort,
        }


@dataclass(frozen=True, slots=True)
class RawCompletion:
    """Adapter output: text plus whatever accounting the backend reported."""

    text: str
    model: str
    usage: dict[str, Any] = field(default_factory=dict)
    isolation: str = "payload"


class LLMAdapter(Protocol):
    """The complete contract between the gateway and any backend."""

    def complete(
        self,
        prompt: str,
        spec: ModelSpec,
        images: Sequence[Path] = (),
        json_schema: dict[str, Any] | None = None,
    ) -> Result[RawCompletion, ProviderError]:
        """Produce one completion. Must not raise for expected failures."""
        ...


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json_payload(text: str) -> str:
    """Pull the JSON object out of a completion.

    Handles raw JSON, fenced blocks, and prose-wrapped objects, in that
    order of preference. Raises SchemaViolation when nothing parseable exists.
    """
    candidates = [text.strip()]
    candidates.extend(match.strip() for match in _FENCE_RE.findall(text))
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        if not candidate:
            continue
        try:
            json.loads(candidate)
        except json.JSONDecodeError:
            continue
        return candidate
    raise SchemaViolation("completion contains no parseable JSON object")


def schema_directive(schema: dict[str, Any]) -> str:
    """Cooperative half of structured output for backends without native support."""
    return (
        "\n\nOUTPUT CONTRACT\n"
        "Return exactly one JSON object and nothing else: no prose, no markdown fences.\n"
        "The object must validate against this JSON Schema (draft 2020-12):\n"
        f"{json.dumps(schema, ensure_ascii=False, sort_keys=True)}"
    )


@dataclass(frozen=True, slots=True)
class CallAudit:
    """What gets persisted for every gateway call."""

    name: str
    provider: str
    model: str
    prompt_sha256: str
    schema_sha256: str
    response_sha256: str
    isolation: str
    attempts: int
    duration_seconds: float
    usage: dict[str, Any]
    image_count: int


class Gateway:
    """Schema-enforced JSON completion over any adapter.

    Args:
        adapter: Backend implementation.
        log_root: Directory receiving per-call audit artifacts. Required —
            an unlogged model call is an unauditable model call.
        provider_attempts: Retries for transient provider failures.
        schema_attempts: Repair retries when output fails validation.
        backoff_initial: First backoff sleep in seconds (exponential, capped).
    """

    def __init__(
        self,
        adapter: LLMAdapter,
        log_root: Path,
        provider_attempts: int = 3,
        schema_attempts: int = 2,
        backoff_initial: float = 2.0,
        backoff_max: float = 30.0,
    ):
        self._adapter = adapter
        self._log_root = log_root
        self._provider_attempts = max(1, provider_attempts)
        self._schema_attempts = max(1, schema_attempts)
        self._backoff_initial = backoff_initial
        self._backoff_max = backoff_max

    def complete_json(
        self,
        name: str,
        prompt: str,
        schema: dict[str, Any],
        spec: ModelSpec,
        images: Sequence[Path] = (),
    ) -> dict[str, Any]:
        """Run one schema-validated JSON completion.

        Raises:
            ProviderError: backend failed after all transient retries.
            SchemaViolation: output never validated within schema_attempts.
        """
        validator = jsonschema.Draft202012Validator(schema)
        jsonschema.Draft202012Validator.check_schema(schema)
        full_prompt = prompt + schema_directive(schema)
        started = time.monotonic()
        attempts = 0
        last_violation: SchemaViolation | None = None
        current_prompt = full_prompt
        for schema_round in range(self._schema_attempts):
            completion = self._complete_with_retry(current_prompt, spec, images)
            attempts += 1
            try:
                payload = extract_json_payload(completion.text)
                value = json.loads(payload)
                errors = sorted(validator.iter_errors(value), key=lambda e: list(e.absolute_path))
                if errors:
                    first = errors[0]
                    raise SchemaViolation(
                        "completion failed schema validation",
                        {
                            "error": first.message,
                            "path": list(first.absolute_path),
                            "error_count": len(errors),
                        },
                    )
            except SchemaViolation as violation:
                last_violation = violation
                self._audit_failure(name, schema_round, completion, violation)
                # Repair round: same contract, plus the concrete validation error.
                current_prompt = (
                    full_prompt
                    + "\n\nPREVIOUS ATTEMPT WAS REJECTED\n"
                    + f"Validation error: {violation}\n"
                    + "Return the corrected JSON object only."
                )
                continue
            self._audit_success(
                name,
                spec,
                prompt,
                schema,
                completion,
                attempts,
                time.monotonic() - started,
                len(images),
            )
            return value
        raise SchemaViolation(
            f"call {name} failed schema validation after {self._schema_attempts} attempts",
            {"last_error": str(last_violation)},
        )

    def _complete_with_retry(
        self, prompt: str, spec: ModelSpec, images: Sequence[Path]
    ) -> RawCompletion:
        delay = self._backoff_initial
        last_error: ProviderError | None = None
        for attempt in range(self._provider_attempts):
            result = self._adapter.complete(prompt, spec, images=images)
            if result.is_ok:
                return result.unwrap()
            last_error = result.error
            if not last_error.details.get("transient", False):
                raise last_error
            if attempt < self._provider_attempts - 1:
                time.sleep(min(delay, self._backoff_max))
                delay *= 2
        assert last_error is not None
        raise last_error

    def _audit_success(
        self,
        name: str,
        spec: ModelSpec,
        prompt: str,
        schema: dict[str, Any],
        completion: RawCompletion,
        attempts: int,
        duration: float,
        image_count: int,
    ) -> None:
        audit = CallAudit(
            name=name,
            provider=spec.provider,
            model=completion.model or spec.model,
            prompt_sha256=sha256_text(prompt),
            schema_sha256=sha256_text(json.dumps(schema, sort_keys=True)),
            response_sha256=sha256_text(completion.text),
            isolation=completion.isolation,
            attempts=attempts,
            duration_seconds=round(duration, 3),
            usage=completion.usage,
            image_count=image_count,
        )
        self._log_root.mkdir(parents=True, exist_ok=True)
        (self._log_root / f"{name}.audit.json").write_text(
            json.dumps(asdict(audit), indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        (self._log_root / f"{name}.response.txt").write_text(completion.text, encoding="utf-8")

    def _audit_failure(
        self, name: str, schema_round: int, completion: RawCompletion, violation: SchemaViolation
    ) -> None:
        self._log_root.mkdir(parents=True, exist_ok=True)
        (self._log_root / f"{name}.rejected-{schema_round}.txt").write_text(
            f"# {violation}\n\n{completion.text}", encoding="utf-8"
        )


def classify_transient(status_code: int | None, message: str) -> bool:
    """Shared transient-failure heuristic for HTTP adapters."""
    if status_code in TRANSIENT_STATUS_CODES:
        return True
    lowered = message.lower()
    return any(
        marker in lowered
        for marker in ("timeout", "timed out", "connection", "temporarily", "overloaded")
    )
