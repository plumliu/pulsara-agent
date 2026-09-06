from __future__ import annotations

import asyncio
import json

import pytest

from pulsara_agent.mcp_credentials import (
    BoundSecretValue,
    EnvironmentVariableReference,
    ManagedLocalCredentialReference,
    McpCredentialBinding,
    McpCredentialMissing,
    McpCredentialOwner,
    LocalMcpOAuthRecord,
    resolve_secret,
    secret_from_dict,
    secret_to_dict,
)
from pulsara_agent.settings import (
    LocalSettingsStore,
    LocalPostgresConfig,
    local_settings_to_dict,
)
from pulsara_agent.mcp_config import _parse_server
from pulsara_agent.conversation_kernel.mcp.sdk_facade import (
    _BoundedTransport,
    McpProtocolConformanceError,
)
from pulsara_agent.conversation_kernel.mcp.wire import DEFAULT_MCP_WIRE_BOUNDS


def test_references_are_closed_and_missing_never_anonymous(monkeypatch):
    with pytest.raises(ValueError):
        EnvironmentVariableReference("PULSARA_API_KEY")
    with pytest.raises(ValueError):
        secret_from_dict("literal-secret")
    monkeypatch.delenv("MCP_TEST_SECRET", raising=False)
    ref = EnvironmentVariableReference("MCP_TEST_SECRET")
    with pytest.raises(McpCredentialMissing):
        resolve_secret(ref)
    monkeypatch.setenv("MCP_TEST_SECRET", "test-value")
    template = BoundSecretValue(("Bearer ", ref, " suffix"))
    assert secret_from_dict(secret_to_dict(template)) == template
    assert resolve_secret(template) == "Bearer test-value suffix"
    with pytest.raises(ValueError):
        secret_from_dict({"parts": ["literal-only"]})


def test_mcp_settings_preserve_other_fields_and_exact_owner(tmp_path):
    async def scenario():
        store = LocalSettingsStore(tmp_path / "private" / "local-settings.yaml")
        owner = McpCredentialOwner("local", "USER", "test")
        other = McpCredentialOwner("local", "workspace:test", "test")
        binding = McpCredentialBinding(owner, "bearer")
        second = McpCredentialBinding(other, "bearer")
        await store.replace_mcp_secrets(owner, ((binding, "private-test-token"),))
        await store.replace_mcp_secrets(other, ((second, "other-token"),))
        await store.save_postgres(LocalPostgresConfig("postgresql://localhost/test"))
        await store.save_dashscope_api_key("embedding", "embedding-token")
        await store.save_dashscope_api_key("rerank", "rerank-token")
        await store.delete_dashscope_api_key("embedding")
        assert store.resolve_mcp_secret(binding) == "private-test-token"
        assert "private-test-token" not in repr(store.read())
        record = LocalMcpOAuthRecord(
            owner,
            "https://example.org/mcp",
            "https://example.org",
            "test-client",
            "read",
            json.dumps({"access_token": "oauth-token"}),
            "{}",
        )
        await store.replace_mcp_authorization(owner, record, expected=None)
        with pytest.raises(ValueError, match="changed"):
            await store.replace_mcp_authorization(owner, record, expected=None)
        await store.remove_mcp_credentials(owner)
        assert store.resolve_mcp_secret(binding) is None
        assert store.resolve_mcp_secret(second) == "other-token"
        assert store.read().mcp_authorization(owner) is None
        assert store.read().postgres.runtime_dsn == "postgresql://localhost/test"
        assert store.read().dashscope_api_key("rerank") == "rerank-token"
        assert "oauth-token" not in json.dumps(local_settings_to_dict(store.read()))
        with pytest.raises(ValueError, match="crosses"):
            await store.replace_mcp_secrets(owner, ((second, "wrong"),))

    asyncio.run(scenario())


def test_managed_rotation_reconnect_identity_but_not_semantics():
    binding = McpCredentialBinding(
        McpCredentialOwner("local", "USER", "test"), "bearer"
    )
    ref = ManagedLocalCredentialReference(binding)
    entry = {
        "transport": {"type": "streamable_http", "endpoint": "https://example.org/mcp"},
        "auth": {"type": "bearer", "reference": secret_to_dict(ref)},
    }
    first = _parse_server("test", entry, secret_resolver=lambda b: "first-token")
    second = _parse_server("test", entry, secret_resolver=lambda b: "second-token")
    assert first.semantic_config_fingerprint == second.semantic_config_fingerprint
    assert first.runtime_config_fingerprint != second.runtime_config_fingerprint
    assert first.resolved_headers() == {"Authorization": "Bearer first-token"}
    assert "first-token" not in repr(first)
    with pytest.raises(ValueError):
        _parse_server("test", {**entry, "public_headers": {"Host": "other.example"}})


def test_decoded_secret_text_is_scrubbed_but_identity_is_not_rewritten():
    transport = _BoundedTransport(DEFAULT_MCP_WIRE_BOUNDS)
    transport._secret_values = ("private-test-token",)
    raw = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": "echo private-test-token"}]},
    }
    parsed = transport._parse(json.dumps(raw).encode(), maximum_bytes=4096)
    assert parsed["result"]["content"][0]["text"] == "echo [credential removed]"
    for invalid in (
        {"name": "private-test-token"},
        {"private-test-token": "data"},
        {"inputSchema": {"description": "private-test-token"}},
    ):
        with pytest.raises(McpProtocolConformanceError):
            transport._parse(json.dumps(invalid).encode(), maximum_bytes=4096)
