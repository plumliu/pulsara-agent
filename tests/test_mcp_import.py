import json

import pytest

from pulsara_agent.capability.mcp_import import read_mcp_import, materialize_mcp_import
from pulsara_agent.mcp_credentials import (
    McpCredentialOwner,
    secret_from_dict,
    resolve_secret,
)
from pulsara_agent.mcp_config import _parse_server


def test_loopback_http_import_requires_explicit_user_selection():
    (draft,) = read_mcp_import('{"url":"http://127.0.0.1:54130/mcp"}', server_id="local")
    owner = McpCredentialOwner("local", "user", "local")
    entry, _ = materialize_mcp_import(draft, owner, classifications={}, values={})
    with pytest.raises(ValueError):
        _parse_server("local", entry)
    entry, secrets = materialize_mcp_import(
        draft, owner, classifications={}, values={}, allow_http_localhost=True
    )
    assert _parse_server("local", entry).transport.allow_http_localhost
    assert not secrets
    with pytest.raises(ValueError, match="布尔"):
        materialize_mcp_import(draft, owner, classifications={}, values={}, allow_http_localhost="true")


def test_jsonc_opencode_command_array_env_and_comments_are_import_only():
    (draft,) = read_mcp_import("""{
      // Selected MCP, not another runtime discovery root.
      "mcp": {"docs": {"type": "local", "command": ["npx", "-y", "example"],
        "environment": {"TOKEN": "private-value", "URL": "https://example.org//literal"},
        "enabled": false, "timeout": 30000,},},
    }""")
    assert "private-value" not in repr(draft)
    assert "private-value" not in json.dumps(draft.preview())
    owner = McpCredentialOwner("local", "user", "docs")
    with pytest.raises(ValueError, match="确认"):
        materialize_mcp_import(draft, owner, classifications={}, values={})
    entry, changes = materialize_mcp_import(
        draft,
        owner,
        classifications={"env:TOKEN": "private", "env:URL": "public"},
        values={},
    )
    config = _parse_server("docs", entry)
    assert config.transport.command == "npx"
    assert config.transport.args == ("-y", "example")
    assert not config.enabled
    assert config.default_tool_timeout_ms == 30000
    assert config.transport.environment == (("URL", "https://example.org//literal"),)
    assert changes[0].value == "private-value"
    assert "private-value" not in json.dumps(entry)


def test_remote_sse_header_template_and_missing_input_never_send_placeholders(
    monkeypatch,
):
    (draft,) = read_mcp_import(
        json.dumps(
            {
                "mcpServers": {
                    "docs": {
                        "url": "https://${HOST:-example.org}/sse",
                        "type": "sse",
                        "headers": {"Authorization": "Bearer {env:DOCS_KEY}"},
                    }
                }
            }
        )
    )
    owner = McpCredentialOwner("local", "user", "docs")
    with pytest.raises(ValueError, match="常量"):
        materialize_mcp_import(draft, owner, classifications={}, values={})
    entry, secrets = materialize_mcp_import(
        draft,
        owner,
        classifications={"header:Authorization:template_literals": "public"},
        values={},
    )
    assert not secrets
    assert entry["transport"] == {"type": "sse", "endpoint": "https://example.org/sse"}
    monkeypatch.setenv("DOCS_KEY", "runtime-value")
    assert (
        resolve_secret(secret_from_dict(entry["auth"]["headers"]["Authorization"]))
        == "Bearer runtime-value"
    )
    assert "runtime-value" not in json.dumps(entry)


def test_oauth_aliases_private_client_secret_and_precise_unknown_field():
    (draft,) = read_mcp_import(
        '{"url":"https://example.org/mcp","oauth":{"clientId":"public-id","clientSecret":"private-client","scope":"read"}}',
        server_id="docs",
    )
    entry, changes = materialize_mcp_import(
        draft,
        McpCredentialOwner("local", "user", "docs"),
        classifications={},
        values={},
    )
    assert entry["auth"]["client_id"] == "public-id"
    assert entry["auth"]["scope"] == "read"
    assert changes[0].value == "private-client"
    assert "private-client" not in json.dumps(entry)
    (unsupported,) = read_mcp_import(
        '{"mcpServers":{"docs":{"url":"https://example.org","headersHelper":"run-secret-command"}}}'
    )
    assert unsupported.issues[0].path == "headersHelper"
    assert "run-secret-command" not in json.dumps(unsupported.preview())
    with pytest.raises(ValueError, match="处理"):
        materialize_mcp_import(
            unsupported,
            McpCredentialOwner("local", "user", "docs"),
            classifications={},
            values={},
        )


@pytest.mark.parametrize(
    "text",
    [
        '{"mcpServers":{},"mcpServers":{}}',
        '{"mcpServers":{"x":{"url":"one","url":"two"}}}',
        '{"mcpServers":{},"mcp":{}}',
    ],
)
def test_ambiguous_or_duplicate_import_never_picks_a_winner(text):
    with pytest.raises(ValueError):
        read_mcp_import(text)


def test_invalid_template_is_inspectable_but_not_installable():
    (draft,) = read_mcp_import('{"mcpServers":{"x":{"url":"${A:-${B}}"}}}')
    assert draft.preview()["issues"]
    with pytest.raises(ValueError):
        materialize_mcp_import(
            draft,
            McpCredentialOwner("local", "user", "x"),
            classifications={},
            values={},
        )


def test_file_reference_never_reads_disk_and_requires_explicit_private_input(tmp_path):
    path = tmp_path / 'secret.txt'
    path.write_text('must-not-read-automatically')
    (draft,) = read_mcp_import(json.dumps({'mcpServers': {'docs': {
        'url': 'https://example.org/mcp', 'headers': {'Authorization': 'Bearer {file:' + str(path) + '}'},
    }}}))
    assert not draft.issues
    variable = draft.preview()['fields'][1]['variables'][0]
    assert variable['file_path'] == str(path)
    owner = McpCredentialOwner('local', 'user', 'docs')
    with pytest.raises(ValueError, match='请填写输入'):
        materialize_mcp_import(draft, owner, classifications={}, values={})
    entry, changes = materialize_mcp_import(
        draft, owner, classifications={}, values={'file:' + str(path): 'explicitly-selected-value'},
    )
    assert changes[0].value == 'Bearer explicitly-selected-value'
    assert 'explicitly-selected-value' not in json.dumps(entry)
    assert 'must-not-read-automatically' not in repr(changes)


def test_explicit_empty_list_and_unrelated_opencode_plugin_do_not_import_components():
    assert read_mcp_import('{"mcp":{},"plugin":["unsupported-js-plugin"]}') == ()


@pytest.mark.parametrize("entry", [
    {"url": "https://example.org/mcp"},
    {"transport": "stdio", "command": "mcp-server"},
    {"transport": {"type": "http", "endpoint": "https://example.org"}},
    {"transport": {"type": "streamable_http", "url": "https://example.org"}},
    {"transport": {"type": "stdio", "command": "mcp-server"}, "tool_timeout_ms": 30000},
])
def test_native_parser_no_longer_dual_reads_external_or_old_flat_shapes(entry):
    with pytest.raises(ValueError):
        _parse_server("docs", entry)
