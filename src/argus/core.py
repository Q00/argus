"""Core primitives shared by every argus package.

Exports:
    ArgusError / ProviderError / SchemaViolation / LedgerError / ExtractionError
    Result: expected-failure channel for the provider layer.
    stable_id / sha256_file / sha256_text: deterministic identity helpers.

Convention (borrowed from ouroboros): exceptions are for bugs and broken
invariants; ``Result`` is for failures the caller is expected to handle,
such as provider rate limits or schema-invalid model output.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from pathlib import Path
from typing import Any


class ArgusError(Exception):
    """Base error carrying structured details.

    Args:
        message: Human-readable description.
        details: Structured context folded into ``str(exc)``.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def __str__(self) -> str:
        if not self.details:
            return self.message
        rendered = ", ".join(f"{key}={value!r}" for key, value in sorted(self.details.items()))
        return f"{self.message} ({rendered})"


class ProviderError(ArgusError):
    """A model backend failed after retries were exhausted."""

    def __init__(
        self,
        message: str,
        provider: str,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message, {"provider": provider, **(details or {})})
        self.provider = provider


class SchemaViolation(ArgusError):
    """Model output failed local JSON-schema or exact-set validation."""


class LedgerError(ArgusError):
    """A ledger invariant was violated (duplicate resolution, backward stage, ...)."""


class ExtractionError(ArgusError):
    """The input package or PDF structure violated the input contract."""


@dataclass(frozen=True, slots=True)
class Result[T, E]:
    """Minimal ok/err container for expected failures.

    Use ``Result.of(value)`` / ``Result.err(error)``. ``unwrap()`` raises the
    contained error when it is an exception, otherwise wraps it in ArgusError.
    """

    _value: T | None = None
    _error: E | None = None
    _is_ok: bool = field(default=True)

    @staticmethod
    def of(value: T) -> "Result[T, E]":  # noqa: UP037
        return Result(_value=value, _is_ok=True)

    @staticmethod
    def err(error: E) -> "Result[T, E]":  # noqa: UP037
        return Result(_error=error, _is_ok=False)

    @property
    def is_ok(self) -> bool:
        return self._is_ok

    @property
    def is_err(self) -> bool:
        return not self._is_ok

    @property
    def error(self) -> E:
        if self._is_ok:
            raise ArgusError("Result.error accessed on an ok result")
        return self._error  # type: ignore[return-value]

    def unwrap(self) -> T:
        if self._is_ok:
            return self._value  # type: ignore[return-value]
        if isinstance(self._error, BaseException):
            raise self._error
        raise ArgusError(f"unwrapped an err result: {self._error!r}")


def stable_id(prefix: str, *parts: object) -> str:
    """Deterministic content-addressed ID: ``prefix_<sha256[:16]>``.

    The unit separator joins parts so ``("a", "bc")`` and ``("ab", "c")``
    hash differently.
    """
    payload = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(payload).hexdigest()[:16]}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
