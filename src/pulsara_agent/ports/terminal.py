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
    "Run one shell command on the local host when the current run permission allows "
    "terminal access. workdir may be any existing local directory; relative paths "
    "use the current working directory remembered for terminal_session_id. The "
    "workspace is the initial working directory, not a sandbox in an authorized "
    "terminal run. Omit workdir to remain in the remembered directory; explicitly "
    "supplying it also makes that directory the session's cwd for later commands. "
    "Omit terminal_session_id to use the default session. Set "
    "tty=true only for programs that need interactive terminal behavior, such as line "
    "editing or a full-screen interface. yield_time_ms controls how long this call waits "
    "initially (0-30000 ms); it does not stop or limit the command. If the response has "
    "status=running, copy its process_id exactly into terminal_process to check status or "
    "output now, wait briefly, send input, close input, or stop the command. In the main "
    "conversation, use terminal_monitor when a long-running command should resume the "
    "conversation later after completion or configured progress instead of repeatedly "
    "checking it. If status is not running, the command has ended or could not start; "
    "use the returned output and do not manage or monitor it. max_output_chars limits "
    "this response, not the command. If the response suggests artifact_read, use it to "
    "read output omitted from the response. Prefer file tools for file reads and edits; "
    "use terminal for tests, builds, git, scripts, package managers, network commands, "
    "and external CLIs."
)
TERMINAL_PROCESS_TOOL_DESCRIPTION = (
    "Perform one immediate follow-up action on commands started by terminal. Actions: "
    "list shows current process records; poll checks current status and output without "
    "waiting; log reads output currently kept for later reading; wait waits for up to "
    "timeout_seconds in this call; write sends data to command input without adding a "
    "newline; submit sends data followed by a newline; close_stdin tells the command "
    "that no more input will be sent without stopping it; and kill stops the command and "
    "its child processes, then waits for them to exit. Except for list, copy the exact "
    "process_id returned by terminal or an earlier response for that process. A "
    "process_id is temporary: it works only in the terminal environment that created it "
    "and cannot be reused after that environment closes or is replaced. For log, poll, "
    "or wait, since_cursor may be copied unchanged from the same process's earlier "
    "output_cursor to request output after that "
    "position; omit it to read the output available now. A cursor may be too old or "
    "invalid, which the response will report. poll, log, and wait return only this call's "
    "result; they do not send another update later. A background process may also be "
    "stopped by the user outside a tool call. An empty terminal_monitor list does not "
    "prove that a process ended: when the user asks for current status, or the next step "
    "depends on that process, query its exact process_id with terminal_process. Avoid "
    "repeated polling without a new reason. After a user-control termination result, "
    "continue other useful work when available; that result is a fact about the process, "
    "not authorization to restart it. For "
    "a long-running command, use terminal_monitor from the main conversation when "
    "available, or continue other useful work. max_output_chars limits the response, not "
    "the command. If the response suggests artifact_read, use it to read omitted output."
)
TERMINAL_MONITOR_TOOL_DESCRIPTION = (
    "Available only in the main conversation. Use this tool for a terminal command that "
    "still has status=running when you want the conversation to resume later after "
    "completion or configured progress instead of polling. Actions are register, list, "
    "and cancel. register returns immediately: copy the running process_id exactly, then "
    "copy the returned monitor_id exactly if later cancellation is needed. Omit "
    "conditions to receive only a completion update; completion reporting remains "
    "enabled while the monitor is active. Add an output condition or "
    "heartbeat_interval_seconds only when progress updates are useful, because each "
    "update may cause the agent to run again. delivery controls how much output a later "
    "update includes and the minimum interval between progress updates; lifetime controls "
    "how long monitoring remains active. list shows active monitors. Later output is "
    "limited; use terminal_process with action=log for more. After register, continue "
    "other independent work; when there is no other work, the current turn may end and "
    "the monitor can resume the conversation later; do not poll merely to wait. An empty "
    "monitor list does not prove that its process ended. cancel stops later updates but "
    "does not stop the command; use "
    "terminal_process with action=kill to stop it. Monitoring ends when its lifetime "
    "expires or the current terminal environment closes or is replaced."
)


class _StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class TerminalInput(_StrictInput):
    command: str = Field(
        min_length=1,
        max_length=1_048_576,
        description="Shell command to run.",
    )
    workdir: str | None = Field(
        default=None,
        min_length=1,
        max_length=4096,
        description=(
            "Optional existing local working directory. Relative paths use the "
            "current working directory remembered for terminal_session_id; absolute "
            "paths and ~ are accepted when terminal access is authorized. Supplying "
            "this field updates the remembered directory for later commands."
        ),
    )
    terminal_session_id: str = Field(
        default="default",
        min_length=1,
        max_length=32,
        pattern=r"^[A-Za-z0-9_-]+$",
        description=(
            "Selects a separately remembered working directory for sequential commands. "
            "Omit to use the default session; this is not a process_id."
        ),
    )
    yield_time_ms: int = Field(
        default=10_000,
        ge=0,
        le=30_000,
        description=(
            "How long this call waits initially. If the command is still running, the "
            "response has status=running; this setting does not stop the command."
        ),
    )
    tty: bool = Field(
        default=False,
        description=(
            "Use interactive terminal behavior only when the program needs line editing, "
            "a full-screen interface, or similar interaction."
        ),
    )
    max_output_chars: int = Field(
        default=DEFAULT_MAX_OUTPUT_CHARS,
        ge=MIN_TERMINAL_OUTPUT_CHARS,
        le=DEFAULT_MAX_OUTPUT_CHARS,
        description=(
            "Maximum output characters to include in this response; this does not stop "
            "or limit the command."
        ),
    )


_TERMINAL_ADAPTER = TypeAdapter(TerminalInput)


class TerminalProcessListInput(_StrictInput):
    action: Literal["list"]
    include_running: bool = Field(
        default=True, description="Include processes that are still running."
    )
    include_finished: bool = Field(
        default=True,
        description="Include records still available for commands that have finished.",
    )


class _ProcessInput(_StrictInput):
    process_id: str = Field(
        min_length=1,
        description="Copy the exact process_id returned for this command.",
    )


class TerminalProcessLogInput(_ProcessInput):
    action: Literal["log"]
    since_cursor: str | None = Field(
        default=None,
        description=(
            "Optional output_cursor copied unchanged from an earlier response for this "
            "exact process; requests output after that position."
        ),
    )
    max_output_chars: int = Field(
        default=DEFAULT_MAX_OUTPUT_CHARS,
        ge=MIN_TERMINAL_OUTPUT_CHARS,
        le=DEFAULT_MAX_OUTPUT_CHARS,
        description="Maximum output characters to include in this response.",
    )


class TerminalProcessPollInput(_ProcessInput):
    action: Literal["poll"]
    since_cursor: str | None = Field(
        default=None,
        description=(
            "Optional output_cursor copied unchanged from an earlier response for this "
            "exact process; requests output after that position."
        ),
    )
    max_output_chars: int = Field(
        default=DEFAULT_MAX_OUTPUT_CHARS,
        ge=MIN_TERMINAL_OUTPUT_CHARS,
        le=DEFAULT_MAX_OUTPUT_CHARS,
        description="Maximum output characters to include in this response.",
    )


class TerminalProcessWaitInput(_ProcessInput):
    action: Literal["wait"]
    since_cursor: str | None = Field(
        default=None,
        description=(
            "Optional output_cursor copied unchanged from an earlier response for this "
            "exact process; requests output after that position."
        ),
    )
    timeout_seconds: int = Field(
        default=DEFAULT_WAIT_TIMEOUT_SECONDS,
        ge=1,
        le=30,
        description=(
            "How long this call waits for the command to finish. If it is still running, "
            "the call returns without arranging a later update."
        ),
    )
    max_output_chars: int = Field(
        default=DEFAULT_MAX_OUTPUT_CHARS,
        ge=MIN_TERMINAL_OUTPUT_CHARS,
        le=DEFAULT_MAX_OUTPUT_CHARS,
        description="Maximum output characters to include in this response.",
    )


class TerminalProcessWriteInput(_ProcessInput):
    action: Literal["write"]
    data: str = Field(description="Exact text to send without adding a newline.")


class TerminalProcessSubmitInput(_ProcessInput):
    action: Literal["submit"]
    data: str = Field(description="Line text to submit without the final newline.")


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
    min_new_output_chars: int = Field(
        default=200,
        ge=1,
        le=65_536,
        description="Minimum amount of new output before a progress update may be sent.",
    )
    quiet_period_ms: int = Field(
        default=500,
        ge=0,
        le=10_000,
        description=(
            "How long output must stop changing before a progress update may be sent."
        ),
    )


class TerminalMonitorConditions(_StrictInput):
    output: TerminalMonitorOutputCondition | None = Field(
        default=None,
        description=(
            "Optional rule for sending output-progress updates before the command ends."
        ),
    )
    heartbeat_interval_seconds: int | None = Field(
        default=None,
        ge=5,
        le=1800,
        description=(
            "Optional interval for progress updates even without enough new output. "
            "Completion updates remain enabled."
        ),
    )


class TerminalMonitorDelivery(_StrictInput):
    max_output_chars: int = Field(
        default=4000,
        ge=512,
        le=32_000,
        description="Maximum command-output characters in each later update.",
    )
    minimum_progress_observation_interval_seconds: int = Field(
        default=5,
        ge=5,
        le=1800,
        description="Minimum interval between progress updates.",
    )


class TerminalMonitorLifetime(_StrictInput):
    maximum_duration_seconds: int = Field(
        default=36_000,
        ge=1,
        le=36_000,
        description=(
            "Stop monitoring after this many seconds even if the command is still "
            "running. Closing or replacing the current terminal environment may stop it "
            "sooner."
        ),
    )


class TerminalMonitorRegisterInput(_StrictInput):
    action: Literal["register"]
    process_id: str = Field(
        min_length=1,
        description="Exact process_id from a status=running terminal result.",
    )
    conditions: TerminalMonitorConditions = Field(
        default=TerminalMonitorConditions(),
        description=(
            "Rules for progress updates before the command ends. Omit both options to "
            "receive only a completion update."
        ),
    )
    delivery: TerminalMonitorDelivery = Field(
        default=TerminalMonitorDelivery(),
        description="Controls output size and minimum spacing for later updates.",
    )
    lifetime: TerminalMonitorLifetime = Field(
        default=TerminalMonitorLifetime(),
        description="Controls how long this monitor remains active.",
    )


class TerminalMonitorListInput(_StrictInput):
    action: Literal["list"]


class TerminalMonitorCancelInput(_StrictInput):
    action: Literal["cancel"]
    monitor_id: str = Field(
        min_length=1,
        description=(
            "Copy the exact monitor_id returned by terminal_monitor with action=register."
        ),
    )


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
