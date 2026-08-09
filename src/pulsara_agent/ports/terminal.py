"""Single public input contract for terminal process and monitor tools."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from enum import StrEnum
from functools import lru_cache
from typing import TYPE_CHECKING, Annotated, Any, Literal, Protocol, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from pulsara_agent.primitives.context import (
    ContextEventReferenceFact,
    FrozenJsonObjectFact,
    context_fingerprint,
    freeze_json,
    thaw_json,
)
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.terminal_observation import (
    TerminalMonitorLifecycleState,
    TerminalNotificationReservationFact,
    TerminalProcessMonitorCancellationSemanticFact,
    TerminalProcessMonitorConditionsFact,
    TerminalProcessMonitorCoreStateFact,
    TerminalProcessMonitorDeliveryPolicyFact,
    TerminalProcessMonitorLifetimeFact,
    TerminalProcessMonitorOutputConditionFact,
    TerminalProcessMonitorPolicyFact,
    TerminalProcessMonitorRegistrationAttributionFact,
    TerminalProcessMonitorRegistrationSemanticFact,
    TerminalProcessObservationReceiptFact,
    TerminalProcessObservationSemanticFact,
)
from pulsara_agent.ports.tool_execution import (
    ToolInvocationOwnerKind,
    ToolPermissionInvocation,
)

if TYPE_CHECKING:
    from pulsara_agent.event import (
        AgentEvent,
        EventContext,
        TerminalProcessMonitorRegisteredEvent,
    )


@dataclass(frozen=True, slots=True)
class PreparedTerminalNotificationReservation:
    reservation: TerminalNotificationReservationFact
    expected_account_revision: int
    expected_account_state_fingerprint: str


class TerminalBackendType(StrEnum):
    LOCAL = "local"


class TerminalIOMode(StrEnum):
    PIPE = "pipe"
    PTY = "pty"


class TerminalStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"
    TIMEOUT = "timeout"
    BLOCKED = "blocked"
    KILLED = "killed"


@dataclass(frozen=True, slots=True)
class TerminalResult:
    status: TerminalStatus
    output: str
    exit_code: int
    cwd: str
    timed_out: bool
    truncated: bool
    error: str | None
    process_id: str | None
    full_output_text: str | None
    metadata: FrozenJsonObjectFact
    observation_semantic: TerminalProcessObservationSemanticFact | None
    completion_event_reference: ContextEventReferenceFact | None

    def to_payload(
        self,
        *,
        terminal_session_id: str,
        backend_type: str,
    ) -> dict[str, object]:
        metadata = thaw_json(self.metadata)
        payload: dict[str, object] = {
            "status": self.status.value,
            "output": self.output,
            "exit_code": self.exit_code,
            "cwd": self.cwd,
            "timed_out": self.timed_out,
            "truncated": self.truncated,
            "error": self.error,
            "process_id": self.process_id,
            "yielded_to_background": (
                self.status is TerminalStatus.RUNNING and self.process_id is not None
            ),
            "terminal_session_id": terminal_session_id,
            "backend_type": backend_type,
            "io_mode": metadata.get("io_mode"),
        }
        for key in (
            "command",
            "duration_seconds",
            "stdin_closed",
            "policy_code",
            "suggested_args",
            "shell",
            "env",
        ):
            if key in metadata:
                payload[key] = metadata[key]
        return payload


@dataclass(frozen=True, slots=True)
class TerminalProcessInfo:
    process_id: str
    terminal_session_id: str
    command: str
    cwd: str
    backend_type: TerminalBackendType
    io_mode: TerminalIOMode
    status: TerminalStatus
    exit_code: int | None
    timed_out: bool
    stdin_closed: bool
    started_at_monotonic: float
    ended_at_monotonic: float | None
    duration_seconds: float

    def to_payload(self) -> dict[str, object]:
        return {
            "process_id": self.process_id,
            "terminal_session_id": self.terminal_session_id,
            "command": self.command,
            "cwd": self.cwd,
            "backend_type": self.backend_type.value,
            "io_mode": self.io_mode.value,
            "status": self.status.value,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "stdin_closed": self.stdin_closed,
            "duration_seconds": self.duration_seconds,
        }


@dataclass(frozen=True, slots=True)
class TerminalProcessLog:
    process: TerminalProcessInfo
    output: str
    truncated: bool
    full_output_text: str | None
    observation_semantic: TerminalProcessObservationSemanticFact | None
    completion_event_reference: ContextEventReferenceFact | None

    def to_payload(self) -> dict[str, object]:
        return {
            "process": self.process.to_payload(),
            "output": self.output,
            "truncated": self.truncated,
        }


@dataclass(frozen=True, slots=True)
class PreparedTerminalProcessMonitorRegistration:
    registration_semantic: TerminalProcessMonitorRegistrationSemanticFact
    registration_attribution: TerminalProcessMonitorRegistrationAttributionFact
    initial_core_state: TerminalProcessMonitorCoreStateFact
    registered_event: TerminalProcessMonitorRegisteredEvent
    notification_reservation: PreparedTerminalNotificationReservation | None
    initial_observation_result: TerminalResult


@dataclass(frozen=True, slots=True)
class PreparedTerminalProcessMonitorCancellation:
    monitor_id: str
    outcome: Literal["cancelled", "already_terminal"]
    cancellation_semantic: TerminalProcessMonitorCancellationSemanticFact | None
    stable_candidates: tuple[AgentEvent, ...]


@dataclass(frozen=True, slots=True)
class TerminalPortInvocationOwner:
    runtime_session_id: str
    run_id: str
    tool_call_id: str
    tool_name: Literal["terminal", "terminal_process", "terminal_monitor"]
    event_context: EventContext
    owner_kind: ToolInvocationOwnerKind
    permission: ToolPermissionInvocation
    owner_fingerprint: str

    def __post_init__(self) -> None:
        if self.event_context.run_id != self.run_id:
            raise ValueError("terminal invocation owner run identity mismatch")
        payload = asdict(self)
        payload.pop("owner_fingerprint")
        expected = context_fingerprint("terminal-port-invocation-owner:v1", payload)
        if self.owner_fingerprint != expected:
            raise ValueError("terminal invocation owner fingerprint mismatch")


def build_terminal_port_invocation_owner(
    *,
    runtime_session_id: str,
    tool_call_id: str,
    tool_name: Literal["terminal", "terminal_process", "terminal_monitor"],
    event_context: EventContext,
    owner_kind: ToolInvocationOwnerKind,
    permission: ToolPermissionInvocation,
) -> TerminalPortInvocationOwner:
    payload = {
        "runtime_session_id": runtime_session_id,
        "run_id": event_context.run_id,
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "event_context": asdict(event_context),
        "owner_kind": owner_kind.value,
        "permission": asdict(permission),
    }
    return TerminalPortInvocationOwner(
        runtime_session_id=runtime_session_id,
        run_id=event_context.run_id,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        event_context=event_context,
        owner_kind=owner_kind,
        permission=permission,
        owner_fingerprint=context_fingerprint(
            "terminal-port-invocation-owner:v1", payload
        ),
    )


class TerminalPortRejectCode(StrEnum):
    MALFORMED_ARGUMENTS = "malformed_arguments"
    ACCESS_OFF = "terminal_access_off"
    HARDLINE_COMMAND = "hardline_terminal_command"
    HARDLINE_PROCESS_INPUT = "hardline_terminal_process_input"
    PROCESS_NOT_FOUND = "process_not_found"
    PROCESS_INPUT_REJECTED = "process_input_rejected"
    PROCESS_CAPACITY_EXHAUSTED = "process_capacity_exhausted"
    MONITOR_OWNER_UNAVAILABLE = "monitor_owner_unavailable"
    MONITOR_CAPACITY_EXHAUSTED = "monitor_capacity_exhausted"
    MONITOR_DUPLICATE = "monitor_duplicate"
    MONITOR_NOT_FOUND = "monitor_not_found"
    CHILD_MONITOR_UNSUPPORTED = "child_monitor_unsupported"
    CONTRACT_MISMATCH = "contract_mismatch"


@dataclass(frozen=True, slots=True)
class TerminalCommandRejectedOutcome:
    outcome_kind: Literal["rejected"]
    command: str | None
    terminal_session_id: str | None
    failure_stage: Literal[
        "argument_validation",
        "permission",
        "adapter_initialization",
        "execution",
        "completion_reservation",
    ]
    reject_code: TerminalPortRejectCode
    sanitized_message: str
    outcome_fingerprint: str


@dataclass(frozen=True, slots=True)
class TerminalProcessRejectedOutcome:
    outcome_kind: Literal["rejected"]
    requested_action: str
    process_id: str | None
    status: Literal["malformed_arguments", "blocked", "not_found", "error"]
    reject_code: TerminalPortRejectCode
    sanitized_message: str
    outcome_fingerprint: str


@dataclass(frozen=True, slots=True)
class TerminalMonitorRejectedOutcome:
    outcome_kind: Literal["rejected"]
    requested_action: str
    process_id: str | None
    monitor_id: str | None
    status: Literal["malformed_arguments", "blocked", "not_found", "error"]
    reject_code: TerminalPortRejectCode
    sanitized_message: str
    outcome_fingerprint: str


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


@dataclass(frozen=True, slots=True)
class TerminalCommandRequest:
    command: str
    workdir: str | None
    terminal_session_id: str
    yield_time_ms: int
    max_output_chars: int
    tty: bool
    max_lifetime_seconds: int | None
    request_fingerprint: str

    def __post_init__(self) -> None:
        if not self.command or not self.terminal_session_id:
            raise ValueError("terminal command and session identity are required")
        if self.yield_time_ms < 0 or self.max_output_chars < 1:
            raise ValueError("terminal command bounds are invalid")
        if self.max_lifetime_seconds is not None and self.max_lifetime_seconds < 1:
            raise ValueError("terminal lifetime must be positive")
        _validate_process_local_fingerprint(
            self,
            field_name="request_fingerprint",
            namespace="terminal-command-request:v1",
        )


def build_terminal_command_request(
    *,
    command: str,
    workdir: str | None,
    terminal_session_id: str,
    yield_time_ms: int,
    max_output_chars: int,
    tty: bool,
    max_lifetime_seconds: int | None = None,
) -> TerminalCommandRequest:
    payload = {
        "command": command,
        "workdir": workdir,
        "terminal_session_id": terminal_session_id,
        "yield_time_ms": yield_time_ms,
        "max_output_chars": max_output_chars,
        "tty": tty,
        "max_lifetime_seconds": max_lifetime_seconds,
    }
    return TerminalCommandRequest(
        **payload,
        request_fingerprint=context_fingerprint("terminal-command-request:v1", payload),
    )


@dataclass(frozen=True, slots=True)
class TerminalCommandCompletedOutcome:
    outcome_kind: Literal["completed"]
    result: TerminalResult
    terminal_session_id: str
    backend_type: TerminalBackendType
    prepared_completion_reservation: PreparedTerminalNotificationReservation | None
    outcome_fingerprint: str


TerminalCommandOutcome: TypeAlias = (
    TerminalCommandCompletedOutcome | TerminalCommandRejectedOutcome
)


class TerminalOutputDeltaSink(Protocol):
    def emit(self, text_delta: str) -> None: ...


class TerminalCommandPort(Protocol):
    def execute(
        self,
        *,
        request: TerminalCommandRequest,
        owner: TerminalPortInvocationOwner,
        output_sink: TerminalOutputDeltaSink | None,
    ) -> TerminalCommandOutcome: ...


TerminalProcessRequest: TypeAlias = TerminalProcessInput


@dataclass(frozen=True, slots=True)
class TerminalProcessInventoryOutcome:
    outcome_kind: Literal["inventory"]
    processes: tuple[TerminalProcessInfo, ...]
    live_process_count: int
    finished_process_count: int
    outcome_fingerprint: str

    def __post_init__(self) -> None:
        live = sum(item.status is TerminalStatus.RUNNING for item in self.processes)
        if self.live_process_count != live:
            raise ValueError("terminal process live count mismatch")
        if self.finished_process_count != len(self.processes) - live:
            raise ValueError("terminal process finished count mismatch")


@dataclass(frozen=True, slots=True)
class TerminalProcessLogOutcome:
    outcome_kind: Literal["log"]
    log: TerminalProcessLog
    observation_receipt: TerminalProcessObservationReceiptFact
    outcome_fingerprint: str


@dataclass(frozen=True, slots=True)
class TerminalProcessObservationOutcome:
    outcome_kind: Literal["observation"]
    action: Literal["poll", "wait", "write", "submit", "close_stdin"]
    result: TerminalResult
    observation_receipt: TerminalProcessObservationReceiptFact | None
    outcome_fingerprint: str


@dataclass(frozen=True, slots=True)
class TerminalProcessKilledOutcome:
    outcome_kind: Literal["killed"]
    action: Literal["kill"]
    result: TerminalResult
    completion_observation_receipt: TerminalProcessObservationReceiptFact
    outcome_fingerprint: str


TerminalProcessOutcome: TypeAlias = (
    TerminalProcessInventoryOutcome
    | TerminalProcessLogOutcome
    | TerminalProcessObservationOutcome
    | TerminalProcessKilledOutcome
    | TerminalProcessRejectedOutcome
)


class TerminalProcessPort(Protocol):
    def execute(
        self,
        *,
        request: TerminalProcessRequest,
        owner: TerminalPortInvocationOwner,
    ) -> TerminalProcessOutcome: ...


TerminalMonitorRequest: TypeAlias = TerminalMonitorInput


@dataclass(frozen=True, slots=True)
class TerminalMonitorRegisteredOutcome:
    outcome_kind: Literal["registered"]
    prepared_registration: PreparedTerminalProcessMonitorRegistration
    outcome_fingerprint: str


@dataclass(frozen=True, slots=True)
class TerminalMonitorInventoryItem:
    monitor_id: str
    process_id: str
    lifecycle_state: TerminalMonitorLifecycleState
    observation_ordinal: int
    has_pending_observation: bool
    item_fingerprint: str


@dataclass(frozen=True, slots=True)
class TerminalMonitorInventoryOutcome:
    outcome_kind: Literal["inventory"]
    items: tuple[TerminalMonitorInventoryItem, ...]
    omitted_monitor_count: int
    outcome_fingerprint: str

    def __post_init__(self) -> None:
        if len(self.items) > 8 or self.omitted_monitor_count < 0:
            raise ValueError("terminal monitor inventory bounds are invalid")


@dataclass(frozen=True, slots=True)
class TerminalMonitorCancelledOutcome:
    outcome_kind: Literal["cancelled"]
    prepared_cancellation: PreparedTerminalProcessMonitorCancellation
    outcome_fingerprint: str


TerminalMonitorOutcome: TypeAlias = (
    TerminalMonitorRegisteredOutcome
    | TerminalMonitorInventoryOutcome
    | TerminalMonitorCancelledOutcome
    | TerminalMonitorRejectedOutcome
)


class TerminalMonitorPort(Protocol):
    def execute(
        self,
        *,
        request: TerminalMonitorRequest,
        owner: TerminalPortInvocationOwner,
    ) -> TerminalMonitorOutcome: ...


def process_local_fingerprint(namespace: str, value: object) -> str:
    if hasattr(value, "model_dump"):
        payload = value.model_dump(mode="json")
    elif hasattr(value, "__dataclass_fields__"):
        payload = asdict(value)
    else:
        payload = value
    return context_fingerprint(namespace, payload)


def _validate_process_local_fingerprint(
    value: object,
    *,
    field_name: str,
    namespace: str,
) -> None:
    payload = asdict(value)
    actual = payload.pop(field_name)
    expected = context_fingerprint(namespace, payload)
    if actual != expected:
        raise ValueError(f"{field_name} mismatch")


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
    "PreparedTerminalNotificationReservation",
    "PreparedTerminalProcessMonitorCancellation",
    "PreparedTerminalProcessMonitorRegistration",
    "ResolvedTerminalMonitorPublicPolicy",
    "TERMINAL_MONITOR_TOOL_DESCRIPTION",
    "TERMINAL_PROCESS_TOOL_DESCRIPTION",
    "TERMINAL_TOOL_DESCRIPTION",
    "TerminalMonitorCancelInput",
    "TerminalMonitorCancelledOutcome",
    "TerminalMonitorInventoryItem",
    "TerminalMonitorInventoryOutcome",
    "TerminalMonitorInput",
    "TerminalMonitorListInput",
    "TerminalMonitorRegisterInput",
    "TerminalMonitorRegisteredOutcome",
    "TerminalMonitorRejectedOutcome",
    "TerminalMonitorPort",
    "TerminalMonitorRequest",
    "TerminalBackendType",
    "TerminalCommandCompletedOutcome",
    "TerminalCommandOutcome",
    "TerminalCommandPort",
    "TerminalCommandRejectedOutcome",
    "TerminalCommandRequest",
    "TerminalIOMode",
    "TerminalOutputDeltaSink",
    "TerminalPortInvocationOwner",
    "TerminalPortRejectCode",
    "TerminalProcessInfo",
    "TerminalProcessInventoryOutcome",
    "TerminalProcessKilledOutcome",
    "TerminalProcessLog",
    "TerminalProcessLogOutcome",
    "TerminalProcessObservationOutcome",
    "TerminalProcessOutcome",
    "TerminalProcessPort",
    "TerminalProcessRejectedOutcome",
    "TerminalProcessRequest",
    "TerminalResult",
    "TerminalStatus",
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
    "build_terminal_command_request",
    "build_terminal_port_invocation_owner",
    "parse_terminal_monitor_input",
    "parse_terminal_process_input",
    "resolve_terminal_monitor_public_policy",
    "terminal_monitor_input_schema",
    "terminal_process_input_schema",
]
