import json
from pathlib import Path

import pytest

from argus.core import ProviderError, Result, SchemaViolation
from argus.gateway.base import (
    Gateway,
    ModelSpec,
    RawCompletion,
    classify_transient,
    extract_json_payload,
)
from fakes import ScriptedAdapter

SPEC = ModelSpec(provider="openai-compat", model="test-model")
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer"],
    "properties": {"answer": {"type": "integer"}},
}


def _gateway(adapter, tmp_path: Path, **kwargs) -> Gateway:
    return Gateway(adapter, tmp_path / "logs", backoff_initial=0.0, **kwargs)


class TestExtractJsonPayload:
    def test_raw_json_passes_through(self):
        assert json.loads(extract_json_payload('{"a": 1}')) == {"a": 1}

    def test_fenced_json_is_unwrapped(self):
        assert json.loads(extract_json_payload('```json\n{"a": 1}\n```')) == {"a": 1}

    def test_prose_wrapped_json_is_found(self):
        assert json.loads(extract_json_payload('Here it is: {"a": 1} done.')) == {"a": 1}

    def test_no_json_raises(self):
        with pytest.raises(SchemaViolation):
            extract_json_payload("no json here")


class TestGateway:
    def test_valid_response_is_returned_and_audited(self, tmp_path: Path):
        adapter = ScriptedAdapter(['{"answer": 7}'])
        value = _gateway(adapter, tmp_path).complete_json("call", "prompt", SCHEMA, SPEC)
        assert value == {"answer": 7}
        audit = json.loads((tmp_path / "logs" / "call.audit.json").read_text())
        assert audit["attempts"] == 1
        assert audit["isolation"] == "payload"
        assert len(audit["prompt_sha256"]) == 64

    def test_schema_invalid_response_triggers_repair_retry(self, tmp_path: Path):
        adapter = ScriptedAdapter(['{"answer": "not an int"}', '{"answer": 7}'])
        value = _gateway(adapter, tmp_path).complete_json("call", "prompt", SCHEMA, SPEC)
        assert value == {"answer": 7}
        assert adapter.calls == 2
        assert (tmp_path / "logs" / "call.rejected-0.txt").is_file()

    def test_exhausted_schema_retries_raise(self, tmp_path: Path):
        adapter = ScriptedAdapter(["garbage", "more garbage"])
        with pytest.raises(SchemaViolation):
            _gateway(adapter, tmp_path, schema_attempts=2).complete_json(
                "call", "prompt", SCHEMA, SPEC
            )

    def test_transient_provider_error_is_retried(self, tmp_path: Path):
        transient = ProviderError("rate limited", provider="x", details={"transient": True})
        adapter = ScriptedAdapter([Result.err(transient), '{"answer": 1}'])
        value = _gateway(adapter, tmp_path).complete_json("call", "prompt", SCHEMA, SPEC)
        assert value == {"answer": 1}
        assert adapter.calls == 2

    def test_permanent_provider_error_raises_immediately(self, tmp_path: Path):
        permanent = ProviderError("bad key", provider="x")
        adapter = ScriptedAdapter([Result.err(permanent)])
        with pytest.raises(ProviderError):
            _gateway(adapter, tmp_path).complete_json("call", "prompt", SCHEMA, SPEC)
        assert adapter.calls == 1

    def test_process_isolation_tier_is_recorded(self, tmp_path: Path):
        adapter = ScriptedAdapter(
            [Result.of(RawCompletion(text='{"answer": 2}', model="cli", isolation="process"))]
        )
        _gateway(adapter, tmp_path).complete_json("call", "prompt", SCHEMA, SPEC)
        audit = json.loads((tmp_path / "logs" / "call.audit.json").read_text())
        assert audit["isolation"] == "process"


class TestClassifyTransient:
    def test_retriable_status_codes(self):
        assert classify_transient(429, "")
        assert classify_transient(503, "")
        assert not classify_transient(401, "")

    def test_message_markers(self):
        assert classify_transient(None, "connection reset by peer")
        assert not classify_transient(None, "invalid request")


class TestCodexProviderFlags:
    def test_env_base_url_and_key_inject_provider(self, monkeypatch):
        from argus.gateway.cli import _codex_provider_flags

        monkeypatch.setenv("OPENAI_BASE_URL", "https://proxy.example/v1")
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        flags = _codex_provider_flags(SPEC)
        assert 'model_provider="argus_env"' in flags
        assert any("proxy.example" in flag for flag in flags)

    def test_spec_endpoint_takes_precedence_over_env(self, monkeypatch):
        from argus.gateway.cli import _codex_provider_flags

        monkeypatch.setenv("OPENAI_BASE_URL", "https://env.example/v1")
        monkeypatch.setenv("MY_KEY", "k")
        spec = ModelSpec(
            provider="codex-cli",
            model="m",
            base_url="https://spec.example/v1",
            api_key_env="MY_KEY",
        )
        flags = _codex_provider_flags(spec)
        assert any("spec.example" in flag for flag in flags)
        assert any('"MY_KEY"' in flag for flag in flags)

    def test_missing_key_disables_injection(self, monkeypatch):
        from argus.gateway.cli import _codex_provider_flags

        monkeypatch.setenv("OPENAI_BASE_URL", "https://proxy.example/v1")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert _codex_provider_flags(SPEC) == []

    def test_no_endpoint_means_native_auth(self, monkeypatch):
        from argus.gateway.cli import _codex_provider_flags

        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        assert _codex_provider_flags(SPEC) == []
