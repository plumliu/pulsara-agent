"""Single public input contract for terminal process and monitor tools."""

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
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.terminal_observation import (
    TerminalProcessMonitorConditionsFact,
    TerminalProcessMonitorDeliveryPolicyFact,
    TerminalProcessMonitorLifetimeFact,
    TerminalProcessMonitorOutputConditionFact,
    TerminalProcessMonitorPolicyFact,
)


DEFAULT_MAX_OUTPUT_CHARS = 32_000
MIN_TERMINAL_OUTPUT_CHARS = 512
DEFAULT_WAIT_TIMEOUT_SECONDS = 30
DEFAULT_MONITOR_OUTPUT_CHARS = 4_000
DEFAULT_MONITOR_OUTPUT_THRESHOLD_CHARS = 200
DEFAULT_MONITOR_QUIET_PERIOD_MS = 500
DEFAULT_MONITOR_PROGRESS_INTERVAL_SECONDS = 5
DEFAULT_MONITOR_DURATION_SECONDS = 10 * 60 * 60
MAXIMUM_MONITOR_PROGRESS_OBSERVATIONS = 119
MONITOR_PROGRESS_RATE_WINDOW_SECONDS = 600
MAXIMUM_MONITOR_PROGRESS_OBSERVATIONS_PER_WINDOW = 60


TERMINAL_TOOL_DESCRIPTION = (
    "Start one shell command inside workspace_root and wait for up to yield_time_ms. "
    "Possible status values are running, success, error, timeout, blocked, and killed. "
    "If status is not running, that invocation has no live process to manage; do not "
    "call terminal_process or terminal_monitor for it. If status is running, copy the "
    "exact process_id from this ToolResult; never invent or rewrite it. Use "
    "terminal_process.wait once when the command is expected to finish within 30 "
    "seconds, terminal_process.poll for one immediate lifecycle check, "
    "terminal_process.log for one immediate retained-output read, or "
    "terminal_monitor.register for future notifications from a long-running process. "
    "Inline output is bounded; when artifacts[] is present, use artifact_read for the "
    "complete retained tool output. Example: "
    'terminal({"command":"uv run pytest -q","yield_time_ms":10000}) -> '
    '{"status":"success",...}; do not pass it to terminal_process or terminal_monitor. '
    "Use the file tools for file operations; reserve terminal for builds, tests, git, "
    "package managers, scripts, network commands, and external CLIs."
)


TERMINAL_PROCESS_TOOL_DESCRIPTION = (
    "Perform an immediate operation on managed processes. Except for list, use the exact "
    "process_id returned by terminal. Actions: list; poll for current lifecycle state "
    "and bounded output; log for retained output; wait once for up to 30 seconds in this "
    "tool call; write without a newline; submit with a newline; close_stdin only when "
    "the program requires EOF; and kill to terminate the process. poll, log, and wait "
    "return only a current ToolResult and never arrange a future wake. Choose poll or "
    "log according to the immediate need; do not call both back-to-back by default. "
    "Returned output is bounded; when artifacts[] is present, use artifact_read for the "
    "complete retained output. If wait still returns running, do not loop wait: use "
    "terminal_monitor.register for a long wait, continue other useful work, or finish "
    "the turn. Example: "
    'terminal_process({"action":"wait","process_id":"<copy exact process_id>",'
    '"timeout_seconds":30}). To stop the process use kill. To stop only future '
    "notifications while leaving the process running, use terminal_monitor.cancel "
    "instead; kill and monitor cancellation are alternative choices."
)


TERMINAL_MONITOR_TOOL_DESCRIPTION = (
    "Register, list, or cancel persistent Host-owned notifications for a managed "
    "process. register returns immediately: copy the exact process_id from terminal, "
    "then copy the exact monitor_id from the registration ToolResult for later cancel. "
    "For a normal long task, omit conditions to disable progress and heartbeat; "
    "completion remains monitored until the monitor's bounded expiry. Add an output "
    "condition or heartbeat only when the user or task actually requires progress or "
    "periodic reports, because each delivery may cause a model call. Progress, "
    "heartbeat, completion, and expiry observations arrive later through the Host "
    "runtime. Example: "
    'terminal_monitor({"action":"register","process_id":"<copy exact process_id>"}); '
    "after registration succeeds, do not poll merely to wait. list returns current "
    "monitors. cancel stops future notifications but leaves the process running; use "
    "terminal_process.kill instead when the process itself must stop."
)


class _StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class TerminalProcessListInput(_StrictInput):
    action: Literal["list"] = Field(description="List managed terminal processes.")
    include_running: bool = Field(
        default=True, description="Include processes that are still running."
    )
    include_finished: bool = Field(
        default=True,
        description="Include processes that have reached a terminal state.",
    )


class TerminalProcessLogInput(_StrictInput):
    action: Literal["log"] = Field(description="Read retained process output.")
    process_id: str = Field(
        min_length=1,
        description="Managed process identifier returned by terminal.",
    )
    max_output_chars: int = Field(
        default=DEFAULT_MAX_OUTPUT_CHARS,
        ge=MIN_TERMINAL_OUTPUT_CHARS,
        le=DEFAULT_MAX_OUTPUT_CHARS,
        description="Maximum number of retained output characters to return.",
    )


class TerminalProcessPollInput(_StrictInput):
    action: Literal["poll"] = Field(
        description="Read the current process state without waiting."
    )
    process_id: str = Field(
        min_length=1,
        description="Managed process identifier returned by terminal.",
    )
    max_output_chars: int = Field(
        default=DEFAULT_MAX_OUTPUT_CHARS,
        ge=MIN_TERMINAL_OUTPUT_CHARS,
        le=DEFAULT_MAX_OUTPUT_CHARS,
        description="Maximum number of retained output characters to return.",
    )


class TerminalProcessWaitInput(_StrictInput):
    action: Literal["wait"] = Field(
        description="Wait briefly for completion in the current tool call."
    )
    process_id: str = Field(
        min_length=1,
        description="Managed process identifier returned by terminal.",
    )
    timeout_seconds: int = Field(
        default=DEFAULT_WAIT_TIMEOUT_SECONDS,
        ge=1,
        le=30,
        description="Maximum foreground wait in seconds.",
    )
    max_output_chars: int = Field(
        default=DEFAULT_MAX_OUTPUT_CHARS,
        ge=MIN_TERMINAL_OUTPUT_CHARS,
        le=DEFAULT_MAX_OUTPUT_CHARS,
        description="Maximum number of retained output characters to return.",
    )


class TerminalProcessWriteInput(_StrictInput):
    action: Literal["write"] = Field(
        description="Write text to standard input without submitting a line."
    )
    process_id: str = Field(
        min_length=1,
        description="Managed process identifier returned by terminal.",
    )
    data: str = Field(description="Text to write to standard input.")


class TerminalProcessSubmitInput(_StrictInput):
    action: Literal["submit"] = Field(
        description="Submit one line to process standard input."
    )
    process_id: str = Field(
        min_length=1,
        description="Managed process identifier returned by terminal.",
    )
    data: str = Field(description="Line text to submit.")


class TerminalProcessCloseStdinInput(_StrictInput):
    action: Literal["close_stdin"] = Field(
        description="Close the process standard input stream."
    )
    process_id: str = Field(
        min_length=1,
        description="Managed process identifier returned by terminal.",
    )


class TerminalProcessKillInput(_StrictInput):
    action: Literal["kill"] = Field(description="Terminate a managed process.")
    process_id: str = Field(
        min_length=1,
        description="Managed process identifier returned by terminal.",
    )


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


class TerminalMonitorOutputConditionInput(_StrictInput):
    min_new_output_chars: int = Field(
        default=DEFAULT_MONITOR_OUTPUT_THRESHOLD_CHARS,
        ge=1,
        le=65_536,
        description="Minimum sanitized output growth needed for a progress observation.",
    )
    quiet_period_ms: int = Field(
        default=DEFAULT_MONITOR_QUIET_PERIOD_MS,
        ge=0,
        le=10_000,
        description="Quiet period after output growth before forming an observation.",
    )


class TerminalMonitorConditionsInput(_StrictInput):
    output: TerminalMonitorOutputConditionInput | None = Field(
        default=None,
        description="Optional progress condition over newly sanitized process output.",
    )
    heartbeat_interval_seconds: int | None = Field(
        default=None,
        ge=5,
        le=1_800,
        description="Optional heartbeat interval; completion is always monitored.",
    )


class TerminalMonitorDeliveryInput(_StrictInput):
    max_output_chars: int = Field(
        default=DEFAULT_MONITOR_OUTPUT_CHARS,
        ge=MIN_TERMINAL_OUTPUT_CHARS,
        le=DEFAULT_MAX_OUTPUT_CHARS,
        description="Maximum output characters included in each observation.",
    )
    minimum_progress_observation_interval_seconds: int = Field(
        default=DEFAULT_MONITOR_PROGRESS_INTERVAL_SECONDS,
        ge=5,
        le=1_800,
        description="Minimum interval between committed progress observations.",
    )


class TerminalMonitorLifetimeInput(_StrictInput):
    maximum_duration_seconds: int = Field(
        default=DEFAULT_MONITOR_DURATION_SECONDS,
        ge=1,
        le=DEFAULT_MONITOR_DURATION_SECONDS,
        description="Maximum monitor lifetime in seconds.",
    )


class TerminalMonitorRegisterInput(_StrictInput):
    action: Literal["register"] = Field(
        description="Register a persistent Host-owned process monitor."
    )
    process_id: str = Field(
        min_length=1,
        description="Managed process identifier returned by terminal.",
    )
    conditions: TerminalMonitorConditionsInput = Field(
        default_factory=TerminalMonitorConditionsInput,
        description="Progress and heartbeat conditions; completion is implicit.",
        json_schema_extra={
            "default": {"output": None, "heartbeat_interval_seconds": None}
        },
    )
    delivery: TerminalMonitorDeliveryInput = Field(
        default_factory=TerminalMonitorDeliveryInput,
        description="Bounded observation delivery policy.",
        json_schema_extra={
            "default": {
                "max_output_chars": DEFAULT_MONITOR_OUTPUT_CHARS,
                "minimum_progress_observation_interval_seconds": (
                    DEFAULT_MONITOR_PROGRESS_INTERVAL_SECONDS
                ),
            }
        },
    )
    lifetime: TerminalMonitorLifetimeInput = Field(
        default_factory=TerminalMonitorLifetimeInput,
        description="Bounded monitor lifetime policy.",
        json_schema_extra={
            "default": {"maximum_duration_seconds": DEFAULT_MONITOR_DURATION_SECONDS}
        },
    )


class TerminalMonitorListInput(_StrictInput):
    action: Literal["list"] = Field(description="List current Host-owned monitors.")


class TerminalMonitorCancelInput(_StrictInput):
    action: Literal["cancel"] = Field(
        description="Cancel a monitor without terminating its process."
    )
    monitor_id: str = Field(
        min_length=1,
        description="Monitor identifier returned by terminal_monitor.register.",
    )


TerminalMonitorInput: TypeAlias = Annotated[
    TerminalMonitorRegisterInput
    | TerminalMonitorListInput
    | TerminalMonitorCancelInput,
    Field(discriminator="action"),
]


_TERMINAL_PROCESS_ADAPTER = TypeAdapter(TerminalProcessInput)
_TERMINAL_MONITOR_ADAPTER = TypeAdapter(TerminalMonitorInput)


@dataclass(frozen=True, slots=True)
class BuiltinToolInputContractBinding:
    tool_name: str
    input_adapter: TypeAdapter[Any]
    frozen_input_schema: FrozenJsonObjectFact
    input_schema_fingerprint: str

    @property
    def input_schema(self) -> dict[str, Any]:
        return thaw_json(self.frozen_input_schema)

    def schema_copy(self) -> dict[str, Any]:
        return self.input_schema


@dataclass(frozen=True, slots=True)
class ResolvedTerminalMonitorPublicPolicy:
    conditions: TerminalProcessMonitorConditionsFact
    delivery: TerminalProcessMonitorDeliveryPolicyFact
    lifetime: TerminalProcessMonitorLifetimeFact
    policy: TerminalProcessMonitorPolicyFact


def parse_terminal_process_input(arguments: object) -> TerminalProcessInput:
    return _TERMINAL_PROCESS_ADAPTER.validate_python(arguments, strict=True)


def parse_terminal_monitor_input(arguments: object) -> TerminalMonitorInput:
    return _TERMINAL_MONITOR_ADAPTER.validate_python(arguments, strict=True)


def resolve_terminal_monitor_public_policy(
    value: TerminalMonitorRegisterInput,
) -> ResolvedTerminalMonitorPublicPolicy:
    output_input = value.conditions.output
    output = (
        None
        if output_input is None
        else build_frozen_fact(
            TerminalProcessMonitorOutputConditionFact,
            schema_version="terminal_process_monitor_output_condition.v1",
            min_new_output_chars=output_input.min_new_output_chars,
            quiet_period_ms=output_input.quiet_period_ms,
        )
    )
    conditions = build_frozen_fact(
        TerminalProcessMonitorConditionsFact,
        schema_version="terminal_process_monitor_conditions.v1",
        output=output,
        heartbeat_interval_seconds=value.conditions.heartbeat_interval_seconds,
    )
    delivery = build_frozen_fact(
        TerminalProcessMonitorDeliveryPolicyFact,
        schema_version="terminal_process_monitor_delivery_policy.v1",
        max_output_chars=value.delivery.max_output_chars,
        minimum_progress_observation_interval_seconds=(
            value.delivery.minimum_progress_observation_interval_seconds
        ),
        maximum_pending_progress_observations=1,
        maximum_committed_progress_observations=(MAXIMUM_MONITOR_PROGRESS_OBSERVATIONS),
        progress_observation_rate_window_seconds=(MONITOR_PROGRESS_RATE_WINDOW_SECONDS),
        maximum_progress_observations_per_rate_window=(
            MAXIMUM_MONITOR_PROGRESS_OBSERVATIONS_PER_WINDOW
        ),
    )
    lifetime = build_frozen_fact(
        TerminalProcessMonitorLifetimeFact,
        schema_version="terminal_process_monitor_lifetime.v1",
        kind="process_lifetime",
        maximum_duration_seconds=value.lifetime.maximum_duration_seconds,
    )
    policy = build_frozen_fact(
        TerminalProcessMonitorPolicyFact,
        schema_version="terminal_process_monitor_policy.v1",
        conditions=conditions,
        delivery=delivery,
        lifetime=lifetime,
    )
    return ResolvedTerminalMonitorPublicPolicy(
        conditions=conditions,
        delivery=delivery,
        lifetime=lifetime,
        policy=policy,
    )


@lru_cache(maxsize=2)
def builtin_tool_input_contract_binding(
    tool_name: Literal["terminal_process", "terminal_monitor"],
) -> BuiltinToolInputContractBinding:
    adapter = (
        _TERMINAL_PROCESS_ADAPTER
        if tool_name == "terminal_process"
        else _TERMINAL_MONITOR_ADAPTER
    )
    schema = _inline_schema_references(adapter.json_schema())
    frozen_schema = freeze_json(schema)
    if not isinstance(frozen_schema, FrozenJsonObjectFact):
        raise AssertionError("terminal public input schema must freeze as an object")
    return BuiltinToolInputContractBinding(
        tool_name=tool_name,
        input_adapter=adapter,
        frozen_input_schema=frozen_schema,
        input_schema_fingerprint=context_fingerprint(
            "builtin-tool-input-schema:v1", [tool_name, schema]
        ),
    )


def terminal_process_input_schema() -> dict[str, Any]:
    return builtin_tool_input_contract_binding("terminal_process").schema_copy()


def terminal_monitor_input_schema() -> dict[str, Any]:
    return builtin_tool_input_contract_binding("terminal_monitor").schema_copy()


def _inline_schema_references(schema: dict[str, Any]) -> dict[str, Any]:
    raw = deepcopy(schema)
    definitions = raw.pop("$defs", {})

    def expand(value: Any) -> Any:
        if isinstance(value, list):
            return [expand(item) for item in value]
        if not isinstance(value, dict):
            return value
        reference = value.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            name = reference.removeprefix("#/$defs/")
            target = definitions.get(name)
            if not isinstance(target, dict):
                raise ValueError(f"unknown terminal input schema reference: {name}")
            siblings = {key: item for key, item in value.items() if key != "$ref"}
            return {**expand(target), **expand(siblings)}
        return {key: expand(item) for key, item in value.items() if key != "title"}

    inlined = expand(raw)
    if not isinstance(inlined, dict):
        raise TypeError("terminal input schema must be an object")
    # OpenAI-compatible providers require function parameters to declare an
    # object at the root even when a discriminated union is expressed by oneOf.
    inlined["type"] = "object"
    # Provider schemas need the oneOf branches, while Pydantic's discriminator
    # mapping points at the removed local $defs and is only useful to the parser.
    inlined.pop("discriminator", None)
    return inlined


__all__ = [
    "BuiltinToolInputContractBinding",
    "DEFAULT_MAX_OUTPUT_CHARS",
    "DEFAULT_MONITOR_DURATION_SECONDS",
    "DEFAULT_MONITOR_OUTPUT_CHARS",
    "DEFAULT_WAIT_TIMEOUT_SECONDS",
    "MAXIMUM_MONITOR_PROGRESS_OBSERVATIONS",
    "MAXIMUM_MONITOR_PROGRESS_OBSERVATIONS_PER_WINDOW",
    "MIN_TERMINAL_OUTPUT_CHARS",
    "MONITOR_PROGRESS_RATE_WINDOW_SECONDS",
    "ResolvedTerminalMonitorPublicPolicy",
    "TERMINAL_MONITOR_TOOL_DESCRIPTION",
    "TERMINAL_PROCESS_TOOL_DESCRIPTION",
    "TERMINAL_TOOL_DESCRIPTION",
    "TerminalMonitorCancelInput",
    "TerminalMonitorInput",
    "TerminalMonitorListInput",
    "TerminalMonitorRegisterInput",
    "TerminalProcessCloseStdinInput",
    "TerminalProcessInput",
    "TerminalProcessKillInput",
    "TerminalProcessListInput",
    "TerminalProcessLogInput",
    "TerminalProcessPollInput",
    "TerminalProcessSubmitInput",
    "TerminalProcessWaitInput",
    "TerminalProcessWriteInput",
    "builtin_tool_input_contract_binding",
    "parse_terminal_monitor_input",
    "parse_terminal_process_input",
    "resolve_terminal_monitor_public_policy",
    "terminal_monitor_input_schema",
    "terminal_process_input_schema",
]
