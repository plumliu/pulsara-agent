"""Closed process-local terminal input contract."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from pulsara_agent.identity import require_id

from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    context_fingerprint,
    freeze_json,
    thaw_json,
)


DEFAULT_MAX_OUTPUT_CHARS = 32_000
MIN_TERMINAL_OUTPUT_CHARS = 512
DEFAULT_WAIT_TIMEOUT_SECONDS = 30

TERMINAL_TOOL_DESCRIPTION = (
    "Run one local shell command. Each call starts a new shell, so its directory "
    "and environment changes stay within that call. A running command returns "
    "process_id for terminal_process to inspect or interact with it. In the ROOT "
    "conversation, terminal_monitor can resume work on completion or configured "
    "progress. Response size and initial wait settings do not limit the command's"
    " lifetime. Use artifact_read for omitted output or artifact_export for "
    "extensive processing."
)
TERMINAL_PROCESS_TOOL_DESCRIPTION = (
    "Control commands started by terminal. Set action to the operation below. "
    "Fields listed for an action are required unless marked optional; use only "
    "that action's fields.\n"
    "list: optional include_running, include_finished.\n"
    "poll: process_id; optional since_cursor, max_output_chars.\n"
    "wait: process_id; optional since_cursor, timeout_seconds, max_output_chars.\n"
    "write: process_id, data; optional yield_time_ms.\n"
    "submit: process_id, data; optional yield_time_ms.\n"
    "close_stdin: process_id.\n"
    "kill: process_id.\n"
    "poll reads now; wait waits up to timeout_seconds. write sends data "
    "unchanged; submit adds one newline to data supplied without its final "
    "newline. close_stdin closes input; kill stops the command and its children "
    "and waits for exit. Omitted optional fields use their defaults; only "
    "since_cursor accepts null. status and exit_code describe process completion;"
    " output or echo alone does not establish completion. Use terminal_monitor in"
    " the ROOT conversation for later updates, artifact_read for omitted logs, "
    "and artifact_export for extensive retrieval."
)
TERMINAL_MONITOR_TOOL_DESCRIPTION = (
    "ROOT only. Resume the conversation when a running command completes or meets"
    " configured progress conditions. Set action as follows; fields are required "
    "unless marked optional, and each action uses only its listed fields.\n"
    "register: process_id; optional conditions, delivery, lifetime.\n"
    "list: no other fields.\n"
    "cancel: monitor_id.\n"
    "register returns immediately; the current turn may end while monitoring "
    "continues. Omitting conditions requests completion only; configured progress"
    " updates also resume the conversation. list shows active monitors, not "
    "process completion. cancel stops updates while the command continues. "
    "Monitoring also ends on expiry or when its terminal environment closes. Use "
    "terminal_process poll for current output or status, and kill to stop a "
    "command."
)


class _StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class TerminalInput(_StrictInput):
    command: str = Field(
        min_length=1,
        max_length=1_048_576,
    )
    workdir: str | None = Field(
        default=None,
        min_length=1,
        max_length=4096,
        description=(
            "Existing directory for this call; workspace-relative, absolute and ~ paths "
            "are supported. Omit for the workspace root."
        ),
    )
    yield_time_ms: int = Field(
        default=10_000,
        ge=0,
        le=30_000,
        description=("Initial observation wait; 0 returns immediately."),
    )
    tty: bool = Field(
        default=False,
        description=(
            "Enable terminal interaction for programs needing line editing or a full-screen interface."
        ),
    )
    max_output_chars: int = Field(
        default=DEFAULT_MAX_OUTPUT_CHARS,
        ge=MIN_TERMINAL_OUTPUT_CHARS,
        le=DEFAULT_MAX_OUTPUT_CHARS,
    )


_TERMINAL_ADAPTER = TypeAdapter(TerminalInput)


class TerminalProcessListInput(_StrictInput):
    action: Literal["list"]
    include_running: bool = Field(
        default=True,
    )
    include_finished: bool = Field(
        default=True,
    )


class _ProcessInput(_StrictInput):
    process_id: str = Field(
        min_length=1,
        description="Exact process_id from this terminal environment.",
    )


class TerminalProcessPollInput(_ProcessInput):
    action: Literal["poll"]
    since_cursor: str | None = Field(
        default=None,
        description=(
            "Exact output_cursor from this process, or omit for retained output. It "
            "advances past observed text, including omitted output."
        ),
    )
    max_output_chars: int = Field(
        default=DEFAULT_MAX_OUTPUT_CHARS,
        ge=MIN_TERMINAL_OUTPUT_CHARS,
        le=DEFAULT_MAX_OUTPUT_CHARS,
        description="Maximum returned output characters; does not limit the command.",
    )


class TerminalProcessWaitInput(_ProcessInput):
    action: Literal["wait"]
    since_cursor: str | None = Field(
        default=None,
        description=(
            "Exact output_cursor from this process, or omit for retained output. It "
            "advances past observed text, including omitted output."
        ),
    )
    timeout_seconds: int = Field(
        default=DEFAULT_WAIT_TIMEOUT_SECONDS,
        ge=1,
        le=30,
    )
    max_output_chars: int = Field(
        default=DEFAULT_MAX_OUTPUT_CHARS,
        ge=MIN_TERMINAL_OUTPUT_CHARS,
        le=DEFAULT_MAX_OUTPUT_CHARS,
        description="Maximum returned output characters; does not limit the command.",
    )


class TerminalProcessWriteInput(_ProcessInput):
    action: Literal["write"]
    data: str = Field(description="Exact UTF-8 text; an empty string is allowed.")
    yield_time_ms: int = Field(
        default=1000,
        ge=0,
        le=30_000,
        description=(
            "Wait for process exit after input is flushed; 0 returns immediately."
        ),
    )


class TerminalProcessSubmitInput(_ProcessInput):
    action: Literal["submit"]
    data: str = Field(description="Exact UTF-8 text; an empty string is allowed.")
    yield_time_ms: int = Field(
        default=1000,
        ge=0,
        le=30_000,
        description=(
            "Wait for process exit after input is flushed; 0 returns immediately."
        ),
    )


class TerminalProcessCloseStdinInput(_ProcessInput):
    action: Literal["close_stdin"]


class TerminalProcessKillInput(_ProcessInput):
    action: Literal["kill"]


TerminalProcessInput: TypeAlias = Annotated[
    TerminalProcessListInput
    | TerminalProcessPollInput
    | TerminalProcessWaitInput
    | TerminalProcessWriteInput
    | TerminalProcessSubmitInput
    | TerminalProcessCloseStdinInput
    | TerminalProcessKillInput,
    Field(discriminator="action"),
]

_TERMINAL_PROCESS_ADAPTER = TypeAdapter(TerminalProcessInput)


class TerminalMonitorOutputCondition(_StrictInput):
    min_new_output_chars: int = Field(
        default=200,
        ge=1,
        le=65_536,
        description="New-output threshold for a progress update.",
    )
    quiet_period_ms: int = Field(
        default=500,
        ge=0,
        le=10_000,
        description=("Required pause in output before a progress update."),
    )


class TerminalMonitorConditions(_StrictInput):
    output: TerminalMonitorOutputCondition | None = Field(
        default=None,
    )
    heartbeat_interval_seconds: int | None = Field(
        default=None,
        ge=5,
        le=1800,
        description=("Progress interval even when there is little new output."),
    )


class TerminalMonitorDelivery(_StrictInput):
    max_output_chars: int = Field(
        default=4000,
        ge=512,
        le=32_000,
        description="Maximum command-output characters in each update.",
    )
    minimum_progress_observation_interval_seconds: int = Field(
        default=5,
        ge=5,
        le=1800,
        description="Minimum spacing between progress updates.",
    )


class TerminalMonitorLifetime(_StrictInput):
    maximum_duration_seconds: int = Field(
        default=36_000,
        ge=1,
        le=36_000,
        description=("Monitoring duration; expiry does not stop the command."),
    )


class TerminalMonitorRegisterInput(_StrictInput):
    action: Literal["register"]
    process_id: str = Field(
        min_length=1,
        description="process_id from a running terminal command.",
    )
    conditions: TerminalMonitorConditions = Field(
        default=TerminalMonitorConditions(),
        description=(
            "Progress triggers in addition to completion; omit for completion only."
        ),
    )
    delivery: TerminalMonitorDelivery = Field(
        default=TerminalMonitorDelivery(),
    )
    lifetime: TerminalMonitorLifetime = Field(
        default=TerminalMonitorLifetime(),
    )


class TerminalMonitorListInput(_StrictInput):
    action: Literal["list"]


class TerminalMonitorCancelInput(_StrictInput):
    action: Literal["cancel"]
    monitor_id: str = Field(
        min_length=1,
        description=("monitor_id returned by register."),
    )

    @field_validator("monitor_id")
    @classmethod
    def canonical_monitor_id(cls, value: str) -> str:
        return require_id(value, "monitor")


TerminalMonitorInput: TypeAlias = Annotated[
    TerminalMonitorRegisterInput
    | TerminalMonitorListInput
    | TerminalMonitorCancelInput,
    Field(discriminator="action"),
]

_TERMINAL_MONITOR_ADAPTER = TypeAdapter(TerminalMonitorInput)


@dataclass(frozen=True, slots=True)
class BuiltinToolInputContractBinding:
    tool_name: Literal["terminal_process"]
    input_adapter: TypeAdapter[Any]
    frozen_input_schema: FrozenJsonObjectFact
    input_schema_fingerprint: str

    def schema_copy(self) -> dict[str, Any]:
        return deepcopy(thaw_json(self.frozen_input_schema))


def parse_terminal_process_input(arguments: object) -> TerminalProcessInput:
    return _TERMINAL_PROCESS_ADAPTER.validate_python(arguments, strict=True)


def parse_terminal_input(arguments: object) -> TerminalInput:
    return _TERMINAL_ADAPTER.validate_python(arguments, strict=True)


def parse_terminal_monitor_input(arguments: object) -> TerminalMonitorInput:
    return _TERMINAL_MONITOR_ADAPTER.validate_python(arguments, strict=True)


@lru_cache(maxsize=1)
def builtin_tool_input_contract_binding() -> BuiltinToolInputContractBinding:
    schema = _inline_schema_references(_TERMINAL_PROCESS_ADAPTER.json_schema())
    frozen = freeze_json(schema)
    if not isinstance(frozen, FrozenJsonObjectFact):
        raise AssertionError("terminal process schema must freeze as an object")
    return BuiltinToolInputContractBinding(
        tool_name="terminal_process",
        input_adapter=_TERMINAL_PROCESS_ADAPTER,
        frozen_input_schema=frozen,
        input_schema_fingerprint=context_fingerprint(
            "builtin-tool-input-schema:v1", ["terminal_process", schema]
        ),
    )


def terminal_process_input_schema() -> dict[str, Any]:
    return builtin_tool_input_contract_binding().schema_copy()


@lru_cache(maxsize=1)
def terminal_input_schema() -> dict[str, Any]:
    return _inline_schema_references(_TERMINAL_ADAPTER.json_schema())


@lru_cache(maxsize=1)
def terminal_monitor_input_schema() -> dict[str, Any]:
    return _inline_schema_references(_TERMINAL_MONITOR_ADAPTER.json_schema())


def _inline_schema_references(schema: dict[str, Any]) -> dict[str, Any]:
    root = deepcopy(schema)
    definitions = root.pop("$defs", {})

    def resolve(value: object) -> object:
        if not isinstance(value, dict):
            return value
        reference = value.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            name = reference.removeprefix("#/$defs/")
            target = definitions.get(name)
            if not isinstance(target, dict):
                raise ValueError("terminal process schema reference is missing")
            merged = deepcopy(target)
            merged.update({key: item for key, item in value.items() if key != "$ref"})
            return resolve(merged)
        resolved = {}
        for key, item in value.items():
            if key == "title":
                continue
            if key in {
                "properties",
                "patternProperties",
                "$defs",
                "definitions",
                "dependentSchemas",
            }:
                resolved[key] = {name: resolve(child) for name, child in item.items()}
            elif key in {"oneOf", "anyOf", "allOf", "prefixItems"}:
                resolved[key] = [resolve(child) for child in item]
            elif key in {
                "items",
                "additionalProperties",
                "additionalItems",
                "contains",
                "propertyNames",
                "not",
                "if",
                "then",
                "else",
                "unevaluatedProperties",
                "unevaluatedItems",
            }:
                resolved[key] = resolve(item)
            else:
                resolved[key] = item
        return resolved

    resolved = resolve(root)
    if not isinstance(resolved, dict):
        raise AssertionError("terminal process schema must remain an object")
    # A discriminated Pydantic union is emitted as a top-level ``oneOf`` with
    # object-shaped branches, but some OpenAI-compatible providers require the
    # function schema itself to declare its object type.  This does not widen
    # the closed action union; every accepted value must still match exactly
    # one of the strict branch schemas below it.
    resolved.setdefault("type", "object")
    # Action signatures live in the function description. Pydantic retains its
    # discriminator for local parsing; exporting it would duplicate those lines
    # in the provider lowerer's generated schema description.
    resolved.pop("discriminator", None)
    return resolved


__all__ = [
    "DEFAULT_MAX_OUTPUT_CHARS",
    "TERMINAL_PROCESS_TOOL_DESCRIPTION",
    "TERMINAL_MONITOR_TOOL_DESCRIPTION",
    "TERMINAL_TOOL_DESCRIPTION",
    "TerminalInput",
    "TerminalProcessInput",
    "TerminalMonitorInput",
    "parse_terminal_input",
    "parse_terminal_monitor_input",
    "parse_terminal_process_input",
    "terminal_input_schema",
    "terminal_process_input_schema",
    "terminal_monitor_input_schema",
]
