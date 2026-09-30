from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from pulsara_agent import cli


@pytest.mark.parametrize("arguments", [
    ["host"],
    ["host", "run", "hello"],
    ["host", "repl"],
])
def test_removed_host_commands_are_rejected_before_runtime_start(
    arguments, monkeypatch, capsys,
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
