"""Bounded, duplicate-safe parser for the Codex-compatible Hook profile."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from typing import Any

from pulsara_agent.hooks.contracts import (
    FrozenHookDefinition,
    FrozenHookMatcherFact,
    FrozenHookSourceProvenance,
    HookDiagnostic,
    HookDiagnosticSeverity,
    HookEventType,
)
from pulsara_agent.hooks.matcher import compile_matcher


MAXIMUM_HOOK_CONFIG_BYTES = 1024 * 1024
MAXIMUM_JSON_NODES = 16_384
MAXIMUM_JSON_DEPTH = 64
MAXIMUM_SCALAR_UTF8_BYTES = 64 * 1024
MAXIMUM_COMMAND_UTF8_BYTES = 8 * 1024
MAXIMUM_MATCHER_UTF8_BYTES = 1024
MAXIMUM_STATUS_MESSAGE_UTF8_BYTES = 4 * 1024
DEFAULT_ADDITIONAL_CONTEXT_LIMIT = 2_500

_ROOT_FIELDS = frozenset({"description", "hooks"})
_GROUP_FIELDS = frozenset({"matcher", "hooks"})
_HANDLER_FIELDS = frozenset(
    {
        "type",
        "command",
        "commandWindows",
        "timeout",
        "statusMessage",
        "additionalContextLimit",
        "async",
    }
)
_MATCHER_IGNORED_EVENTS = {
    HookEventType.USER_PROMPT_SUBMIT_EVENT,
    HookEventType.STOP_EVENT,
}
_CONTEXT_EVENTS = {
    HookEventType.SESSION_START_EVENT,
    HookEventType.USER_PROMPT_SUBMIT_EVENT,
    HookEventType.PRE_TOOL_USE_EVENT,
    HookEventType.POST_TOOL_USE_EVENT,
    HookEventType.SUBAGENT_START_EVENT,
}


class HookConfigParseError(ValueError):
    pass


class _DuplicateKey(HookConfigParseError):
    pass


@dataclass(frozen=True, slots=True)
class ParsedHookConfig:
    provenance: FrozenHookSourceProvenance
    definitions: tuple[FrozenHookDefinition, ...]
    diagnostics: tuple[HookDiagnostic, ...]


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateKey(f"duplicate JSON object key: {key}")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise HookConfigParseError(f"non-finite JSON number is forbidden: {value}")


def _bounded_json(raw: bytes) -> object:
    if len(raw) > MAXIMUM_HOOK_CONFIG_BYTES:
        raise HookConfigParseError("hooks.json exceeds 1 MiB")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HookConfigParseError("hooks.json is not valid UTF-8") from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_constant,
        )
    except HookConfigParseError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise HookConfigParseError("hooks.json is not strict JSON") from exc
    _validate_json_shape(value)
    return value


def _validate_json_shape(root: object) -> None:
    nodes = 0
    stack: list[tuple[object, int]] = [(root, 1)]
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > MAXIMUM_JSON_NODES:
            raise HookConfigParseError("hooks.json exceeds the JSON node bound")
        if depth > MAXIMUM_JSON_DEPTH:
            raise HookConfigParseError("hooks.json exceeds the JSON depth bound")
        if isinstance(value, str):
            if len(value.encode("utf-8")) > MAXIMUM_SCALAR_UTF8_BYTES:
                raise HookConfigParseError("hooks.json contains an oversized scalar")
        elif isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(key, str):
                    raise HookConfigParseError("JSON map keys must be strings")
                if len(key.encode("utf-8")) > MAXIMUM_SCALAR_UTF8_BYTES:
                    raise HookConfigParseError("hooks.json contains an oversized key")
                stack.append((item, depth + 1))
        elif isinstance(value, list):
            stack.extend((item, depth + 1) for item in value)
        elif value is None or isinstance(value, (bool, int, float)):
            continue
        else:
            raise HookConfigParseError("hooks.json contains a non-JSON value")


def parse_hook_config(
    raw: bytes, *, provenance: FrozenHookSourceProvenance
) -> ParsedHookConfig:
    root = _bounded_json(raw)
    if not isinstance(root, dict):
        raise HookConfigParseError("hooks.json root must be an object")
    diagnostics: list[HookDiagnostic] = []
    for field in root.keys() - _ROOT_FIELDS:
        diagnostics.append(
            _diag("HOOK_CONFIG_UNKNOWN_ROOT_FIELD", f"ignored root field: {field}")
        )
    description = root.get("description")
    if description is not None and not isinstance(description, str):
        raise HookConfigParseError("description must be a string")
    hooks = root.get("hooks", {})
    if not isinstance(hooks, dict):
        raise HookConfigParseError("hooks must be an object")

    definitions: list[FrozenHookDefinition] = []
    next_ordinal = 0
    for event_ordinal, (event_name, groups) in enumerate(hooks.items()):
        try:
            event_type = HookEventType.from_external_name(event_name)
        except ValueError:
            diagnostics.append(
                _diag(
                    "HOOK_CONFIG_UNKNOWN_EVENT",
                    f"unsupported Hook event entry: {event_name}",
                )
            )
            continue
        if not isinstance(groups, list):
            diagnostics.append(
                _diag(
                    "HOOK_CONFIG_INVALID_EVENT_ARRAY",
                    f"{event_name} must contain an array",
                    event_type,
                )
            )
            continue
        for group_ordinal, group in enumerate(groups):
            if not isinstance(group, dict):
                diagnostics.append(
                    _diag(
                        "HOOK_CONFIG_INVALID_GROUP",
                        f"{event_name} group {group_ordinal} is not an object",
                        event_type,
                    )
                )
                continue
            unknown_group = group.keys() - _GROUP_FIELDS
            matcher = group.get("matcher", "")
            handlers = group.get("hooks")
            if unknown_group or not isinstance(matcher, str) or not isinstance(
                handlers, list
            ):
                diagnostics.append(
                    _diag(
                        "HOOK_CONFIG_INVALID_GROUP",
                        f"{event_name} group {group_ordinal} is unsupported",
                        event_type,
                    )
                )
                continue
            if len(matcher.encode("utf-8")) > MAXIMUM_MATCHER_UTF8_BYTES:
                diagnostics.append(
                    _diag(
                        "HOOK_CONFIG_MATCHER_BOUND_EXCEEDED",
                        f"{event_name} group {group_ordinal} matcher exceeds 1 KiB",
                        event_type,
                    )
                )
                continue
            ignored = event_type in _MATCHER_IGNORED_EVENTS
            matches_all = matcher in {"", "*"} or ignored
            if ignored and matcher not in {"", "*"}:
                diagnostics.append(
                    _diag(
                        "HOOK_CONFIG_MATCHER_IGNORED",
                        f"{event_name} ignores matcher by compatibility profile",
                        event_type,
                        HookDiagnosticSeverity.INFO,
                    )
                )
            if not matches_all:
                try:
                    compile_matcher(matcher)
                except ValueError:
                    diagnostics.append(
                        _diag(
                            "HOOK_CONFIG_INVALID_MATCHER",
                            f"{event_name} group {group_ordinal} matcher is not valid RE2",
                            event_type,
                        )
                    )
                    continue
            matcher_fact = FrozenHookMatcherFact(
                pattern=matcher,
                matches_all=matches_all,
                ignored_by_profile=ignored,
            )
            for handler_ordinal, handler in enumerate(handlers):
                definition = _parse_handler(
                    handler,
                    provenance=provenance,
                    event_type=event_type,
                    event_ordinal=event_ordinal,
                    group_ordinal=group_ordinal,
                    handler_ordinal=handler_ordinal,
                    source_local_definition_ordinal=next_ordinal,
                    matcher=matcher_fact,
                    diagnostics=diagnostics,
                )
                if definition is not None:
                    definitions.append(definition)
                    next_ordinal += 1
    final_provenance = replace(provenance, description=description)
    if final_provenance is not provenance:
        definitions = [
            replace(definition, provenance=final_provenance)
            for definition in definitions
        ]
    return ParsedHookConfig(
        final_provenance, tuple(definitions), tuple(diagnostics)
    )


def _parse_handler(
    value: object,
    *,
    provenance: FrozenHookSourceProvenance,
    event_type: HookEventType,
    event_ordinal: int,
    group_ordinal: int,
    handler_ordinal: int,
    source_local_definition_ordinal: int,
    matcher: FrozenHookMatcherFact,
    diagnostics: list[HookDiagnostic],
) -> FrozenHookDefinition | None:
    label = f"{event_type.external_name} handler {group_ordinal}:{handler_ordinal}"
    if not isinstance(value, dict):
        diagnostics.append(_diag("HOOK_CONFIG_INVALID_HANDLER", label, event_type))
        return None
    handler_type = value.get("type")
    if handler_type in {"prompt", "agent"}:
        diagnostics.append(
            _diag(
                "HOOK_CONFIG_KNOWN_UNSUPPORTED_HANDLER",
                f"{label} type {handler_type!r} is skipped",
                event_type,
                HookDiagnosticSeverity.INFO,
            )
        )
        return None
    if handler_type != "command" or value.keys() - _HANDLER_FIELDS:
        diagnostics.append(
            _diag("HOOK_CONFIG_UNSUPPORTED_HANDLER", label, event_type)
        )
        return None
    command = value.get("command")
    command_windows = value.get("commandWindows")
    timeout = value.get("timeout")
    status = value.get("statusMessage")
    context_limit = value.get(
        "additionalContextLimit", DEFAULT_ADDITIONAL_CONTEXT_LIMIT
    )
    asynchronous = value.get("async", False)
    valid = isinstance(command, str) and bool(command)
    valid = valid and len(command.encode("utf-8")) <= MAXIMUM_COMMAND_UTF8_BYTES
    valid = valid and (
        command_windows is None
        or (
            isinstance(command_windows, str)
            and bool(command_windows)
            and len(command_windows.encode("utf-8"))
            <= MAXIMUM_COMMAND_UTF8_BYTES
        )
    )
    valid = valid and (
        status is None
        or (
            isinstance(status, str)
            and len(status.encode("utf-8")) <= MAXIMUM_STATUS_MESSAGE_UTF8_BYTES
        )
    )
    valid = valid and isinstance(context_limit, int) and not isinstance(
        context_limit, bool
    ) and context_limit >= 0
    valid = valid and isinstance(asynchronous, bool)
    default_timeout = 1 if event_type is HookEventType.SESSION_END_EVENT else 600
    if timeout is None:
        timeout = default_timeout
    valid = valid and isinstance(timeout, int) and not isinstance(timeout, bool)
    if valid:
        valid = (1 <= timeout <= 3) if (
            event_type is HookEventType.SESSION_END_EVENT
        ) else (1 <= timeout <= 600)
    if not valid:
        diagnostics.append(_diag("HOOK_CONFIG_INVALID_HANDLER", label, event_type))
        return None
    assert isinstance(command, str)
    assert command_windows is None or isinstance(command_windows, str)
    assert status is None or isinstance(status, str)
    assert isinstance(timeout, int)
    assert isinstance(context_limit, int)
    if event_type is HookEventType.SESSION_END_EVENT and asynchronous:
        diagnostics.append(
            _diag(
                "HOOK_CONFIG_SESSION_END_ASYNC_IGNORED",
                f"{label} is forced synchronous",
                event_type,
                HookDiagnosticSeverity.INFO,
            )
        )
        asynchronous = False
    if (
        "additionalContextLimit" in value
        and event_type not in _CONTEXT_EVENTS
    ):
        diagnostics.append(
            _diag(
                "HOOK_CONFIG_CONTEXT_LIMIT_IGNORED",
                f"{label} does not accept additional context",
                event_type,
                HookDiagnosticSeverity.INFO,
            )
        )
    return FrozenHookDefinition(
        provenance=provenance,
        source_local_definition_ordinal=source_local_definition_ordinal,
        event_ordinal=event_ordinal,
        group_ordinal=group_ordinal,
        handler_ordinal=handler_ordinal,
        event_type=event_type,
        matcher=matcher,
        command=command,
        command_windows=command_windows,
        timeout_seconds=timeout,
        asynchronous=asynchronous,
        status_message=status,
        additional_context_limit=context_limit,
    )


def _diag(
    code: str,
    message: str,
    event_type: HookEventType | None = None,
    severity: HookDiagnosticSeverity = HookDiagnosticSeverity.WARNING,
) -> HookDiagnostic:
    return HookDiagnostic(code, message, severity, event_type)


__all__ = [
    "DEFAULT_ADDITIONAL_CONTEXT_LIMIT",
    "HookConfigParseError",
    "MAXIMUM_HOOK_CONFIG_BYTES",
    "ParsedHookConfig",
    "parse_hook_config",
]
