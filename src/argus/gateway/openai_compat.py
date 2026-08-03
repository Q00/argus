"""OpenAI-compatible chat-completions adapter.

Covers OpenAI, vLLM, OpenRouter, Ollama, and anything else speaking
``POST {base_url}/chat/completions``. Isolation is ``payload``: the model
receives exactly the prompt and images we send — nothing else exists.
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
import os
from pathlib import Path
from typing import Any

import httpx

from argus.core import ProviderError, Result
from argus.gateway.base import ModelSpec, RawCompletion, classify_transient

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_API_KEY_ENV = "OPENAI_API_KEY"


def _image_part(path: Path) -> dict[str, Any]:
    suffix = path.suffix.lower().lstrip(".") or "png"
    media = "jpeg" if suffix in {"jpg", "jpeg"} else suffix
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:image/{media};base64,{encoded}"},
    }


class OpenAICompatAdapter:
    """Adapter for any OpenAI-compatible endpoint.

    Args:
        timeout_seconds: End-to-end HTTP timeout per request.
    """

    def __init__(self, timeout_seconds: float = 600.0):
        self._timeout = timeout_seconds

    def complete(
        self,
        prompt: str,
        spec: ModelSpec,
        images: Sequence[Path] = (),
        json_schema: dict[str, Any] | None = None,
    ) -> Result[RawCompletion, ProviderError]:
        base_url = (spec.base_url or DEFAULT_BASE_URL).rstrip("/")
        key_env = spec.api_key_env or DEFAULT_API_KEY_ENV
        api_key = os.environ.get(key_env, "").strip()
        if not api_key:
            return Result.err(
                ProviderError(f"API key env {key_env} is unset", provider="openai-compat")
            )
        content: str | list[dict[str, Any]]
        if images:
            content = [{"type": "text", "text": prompt}]
            content.extend(_image_part(path) for path in images)
        else:
            content = prompt
        body: dict[str, Any] = {
            "model": spec.model,
            "messages": [{"role": "user", "content": content}],
            "temperature": spec.temperature,
            "max_tokens": spec.max_output_tokens,
        }
        if spec.reasoning_effort:
            body["reasoning_effort"] = spec.reasoning_effort
        try:
            response = httpx.post(
                f"{base_url}/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            return Result.err(
                ProviderError(
                    f"HTTP transport failure: {exc}",
                    provider="openai-compat",
                    details={"transient": True},
                )
            )
        if response.status_code != 200:
            return Result.err(
                ProviderError(
                    f"endpoint returned {response.status_code}",
                    provider="openai-compat",
                    details={
                        "status": response.status_code,
                        "body": response.text[:2000],
                        "transient": classify_transient(response.status_code, response.text),
                    },
                )
            )
        payload = response.json()
        try:
            text = payload["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            return Result.err(
                ProviderError(
                    "response missing choices[0].message.content",
                    provider="openai-compat",
                    details={"body": response.text[:2000]},
                )
            )
        return Result.of(
            RawCompletion(
                text=text,
                model=str(payload.get("model", spec.model)),
                usage=dict(payload.get("usage") or {}),
                isolation="payload",
            )
        )
