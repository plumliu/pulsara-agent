import asyncio

import pytest

from pulsara_agent.runtime.permission import (
    AllowAllPermissionGate,
    ApprovalPolicy,
    EffectivePermissionPolicy,
    PermissionDecisionKind,
    PermissionProfile,
    PolicyPermissionGate,
    TerminalAccess,
    default_permission_policy,
    resolve_permission_policy,
)
from pulsara_agent.tools.base import ToolCall


def test_default_permission_policy_without_workspace_kind_is_read_only() -> None:
    policy = default_permission_policy()

    assert policy.profile is PermissionProfile.READ_ONLY
    assert policy.approval is ApprovalPolicy.ON_REQUEST
    assert policy.terminal is TerminalAccess.OFF


def test_default_permission_policy_treats_project_as_trusted_host() -> None:
    policy = default_permission_policy(workspace_kind="project")

    assert policy.profile is PermissionProfile.TRUSTED_HOST
    assert policy.approval is ApprovalPolicy.RISKY_ONLY
    assert policy.terminal is TerminalAccess.ALLOW
    assert policy.execution_boundary == "host"
    assert policy.network_isolated is False


def test_default_permission_policy_keeps_inspect_read_only() -> None:
    policy = default_permission_policy(workspace_kind="project", intent="inspect")

    assert policy.profile is PermissionProfile.READ_ONLY
    assert policy.approval is ApprovalPolicy.ON_REQUEST
    assert policy.terminal is TerminalAccess.OFF


def test_resolve_permission_policy_uses_cli_over_env() -> None:
    policy = resolve_permission_policy(
        env={
            "PULSARA_PERMISSION_PROFILE": "read_only",
            "PULSARA_APPROVAL_POLICY": "on_request",
            "PULSARA_TERMINAL_ACCESS": "off",
        },
        profile="trusted_host",
        approval="never",
        terminal="allow",
    )

    assert policy.profile is PermissionProfile.TRUSTED_HOST
    assert policy.approval is ApprovalPolicy.NEVER
    assert policy.terminal is TerminalAccess.ALLOW


@pytest.mark.parametrize("terminal", ["ask", "allow"])
def test_resolve_permission_policy_rejects_read_only_with_terminal(terminal: str) -> None:
    with pytest.raises(ValueError, match="read_only"):
        resolve_permission_policy(profile="read_only", terminal=terminal, env={})


@pytest.mark.parametrize("profile", ["trusted_host", "workspace_guarded"])
@pytest.mark.parametrize("approval", ["never", "risky_only", "on_request"])
@pytest.mark.parametrize("terminal", ["off", "allow", "ask"])
def test_resolve_permission_policy_accepts_non_read_only_cross_product(
    profile: str,
    approval: str,
    terminal: str,
) -> None:
    policy = resolve_permission_policy(profile=profile, approval=approval, terminal=terminal, env={})

    assert policy.profile.value == profile
    assert policy.approval.value == approval
    assert policy.terminal.value == terminal


@pytest.mark.parametrize("approval", ["never", "risky_only", "on_request"])
def test_resolve_permission_policy_accepts_read_only_with_terminal_off(approval: str) -> None:
    policy = resolve_permission_policy(profile="read_only", approval=approval, terminal="off", env={})

    assert policy.profile is PermissionProfile.READ_ONLY
    assert policy.approval.value == approval
    assert policy.terminal is TerminalAccess.OFF


def test_resolve_permission_policy_env_can_select_ask() -> None:
    policy = resolve_permission_policy(
        profile="trusted_host",
        env={"PULSARA_TERMINAL_ACCESS": "ask"},
    )

    assert policy.profile is PermissionProfile.TRUSTED_HOST
    assert policy.terminal is TerminalAccess.ASK


def test_resolve_permission_policy_cli_overrides_env_for_ask_and_on_request() -> None:
    policy = resolve_permission_policy(
        env={
            "PULSARA_APPROVAL_POLICY": "never",
            "PULSARA_TERMINAL_ACCESS": "off",
        },
        profile="trusted_host",
        approval="on_request",
        terminal="ask",
    )

    assert policy.approval is ApprovalPolicy.ON_REQUEST
    assert policy.terminal is TerminalAccess.ASK


def test_policy_gate_denies_tools_hidden_by_terminal_off() -> None:
    gate = PolicyPermissionGate(
        EffectivePermissionPolicy(
            profile=PermissionProfile.WORKSPACE_GUARDED,
            approval=ApprovalPolicy.RISKY_ONLY,
            terminal=TerminalAccess.OFF,
        ),
        inner=AllowAllPermissionGate(),
    )

    decision = asyncio.run(gate.evaluate([ToolCall(id="call:term", name="terminal", arguments={"command": "pwd"})]))

    assert decision.kind is PermissionDecisionKind.DENY
    assert "terminal" in (decision.reason or "")


def test_policy_gate_risky_terminal_command_waits_for_user() -> None:
    gate = PolicyPermissionGate(
        EffectivePermissionPolicy(
            profile=PermissionProfile.TRUSTED_HOST,
            approval=ApprovalPolicy.RISKY_ONLY,
            terminal=TerminalAccess.ALLOW,
        ),
        inner=AllowAllPermissionGate(),
    )

    decision = asyncio.run(
        gate.evaluate([ToolCall(id="call:term", name="terminal", arguments={"command": "rm -rf build"})])
    )

    assert decision.kind is PermissionDecisionKind.WAIT_FOR_USER
    assert decision.suggested_rules[0]["reason"] == "dangerous_terminal_command"


def test_policy_gate_never_allows_non_hardline_risky_terminal_command() -> None:
    gate = PolicyPermissionGate(
        EffectivePermissionPolicy(
            profile=PermissionProfile.TRUSTED_HOST,
            approval=ApprovalPolicy.NEVER,
            terminal=TerminalAccess.ALLOW,
        ),
        inner=AllowAllPermissionGate(),
    )

    decision = asyncio.run(
        gate.evaluate([ToolCall(id="call:term", name="terminal", arguments={"command": "rm -rf build"})])
    )

    assert decision.kind is PermissionDecisionKind.ALLOW


def test_policy_gate_ask_overrides_never_for_terminal() -> None:
    gate = PolicyPermissionGate(
        EffectivePermissionPolicy(
            profile=PermissionProfile.TRUSTED_HOST,
            approval=ApprovalPolicy.NEVER,
            terminal=TerminalAccess.ASK,
        ),
        inner=AllowAllPermissionGate(),
    )

    decision = asyncio.run(
        gate.evaluate([ToolCall(id="call:term", name="terminal", arguments={"command": "printf ok"})])
    )

    assert decision.kind is PermissionDecisionKind.WAIT_FOR_USER
    assert decision.suggested_rules[0]["reason"] == "terminal_access_ask"


def test_policy_gate_hardline_terminal_command_denies_even_when_never() -> None:
    gate = PolicyPermissionGate(
        EffectivePermissionPolicy(
            profile=PermissionProfile.TRUSTED_HOST,
            approval=ApprovalPolicy.NEVER,
            terminal=TerminalAccess.ALLOW,
        ),
        inner=AllowAllPermissionGate(),
    )

    decision = asyncio.run(
        gate.evaluate([ToolCall(id="call:term", name="terminal", arguments={"command": "rm -rf /"})])
    )

    assert decision.kind is PermissionDecisionKind.DENY
    assert decision.suggested_rules[0]["reason"] == "hardline_terminal_command"


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /*",
        "rm -rf ~",
        "rm -rf $HOME",
        "rm -rf /home",
        "rm -rf /home/",
        "rm -rf /home/*",
        "rm -fr /etc",
        "rm -fr /usr/",
        "rm -fr /var/*",
        "dd if=/dev/zero of=/dev/nvme0n1 bs=1m",
        "dd if=/dev/zero of=/dev/vda bs=1m",
        "dd if=/dev/zero of=/dev/mmcblk0 bs=1m",
        "dd if=/dev/zero of=/dev/hda bs=1m",
    ],
)
def test_policy_gate_hardline_terminal_variants_deny_even_when_never(command: str) -> None:
    gate = PolicyPermissionGate(
        EffectivePermissionPolicy(
            profile=PermissionProfile.TRUSTED_HOST,
            approval=ApprovalPolicy.NEVER,
            terminal=TerminalAccess.ALLOW,
        ),
        inner=AllowAllPermissionGate(),
    )

    decision = asyncio.run(
        gate.evaluate([ToolCall(id="call:term", name="terminal", arguments={"command": command})])
    )

    assert decision.kind is PermissionDecisionKind.DENY
    assert decision.suggested_rules[0]["reason"] == "hardline_terminal_command"


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /home/alice/project/build",
        "rm -rf /usr/local/bin/oldtool",
        "rm -rf /var/folders/zz/T/tmp123",
        "rm -rf /var/log/myapp",
        "rm -rf /etc/nginx/sites-enabled/old",
    ],
)
def test_policy_gate_never_allows_nested_absolute_rm_paths(command: str) -> None:
    gate = PolicyPermissionGate(
        EffectivePermissionPolicy(
            profile=PermissionProfile.TRUSTED_HOST,
            approval=ApprovalPolicy.NEVER,
            terminal=TerminalAccess.ALLOW,
        ),
        inner=AllowAllPermissionGate(),
    )

    decision = asyncio.run(
        gate.evaluate([ToolCall(id="call:term", name="terminal", arguments={"command": command})])
    )

    assert decision.kind is PermissionDecisionKind.ALLOW


@pytest.mark.parametrize("action", ["write", "submit"])
def test_policy_gate_hardline_terminal_process_input_denies_even_when_never(action: str) -> None:
    gate = PolicyPermissionGate(
        EffectivePermissionPolicy(
            profile=PermissionProfile.TRUSTED_HOST,
            approval=ApprovalPolicy.NEVER,
            terminal=TerminalAccess.ALLOW,
        ),
        inner=AllowAllPermissionGate(),
    )

    decision = asyncio.run(
        gate.evaluate(
            [
                ToolCall(
                    id="call:process",
                    name="terminal_process",
                    arguments={
                        "action": action,
                        "process_id": "terminal-process:fake",
                        "data": "rm -rf /",
                    },
                )
            ]
        )
    )

    assert decision.kind is PermissionDecisionKind.DENY
    assert decision.suggested_rules[0]["reason"] == "hardline_terminal_process_input"


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("write_file", {"path": "x", "content": "y"}),
        ("edit_file", {"path": "x", "old": "a", "new": "b"}),
    ],
)
def test_policy_gate_on_request_file_write_tools_wait_for_user(tool_name: str, arguments: dict) -> None:
    gate = PolicyPermissionGate(
        EffectivePermissionPolicy(
            profile=PermissionProfile.WORKSPACE_GUARDED,
            approval=ApprovalPolicy.ON_REQUEST,
            terminal=TerminalAccess.OFF,
        ),
        inner=AllowAllPermissionGate(),
    )

    decision = asyncio.run(gate.evaluate([ToolCall(id=f"call:{tool_name}", name=tool_name, arguments=arguments)]))

    assert decision.kind is PermissionDecisionKind.WAIT_FOR_USER
    assert decision.suggested_rules[0] == {"tool": tool_name, "reason": "write_tool_on_request"}


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("terminal", {"command": "printf ok"}),
        ("terminal_process", {"action": "kill", "process_id": "terminal-process:fake"}),
    ],
)
def test_policy_gate_on_request_terminal_tools_wait_for_user(tool_name: str, arguments: dict) -> None:
    gate = PolicyPermissionGate(
        EffectivePermissionPolicy(
            profile=PermissionProfile.TRUSTED_HOST,
            approval=ApprovalPolicy.ON_REQUEST,
            terminal=TerminalAccess.ALLOW,
        ),
        inner=AllowAllPermissionGate(),
    )

    decision = asyncio.run(gate.evaluate([ToolCall(id=f"call:{tool_name}", name=tool_name, arguments=arguments)]))

    assert decision.kind is PermissionDecisionKind.WAIT_FOR_USER
    assert decision.suggested_rules[0] == {"tool": tool_name, "reason": "terminal_on_request"}


@pytest.mark.parametrize("action", ["list", "log", "poll", "wait"])
def test_policy_gate_terminal_process_read_only_actions_do_not_wait_under_ask_or_on_request(action: str) -> None:
    gate = PolicyPermissionGate(
        EffectivePermissionPolicy(
            profile=PermissionProfile.TRUSTED_HOST,
            approval=ApprovalPolicy.ON_REQUEST,
            terminal=TerminalAccess.ASK,
        ),
        inner=AllowAllPermissionGate(),
    )
    arguments = {"action": action, "process_id": "terminal-process:fake"}
    if action == "list":
        arguments.pop("process_id")

    decision = asyncio.run(
        gate.evaluate([ToolCall(id="call:process", name="terminal_process", arguments=arguments)])
    )

    assert decision.kind is PermissionDecisionKind.ALLOW


@pytest.mark.parametrize("action", ["kill", "write", "submit", "close_stdin"])
def test_policy_gate_terminal_access_ask_waits_for_side_effect_terminal_process_actions(action: str) -> None:
    gate = PolicyPermissionGate(
        EffectivePermissionPolicy(
            profile=PermissionProfile.TRUSTED_HOST,
            approval=ApprovalPolicy.NEVER,
            terminal=TerminalAccess.ASK,
        ),
        inner=AllowAllPermissionGate(),
    )
    arguments = {"action": action, "process_id": "terminal-process:fake"}
    if action in {"write", "submit"}:
        arguments["data"] = "printf ok"

    decision = asyncio.run(
        gate.evaluate([ToolCall(id="call:process", name="terminal_process", arguments=arguments)])
    )

    assert decision.kind is PermissionDecisionKind.WAIT_FOR_USER
    assert decision.suggested_rules[0] == {"tool": "terminal_process", "reason": "terminal_access_ask"}


def test_policy_gate_terminal_access_ask_takes_precedence_over_on_request() -> None:
    gate = PolicyPermissionGate(
        EffectivePermissionPolicy(
            profile=PermissionProfile.TRUSTED_HOST,
            approval=ApprovalPolicy.ON_REQUEST,
            terminal=TerminalAccess.ASK,
        ),
        inner=AllowAllPermissionGate(),
    )

    decision = asyncio.run(
        gate.evaluate([ToolCall(id="call:term", name="terminal", arguments={"command": "printf ok"})])
    )

    assert decision.kind is PermissionDecisionKind.WAIT_FOR_USER
    assert decision.suggested_rules[0] == {"tool": "terminal", "reason": "terminal_access_ask"}


def test_policy_gate_hardline_terminal_command_denies_under_ask() -> None:
    gate = PolicyPermissionGate(
        EffectivePermissionPolicy(
            profile=PermissionProfile.TRUSTED_HOST,
            approval=ApprovalPolicy.ON_REQUEST,
            terminal=TerminalAccess.ASK,
        ),
        inner=AllowAllPermissionGate(),
    )

    decision = asyncio.run(
        gate.evaluate([ToolCall(id="call:term", name="terminal", arguments={"command": "rm -rf /"})])
    )

    assert decision.kind is PermissionDecisionKind.DENY
    assert decision.suggested_rules[0]["reason"] == "hardline_terminal_command"


def test_policy_gate_hardline_terminal_process_input_denies_under_on_request() -> None:
    gate = PolicyPermissionGate(
        EffectivePermissionPolicy(
            profile=PermissionProfile.TRUSTED_HOST,
            approval=ApprovalPolicy.ON_REQUEST,
            terminal=TerminalAccess.ALLOW,
        ),
        inner=AllowAllPermissionGate(),
    )

    decision = asyncio.run(
        gate.evaluate(
            [
                ToolCall(
                    id="call:process",
                    name="terminal_process",
                    arguments={
                        "action": "submit",
                        "process_id": "terminal-process:fake",
                        "data": "rm -rf /",
                    },
                )
            ]
        )
    )

    assert decision.kind is PermissionDecisionKind.DENY
    assert decision.suggested_rules[0]["reason"] == "hardline_terminal_process_input"
