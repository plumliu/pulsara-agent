"""Closed process-local terminal input contract."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

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
    "Start one shell command inside workspace_root and wait for up to yield_time_ms. "
    "A running result owns one Host-scoped process_id; use terminal_process for "
    "bounded inspection, input, kill, or join. Processes never resume across Hosts."
)
TERMINAL_PROCESS_TOOL_DESCRIPTION = (
    "Operate on Host-scoped terminal processes. Actions are list, log, poll, wait, "
    "write, submit, close_stdin, and kill. Use the exact process_id returned by "
    "terminal; retained output and foreground waits are bounded."
)
TERMINAL_MONITOR_TOOL_DESCRIPTION = (
    "Register, list, or cancel bounded future observations for one running "
    "Host-scoped Terminal process. Registration never survives Host close, "
    "cancel does not kill the process, and completion is always observed."
)


class _StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class TerminalInput(_StrictInput):
    command: str = Field(min_length=1, max_length=1_048_576)
    workdir: str | None = Field(default=None, min_length=1, max_length=4096)
    terminal_session_id: str = Field(
        default="default", min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_-]+$"
    )
    yield_time_ms: int = Field(default=10_000, ge=0, le=30_000)
    tty: bool = False
    max_output_chars: int = Field(
        default=DEFAULT_MAX_OUTPUT_CHARS,
        ge=MIN_TERMINAL_OUTPUT_CHARS,
        le=DEFAULT_MAX_OUTPUT_CHARS,
    )


_TERMINAL_ADAPTER = TypeAdapter(TerminalInput)


class TerminalProcessListInput(_StrictInput):
    action: Literal["list"]
    include_running: bool = True
    include_finished: bool = True


class _ProcessInput(_StrictInput):
    process_id: str = Field(min_length=1)


class TerminalProcessLogInput(_ProcessInput):
    action: Literal["log"]
    since_cursor: str | None = None
    max_output_chars: int = Field(
        default=DEFAULT_MAX_OUTPUT_CHARS,
        ge=MIN_TERMINAL_OUTPUT_CHARS,
        le=DEFAULT_MAX_OUTPUT_CHARS,
    )


class TerminalProcessPollInput(_ProcessInput):
    action: Literal["poll"]
    since_cursor: str | None = None
    max_output_chars: int = Field(
        default=DEFAULT_MAX_OUTPUT_CHARS,
        ge=MIN_TERMINAL_OUTPUT_CHARS,
        le=DEFAULT_MAX_OUTPUT_CHARS,
    )


class TerminalProcessWaitInput(_ProcessInput):
    action: Literal["wait"]
    since_cursor: str | None = None
    timeout_seconds: int = Field(default=DEFAULT_WAIT_TIMEOUT_SECONDS, ge=1, le=30)
    max_output_chars: int = Field(
        default=DEFAULT_MAX_OUTPUT_CHARS,
        ge=MIN_TERMINAL_OUTPUT_CHARS,
        le=DEFAULT_MAX_OUTPUT_CHARS,
    )


class TerminalProcessWriteInput(_ProcessInput):
    action: Literal["write"]
    data: str


class TerminalProcessSubmitInput(_ProcessInput):
    action: Literal["submit"]
    data: str


class TerminalProcessCloseStdinInput(_ProcessInput):
    action: Literal["close_stdin"]


class TerminalProcessKillInput(_ProcessInput):
    action: Literal["kill"]


TerminalProcessInput: TypeAlias = Annotated[
    TerminalProcessListInput
    | TerminalProcessLogInput
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
    min_new_output_chars: int = Field(default=200, ge=1, le=65_536)
    quiet_period_ms: int = Field(default=500, ge=0, le=10_000)


class TerminalMonitorConditions(_StrictInput):
    output: TerminalMonitorOutputCondition | None = None
    heartbeat_interval_seconds: int | None = Field(default=None, ge=5, le=1800)


class TerminalMonitorDelivery(_StrictInput):
    max_output_chars: int = Field(default=4000, ge=512, le=32_000)
    minimum_progress_observation_interval_seconds: int = Field(
        default=5, ge=5, le=1800
    )


class TerminalMonitorLifetime(_StrictInput):
    maximum_duration_seconds: int = Field(default=36_000, ge=1, le=36_000)


class TerminalMonitorRegisterInput(_StrictInput):
    action: Literal["register"]
    process_id: str = Field(min_length=1)
    conditions: TerminalMonitorConditions = TerminalMonitorConditions()
    delivery: TerminalMonitorDelivery = TerminalMonitorDelivery()
    lifetime: TerminalMonitorLifetime = TerminalMonitorLifetime()


class TerminalMonitorListInput(_StrictInput):
    action: Literal["list"]


class TerminalMonitorCancelInput(_StrictInput):
    action: Literal["cancel"]
    monitor_id: str = Field(min_length=1)


TerminalMonitorInput: TypeAlias = Annotated[
    TerminalMonitorRegisterInput | TerminalMonitorListInput | TerminalMonitorCancelInput,
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
        if isinstance(value, list):
            return [resolve(item) for item in value]
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
        return {key: resolve(item) for key, item in value.items()}

    resolved = resolve(root)
    if not isinstance(resolved, dict):
        raise AssertionError("terminal process schema must remain an object")
    # A discriminated Pydantic union is emitted as a top-level ``oneOf`` with
    # object-shaped branches, but some OpenAI-compatible providers require the
    # function schema itself to declare its object type.  This does not widen
    # the closed action union; every accepted value must still match exactly
    # one of the strict branch schemas below it.
    resolved.setdefault("type", "object")
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
