"""Closed process-local terminal input contract."""

from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator
from pulsara_agent.identity import ID_PAYLOAD_PATTERN


DEFAULT_WAIT_TIMEOUT_SECONDS = 30
EXEC_PROCESS_TOOL_DESCRIPTION = "Start a shell command and observe its complete available public output. A PROCESS Target supports later metadata queries and wait/write/control. Initial wait does not limit process lifetime. Native NEXT reads a frozen result; JS returns the full observation."
QUERY_PROCESSES_TOOL_DESCRIPTION = "Query this session's retained process metadata without reading or consuming logs. Omit target to list all current records, or provide one PROCESS Target."
MANAGE_PROCESS_TOOL_DESCRIPTION = "Observe or control a process in this session. wait: target, optional timeout_seconds (0–30; default 30); write: target,data,optional yield_time_ms (0–30000; default 1000); close_stdin/kill: target only. wait/write return all pending public output and frozen process facts. Nonzero process exit is a successful observation. No action is rerun by NEXT."


class _StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    @model_validator(mode="before")
    @classmethod
    def reject_null_fields(cls, value):
        if isinstance(value, dict) and any(item is None for item in value.values()):
            raise ValueError("Unused fields must be omitted, not null")
        return value


class TerminalInput(_StrictInput):
    command: str = Field(
        min_length=1,
        max_length=1_048_576,
        pattern=r"\S",
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


_TERMINAL_ADAPTER = TypeAdapter(TerminalInput)


class QueryProcessesListInput(_StrictInput):
    pass


class TerminalProcessTarget(_StrictInput):
    kind: Literal["PROCESS"]
    process_id: str = Field(
        pattern=rf"^process:{ID_PAYLOAD_PATTERN}$",
        description="Exact process_id from this terminal environment.",
    )


class _ProcessInput(_StrictInput):
    target: TerminalProcessTarget


class QueryProcessInput(_ProcessInput):
    pass


QueryProcessesInput: TypeAlias = QueryProcessesListInput | QueryProcessInput
_QUERY_PROCESSES_ADAPTER = TypeAdapter(QueryProcessesInput)


class TerminalProcessWaitInput(_ProcessInput):
    action: Literal["wait"]
    timeout_seconds: int = Field(
        default=DEFAULT_WAIT_TIMEOUT_SECONDS,
        ge=0,
        le=30,
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


class TerminalProcessCloseStdinInput(_ProcessInput):
    action: Literal["close_stdin"]


class TerminalProcessKillInput(_ProcessInput):
    action: Literal["kill"]


ManageProcessInput: TypeAlias = Annotated[
    TerminalProcessWaitInput
    | TerminalProcessWriteInput
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


def terminal_result_schema(name: str) -> dict[str, Any]:
    """Complete terminal business snapshots; process state is not call status."""
    from pulsara_agent.ports.tool_execution import tool_business_value_schema

    target = _inline_schema_references(TypeAdapter(TerminalProcessTarget).json_schema())
    if name in {"exec_process", "manage_process"}:
        success = [tool_business_value_schema(
            fields={"target": target}, required_fields=("target",),
            meta={"process_status": {"const": state}, "exit_code": code,
                  "cwd": {"type": "string"}},
            required_meta=("process_status", "exit_code", "cwd"),
        ) for state, code in (
            ("RUNNING", {"type": "null"}), ("EXITED", {"type": "integer"}),
        )]
        return {"oneOf": [*success, tool_business_value_schema(
            fields={"target": target},
            required_fields=("target",) if name == "manage_process" else (),
            statuses=("ERROR", "CANCELLED"),
        )]}
    if name != "query_processes":
        raise ValueError("Unknown terminal tool")
    common = {
        "target": target, "command": {"type": "string"}, "cwd": {"type": "string"},
        "stdin_closed": {"type": "boolean"}, "duration_seconds": {"type": "number", "minimum": 0},
    }
    records = [{"type": "object", "properties": {
        **common, "process_status": {"const": state}, "exit_code": code,
    }, "required": [*common, "process_status", "exit_code"], "additionalProperties": False}
        for state, code in (("RUNNING", {"type": "null"}), ("EXITED", {"type": "integer"}))]
    content = {"type": "array", "items": {"oneOf": [
        {"type": "object", "properties": {"type": {"const": "text"}, "value": {"type": "string"}},
            "required": ["type", "value"], "additionalProperties": False},
        {"type": "object", "properties": {"type": {"const": "object"}, "value": {"oneOf": records}},
            "required": ["type", "value"], "additionalProperties": False},
    ]}}
    return {"oneOf": [tool_business_value_schema(content_schema=content),
        tool_business_value_schema(statuses=("ERROR", "CANCELLED"))]}


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
                resolved[key] = [
                    resolve(child)
                    for child in item
                    if not (
                        key == "anyOf"
                        and isinstance(child, dict)
                        and child.get("type") == "null"
                    )
                ]
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
                if key == "default" and item is None:
                    continue
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
    "terminal_result_schema",
    "manage_process_input_schema",
]
