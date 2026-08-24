"""Pure contracts for the independent Hook subsystem.

This module intentionally has no dependency on the conversation repository,
ToolRuntime, provider adapters, or Plugins.  Lifecycle owners construct the
private envelope and consume one of the five closed outcome families.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Generic, Mapping, Protocol, TypeVar


type JsonScalar = None | bool | int | float | str
type JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class HookEventType(StrEnum):
    SESSION_START_EVENT = "SessionStartEvent"
    SESSION_END_EVENT = "SessionEndEvent"
    USER_PROMPT_SUBMIT_EVENT = "UserPromptSubmitEvent"
    PRE_TOOL_USE_EVENT = "PreToolUseEvent"
    PERMISSION_REQUEST_EVENT = "PermissionRequestEvent"
    POST_TOOL_USE_EVENT = "PostToolUseEvent"
    PRE_COMPACT_EVENT = "PreCompactEvent"
    POST_COMPACT_EVENT = "PostCompactEvent"
    SUBAGENT_START_EVENT = "SubagentStartEvent"
    SUBAGENT_STOP_EVENT = "SubagentStopEvent"
    STOP_EVENT = "StopEvent"

    @property
    def external_name(self) -> str:
        return self.value.removesuffix("Event")

    @classmethod
    def from_external_name(cls, value: str) -> "HookEventType":
        try:
            return _HOOK_EVENT_BY_EXTERNAL_NAME[value]
        except KeyError as exc:
            raise ValueError(f"unsupported Hook event: {value}") from exc


HOOK_EVENT_TYPES = tuple(item.value for item in HookEventType)
_HOOK_EVENT_BY_EXTERNAL_NAME = {
    item.external_name: item for item in HookEventType
}


class HookSourceKind(StrEnum):
    USER_FILE = "USER_FILE"
    WORKSPACE_FILE = "WORKSPACE_FILE"


class HookVisibilityScope(StrEnum):
    USER = "USER"
    WORKSPACE = "WORKSPACE"


class HookSourceSnapshotDisposition(StrEnum):
    COMPLETE = "COMPLETE"
    UNAVAILABLE = "UNAVAILABLE"


class HookTrustDisposition(StrEnum):
    TRUSTED = "TRUSTED"
    UNTRUSTED = "UNTRUSTED"
    MODIFIED = "MODIFIED"
    DISABLED = "DISABLED"
    UNAVAILABLE = "UNAVAILABLE"


class HookDiagnosticSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class HookDiagnostic:
    code: str
    message: str
    severity: HookDiagnosticSeverity = HookDiagnosticSeverity.WARNING
    event_type: HookEventType | None = None
    source_label: str | None = None
    event_dispatch_ordinal: int | None = None
    source_ordinal: int | None = None
    definition_ordinal: int | None = None
    diagnostic_ordinal: int = 0


@dataclass(frozen=True, slots=True)
class HookSourceIdentity:
    kind: HookSourceKind
    canonical_path: Path
    visibility_scope: HookVisibilityScope
    workspace_state_key: str | None = None

    def __post_init__(self) -> None:
        if not self.canonical_path.is_absolute():
            raise ValueError("Hook source path must be absolute")
        if (
            self.kind is HookSourceKind.WORKSPACE_FILE
            and not self.workspace_state_key
        ):
            raise ValueError("workspace Hook source requires a state key")
        if (
            self.kind is HookSourceKind.USER_FILE
            and self.workspace_state_key is not None
        ):
            raise ValueError("user Hook source cannot carry a workspace key")


@dataclass(frozen=True, slots=True)
class HookTrustSubject:
    source_kind: HookSourceKind
    stable_locator: str


@dataclass(frozen=True, slots=True)
class FrozenHookSourceProvenance:
    identity: HookSourceIdentity
    trust_subject: HookTrustSubject
    description: str | None
    display_label: str
    declaration_environment: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class FrozenHookMatcherFact:
    pattern: str
    matches_all: bool
    ignored_by_profile: bool = False


@dataclass(frozen=True, slots=True)
class FrozenHookDefinition:
    provenance: FrozenHookSourceProvenance
    source_local_definition_ordinal: int
    event_ordinal: int
    group_ordinal: int
    handler_ordinal: int
    event_type: HookEventType
    matcher: FrozenHookMatcherFact
    command: str
    command_windows: str | None
    timeout_seconds: int
    asynchronous: bool
    status_message: str | None
    additional_context_limit: int

    def selected_command(self, *, windows: bool) -> str:
        if windows and self.command_windows is not None:
            return self.command_windows
        return self.command


@dataclass(frozen=True, slots=True)
class HookSourceTrustAssessment:
    disposition: HookTrustDisposition
    current_definition_digest: str | None
    trusted_definition_digest: str | None
    enabled: bool
    trusted_at: str | None


@dataclass(frozen=True, slots=True)
class FrozenHookSourceSnapshot:
    provenance: FrozenHookSourceProvenance
    disposition: HookSourceSnapshotDisposition
    definitions: tuple[FrozenHookDefinition, ...]
    diagnostics: tuple[HookDiagnostic, ...]
    trust: HookSourceTrustAssessment

    @property
    def runnable(self) -> bool:
        return (
            self.disposition is HookSourceSnapshotDisposition.COMPLETE
            and self.trust.disposition is HookTrustDisposition.TRUSTED
        )


@dataclass(frozen=True, slots=True)
class FrozenHookDefinitionView:
    source_snapshots: tuple[FrozenHookSourceSnapshot, ...]

    def selected_definitions(
        self, event_type: HookEventType
    ) -> tuple[tuple[int, FrozenHookDefinition], ...]:
        selected: list[tuple[int, FrozenHookDefinition]] = []
        for source_ordinal, snapshot in enumerate(self.source_snapshots):
            if not snapshot.runnable:
                continue
            selected.extend(
                (source_ordinal, definition)
                for definition in snapshot.definitions
                if definition.event_type is event_type
            )
        return tuple(selected)


class HookScopeKind(StrEnum):
    ROOT = "ROOT"
    CHILD = "CHILD"


@dataclass(frozen=True, slots=True)
class HookDispatchScopeRef:
    """Opaque, owner-issued delivery scope; never serialized to command stdin."""

    host_session_owner: object = field(repr=False, compare=False)
    workspace_owner: object = field(repr=False, compare=False)
    kind: HookScopeKind
    child_task_id: str | None = None

    def __post_init__(self) -> None:
        if (self.kind is HookScopeKind.CHILD) != (self.child_task_id is not None):
            raise ValueError("Hook child scope identity is inconsistent")


@dataclass(frozen=True, slots=True)
class SessionStartRef:
    attempt_token: object = field(repr=False, compare=False)
    source: str


@dataclass(frozen=True, slots=True)
class SessionEndRef:
    close_attempt_token: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class DirectPromptRef:
    command_id: str
    turn_id: str
    entry_id: str
    context_revision_id: str


@dataclass(frozen=True, slots=True)
class QueuedPromptRef:
    command_id: str
    queue_item_id: str
    turn_id: str
    entry_id: str
    context_revision_id: str


@dataclass(frozen=True, slots=True)
class PreToolRef:
    turn_id: str
    assistant_entry_id: str
    tool_call_id: str
    resolved_tool_identity: str


@dataclass(frozen=True, slots=True)
class PermissionRef:
    turn_id: str
    tool_call_id: str
    request_nonce: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class PostToolRef:
    turn_id: str
    tool_call_id: str
    result_id: str
    result_entry_id: str
    result_state: str
    epoch_nonce: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class PreCompactRef:
    attempt_token: object = field(repr=False, compare=False)
    target_scope: str
    target_turn_id: str
    trigger: str


@dataclass(frozen=True, slots=True)
class PostCompactRef:
    attempt_token: object = field(repr=False, compare=False)
    adopted_snapshot_id: str
    adopted_binding_revision_id: str


@dataclass(frozen=True, slots=True)
class SubagentStartRef:
    task_start_event_id: str
    task_id: str
    child_turn_id: str


@dataclass(frozen=True, slots=True)
class SubagentStopRef:
    task_id: str
    completion_kind: str
    completion_entry_id: str


@dataclass(frozen=True, slots=True)
class StopRef:
    turn_id: str
    assistant_entry_id: str
    epoch_nonce: object = field(repr=False, compare=False)
    binding_revision_id: str = ""


type HookDispatchCausalRef = (
    SessionStartRef
    | SessionEndRef
    | DirectPromptRef
    | QueuedPromptRef
    | PreToolRef
    | PermissionRef
    | PostToolRef
    | PreCompactRef
    | PostCompactRef
    | SubagentStartRef
    | SubagentStopRef
    | StopRef
)


class HookPublicInput(Protocol):
    @property
    def event_type(self) -> HookEventType: ...

    def to_wire(self) -> dict[str, JsonValue]: ...


class HookSecretScrubber(Protocol):
    """Process-local scrub authority retained through the final output sink."""

    def scrub_text(self, raw: str) -> str: ...


@dataclass(frozen=True, slots=True)
class _HookInputBase:
    session_id: str
    cwd: str
    model: str

    def _common(self, event_type: HookEventType) -> dict[str, JsonValue]:
        return {
            "session_id": self.session_id,
            "transcript_path": None,
            "cwd": self.cwd,
            "hook_event_name": event_type.external_name,
            "model": self.model,
        }


@dataclass(frozen=True, slots=True)
class SessionStartInput(_HookInputBase):
    source: str
    permission_mode: str
    event_type = HookEventType.SESSION_START_EVENT

    def to_wire(self) -> dict[str, JsonValue]:
        return self._common(self.event_type) | {
            "source": self.source,
            "permission_mode": self.permission_mode,
        }


@dataclass(frozen=True, slots=True)
class SessionEndInput(_HookInputBase):
    reason: str = "other"
    event_type = HookEventType.SESSION_END_EVENT

    def to_wire(self) -> dict[str, JsonValue]:
        return self._common(self.event_type) | {"reason": self.reason}


@dataclass(frozen=True, slots=True)
class UserPromptSubmitInput(_HookInputBase):
    turn_id: str
    prompt: str
    permission_mode: str
    event_type = HookEventType.USER_PROMPT_SUBMIT_EVENT

    def to_wire(self) -> dict[str, JsonValue]:
        return self._common(self.event_type) | {
            "turn_id": self.turn_id,
            "prompt": self.prompt,
            "permission_mode": self.permission_mode,
        }


@dataclass(frozen=True, slots=True)
class PreToolUseInput(_HookInputBase):
    turn_id: str
    tool_name: str
    tool_use_id: str
    tool_input: JsonValue
    permission_mode: str
    pulsara_tool_name: str | None = None
    event_type = HookEventType.PRE_TOOL_USE_EVENT

    def to_wire(self) -> dict[str, JsonValue]:
        value = self._common(self.event_type) | {
            "turn_id": self.turn_id,
            "tool_name": self.tool_name,
            "tool_use_id": self.tool_use_id,
            "tool_input": self.tool_input,
            "permission_mode": self.permission_mode,
        }
        if self.pulsara_tool_name is not None:
            value["pulsara_tool_name"] = self.pulsara_tool_name
        return value


@dataclass(frozen=True, slots=True)
class PermissionRequestInput(_HookInputBase):
    turn_id: str
    tool_name: str
    tool_input: JsonValue
    permission_mode: str
    pulsara_tool_name: str | None = None
    event_type = HookEventType.PERMISSION_REQUEST_EVENT

    def to_wire(self) -> dict[str, JsonValue]:
        value = self._common(self.event_type) | {
            "turn_id": self.turn_id,
            "tool_name": self.tool_name,
            "tool_input": self.tool_input,
            "permission_mode": self.permission_mode,
        }
        if self.pulsara_tool_name is not None:
            value["pulsara_tool_name"] = self.pulsara_tool_name
        return value


@dataclass(frozen=True, slots=True)
class PostToolUseInput(_HookInputBase):
    turn_id: str
    tool_name: str
    tool_use_id: str
    tool_input: JsonValue
    tool_response: JsonValue
    permission_mode: str
    pulsara_tool_name: str | None = None
    event_type = HookEventType.POST_TOOL_USE_EVENT

    def to_wire(self) -> dict[str, JsonValue]:
        value = self._common(self.event_type) | {
            "turn_id": self.turn_id,
            "tool_name": self.tool_name,
            "tool_use_id": self.tool_use_id,
            "tool_input": self.tool_input,
            "tool_response": self.tool_response,
            "permission_mode": self.permission_mode,
        }
        if self.pulsara_tool_name is not None:
            value["pulsara_tool_name"] = self.pulsara_tool_name
        return value


@dataclass(frozen=True, slots=True)
class PreCompactInput(_HookInputBase):
    turn_id: str
    trigger: str
    event_type = HookEventType.PRE_COMPACT_EVENT

    def to_wire(self) -> dict[str, JsonValue]:
        return self._common(self.event_type) | {
            "turn_id": self.turn_id,
            "trigger": self.trigger,
        }


@dataclass(frozen=True, slots=True)
class PostCompactInput(_HookInputBase):
    turn_id: str
    trigger: str
    event_type = HookEventType.POST_COMPACT_EVENT

    def to_wire(self) -> dict[str, JsonValue]:
        return self._common(self.event_type) | {
            "turn_id": self.turn_id,
            "trigger": self.trigger,
        }


@dataclass(frozen=True, slots=True)
class SubagentStartInput(_HookInputBase):
    turn_id: str
    agent_id: str
    agent_type: str
    permission_mode: str
    event_type = HookEventType.SUBAGENT_START_EVENT

    def to_wire(self) -> dict[str, JsonValue]:
        return self._common(self.event_type) | {
            "turn_id": self.turn_id,
            "agent_id": self.agent_id,
            "agent_type": self.agent_type,
            "permission_mode": self.permission_mode,
        }


@dataclass(frozen=True, slots=True)
class SubagentStopInput(_HookInputBase):
    turn_id: str
    agent_id: str
    agent_type: str
    stop_hook_active: bool
    last_assistant_message: str | None
    permission_mode: str
    agent_transcript_path: None = None
    event_type = HookEventType.SUBAGENT_STOP_EVENT

    def to_wire(self) -> dict[str, JsonValue]:
        return self._common(self.event_type) | {
            "turn_id": self.turn_id,
            "agent_id": self.agent_id,
            "agent_type": self.agent_type,
            "agent_transcript_path": None,
            "stop_hook_active": self.stop_hook_active,
            "last_assistant_message": self.last_assistant_message,
            "permission_mode": self.permission_mode,
        }


@dataclass(frozen=True, slots=True)
class StopInput(_HookInputBase):
    turn_id: str
    stop_hook_active: bool
    last_assistant_message: str | None
    permission_mode: str
    event_type = HookEventType.STOP_EVENT

    def to_wire(self) -> dict[str, JsonValue]:
        return self._common(self.event_type) | {
            "turn_id": self.turn_id,
            "stop_hook_active": self.stop_hook_active,
            "last_assistant_message": self.last_assistant_message,
            "permission_mode": self.permission_mode,
        }


TInput = TypeVar("TInput", bound=HookPublicInput)


@dataclass(frozen=True, slots=True)
class HookDispatchEnvelope(Generic[TInput]):
    definition_view: FrozenHookDefinitionView = field(repr=False, compare=False)
    scope: HookDispatchScopeRef = field(repr=False, compare=False)
    public_input: TInput
    causal_ref: HookDispatchCausalRef = field(repr=False, compare=False)
    deadline_monotonic: float | None
    cancellation_signal: object | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class HookContextEntry:
    definition: FrozenHookDefinition
    source_ordinal: int
    event_dispatch_ordinal: int
    text: str
    secret_scrubber: HookSecretScrubber = field(repr=False, compare=False)


class GateDecision(StrEnum):
    PROCEED = "PROCEED"
    BLOCK = "BLOCK"


class PermissionDecision(StrEnum):
    ABSTAIN = "ABSTAIN"
    ALLOW = "ALLOW"
    DENY = "DENY"


class ContinuationDecision(StrEnum):
    TERMINALIZE = "TERMINALIZE"
    CONTINUE_ONCE = "CONTINUE_ONCE"


@dataclass(frozen=True, slots=True)
class ObserveOutcome:
    diagnostics: tuple[HookDiagnostic, ...] = ()


@dataclass(frozen=True, slots=True)
class ContextOutcome:
    context_entries: tuple[HookContextEntry, ...] = ()
    diagnostics: tuple[HookDiagnostic, ...] = ()


@dataclass(frozen=True, slots=True)
class GateOutcome:
    decision: GateDecision = GateDecision.PROCEED
    reason: str | None = None
    context_entries: tuple[HookContextEntry, ...] = ()
    diagnostics: tuple[HookDiagnostic, ...] = ()


@dataclass(frozen=True, slots=True)
class PermissionOutcome:
    decision: PermissionDecision = PermissionDecision.ABSTAIN
    reason: str | None = None
    diagnostics: tuple[HookDiagnostic, ...] = ()


@dataclass(frozen=True, slots=True)
class ContinuationOutcome:
    decision: ContinuationDecision = ContinuationDecision.TERMINALIZE
    reason: str | None = None
    diagnostics: tuple[HookDiagnostic, ...] = ()
    continuation_source: HookContextEntry | None = None


type HookDispatchOutcome = (
    ObserveOutcome
    | ContextOutcome
    | GateOutcome
    | PermissionOutcome
    | ContinuationOutcome
)


EVENT_OUTCOME_FAMILY: Mapping[HookEventType, type[HookDispatchOutcome]] = {
    HookEventType.SESSION_START_EVENT: GateOutcome,
    HookEventType.SESSION_END_EVENT: ObserveOutcome,
    HookEventType.USER_PROMPT_SUBMIT_EVENT: GateOutcome,
    HookEventType.PRE_TOOL_USE_EVENT: GateOutcome,
    HookEventType.PERMISSION_REQUEST_EVENT: PermissionOutcome,
    HookEventType.POST_TOOL_USE_EVENT: ContextOutcome,
    HookEventType.PRE_COMPACT_EVENT: GateOutcome,
    HookEventType.POST_COMPACT_EVENT: GateOutcome,
    HookEventType.SUBAGENT_START_EVENT: ContextOutcome,
    HookEventType.SUBAGENT_STOP_EVENT: ContinuationOutcome,
    HookEventType.STOP_EVENT: ContinuationOutcome,
}


def external_permission_mode(
    effective_mode: str, *, active_plan_workflow: bool = False
) -> str:
    if effective_mode == "ask-permissions":
        return "default"
    if effective_mode == "accept-edits":
        return "acceptEdits"
    if effective_mode == "read-only":
        return "plan" if active_plan_workflow else "dontAsk"
    if effective_mode == "bypass-permissions":
        return "bypassPermissions"
    raise ValueError(f"unknown permission mode: {effective_mode}")


__all__ = [
    "ContinuationDecision",
    "ContinuationOutcome",
    "ContextOutcome",
    "DirectPromptRef",
    "EVENT_OUTCOME_FAMILY",
    "FrozenHookDefinition",
    "FrozenHookDefinitionView",
    "FrozenHookMatcherFact",
    "FrozenHookSourceProvenance",
    "FrozenHookSourceSnapshot",
    "GateDecision",
    "GateOutcome",
    "HOOK_EVENT_TYPES",
    "HookContextEntry",
    "HookDiagnostic",
    "HookDiagnosticSeverity",
    "HookDispatchCausalRef",
    "HookDispatchEnvelope",
    "HookDispatchOutcome",
    "HookDispatchScopeRef",
    "HookEventType",
    "HookPublicInput",
    "HookScopeKind",
    "HookSecretScrubber",
    "HookSourceIdentity",
    "HookSourceKind",
    "HookSourceSnapshotDisposition",
    "HookSourceTrustAssessment",
    "HookTrustDisposition",
    "HookTrustSubject",
    "HookVisibilityScope",
    "JsonScalar",
    "JsonValue",
    "ObserveOutcome",
    "PermissionRef",
    "PermissionRequestInput",
    "PermissionDecision",
    "PermissionOutcome",
    "PostCompactInput",
    "PostCompactRef",
    "PostToolRef",
    "PostToolUseInput",
    "PreCompactInput",
    "PreCompactRef",
    "PreToolRef",
    "PreToolUseInput",
    "QueuedPromptRef",
    "SessionEndInput",
    "SessionEndRef",
    "SessionStartInput",
    "SessionStartRef",
    "StopInput",
    "StopRef",
    "SubagentStartInput",
    "SubagentStartRef",
    "SubagentStopInput",
    "SubagentStopRef",
    "UserPromptSubmitInput",
    "external_permission_mode",
]
