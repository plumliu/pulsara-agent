from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from pulsara_agent import cli


@pytest.mark.parametrize(
    "arguments",
    [
        ["host"],
        ["host", "run", "hello"],
        ["host", "repl"],
    ],
)
def test_removed_host_commands_are_rejected_before_runtime_start(
    arguments,
    monkeypatch,
    capsys,
):
    def unexpected_runtime_start():
        raise AssertionError("removed CLI commands must not start the runtime")

    monkeypatch.setattr(cli, "_runtime_services", unexpected_runtime_start)
    monkeypatch.setattr("sys.argv", ["pulsara", *arguments])
    with pytest.raises(SystemExit) as rejected:
        cli.main()
    assert rejected.value.code == 2
    assert "invalid choice: 'host'" in capsys.readouterr().err


def test_public_cli_help_lists_app_and_administration_commands():
    help_text = cli.build_parser().format_help()
    assert "{app,skills,plugins,mcp,hooks,db,config-check}" in help_text


def test_app_command_dispatches_to_web_application(monkeypatch):
    launch = AsyncMock()
    monkeypatch.setattr(cli, "_local_web_app", launch)
    monkeypatch.setattr("sys.argv", ["pulsara", "app", "--port", "8765", "--no-open"])
    cli.main()
    launch.assert_awaited_once()
    arguments = launch.await_args.args[0]
    assert arguments.port == 8765
    assert arguments.no_open is True
    assert not hasattr(arguments, "workspace")


@pytest.mark.parametrize(
    "arguments",
    [
        ["skills", "install", "--scope", "workspace", "/absent/source"],
        ["plugins", "add", "--scope", "workspace", "/absent/source"],
        ["plugins", "disable", "--scope", "workspace", "example"],
        ["hooks", "disable", "--scope", "workspace"],
    ],
)
def test_project_admin_write_requires_explicit_directory_before_effects(
    arguments, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    args = cli.build_parser().parse_args(arguments)
    handler = {
        "skills": cli._skills_command,
        "plugins": cli._plugins_command,
        "hooks": cli._hooks_command,
    }[arguments[0]]
    with pytest.raises(ValueError, match="requires --workspace"):
        handler(args)
    assert not (tmp_path / ".pulsara").exists()


def test_admin_query_workspace_is_absent_without_explicit_target(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert cli._resolved_skill_workspace(None) is None
    assert cli._resolved_skill_workspace(str(tmp_path)) == tmp_path


@pytest.mark.parametrize(
    "arguments",
    [
        ["mcp", "reconnect", "example"],
        [
            "mcp",
            "add",
            "example",
            "--url",
            "https://example.org/mcp",
            "--scope",
            "ROOT_ONLY",
        ],
    ],
)
def test_removed_mcp_cli_contract_is_not_an_alias(arguments):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(arguments)


def test_mcp_tool_visibility_preserves_invocation_policy():
    args = cli.build_parser().parse_args(
        [
            "mcp",
            "add",
            "example",
            "--url",
            "https://example.org/mcp",
            "--tool-visibility",
            "ROOT_AND_SUBAGENTS",
        ]
    )
    assert args.tool_visibility == "ROOT_AND_SUBAGENTS" and not hasattr(args, "scope")
