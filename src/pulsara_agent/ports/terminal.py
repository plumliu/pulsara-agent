"""Closed process-local terminal input contract."""

from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter



DEFAULT_MAX_OUTPUT_CHARS = 32_000
MIN_TERMINAL_OUTPUT_CHARS = 512
DEFAULT_WAIT_TIMEOUT_SECONDS = 30

EXEC_PROCESS_TOOL_DESCRIPTION = (
    "Run one local shell command. Each call starts a new shell, so its directory and "
    "environment changes stay within that call. A running command returns a PROCESS "
    "target for query_processes metadata or manage_process output and interaction. "
    "Response "
    "size and initial wait settings do not limit the command's lifetime. Use "
    "read_artifact for omitted output or export_artifact for extensive processing."
)

QUERY_PROCESSES_TOOL_DESCRIPTION = (
    "Query managed process metadata without returning logs or consuming output. To "
    "list processes, omit target; optional fields: include_running, include_finished. "
    "To inspect one process's command and current metadata, pass only its PROCESS "
    "target, without list filters. For output, use manage_process with poll or wait. "
    "Omit unused fields; target does not accept null."
)

MANAGE_PROCESS_TOOL_DESCRIPTION = (
    "Control commands started by exec_process. Copy a PROCESS target from process "
    "results; reuse it across actions. Fields listed for an action are required "
    "unless marked optional; use only that action's fields.\n"
    "poll: target (PROCESS); optional since_cursor, max_output_chars.\n"
    "wait: target (PROCESS); optional since_cursor, timeout_seconds, max_output_chars.\n"
    "write: target (PROCESS), data; optional yield_time_ms.\n"
    "submit: target (PROCESS), data; optional yield_time_ms.\n"
    "close_stdin: target (PROCESS).\n"
    "kill: target (PROCESS).\n"
    "poll reads now; wait waits up to timeout_seconds. write sends data unchanged; "
    "submit unconditionally adds one LF to data. close_stdin closes input; kill stops "
    "the command and its children and waits for exit. Omitted optional fields use "
    "their defaults; only since_cursor accepts null. status and exit_code describe "
    "process completion; output or echo alone does not establish completion. Use "
    "manage_process wait for later updates, read_artifact for omitted logs, and "
    "export_artifact for extensive retrieval."
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


class QueryProcessesListInput(_StrictInput):
    include_running: bool = Field(
        default=True,
    )
    include_finished: bool = Field(
        default=True,
    )


class TerminalProcessTarget(_StrictInput):
    kind: Literal["PROCESS"]
    process_id: str = Field(
        min_length=1,
        description="Exact process_id from this terminal environment.",
    )


class _ProcessInput(_StrictInput):
    target: TerminalProcessTarget


class QueryProcessInput(_ProcessInput):
    pass


QueryProcessesInput: TypeAlias = QueryProcessesListInput | QueryProcessInput
_QUERY_PROCESSES_ADAPTER = TypeAdapter(QueryProcessesInput)


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


ManageProcessInput: TypeAlias = Annotated[
    TerminalProcessPollInput
    | TerminalProcessWaitInput
    | TerminalProcessWriteInput
    | TerminalProcessSubmitInput
    | TerminalProcessCloseStdinInput
    | TerminalProcessKillInput,
    Field(discriminator="action"),
]

_MANAGE_PROCESS_ADAPTER = TypeAdapter(ManageProcessInput)


def parse_exec_process_input(arguments: object) -> TerminalInput:
    return _TERMINAL_ADAPTER.validate_python(arguments, strict=True)


def parse_query_processes_input(arguments: object) -> QueryProcessesInput:
    return _QUERY_PROCESSES_ADAPTER.validate_python(arguments, strict=True)


def parse_manage_process_input(arguments: object) -> ManageProcessInput:
    return _MANAGE_PROCESS_ADAPTER.validate_python(arguments, strict=True)


@lru_cache(maxsize=1)
def exec_process_input_schema() -> dict[str, Any]:
    return _inline_schema_references(_TERMINAL_ADAPTER.json_schema())


@lru_cache(maxsize=1)
def query_processes_input_schema() -> dict[str, Any]:
    return _inline_schema_references(_QUERY_PROCESSES_ADAPTER.json_schema())


def manage_process_input_schema() -> dict[str, Any]:
    return _inline_schema_references(_MANAGE_PROCESS_ADAPTER.json_schema())


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
    "EXEC_PROCESS_TOOL_DESCRIPTION",
    "QUERY_PROCESSES_TOOL_DESCRIPTION",
    "MANAGE_PROCESS_TOOL_DESCRIPTION",
    "TerminalInput",
    "ManageProcessInput",
    "QueryProcessesInput",
    "QueryProcessesListInput",
    "QueryProcessInput",
    "parse_exec_process_input",
    "parse_query_processes_input",
    "parse_manage_process_input",
    "exec_process_input_schema",
    "query_processes_input_schema",
    "manage_process_input_schema",
]
