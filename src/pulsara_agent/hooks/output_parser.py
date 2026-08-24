"""Single bounded parser transaction for Hook command output."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any

from pulsara_agent.hooks.config_parser import (
    MAXIMUM_JSON_DEPTH,
    MAXIMUM_JSON_NODES,
    MAXIMUM_SCALAR_UTF8_BYTES,
)
from pulsara_agent.hooks.contracts import (
    FrozenHookDefinition,
    HookDiagnostic,
    HookEventType,
    PermissionDecision,
)
from pulsara_agent.hooks.executor import HookCommandExecution


_CONTROL_KEYS = frozenset(
    {
        "continue",
        "stopReason",
        "suppressOutput",
        "decision",
        "permissionDecision",
        "permissionDecisionReason",
        "updatedInput",
        "updatedMCPToolOutput",
        "updatedPermissions",
        "interrupt",
        "rewrite",
    }
)
_COMMON_CONTROL_EVENTS = {
    HookEventType.SESSION_START_EVENT,
    HookEventType.USER_PROMPT_SUBMIT_EVENT,
    HookEventType.PRE_COMPACT_EVENT,
    HookEventType.POST_COMPACT_EVENT,
    HookEventType.SUBAGENT_STOP_EVENT,
    HookEventType.STOP_EVENT,
}
_PLAIN_CONTEXT_EVENTS = {
    HookEventType.SESSION_START_EVENT,
    HookEventType.USER_PROMPT_SUBMIT_EVENT,
    HookEventType.SUBAGENT_START_EVENT,
}


@dataclass(frozen=True, slots=True)
class ValidHandlerContribution:
    definition: FrozenHookDefinition
    event_dispatch_ordinal: int
    source_ordinal: int
    context_text: str | None = None
    gate_block: bool = False
    permission_decision: PermissionDecision | None = None
    continuation_request: bool = False
    explicit_terminalize: bool = False
    reason: str | None = None
    diagnostics: tuple[HookDiagnostic, ...] = ()
    scrub_set: object | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class InvalidHandlerOutput:
    definition: FrozenHookDefinition
    event_dispatch_ordinal: int
    source_ordinal: int
    diagnostics: tuple[HookDiagnostic, ...]


@dataclass(frozen=True, slots=True)
class HandlerFailure:
    definition: FrozenHookDefinition
    event_dispatch_ordinal: int
    source_ordinal: int
    diagnostics: tuple[HookDiagnostic, ...]


type ParsedHandlerOutput = (
    ValidHandlerContribution | InvalidHandlerOutput | HandlerFailure
)


def parse_handler_output(execution: HookCommandExecution) -> ParsedHandlerOutput:
    definition = execution.definition
    event = definition.event_type
    diagnostics = list(execution.diagnostics)
    if execution.failure_code is not None:
        return HandlerFailure(
            definition,
            execution.event_dispatch_ordinal,
            execution.source_ordinal,
            tuple(diagnostics),
        )
    try:
        stderr = execution.scrub_set.scrub_bytes(execution.stderr).decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        return _invalid(execution, "HOOK_STDERR_INVALID")
    if stderr:
        diagnostics.append(_diag(execution, "HOOK_STDERR", stderr))
    if execution.exit_code == 2:
        reason = stderr.strip() or None
        if reason is None:
            diagnostics.append(_diag(execution, "HOOK_EXIT_2_EMPTY_REASON", "exit 2"))
        if event in {
            HookEventType.PRE_TOOL_USE_EVENT,
            HookEventType.USER_PROMPT_SUBMIT_EVENT,
        }:
            return ValidHandlerContribution(
                definition,
                execution.event_dispatch_ordinal,
                execution.source_ordinal,
                gate_block=True,
                reason=reason,
                diagnostics=tuple(diagnostics),
            )
        if event in {
            HookEventType.SUBAGENT_STOP_EVENT,
            HookEventType.STOP_EVENT,
        }:
            return ValidHandlerContribution(
                definition,
                execution.event_dispatch_ordinal,
                execution.source_ordinal,
                continuation_request=True,
                reason=reason,
                diagnostics=tuple(diagnostics),
            )
        return HandlerFailure(
            definition,
            execution.event_dispatch_ordinal,
            execution.source_ordinal,
            (*diagnostics, _diag(execution, "HOOK_UNSUPPORTED_EXIT_2", "exit 2")),
        )
    if execution.exit_code != 0:
        return HandlerFailure(
            definition,
            execution.event_dispatch_ordinal,
            execution.source_ordinal,
            (*diagnostics, _diag(execution, "HOOK_NONZERO_EXIT", "nonzero exit")),
        )
    raw = execution.stdout
    if not raw:
        return ValidHandlerContribution(
            definition,
            execution.event_dispatch_ordinal,
            execution.source_ordinal,
            diagnostics=tuple(diagnostics),
        )
    leading = raw.lstrip()[:1]
    if leading in {b"{", b"["}:
        try:
            value = _decode_json_output(raw, execution)
        except ValueError:
            return _invalid(execution, "HOOK_OUTPUT_INVALID_JSON", diagnostics)
        if not isinstance(value, dict):
            return _invalid(execution, "HOOK_OUTPUT_ROOT_NOT_OBJECT", diagnostics)
        if definition.asynchronous and _contains_control_key(value):
            return _invalid(execution, "HOOK_ASYNC_CONTROL_UNSUPPORTED", diagnostics)
        try:
            return _parse_json_object(execution, value, diagnostics)
        except ValueError:
            return _invalid(execution, "HOOK_OUTPUT_UNSUPPORTED", diagnostics)
    try:
        plain = execution.scrub_set.scrub_bytes(raw).decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        return _invalid(execution, "HOOK_OUTPUT_INVALID_UTF8", diagnostics)
    if definition.asynchronous and event not in _PLAIN_CONTEXT_EVENTS:
        diagnostics.append(_diag(execution, "HOOK_PLAIN_OUTPUT_DIAGNOSTIC", plain))
        return ValidHandlerContribution(
            definition,
            execution.event_dispatch_ordinal,
            execution.source_ordinal,
            diagnostics=tuple(diagnostics),
        )
    if event in _PLAIN_CONTEXT_EVENTS:
        return ValidHandlerContribution(
            definition,
            execution.event_dispatch_ordinal,
            execution.source_ordinal,
            context_text=plain,
            diagnostics=tuple(diagnostics),
        )
    if event in {
        HookEventType.SUBAGENT_STOP_EVENT,
        HookEventType.STOP_EVENT,
    }:
        return _invalid(execution, "HOOK_PLAIN_CONTINUATION_UNSUPPORTED", diagnostics)
    diagnostics.append(_diag(execution, "HOOK_PLAIN_OUTPUT_DIAGNOSTIC", plain))
    return ValidHandlerContribution(
        definition,
        execution.event_dispatch_ordinal,
        execution.source_ordinal,
        diagnostics=tuple(diagnostics),
    )


def _parse_json_object(
    execution: HookCommandExecution,
    value: dict[str, Any],
    diagnostics: list[HookDiagnostic],
) -> ValidHandlerContribution:
    event = execution.definition.event_type
    root_allowed = {"systemMessage"}
    if event in _COMMON_CONTROL_EVENTS:
        root_allowed.update({"continue", "stopReason", "suppressOutput"})
    if event in {
        HookEventType.POST_TOOL_USE_EVENT,
        HookEventType.SUBAGENT_START_EVENT,
    }:
        root_allowed.add("continue")
    if event in {
        HookEventType.SESSION_START_EVENT,
        HookEventType.USER_PROMPT_SUBMIT_EVENT,
        HookEventType.PRE_TOOL_USE_EVENT,
        HookEventType.POST_TOOL_USE_EVENT,
        HookEventType.SUBAGENT_START_EVENT,
        HookEventType.PERMISSION_REQUEST_EVENT,
    }:
        root_allowed.add("hookSpecificOutput")
    if event in {
        HookEventType.USER_PROMPT_SUBMIT_EVENT,
        HookEventType.PRE_TOOL_USE_EVENT,
        HookEventType.SUBAGENT_STOP_EVENT,
        HookEventType.STOP_EVENT,
    }:
        root_allowed.update({"decision", "reason"})
    if value.keys() - root_allowed:
        raise ValueError("unknown Hook output field")
    system_message = value.get("systemMessage")
    if system_message is not None:
        if not isinstance(system_message, str):
            raise ValueError("systemMessage must be a string")
        diagnostics.append(_diag(execution, "HOOK_SYSTEM_MESSAGE", system_message))
    if event is HookEventType.SESSION_END_EVENT:
        if value.keys() - {"systemMessage"}:
            raise ValueError("SessionEnd is observe-only")
        return _neutral(execution, diagnostics)

    context: str | None = None
    gate_block = False
    permission: PermissionDecision | None = None
    continuation = False
    explicit_terminalize = False
    reason: str | None = None
    specific = value.get("hookSpecificOutput")
    if specific is not None:
        if not isinstance(specific, dict):
            raise ValueError("hookSpecificOutput must be an object")
        context, gate_block, permission, reason = _parse_specific(
            execution, specific
        )

    if event in _COMMON_CONTROL_EVENTS:
        suppress = value.get("suppressOutput")
        if suppress is not None:
            if not isinstance(suppress, bool) or suppress:
                raise ValueError("output suppression is unsupported")
        has_continue = "continue" in value
        continue_value = value.get("continue")
        stop_reason = value.get("stopReason")
        if has_continue and not isinstance(continue_value, bool):
            raise ValueError("continue must be boolean")
        if stop_reason is not None and (
            not isinstance(stop_reason, str) or continue_value is not False
        ):
            raise ValueError("stopReason requires continue:false")
        if continue_value is False:
            if event in {
                HookEventType.SUBAGENT_STOP_EVENT,
                HookEventType.STOP_EVENT,
            }:
                explicit_terminalize = True
            else:
                gate_block = True
            reason = stop_reason or reason

    if event is HookEventType.POST_TOOL_USE_EVENT:
        if "suppressOutput" in value or value.get("continue") is False:
            raise ValueError("PostTool mutation is unsupported")
        if "continue" in value and value["continue"] is not True:
            raise ValueError("PostTool continue must be true")
        if "stopReason" in value:
            raise ValueError("PostTool stopReason is unsupported")
    if event is HookEventType.SUBAGENT_START_EVENT and "continue" in value:
        if not isinstance(value["continue"], bool):
            raise ValueError("SubagentStart continue must be boolean")
        diagnostics.append(
            _diag(execution, "HOOK_SUBAGENT_START_CONTINUE_IGNORED", "continue")
        )
    legacy = value.get("decision")
    legacy_reason = value.get("reason")
    if legacy is not None or legacy_reason is not None:
        if legacy != "block" or not isinstance(legacy_reason, str) or not legacy_reason:
            raise ValueError("legacy block decision is invalid")
        if event in {
            HookEventType.USER_PROMPT_SUBMIT_EVENT,
            HookEventType.PRE_TOOL_USE_EVENT,
        }:
            gate_block = True
        elif event in {
            HookEventType.SUBAGENT_STOP_EVENT,
            HookEventType.STOP_EVENT,
        }:
            continuation = True
        else:
            raise ValueError("legacy block decision is unsupported")
        reason = legacy_reason
    if continuation and explicit_terminalize:
        raise ValueError("continuation conflicts with terminalize")
    if execution.definition.asynchronous and any(
        (gate_block, permission is not None, continuation, explicit_terminalize)
    ):
        raise ValueError("background control is unsupported")
    return ValidHandlerContribution(
        execution.definition,
        execution.event_dispatch_ordinal,
        execution.source_ordinal,
        context,
        gate_block,
        permission,
        continuation,
        explicit_terminalize,
        reason,
        tuple(diagnostics),
    )


def _parse_specific(
    execution: HookCommandExecution, specific: dict[str, Any]
) -> tuple[str | None, bool, PermissionDecision | None, str | None]:
    event = execution.definition.event_type
    expected_name = event.external_name
    if specific.get("hookEventName") != expected_name:
        raise ValueError("hookEventName mismatch")
    context: str | None = None
    block = False
    permission: PermissionDecision | None = None
    reason: str | None = None
    if event in {
        HookEventType.SESSION_START_EVENT,
        HookEventType.USER_PROMPT_SUBMIT_EVENT,
        HookEventType.POST_TOOL_USE_EVENT,
        HookEventType.SUBAGENT_START_EVENT,
    }:
        allowed = {"hookEventName", "additionalContext"}
        if specific.keys() - allowed:
            raise ValueError("unsupported hook-specific output")
        context = specific.get("additionalContext")
        if context is not None and not isinstance(context, str):
            raise ValueError("additionalContext must be a string")
    elif event is HookEventType.PRE_TOOL_USE_EVENT:
        allowed = {
            "hookEventName",
            "permissionDecision",
            "permissionDecisionReason",
            "additionalContext",
        }
        if specific.keys() - allowed:
            raise ValueError("unsupported PreTool output")
        decision = specific.get("permissionDecision")
        if decision is not None:
            if decision != "deny":
                raise ValueError("PreTool only supports deny")
            block = True
        reason = specific.get("permissionDecisionReason")
        if reason is not None and (not isinstance(reason, str) or decision != "deny"):
            raise ValueError("permissionDecisionReason is invalid")
        context = specific.get("additionalContext")
        if context is not None and not isinstance(context, str):
            raise ValueError("additionalContext must be a string")
    elif event is HookEventType.PERMISSION_REQUEST_EVENT:
        if specific.keys() - {"hookEventName", "decision"}:
            raise ValueError("unsupported PermissionRequest output")
        decision = specific.get("decision")
        if decision is not None:
            if not isinstance(decision, dict) or decision.keys() - {
                "behavior",
                "message",
            }:
                raise ValueError("permission decision is invalid")
            behavior = decision.get("behavior")
            if behavior == "allow":
                permission = PermissionDecision.ALLOW
            elif behavior == "deny":
                permission = PermissionDecision.DENY
            else:
                raise ValueError("permission behavior is invalid")
            reason = decision.get("message")
            if reason is not None and not isinstance(reason, str):
                raise ValueError("permission message is invalid")
    else:
        raise ValueError("hookSpecificOutput is unsupported for event")
    return context, block, permission, reason


def _decode_json_output(raw: bytes, execution: HookCommandExecution) -> object:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("invalid UTF-8") from exc

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate output key")
            value[key] = item
        return value

    try:
        value = json.loads(
            text,
            object_pairs_hook=pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("non-finite number")
            ),
        )
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValueError("invalid Hook JSON") from exc
    value = execution.scrub_set.scrub_json(value)
    _validate_output_shape(value)
    return value


def _validate_output_shape(root: object) -> None:
    nodes = 0
    stack: list[tuple[object, int, bool]] = [(root, 1, False)]
    while stack:
        value, depth, is_additional_context = stack.pop()
        nodes += 1
        if nodes > MAXIMUM_JSON_NODES or depth > MAXIMUM_JSON_DEPTH:
            raise ValueError("Hook output structure bound exceeded")
        if isinstance(value, str):
            limit = 1024 * 1024 if is_additional_context else MAXIMUM_SCALAR_UTF8_BYTES
            if len(value.encode("utf-8")) > limit:
                raise ValueError("Hook output scalar bound exceeded")
        elif isinstance(value, dict):
            for key, item in value.items():
                if len(key.encode("utf-8")) > MAXIMUM_SCALAR_UTF8_BYTES:
                    raise ValueError("Hook output key bound exceeded")
                stack.append((item, depth + 1, key == "additionalContext"))
        elif isinstance(value, list):
            stack.extend((item, depth + 1, False) for item in value)
        elif value is None or isinstance(value, (bool, int, float)):
            continue
        else:
            raise ValueError("Hook output is not JSON")


def _contains_control_key(root: object) -> bool:
    stack = [root]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            if value.keys() & _CONTROL_KEYS:
                return True
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    return False


def _neutral(
    execution: HookCommandExecution, diagnostics: list[HookDiagnostic]
) -> ValidHandlerContribution:
    return ValidHandlerContribution(
        execution.definition,
        execution.event_dispatch_ordinal,
        execution.source_ordinal,
        diagnostics=tuple(diagnostics),
    )


def _invalid(
    execution: HookCommandExecution,
    code: str,
    diagnostics: list[HookDiagnostic] | None = None,
) -> InvalidHandlerOutput:
    return InvalidHandlerOutput(
        execution.definition,
        execution.event_dispatch_ordinal,
        execution.source_ordinal,
        (*(diagnostics or ()), _diag(execution, code, code)),
    )


def _diag(
    execution: HookCommandExecution, code: str, message: str
) -> HookDiagnostic:
    try:
        safe = execution.scrub_set.scrub_text(message)
    except ValueError:
        safe = code
    return HookDiagnostic(
        code,
        safe,
        event_type=execution.definition.event_type,
        source_label=execution.definition.provenance.display_label,
        event_dispatch_ordinal=execution.event_dispatch_ordinal,
        source_ordinal=execution.source_ordinal,
        definition_ordinal=execution.definition.source_local_definition_ordinal,
    )


__all__ = [
    "HandlerFailure",
    "InvalidHandlerOutput",
    "ValidHandlerContribution",
    "parse_handler_output",
]
