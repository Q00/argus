"""Anthropic Messages API adapter.

Isolation is ``payload``: the model receives exactly what we send.
Structured output is enforced cooperatively by the gateway (directive +
local jsonschema validation), which keeps behavior identical across
backends instead of depending on per-provider features.
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

DEFAULT_BASE_URL = "https://api.anthropic.com"
DEFAULT_API_KEY_ENV = "ANTHROPIC_API_KEY"
API_VERSION = "2023-06-01"

_MEDIA_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}


def _image_block(path: Path) -> dict[str, Any]:
    suffix = path.suffix.lower().lstrip(".")
    media = _MEDIA_TYPES.get(suffix, "image/png")
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media,
            "data": base64.b64encode(path.read_bytes()).decode("ascii"),
        },
    }


class AnthropicAdapter:
    """Adapter for the Anthropic Messages API."""

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
                ProviderError(f"API key env {key_env} is unset", provider="anthropic")
            )
        blocks: list[dict[str, Any]] = [_image_block(path) for path in images]
        blocks.append({"type": "text", "text": prompt})
        body: dict[str, Any] = {
            "model": spec.model,
            "max_tokens": spec.max_output_tokens,
            "temperature": spec.temperature,
            "messages": [{"role": "user", "content": blocks}],
        }
        try:
            response = httpx.post(
                f"{base_url}/v1/messages",
                json=body,
                headers={"x-api-key": api_key, "anthropic-version": API_VERSION},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            return Result.err(
                ProviderError(
                    f"HTTP transport failure: {exc}",
                    provider="anthropic",
                    details={"transient": True},
                )
            )
        if response.status_code != 200:
            return Result.err(
                ProviderError(
                    f"endpoint returned {response.status_code}",
                    provider="anthropic",
                    details={
                        "status": response.status_code,
                        "body": response.text[:2000],
                        "transient": classify_transient(response.status_code, response.text),
                    },
                )
            )
        payload = response.json()
        text = "".join(
            block.get("text", "")
            for block in payload.get("content", [])
            if isinstance(block, dict) and block.get("type") == "text"
        )
        if not text:
            return Result.err(
                ProviderError(
                    "response contains no text blocks",
                    provider="anthropic",
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
