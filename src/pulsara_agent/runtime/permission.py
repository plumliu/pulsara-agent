"""Host-side permission policy for the main agent loop."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal, Mapping, Protocol

from pulsara_agent.capability.call_classifier import (
    CapabilityCallClassifier,
    DefaultCapabilityCallClassifier,
)
from pulsara_agent.capability.descriptor import CapabilityAvailability
from pulsara_agent.capability.exposure import CapabilityExposurePlan
from pulsara_agent.runtime.terminal_risk import (
    is_hardline_terminal_command,
    is_risky_terminal_command,
    is_sensitive_terminal_command,
)
from pulsara_agent.runtime.tool_taxonomy import FILE_WRITE_TOOL_NAMES, TERMINAL_TOOL_NAMES
from pulsara_agent.tools.base import ToolCall


class PermissionDecisionKind(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    WAIT_FOR_USER = "wait_for_user"


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    kind: PermissionDecisionKind
    reason: str | None = None
    suggested_rules: list[dict] = field(default_factory=list)

    @classmethod
    def allow(cls) -> "PermissionDecision":
        return cls(kind=PermissionDecisionKind.ALLOW)


class PermissionGate(Protocol):
    async def evaluate(self, calls: list[ToolCall]) -> PermissionDecision: ...


class AllowAllPermissionGate:
    async def evaluate(self, calls: list[ToolCall]) -> PermissionDecision:
        return PermissionDecision.allow()


class PermissionProfile(StrEnum):
    TRUSTED_HOST = "trusted_host"
    WORKSPACE_GUARDED = "workspace_guarded"
    READ_ONLY = "read_only"


class ApprovalPolicy(StrEnum):
    NEVER = "never"
    RISKY_ONLY = "risky_only"
    ON_REQUEST = "on_request"


class TerminalAccess(StrEnum):
    OFF = "off"
    ALLOW = "allow"
    ASK = "ask"


class PermissionMode(StrEnum):
    """Named permission presets. The main product path (see PERMISSION_POLICY_CONTRACT)."""

    READ_ONLY = "read-only"
    ASK_PERMISSIONS = "ask-permissions"
    ACCEPT_EDITS = "accept-edits"
    BYPASS_PERMISSIONS = "bypass-permissions"


DEFAULT_PERMISSION_MODE = PermissionMode.BYPASS_PERMISSIONS


@dataclass(frozen=True, slots=True)
class EffectivePermissionPolicy:
    profile: PermissionProfile
    approval: ApprovalPolicy
    terminal: TerminalAccess
    execution_boundary: Literal["host"] = "host"
    network_isolated: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "profile": self.profile.value,
            "approval_policy": self.approval.value,
            "terminal_access": self.terminal.value,
            "execution_boundary": self.execution_boundary,
            "network_isolated": self.network_isolated,
            "filesystem": {
                "read_file_scope": "host_local_text",
                "search_files_scope": "host_local_text_guarded_broad_roots",
                "write_file_scope": "workspace_only",
                "terminal": "host_shell" if self.terminal is not TerminalAccess.OFF else "off",
            },
        }


TERMINAL_PROCESS_READ_ONLY_ACTIONS = frozenset({"list", "log", "poll", "wait"})

# read-only profile allowlist (PERMISSION_POLICY_CONTRACT §3): only tools that
# cause no user-workspace write / terminal / durable-memory write side effect.
# read_file/search_files are host-local ordinary-text read capabilities, not
# workspace-only capabilities; this is an intentional read-only/plan-mode grant.
# read-only is fail-closed — anything not listed here is denied, so a new
# side-effecting tool is blocked under read-only by default. This set must stay
# in sync with the built-in tools whose is_read_only is True (enforced by a
# drift test). terminal_process observe actions (list/log/poll/wait) are NOT
# here yet — that action-level exemption is a separate refinement.
READ_ONLY_ALLOWED_TOOL_NAMES = frozenset(
    {
        "read_file",
        "search_files",
        "artifact_read",
        "memory_search",
        "memory_get",
        "memory_explain",
        "todo",
    }
)


# Named presets are the main product path (PERMISSION_POLICY_CONTRACT §2).
# read-only's approval is contractually n/a (inert because mutating tools are
# blocked before approval is evaluated); ON_REQUEST is stored as a placeholder.
_PRESET_POLICIES: dict[PermissionMode, EffectivePermissionPolicy] = {
    PermissionMode.READ_ONLY: EffectivePermissionPolicy(
        profile=PermissionProfile.READ_ONLY,
        approval=ApprovalPolicy.ON_REQUEST,
        terminal=TerminalAccess.OFF,
    ),
    PermissionMode.ASK_PERMISSIONS: EffectivePermissionPolicy(
        profile=PermissionProfile.TRUSTED_HOST,
        approval=ApprovalPolicy.ON_REQUEST,
        terminal=TerminalAccess.ASK,
    ),
    PermissionMode.ACCEPT_EDITS: EffectivePermissionPolicy(
        profile=PermissionProfile.TRUSTED_HOST,
        approval=ApprovalPolicy.NEVER,
        terminal=TerminalAccess.ASK,
    ),
    PermissionMode.BYPASS_PERMISSIONS: EffectivePermissionPolicy(
        profile=PermissionProfile.TRUSTED_HOST,
        approval=ApprovalPolicy.NEVER,
        terminal=TerminalAccess.ALLOW,
    ),
}


def parse_permission_mode(value: str | PermissionMode) -> PermissionMode:
    if isinstance(value, PermissionMode):
        return value
    normalized = str(value).strip()
    try:
        return PermissionMode(normalized)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in PermissionMode)
        raise ValueError(
            f"Invalid permission mode: {value!r} (expected one of: {allowed})"
        ) from exc


def preset_to_policy(mode: str | PermissionMode) -> EffectivePermissionPolicy:
    return _PRESET_POLICIES[parse_permission_mode(mode)]


def mode_for_policy(policy: EffectivePermissionPolicy) -> PermissionMode | None:
    """Reverse-map a policy to its preset mode, or None for a custom triple."""
    for mode, preset in _PRESET_POLICIES.items():
        if (preset.profile, preset.approval, preset.terminal) == (
            policy.profile,
            policy.approval,
            policy.terminal,
        ):
            return mode
    return None


@dataclass(slots=True)
class PermissionState:
    """Mutable holder so the gate and terminal tools read one live policy
    reference. Switching mode mutates .policy/.mode in place; everyone sees it
    on the next turn. The frozen EffectivePermissionPolicy stays a value."""

    policy: EffectivePermissionPolicy
    mode: PermissionMode | None = None

    @classmethod
    def from_policy(cls, policy: EffectivePermissionPolicy) -> "PermissionState":
        return cls(policy=policy, mode=mode_for_policy(policy))


def default_permission_policy(
    *,
    intent: Literal["run", "inspect"] = "run",
) -> EffectivePermissionPolicy:
    # inspect is read-only diagnostics; run defaults to the most aggressive
    # preset (bypass-permissions). workspace_kind no longer influences this.
    if intent == "inspect":
        return preset_to_policy(PermissionMode.READ_ONLY)
    return preset_to_policy(DEFAULT_PERMISSION_MODE)


def resolve_permission_policy(
    *,
    intent: Literal["run", "inspect"] = "run",
    profile: str | PermissionProfile | None = None,
    approval: str | ApprovalPolicy | None = None,
    terminal: str | TerminalAccess | None = None,
    env: Mapping[str, str] | None = None,
    prefix: str = "PULSARA",
) -> EffectivePermissionPolicy:
    # Custom three-axis feature entry (PERMISSION_POLICY_CONTRACT §7), not the
    # main path. The main path is preset_to_policy(). Any axis left unspecified
    # falls back to the bypass default (PERMISSION_POLICY_CONTRACT §6); no
    # workspace_kind / risky_only inference happens here.
    environ = os.environ if env is None else env
    base = _profile_default(
        _parse_enum(
            PermissionProfile,
            profile or _env_value(environ, prefix, "PERMISSION_PROFILE"),
            option_name="permission profile",
        ),
        intent=intent,
    )
    resolved = EffectivePermissionPolicy(
        profile=base.profile,
        approval=_parse_enum(
            ApprovalPolicy,
            approval or _env_value(environ, prefix, "APPROVAL_POLICY"),
            option_name="approval policy",
            default=base.approval,
        ),
        terminal=_parse_enum(
            TerminalAccess,
            terminal or _env_value(environ, prefix, "TERMINAL_ACCESS"),
            option_name="terminal access",
            default=base.terminal,
        ),
    )
    _validate_policy(resolved)
    return resolved


def is_tool_allowed_by_policy(tool_name: str, policy: EffectivePermissionPolicy) -> bool:
    # read-only is fail-closed: allow ONLY tools with no external side effect.
    # Anything not in the allowlist (write/terminal/remember_*/future tools) is
    # denied, closing PERMISSION_POLICY_CONTRACT §3 literally.
    if policy.profile is PermissionProfile.READ_ONLY:
        return tool_name in READ_ONLY_ALLOWED_TOOL_NAMES
    # terminal=off independently hides terminal tools (custom-policy axis).
    if policy.terminal is TerminalAccess.OFF and tool_name in TERMINAL_TOOL_NAMES:
        return False
    return True


def evaluate_capability_exposure_access(
    call: ToolCall,
    exposure: CapabilityExposurePlan,
) -> PermissionDecision | None:
    descriptor = exposure.descriptors_by_name.get(call.name)
    if descriptor is None:
        return PermissionDecision(
            kind=PermissionDecisionKind.DENY,
            reason=f"Unknown tool: {call.name} (capability_descriptor_missing)",
        )
    if descriptor.availability is CapabilityAvailability.UNAVAILABLE:
        return PermissionDecision(
            kind=PermissionDecisionKind.DENY,
            reason=f"capability_unavailable: {call.name}",
        )
    if call.name in exposure.hidden_names:
        return PermissionDecision(
            kind=PermissionDecisionKind.DENY,
            reason=f"capability_hidden_in_current_exposure: {call.name}",
        )
    if call.name not in exposure.callable_names:
        return PermissionDecision(
            kind=PermissionDecisionKind.DENY,
            reason=f"capability_not_callable_in_current_exposure: {call.name}",
        )
    return None


class PolicyPermissionGate:
    def __init__(
        self,
        policy: EffectivePermissionPolicy | PermissionState,
        inner: PermissionGate,
    ) -> None:
        # Accept a live PermissionState holder (so mode switches are picked up
        # next turn) or a bare policy (wrapped into a fresh holder for callers
        # and tests that pass a static policy).
        self._state = policy if isinstance(policy, PermissionState) else PermissionState.from_policy(policy)
        self.inner = inner

    @property
    def policy(self) -> EffectivePermissionPolicy:
        return self._state.policy

    async def evaluate(
        self,
        calls: list[ToolCall],
        *,
        exposure: CapabilityExposurePlan | None = None,
        classifier: CapabilityCallClassifier | None = None,
    ) -> PermissionDecision:
        for call in calls:
            decision = (
                self._evaluate_capability_call(call, exposure, classifier or DefaultCapabilityCallClassifier())
                if exposure is not None
                else self._evaluate_call(call)
            )
            if decision.kind is not PermissionDecisionKind.ALLOW:
                return decision
        base = await self.inner.evaluate(calls)
        return base

    def _evaluate_capability_call(
        self,
        call: ToolCall,
        exposure: CapabilityExposurePlan,
        classifier: CapabilityCallClassifier,
    ) -> PermissionDecision:
        exposure_decision = evaluate_capability_exposure_access(call, exposure)
        if exposure_decision is not None:
            return exposure_decision
        descriptor = exposure.descriptors_by_name[call.name]
        if call.name == "terminal":
            command = call.arguments.get("command")
            if isinstance(command, str) and is_hardline_terminal_command(command):
                return PermissionDecision(
                    kind=PermissionDecisionKind.DENY,
                    reason="terminal command blocked by hardline permission policy",
                    suggested_rules=[
                        {
                            "tool": "terminal",
                            "reason": "hardline_terminal_command",
                            "command": command,
                        }
                    ],
                )
        if call.name == "terminal_process":
            terminal_input = _terminal_process_input(call)
            if terminal_input is not None and is_hardline_terminal_command(terminal_input):
                return PermissionDecision(
                    kind=PermissionDecisionKind.DENY,
                    reason="terminal process input blocked by hardline permission policy",
                    suggested_rules=[
                        {
                            "tool": "terminal_process",
                            "reason": "hardline_terminal_process_input",
                        }
                    ],
                )
        classification = classifier.classify(call, descriptor)
        if self.policy.profile is PermissionProfile.READ_ONLY:
            if not descriptor.is_read_only or call.name not in READ_ONLY_ALLOWED_TOOL_NAMES:
                return PermissionDecision(
                    kind=PermissionDecisionKind.DENY,
                    reason=f"tool '{call.name}' is not allowed by permission policy",
                )
        elif self.policy.terminal is TerminalAccess.OFF and call.name in TERMINAL_TOOL_NAMES:
            return PermissionDecision(
                kind=PermissionDecisionKind.DENY,
                reason=f"tool '{call.name}' is not allowed by permission policy",
            )

        if classification.effective_permission_category == "terminal_process_observe":
            if self.policy.profile is not PermissionProfile.READ_ONLY and self.policy.terminal is not TerminalAccess.OFF:
                return PermissionDecision.allow()
            return PermissionDecision(
                kind=PermissionDecisionKind.DENY,
                reason=f"tool '{call.name}' is not allowed by permission policy",
            )
        if classification.effective_permission_category == "terminal":
            return self._evaluate_terminal_call(call)
        if (
            self.policy.approval is ApprovalPolicy.ON_REQUEST
            and classification.effective_permission_category == "filesystem_write"
        ):
            return PermissionDecision(
                kind=PermissionDecisionKind.WAIT_FOR_USER,
                reason="file write tool requires user confirmation by approval policy",
                suggested_rules=[{"tool": call.name, "reason": "write_tool_on_request"}],
            )
        if self.policy.approval is ApprovalPolicy.ON_REQUEST and classification.effective_is_destructive:
            return PermissionDecision(
                kind=PermissionDecisionKind.WAIT_FOR_USER,
                reason="destructive tool requires user confirmation by approval policy",
                suggested_rules=[{"tool": call.name, "reason": "destructive_tool_on_request"}],
            )
        return PermissionDecision.allow()

    def _evaluate_call(self, call: ToolCall) -> PermissionDecision:
        if call.name == "terminal":
            command = call.arguments.get("command")
            if isinstance(command, str) and is_hardline_terminal_command(command):
                return PermissionDecision(
                    kind=PermissionDecisionKind.DENY,
                    reason="terminal command blocked by hardline permission policy",
                    suggested_rules=[
                        {
                            "tool": "terminal",
                            "reason": "hardline_terminal_command",
                            "command": command,
                        }
                    ],
                )
        if call.name == "terminal_process":
            terminal_input = _terminal_process_input(call)
            if terminal_input is not None and is_hardline_terminal_command(terminal_input):
                return PermissionDecision(
                    kind=PermissionDecisionKind.DENY,
                    reason="terminal process input blocked by hardline permission policy",
                    suggested_rules=[
                        {
                            "tool": "terminal_process",
                            "reason": "hardline_terminal_process_input",
                        }
                    ],
                )
        if not is_tool_allowed_by_policy(call.name, self.policy):
            return PermissionDecision(
                kind=PermissionDecisionKind.DENY,
                reason=f"tool '{call.name}' is not allowed by permission policy",
            )
        if call.name in TERMINAL_TOOL_NAMES:
            return self._evaluate_terminal_call(call)
        if self.policy.approval is ApprovalPolicy.ON_REQUEST and call.name in FILE_WRITE_TOOL_NAMES:
            return PermissionDecision(
                kind=PermissionDecisionKind.WAIT_FOR_USER,
                reason="file write tool requires user confirmation by approval policy",
                suggested_rules=[{"tool": call.name, "reason": "write_tool_on_request"}],
            )
        return PermissionDecision.allow()

    def _evaluate_terminal_call(self, call: ToolCall) -> PermissionDecision:
        if call.name == "terminal_process" and _terminal_process_action(call) in TERMINAL_PROCESS_READ_ONLY_ACTIONS:
            return PermissionDecision.allow()
        if self.policy.terminal is TerminalAccess.ASK:
            return PermissionDecision(
                kind=PermissionDecisionKind.WAIT_FOR_USER,
                reason="terminal access requires user confirmation by permission policy",
                suggested_rules=[{"tool": call.name, "reason": "terminal_access_ask"}],
            )
        if self.policy.approval is ApprovalPolicy.ON_REQUEST:
            return PermissionDecision(
                kind=PermissionDecisionKind.WAIT_FOR_USER,
                reason="terminal tool requires user confirmation by approval policy",
                suggested_rules=[{"tool": call.name, "reason": "terminal_on_request"}],
            )
        if call.name == "terminal" and self.policy.approval is ApprovalPolicy.RISKY_ONLY:
            command = call.arguments.get("command")
            if isinstance(command, str) and is_risky_terminal_command(command):
                reason = (
                    "sensitive_terminal_command"
                    if is_sensitive_terminal_command(command)
                    else "dangerous_terminal_command"
                )
                return PermissionDecision(
                    kind=PermissionDecisionKind.WAIT_FOR_USER,
                    reason="terminal command requires user confirmation before execution",
                    suggested_rules=[
                        {
                            "tool": "terminal",
                            "reason": reason,
                            "command": command,
                        }
                    ],
                )
        return PermissionDecision.allow()


def _profile_default(
    profile: PermissionProfile | None,
    *,
    intent: Literal["run", "inspect"],
) -> EffectivePermissionPolicy:
    if profile is None:
        return default_permission_policy(intent=intent)
    if profile is PermissionProfile.READ_ONLY:
        # read_only's approval is contractually inert; terminal must be off.
        return EffectivePermissionPolicy(
            profile=PermissionProfile.READ_ONLY,
            approval=ApprovalPolicy.ON_REQUEST,
            terminal=TerminalAccess.OFF,
        )
    # PERMISSION_POLICY_CONTRACT §6: default inference no longer produces
    # risky_only. A mutating profile (trusted_host / workspace_guarded) given
    # without explicit approval/terminal falls back to the bypass default
    # (never/allow). risky_only and workspace_guarded remain valid only as
    # explicitly-passed custom axis values, never inferred here.
    return EffectivePermissionPolicy(
        profile=profile,
        approval=ApprovalPolicy.NEVER,
        terminal=TerminalAccess.ALLOW,
    )


def _env_value(environ: Mapping[str, str], prefix: str, suffix: str) -> str | None:
    value = environ.get(f"{prefix}_{suffix}")
    if value is None:
        return None
    value = value.strip()
    return value or None


def _parse_enum(enum_type, value, *, option_name: str, default=None):
    if value is None:
        return default
    if isinstance(value, enum_type):
        return value
    normalized = str(value).strip()
    try:
        return enum_type(normalized)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in enum_type)
        raise ValueError(f"Invalid {option_name}: {value!r} (expected one of: {allowed})") from exc


def _validate_policy(policy: EffectivePermissionPolicy) -> None:
    if policy.profile is PermissionProfile.READ_ONLY and policy.terminal is not TerminalAccess.OFF:
        raise ValueError("read_only permission profile requires terminal_access=off")


def _terminal_process_input(call: ToolCall) -> str | None:
    action = _terminal_process_action(call)
    if action not in {"write", "submit"}:
        return None
    data = call.arguments.get("data")
    return data if isinstance(data, str) else None


def _terminal_process_action(call: ToolCall) -> str | None:
    action = call.arguments.get("action")
    return action if isinstance(action, str) else None
