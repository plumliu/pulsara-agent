import asyncio

import pytest

from pulsara_agent.runtime.permission import (
    AllowAllPermissionGate,
    ApprovalPolicy,
    DEFAULT_PERMISSION_MODE,
    EffectivePermissionPolicy,
    PermissionDecisionKind,
    PermissionMode,
    PermissionProfile,
    PolicyPermissionGate,
    TerminalAccess,
    default_permission_policy,
    parse_permission_mode,
    preset_to_policy,
    resolve_permission_policy,
)
from pulsara_agent.tools.base import ToolCall


def test_default_permission_policy_defaults_to_bypass() -> None:
    policy = default_permission_policy()

    assert policy.profile is PermissionProfile.TRUSTED_HOST
    assert policy.approval is ApprovalPolicy.NEVER
    assert policy.terminal is TerminalAccess.ALLOW
    assert policy.execution_boundary == "host"
    assert policy.network_isolated is False


def test_default_permission_policy_run_intent_matches_default_mode() -> None:
    assert default_permission_policy() == preset_to_policy(DEFAULT_PERMISSION_MODE)
    assert DEFAULT_PERMISSION_MODE is PermissionMode.BYPASS_PERMISSIONS


def test_default_permission_policy_keeps_inspect_read_only() -> None:
    policy = default_permission_policy(intent="inspect")

    assert policy.profile is PermissionProfile.READ_ONLY
    assert policy.approval is ApprovalPolicy.ON_REQUEST
    assert policy.terminal is TerminalAccess.OFF


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("read-only", (PermissionProfile.READ_ONLY, ApprovalPolicy.ON_REQUEST, TerminalAccess.OFF)),
        ("ask-permissions", (PermissionProfile.TRUSTED_HOST, ApprovalPolicy.ON_REQUEST, TerminalAccess.ASK)),
        ("accept-edits", (PermissionProfile.TRUSTED_HOST, ApprovalPolicy.NEVER, TerminalAccess.ASK)),
        ("bypass-permissions", (PermissionProfile.TRUSTED_HOST, ApprovalPolicy.NEVER, TerminalAccess.ALLOW)),
    ],
)
def test_preset_to_policy_resolves_contract_triples(mode, expected) -> None:
    policy = preset_to_policy(mode)

    assert (policy.profile, policy.approval, policy.terminal) == expected


def test_parse_permission_mode_accepts_all_named_presets() -> None:
    for mode in PermissionMode:
        assert parse_permission_mode(mode.value) is mode
        assert parse_permission_mode(mode) is mode


def test_parse_permission_mode_rejects_unknown_value() -> None:
    with pytest.raises(ValueError):
        parse_permission_mode("paranoid")


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


def test_bypass_preset_still_denies_hardline_terminal_command() -> None:
    # Contract §5: bypass-permissions means "no approval", NOT "no protection".
    # The hardline floor applies to the preset entry too.
    gate = PolicyPermissionGate(
        preset_to_policy(PermissionMode.BYPASS_PERMISSIONS),
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


# --- Step 3: default-inference cleanup (PERMISSION_POLICY_CONTRACT §6) --------
# risky_only / workspace_guarded are no longer inferred by default. They remain
# valid ONLY as explicitly-passed custom axis values.


def test_profile_default_no_longer_infers_risky_only() -> None:
    # Bare custom profile (no approval/terminal) must fall back to bypass
    # defaults (never/allow), not the old risky_only inference.
    policy = resolve_permission_policy(profile="trusted_host", env={})

    assert policy.profile is PermissionProfile.TRUSTED_HOST
    assert policy.approval is ApprovalPolicy.NEVER
    assert policy.terminal is TerminalAccess.ALLOW


def test_profile_default_workspace_guarded_bare_falls_back_to_bypass() -> None:
    # workspace_guarded is no longer a special default branch; bare it resolves
    # to the bypass default while keeping the explicitly-named profile.
    policy = resolve_permission_policy(profile="workspace_guarded", env={})

    assert policy.profile is PermissionProfile.WORKSPACE_GUARDED
    assert policy.approval is ApprovalPolicy.NEVER
    assert policy.terminal is TerminalAccess.ALLOW


def test_workspace_guarded_and_risky_only_still_valid_as_explicit_axes() -> None:
    # The vocabulary is preserved: explicitly passing the demoted axis values
    # still constructs a valid policy (custom three-axis feature, §7).
    policy = resolve_permission_policy(
        profile="workspace_guarded",
        approval="risky_only",
        terminal="off",
        env={},
    )

    assert policy.profile is PermissionProfile.WORKSPACE_GUARDED
    assert policy.approval is ApprovalPolicy.RISKY_ONLY
    assert policy.terminal is TerminalAccess.OFF


def test_read_only_profile_default_keeps_terminal_off() -> None:
    policy = resolve_permission_policy(profile="read_only", env={})

    assert policy.profile is PermissionProfile.READ_ONLY
    assert policy.terminal is TerminalAccess.OFF


def test_resolve_permission_policy_has_no_workspace_kind_parameter() -> None:
    import inspect

    params = inspect.signature(resolve_permission_policy).parameters
    assert "workspace_kind" not in params


# --- Step 3: hardline cross-entry enforcement (CONTRACT §5/§9) ---------------
# The hardline judgment is a single reused function; it is enforced
# independently at three layers so no terminal entry can bypass it:
#   1. PolicyPermissionGate (before approval)
#   2. TerminalExecPolicy   (before spawn)
#   3. TerminalProcessTool  (before write/submit)

_HARDLINE_COMMAND = "rm -rf /"
_BENIGN_COMMAND = "printf ok"


@pytest.mark.parametrize("mode", list(PermissionMode))
def test_hardline_terminal_denied_under_every_preset(mode: PermissionMode) -> None:
    # read-only blocks terminal as "not allowed by policy"; the mutating presets
    # block the same command via the hardline floor. Either way: never ALLOW.
    gate = PolicyPermissionGate(preset_to_policy(mode), inner=AllowAllPermissionGate())

    decision = asyncio.run(
        gate.evaluate([ToolCall(id="call:term", name="terminal", arguments={"command": _HARDLINE_COMMAND})])
    )

    assert decision.kind is PermissionDecisionKind.DENY


@pytest.mark.parametrize("mode", [PermissionMode.ASK_PERMISSIONS, PermissionMode.ACCEPT_EDITS, PermissionMode.BYPASS_PERMISSIONS])
def test_hardline_terminal_process_input_denied_under_mutating_presets(mode: PermissionMode) -> None:
    gate = PolicyPermissionGate(preset_to_policy(mode), inner=AllowAllPermissionGate())

    decision = asyncio.run(
        gate.evaluate(
            [
                ToolCall(
                    id="call:process",
                    name="terminal_process",
                    arguments={"action": "write", "process_id": "terminal-process:fake", "data": _HARDLINE_COMMAND},
                )
            ]
        )
    )

    assert decision.kind is PermissionDecisionKind.DENY
    assert decision.suggested_rules[0]["reason"] == "hardline_terminal_process_input"


def test_exec_policy_blocks_hardline_independently_of_gate(tmp_path) -> None:
    # The spawn-boundary layer blocks hardline on its own, without any gate.
    from pulsara_agent.runtime.terminal.models import TerminalRequest
    from pulsara_agent.runtime.terminal.policy import ExecPolicyDecisionKind, TerminalExecPolicy

    policy = TerminalExecPolicy(tmp_path)

    blocked = policy.evaluate(TerminalRequest(command=_HARDLINE_COMMAND), current_cwd=tmp_path)
    allowed = policy.evaluate(TerminalRequest(command=_BENIGN_COMMAND), current_cwd=tmp_path)

    assert blocked.kind is ExecPolicyDecisionKind.BLOCK
    assert blocked.code == "hardline_terminal_command"
    assert allowed.kind is ExecPolicyDecisionKind.ALLOW


def test_terminal_process_tool_blocks_hardline_input_independently_of_gate(tmp_path) -> None:
    # The tool-execution layer blocks hardline stdin on its own, even after an
    # approval-resume path that does not re-run the gate.
    import json

    from pulsara_agent.tools.builtins.terminal_process import TerminalProcessTool

    tool = TerminalProcessTool(
        tmp_path,
        permission_policy=preset_to_policy(PermissionMode.BYPASS_PERMISSIONS),
    )

    result = tool.execute(
        ToolCall(
            id="call:process",
            name="terminal_process",
            arguments={"action": "submit", "process_id": "terminal-process:fake", "data": _HARDLINE_COMMAND},
        )
    )
    payload = json.loads(result.output)

    assert payload["status"] == "blocked"
    assert payload["policy_code"] == "hardline_terminal_process_input"


def test_all_three_layers_share_one_hardline_judgment(monkeypatch, tmp_path) -> None:
    # Defense-in-depth, single source of truth: all three enforcement layers
    # call the SAME is_hardline_terminal_command. Patch each layer's bound
    # reference to flip a benign command into "hardline" and confirm all three
    # deny it in lockstep (proving there is no second, divergent judgment).
    import json

    import pulsara_agent.runtime.permission as permission_mod
    import pulsara_agent.runtime.terminal.policy as policy_mod
    import pulsara_agent.tools.builtins.terminal_process as process_mod
    from pulsara_agent.runtime.terminal.models import TerminalRequest
    from pulsara_agent.runtime.terminal.policy import ExecPolicyDecisionKind, TerminalExecPolicy
    from pulsara_agent.tools.builtins.terminal_process import TerminalProcessTool

    sentinel = "totally_benign_marker_cmd"

    def fake_hardline(command: str) -> bool:
        return sentinel in command

    monkeypatch.setattr(permission_mod, "is_hardline_terminal_command", fake_hardline)
    monkeypatch.setattr(policy_mod, "is_hardline_terminal_command", fake_hardline)
    monkeypatch.setattr(process_mod, "is_hardline_terminal_command", fake_hardline)

    # Layer 1: gate
    gate = PolicyPermissionGate(preset_to_policy(PermissionMode.BYPASS_PERMISSIONS), inner=AllowAllPermissionGate())
    gate_decision = asyncio.run(
        gate.evaluate([ToolCall(id="call:t", name="terminal", arguments={"command": sentinel})])
    )
    # Layer 2: exec policy
    exec_decision = TerminalExecPolicy(tmp_path).evaluate(TerminalRequest(command=sentinel), current_cwd=tmp_path)
    # Layer 3: tool write
    tool = TerminalProcessTool(tmp_path, permission_policy=preset_to_policy(PermissionMode.BYPASS_PERMISSIONS))
    tool_payload = json.loads(
        tool.execute(
            ToolCall(
                id="call:p",
                name="terminal_process",
                arguments={"action": "write", "process_id": "terminal-process:fake", "data": sentinel},
            )
        ).output
    )

    assert gate_decision.kind is PermissionDecisionKind.DENY
    assert exec_decision.kind is ExecPolicyDecisionKind.BLOCK
    assert tool_payload["policy_code"] == "hardline_terminal_process_input"
