"""Immutable, provider-neutral contracts for structured model input.

This module is intentionally pure: it owns no Kernel, transport, repository,
clock, filesystem, or callback capability.  Every value crossing this boundary
is immutable and finite before the compiler is invoked.
"""

from __future__ import annotations

from pulsara_agent.identity import require_id
from pulsara_agent.scheduling.contracts import ScheduledProvenance

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from hashlib import sha256
from typing import TYPE_CHECKING, Literal, Mapping, Protocol

if TYPE_CHECKING:
    from pulsara_agent.conversation_kernel.native_tool_delivery import NativeToolPage
    from pulsara_agent.model_input.continuity import InstalledObservationState

from pulsara_agent.llm.estimator import FinalWireTokenEstimate
from pulsara_agent.llm.input import (
    FrozenPromptPart,
    LLMContentPart,
    PromptAnnotationPart,
    prompt_provider_parts,
    LLMImagePart,
    LLMMessage,
    LLMTextPart,
    MessageRole,
    content_has_image,
    join_text_content,
    llm_content_identity_value,
)
from pulsara_agent.ports.terminal_observation import (
    CellOutputSource, CellOutputImageReadFailure, cell_output_identity_value,
)
from pulsara_agent.ports.tool_output import ToolOutputUnavailableReason
from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    canonical_json_bytes,
    context_fingerprint,
)
from pulsara_agent.primitives.model_call import (
    ResolvedModelCallFact,
    ResolvedModelTargetFact,
    TokenEstimatorFact,
)
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.plan_workflow import (
    PlanDraftContentIdentity,
    PlanHandoffKind,
    PlanWorkflowEnteredBy,
    PlanWorkflowStatus,
    extract_plan_draft,
    plan_draft_utf8_digest,
)
from pulsara_agent.primitives.run_permission import (
    FrozenRunPermissionSnapshot,
    RunPermissionOverlay,
)
from pulsara_agent.primitives.tool_observation import (
    FrozenToolObservationTimingFact,
    canonical_utc_timestamp,
)


SHA256_PREFIX = "sha256:"


class ModelInputScopeKind(StrEnum):
    ROOT = "ROOT"
    SUBAGENT = "SUBAGENT"


class CapabilityActivationSubjectKind(StrEnum):
    ROOT_HUMAN_PROMPT = "ROOT_HUMAN_PROMPT"
    ROOT_NON_HUMAN_TRIGGER = "ROOT_NON_HUMAN_TRIGGER"


class CanonicalInputOriginKind(StrEnum):
    HUMAN_MESSAGE = "HUMAN_MESSAGE"
    SCHEDULED_TASK = "SCHEDULED_TASK"
    HUMAN_STEER = "HUMAN_STEER"
    AGENT_INSTRUCTION = "AGENT_INSTRUCTION"
    AGENT_MATERIAL = "AGENT_MATERIAL"
    AGENT_MESSAGE = "AGENT_MESSAGE"
    CELL_OUTPUT = "CELL_OUTPUT"
    PLAN_CONTINUATION = "PLAN_CONTINUATION"


class ContextSourceKind(StrEnum):
    BASE_SYSTEM = "BASE_SYSTEM"
    SKILL_CATALOG = "SKILL_CATALOG"
    MCP_CATALOG = "MCP_CATALOG"
    SKILL_CATALOG_CHANGES = "SKILL_CATALOG_CHANGES"
    MCP_CATALOG_CHANGES = "MCP_CATALOG_CHANGES"
    ACTIVE_SKILL = "ACTIVE_SKILL"
    TURN_CONTEXT = "TURN_CONTEXT"
    PLAN_MODE = "PLAN_MODE"
    APPROVED_PLAN = "APPROVED_PLAN"
    PREVIOUS_TURN_OUTCOME = "PREVIOUS_TURN_OUTCOME"
    USER_ACTIONS = "USER_ACTIONS"
    MEMORY_RECALL = "MEMORY_RECALL"
    MEMORY_WRITE_HINT = "MEMORY_WRITE_HINT"
    COMPACTION_RUNTIME_HANDOFF = "COMPACTION_RUNTIME_HANDOFF"


class ContextChannel(StrEnum):
    SYSTEM = "SYSTEM"
    CONTEXT_MESSAGE = "CONTEXT_MESSAGE"
    RUNTIME_OBSERVATION = "RUNTIME_OBSERVATION"


class ContextBudgetClass(StrEnum):
    MUST_KEEP = "MUST_KEEP"
    OPTIONAL = "OPTIONAL"


class ContextPublicDiagnosticCode(StrEnum):
    OPTIONAL_SOURCE_UNAVAILABLE = "OPTIONAL_SOURCE_UNAVAILABLE"
    CAPABILITY_DISCOVERY_INCOMPLETE = "CAPABILITY_DISCOVERY_INCOMPLETE"
    ACTIVE_SKILL_NOT_FOUND = "ACTIVE_SKILL_NOT_FOUND"
    ACTIVE_SKILL_UNAVAILABLE = "ACTIVE_SKILL_UNAVAILABLE"
    CATALOG_TRUNCATED = "CATALOG_TRUNCATED"
    RUNTIME_CLOCK_UNAVAILABLE = "RUNTIME_CLOCK_UNAVAILABLE"
    SOURCE_DEGRADED = "SOURCE_DEGRADED"
    SOURCE_OMITTED = "SOURCE_OMITTED"
    TOOL_RESULT_DEGRADED = "TOOL_RESULT_DEGRADED"
    TOOL_RESULT_BODY_OMITTED = "TOOL_RESULT_BODY_OMITTED"
    SOURCE_VARIANT_NON_PROGRESS = "SOURCE_VARIANT_NON_PROGRESS"
    DECISION_SAMPLE_TRUNCATED = "DECISION_SAMPLE_TRUNCATED"


class ModelInputCompileFailureKind(StrEnum):
    MODEL_TARGET_PREPARATION_FAILED = "MODEL_TARGET_PREPARATION_FAILED"
    MODEL_SWITCH_REQUIRES_COMPACTION = "MODEL_SWITCH_REQUIRES_COMPACTION"
    TOOL_SURFACE_INVALID = "TOOL_SURFACE_INVALID"
    REQUIRED_SOURCE_UNAVAILABLE = "REQUIRED_SOURCE_UNAVAILABLE"
    SOURCE_CONTRACT_INVALID = "SOURCE_CONTRACT_INVALID"
    PROVIDER_INPUT_WIRE_BYTES_EXCEEDED = "PROVIDER_INPUT_WIRE_BYTES_EXCEEDED"
    COMPILE_WORKING_SET_EXCEEDED = "COMPILE_WORKING_SET_EXCEEDED"
    PROTECTED_TRANSCRIPT_EXCEEDS_BUDGET = "PROTECTED_TRANSCRIPT_EXCEEDS_BUDGET"
    PREFIX_EPOCH_BUDGET_EXHAUSTED = "PREFIX_EPOCH_BUDGET_EXHAUSTED"
    CANONICAL_PREFIX_CONFLICT = "CANONICAL_PREFIX_CONFLICT"
    CANONICAL_DELTA_NOT_PROVIDER_SAFE = "CANONICAL_DELTA_NOT_PROVIDER_SAFE"
    REQUIRED_CONTEXT_EXCEEDS_BUDGET = "REQUIRED_CONTEXT_EXCEEDS_BUDGET"
    TOOL_SCHEMA_EXCEEDS_BUDGET = "TOOL_SCHEMA_EXCEEDS_BUDGET"
    FINAL_INPUT_BINDING_MISMATCH = "FINAL_INPUT_BINDING_MISMATCH"
    DEADLINE_EXPIRED = "DEADLINE_EXPIRED"


class StructuredModelInputCompileError(ValueError):
    def __init__(self, kind: ModelInputCompileFailureKind) -> None:
        self.kind = kind
        super().__init__(f"structured model input compile failed: {kind.value}")


MAXIMUM_BUILTIN_NATIVE_TOOL_COUNT = 64


@dataclass(frozen=True, slots=True)
class StructuredModelInputLimits:
    # Provider tools contain the fixed builtin catalog only.
    maximum_diagnostics: int = 64
    maximum_public_diagnostic_bytes: int = 32 << 10
    maximum_decision_samples: int = 64

    def __post_init__(self) -> None:
        if (
            min(
                self.maximum_diagnostics,
                self.maximum_public_diagnostic_bytes,
                self.maximum_decision_samples,
            )
            < 1
        ):
            raise ValueError("structured model input limits must be positive")


STRUCTURED_MODEL_INPUT_LIMITS = StructuredModelInputLimits()


@dataclass(frozen=True, slots=True)
class FrozenToolSpec:
    name: str
    description: str
    parameters: FrozenJsonObjectFact = field(repr=False)
    descriptor_fingerprint: str
    output_schema: FrozenJsonObjectFact | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.name or not self.descriptor_fingerprint:
            raise ValueError("frozen tool specification identity is incomplete")
        if not isinstance(self.parameters, FrozenJsonObjectFact):
            raise TypeError("tool parameters must be a frozen JSON object")
        if self.output_schema is not None and not isinstance(
            self.output_schema, FrozenJsonObjectFact
        ):
            raise TypeError("tool output schema must be a frozen JSON object")

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(
            {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            }
        )


@dataclass(frozen=True, slots=True)
class FrozenModelToolSurface:
    conversation_scope_kind: ModelInputScopeKind
    tool_specs: tuple[FrozenToolSpec, ...] = field(repr=False)
    surface_fingerprint: str

    def __post_init__(self) -> None:
        names = tuple(tool.name for tool in self.tool_specs)
        if names != tuple(sorted(names)) or len(names) != len(set(names)):
            raise ValueError("frozen tool specs must be sorted and unique")
        expected = model_tool_surface_fingerprint(
            self.conversation_scope_kind, self.tool_specs
        )
        if self.surface_fingerprint != expected:
            raise ValueError("tool surface fingerprint mismatch")


def model_tool_surface_fingerprint(
    scope: ModelInputScopeKind, tools: tuple[FrozenToolSpec, ...]
) -> str:
    return context_fingerprint(
        "model-input-tool-surface:v2",
        {
            "scope": scope.value,
            "tools": tuple(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                    "descriptor_fingerprint": tool.descriptor_fingerprint,
                }
                for tool in tools
            ),
        },
    )


class ModelInputTokenEstimator(Protocol):
    fact: TokenEstimatorFact

    def estimate_text(self, text: str) -> int: ...

    def estimate_json(self, value: object) -> int: ...

    def estimate_wire_json_component(self, value: object) -> int: ...

    def estimate_ordered_wire_json_components(
        self,
        *,
        ordered_input_items: tuple[object, ...],
        ordered_input_sources: tuple[LLMMessage | None, ...],
    ) -> FinalWireTokenEstimate: ...

    def estimate_final_wire_json_components(
        self,
        *,
        fixed_context: object,
        ordered_input_items: tuple[object, ...],
        ordered_input_sources: tuple[LLMMessage | None, ...],
    ) -> FinalWireTokenEstimate: ...


class ProviderWireSemanticInput(Protocol):
    """The complete structural input needed by provider-wire planning.

    Both executable compiler output and non-executable compaction carriers
    satisfy this seam.  No continuity or provider-open authority is implied.
    """

    canonical_input_identity: CanonicalModelInputIdentity
    system_prompt: str
    messages: tuple[LLMMessage, ...]
    message_placements: tuple["FrozenCompiledMessagePlacement", ...]
    tools: tuple[FrozenToolSpec, ...]
    compile_binding_fingerprint: str


@dataclass(frozen=True, slots=True)
class ModelInputCompileBinding:
    call_fact: ResolvedModelCallFact
    target_fact: ResolvedModelTargetFact
    estimator: ModelInputTokenEstimator = field(repr=False, compare=False)
    estimator_fingerprint: str
    effective_input_budget_tokens: int
    effective_output_tokens: int
    tool_surface: FrozenModelToolSurface = field(repr=False)
    binding_fingerprint: str

    def __post_init__(self) -> None:
        if self.call_fact.target != self.target_fact:
            raise ValueError("compile call and target facts do not join")
        if self.call_fact.purpose.value != "agent_model_loop":
            raise ValueError("compile binding is not an agent model loop")
        if self.estimator.fact.estimator_fingerprint != self.estimator_fingerprint:
            raise ValueError("compile estimator fingerprint mismatch")
        if (
            self.target_fact.token_estimator.estimator_fingerprint
            != self.estimator_fingerprint
        ):
            raise ValueError("compile target estimator fingerprint mismatch")
        if min(self.effective_input_budget_tokens, self.effective_output_tokens) < 1:
            raise ValueError("compile binding budgets must be positive")
        if (
            self.effective_input_budget_tokens
            > self.target_fact.context_budget.input_budget_tokens
        ):
            raise ValueError("compile input budget exceeds the resolved target")
        if (
            self.effective_output_tokens
            != self.target_fact.context_budget.effective_output_tokens
        ):
            raise ValueError("compile output budget differs from the resolved target")
        expected = model_input_compile_binding_fingerprint(
            call_fact=self.call_fact,
            target_fact=self.target_fact,
            estimator_fingerprint=self.estimator_fingerprint,
            effective_input_budget_tokens=self.effective_input_budget_tokens,
            effective_output_tokens=self.effective_output_tokens,
            tool_surface=self.tool_surface,
        )
        if self.binding_fingerprint != expected:
            raise ValueError("compile binding fingerprint mismatch")


def model_input_compile_binding_fingerprint(
    *,
    call_fact: ResolvedModelCallFact,
    target_fact: ResolvedModelTargetFact,
    estimator_fingerprint: str,
    effective_input_budget_tokens: int,
    effective_output_tokens: int,
    tool_surface: FrozenModelToolSurface,
) -> str:
    return context_fingerprint(
        "model-input-compile-binding:v1",
        {
            "call_fact": call_fact,
            "target_fact": target_fact,
            "estimator_fingerprint": estimator_fingerprint,
            "effective_input_budget_tokens": effective_input_budget_tokens,
            "effective_output_tokens": effective_output_tokens,
            "tool_surface_fingerprint": tool_surface.surface_fingerprint,
        },
    )


@dataclass(frozen=True, slots=True)
class ContextSourceCandidate:
    source_kind: ContextSourceKind
    channel: ContextChannel
    text: str = field(repr=False)
    placement_ordinal: int
    budget_class: ContextBudgetClass = ContextBudgetClass.MUST_KEEP

    def __post_init__(self) -> None:
        if (
            not self.text
            or type(self.placement_ordinal) is not int
            or self.placement_ordinal < 0
        ):
            raise ValueError("context source requires content and an ordered placement")
        self.text.encode("utf-8")
        if (self.source_kind is ContextSourceKind.BASE_SYSTEM) != (
            self.channel is ContextChannel.SYSTEM
        ):
            raise ValueError("only the base instruction belongs in SYSTEM")
        if (
            self.budget_class is not ContextBudgetClass.MUST_KEEP
            and self.source_kind is not ContextSourceKind.USER_ACTIONS
        ):
            raise ValueError("only advisory user actions may be omitted as a source")

    @property
    def source_semantic_fingerprint(self) -> str:
        return context_fingerprint(
            "pulsara.context-source",
            {
                "source": self.source_kind.value,
                "channel": self.channel.value,
                "text": self.text,
                "placement": self.placement_ordinal,
                "budget": self.budget_class.value,
            },
        )


@dataclass(frozen=True, slots=True)
class ContextSourceCollectionDiagnostic:
    code: ContextPublicDiagnosticCode
    severity: Literal["INFO", "WARNING", "ERROR"]
    source_kind: ContextSourceKind | None

    def __post_init__(self) -> None:
        if self.severity not in {"INFO", "WARNING", "ERROR"}:
            raise ValueError("context diagnostic severity is not closed")


@dataclass(frozen=True, slots=True)
class CollectedContextSources:
    candidates: tuple[ContextSourceCandidate, ...]
    diagnostics: tuple[ContextSourceCollectionDiagnostic, ...] = ()
    hook_context: LLMMessage | None = field(default=None, repr=False)
    observation_state: InstalledObservationState | None = field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        kinds = tuple(item.source_kind for item in self.candidates)
        if len(kinds) != len(set(kinds)):
            raise ValueError("collected source kinds are duplicated")
        if (
            self.hook_context is not None
            and self.hook_context.role is not MessageRole.USER
        ):
            raise ValueError("Hook context must use its independent user-role message")

    @property
    def collection_fingerprint(self) -> str:
        return context_fingerprint(
            "pulsara.collected-context-sources",
            {
                "candidates": tuple(
                    item.source_semantic_fingerprint for item in self.candidates
                ),
                "hook_context": None
                if self.hook_context is None
                else _llm_message_value(self.hook_context),
                "diagnostics": tuple(
                    (
                        item.code.value,
                        item.severity,
                        None if item.source_kind is None else item.source_kind.value,
                    )
                    for item in self.diagnostics
                ),
            },
        )


@dataclass(frozen=True, slots=True)
class ProviderToolCall:
    tool_call_id: str
    tool_name: str
    arguments: FrozenJsonObjectFact = field(repr=False)

    def __post_init__(self) -> None:
        if not self.tool_call_id or not self.tool_name:
            raise ValueError("provider tool call identity is incomplete")
        if not isinstance(self.arguments, FrozenJsonObjectFact):
            raise TypeError("provider tool arguments must be frozen JSON")


@dataclass(frozen=True, slots=True)
class ProviderToolResultContextMetadata:
    session_id: str
    result_id: str
    result_entry_id: str
    result_state: str
    output_unavailability_reason: ToolOutputUnavailableReason | None
    timing: FrozenToolObservationTimingFact
    public_fields: Mapping[str, object] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        from pulsara_agent.ports.tool_execution import freeze_tool_json_object

        object.__setattr__(
            self, "public_fields", freeze_tool_json_object(self.public_fields)
        )
        if not self.result_id:
            raise ValueError("tool result canonical identity is empty")
        if self.result_state not in {
            "SUCCESS",
            "APPLICATION_ERROR",
            "SYSTEM_ERROR",
            "CANCELLED",
            "INVALID_ARGUMENTS",
            "PERMISSION_DENIED",
            "TOOL_UNAVAILABLE",
            "CANCELLED_BEFORE_DISPATCH",
        }:
            raise ValueError("tool result state is not closed")
        if not self.session_id or not self.result_entry_id:
            raise ValueError("tool result source identity is incomplete")
        if not isinstance(self.timing, FrozenToolObservationTimingFact):
            raise TypeError("tool result timing must be a frozen canonical fact")


class FrozenProviderInputItemKind(StrEnum):
    CONTEXT_SNAPSHOT = "CONTEXT_SNAPSHOT"
    USER = "USER"
    TERMINAL_OBSERVATION = "TERMINAL_OBSERVATION"
    ASSISTANT = "ASSISTANT"
    ASSISTANT_TOOL_REQUEST = "ASSISTANT_TOOL_REQUEST"
    TOOL_RESULT = "TOOL_RESULT"
    TOOL_RESULT_CLOSURE = "TOOL_RESULT_CLOSURE"
    LATE_TOOL_OUTCOME = "LATE_TOOL_OUTCOME"
    PLAN_CONTINUATION = "PLAN_CONTINUATION"


class CompactionContinuationMode(StrEnum):
    RESUME_ACTIVE_TURN = "RESUME_ACTIVE_TURN"
    AWAIT_NEXT_USER = "AWAIT_NEXT_USER"


def validate_compaction_request_shape(
    *,
    item_kind: FrozenProviderInputItemKind,
    input_origin: CanonicalInputOriginKind | None,
    active: bool,
    has_image: bool,
) -> None:
    """Close the shared compaction request kind/origin/content union."""

    origins = {
        FrozenProviderInputItemKind.USER: {
            CanonicalInputOriginKind.HUMAN_MESSAGE,
            CanonicalInputOriginKind.SCHEDULED_TASK,
            CanonicalInputOriginKind.HUMAN_STEER,
            CanonicalInputOriginKind.AGENT_INSTRUCTION,
            CanonicalInputOriginKind.AGENT_MATERIAL,
            CanonicalInputOriginKind.AGENT_MESSAGE,
        },
        FrozenProviderInputItemKind.PLAN_CONTINUATION: {
            CanonicalInputOriginKind.PLAN_CONTINUATION
        },
        FrozenProviderInputItemKind.TERMINAL_OBSERVATION: {
            CanonicalInputOriginKind.CELL_OUTPUT
        },
    }
    allowed = origins.get(item_kind)
    if allowed is None or input_origin not in allowed:
        raise ValueError("compaction request kind/origin union is invalid")
    if active and input_origin in {
        CanonicalInputOriginKind.HUMAN_STEER,
        CanonicalInputOriginKind.AGENT_MATERIAL,
        CanonicalInputOriginKind.AGENT_MESSAGE,
    }:
        raise ValueError("compaction active request origin is invalid")
    if has_image and not (
        item_kind is FrozenProviderInputItemKind.USER
        and input_origin
        in {
            CanonicalInputOriginKind.HUMAN_MESSAGE,
            CanonicalInputOriginKind.HUMAN_STEER,
            CanonicalInputOriginKind.AGENT_INSTRUCTION,
            CanonicalInputOriginKind.AGENT_MATERIAL,
            CanonicalInputOriginKind.AGENT_MESSAGE,
        }
    ):
        raise ValueError("compaction request origin cannot carry image content")


@dataclass(frozen=True, slots=True)
class CompactionSnapshotCarrier:
    """Summary and canonical-row selection for one context snapshot.

    Session ownership, row kinds, ordering and prefix cut are checked by the
    canonical reader against its frozen read. No original content is stored here.
    """

    continuation_mode: CompactionContinuationMode
    active_request_entry_id: str | None
    earlier_context_summary: str = field(repr=False)
    retained_entry_ids: tuple[str, ...]
    body: bytes = field(repr=False)
    content_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.continuation_mode, CompactionContinuationMode):
            raise TypeError("snapshot continuation mode must be typed")
        if not isinstance(self.retained_entry_ids, tuple):
            raise TypeError("snapshot retained entry IDs must be an ordered tuple")
        for entry_id in self.retained_entry_ids:
            require_id(entry_id, "entry")
        if len(set(self.retained_entry_ids)) != len(self.retained_entry_ids):
            raise ValueError("snapshot retained entry IDs must be unique")
        if self.active_request_entry_id is not None:
            require_id(self.active_request_entry_id, "entry")
        resume = self.continuation_mode is CompactionContinuationMode.RESUME_ACTIVE_TURN
        if resume != (self.active_request_entry_id is not None):
            raise ValueError("snapshot continuation/active-request union is invalid")
        if not isinstance(self.earlier_context_summary, str):
            raise TypeError("snapshot summary must be text")
        if not self.earlier_context_summary.strip():
            raise ValueError("snapshot summary is empty")
        expected = canonical_json_bytes(
            {
                "continuation": {
                    "mode": self.continuation_mode.value,
                    "active_request_entry_id": self.active_request_entry_id,
                },
                "earlier_context_summary": self.earlier_context_summary,
                "retained_entry_ids": self.retained_entry_ids,
            }
        )
        if not isinstance(self.body, bytes) or self.body != expected:
            raise ValueError("snapshot carrier is not canonical JSON")
        if self.content_digest != "sha256:" + sha256(self.body).hexdigest():
            raise ValueError("snapshot carrier digest mismatch")

    @property
    def canonical_expanded_bytes(self) -> int:
        """The carrier's charge; selected original rows are charged separately."""

        return len(self.body)


class ProviderToolResultClosureKind(StrEnum):
    INTERRUPTED_BEFORE_DISPATCH = "interrupted_before_dispatch"
    INTERRUPTED_MAY_HAVE_PARTIALLY_EXECUTED = "interrupted_may_have_partially_executed"
    PLAN_INTERACTION_ABORTED = "plan_interaction_aborted"


@dataclass(frozen=True, slots=True)
class ProviderToolResultClosure:
    assistant_entry_id: str
    tool_call_id: str
    closure_kind: ProviderToolResultClosureKind
    target_provider_input_through_sequence: int


@dataclass(frozen=True, slots=True)
class LateToolOutcomeObservation:
    assistant_entry_id: str
    tool_call_id: str
    result_entry_id: str
    result_entry_sequence: int
    result_state: str


@dataclass(frozen=True, slots=True)
class FrozenProviderInputItem:
    item_kind: FrozenProviderInputItemKind
    source_entry_id: str | None
    source_entry_sequence: int | None
    source_turn_id: str | None
    content: tuple[FrozenPromptPart | CellOutputImageReadFailure, ...] | CompactionSnapshotCarrier = field(
        repr=False
    )
    input_origin: CanonicalInputOriginKind | None = None
    scheduled_input: ScheduledProvenance | None = None
    tool_calls: tuple[ProviderToolCall, ...] = field(default=(), repr=False)
    tool_call_id: str | None = None
    tool_request_entry_id: str | None = None
    tool_result_context: ProviderToolResultContextMetadata | None = field(
        default=None, repr=False
    )
    tool_result_body_text: str | None = field(default=None, repr=False)
    tool_call_ordinal: int | None = None
    tool_call_arguments: FrozenJsonObjectFact | None = field(default=None, repr=False)
    tool_call_name: str | None = None
    native_page: NativeToolPage | None = field(default=None, repr=False)
    cell_output_source: CellOutputSource | None = None

    def __post_init__(self) -> None:
        if (self.item_kind is FrozenProviderInputItemKind.TERMINAL_OBSERVATION) != (
            isinstance(self.cell_output_source, CellOutputSource)
        ):
            raise ValueError("Cell output source facts must accompany its observation")
        snapshot_kind = self.item_kind is FrozenProviderInputItemKind.CONTEXT_SNAPSHOT
        if snapshot_kind:
            if not isinstance(self.content, CompactionSnapshotCarrier):
                raise TypeError("context snapshot content must be its typed carrier")
        elif not isinstance(self.content, tuple) or any(
            not isinstance(part, (LLMTextPart, LLMImagePart, PromptAnnotationPart, CellOutputImageReadFailure))
            or (isinstance(part, CellOutputImageReadFailure)
                and self.item_kind is not FrozenProviderInputItemKind.TERMINAL_OBSERVATION)
            or (
                isinstance(part, PromptAnnotationPart)
                and self.item_kind is not FrozenProviderInputItemKind.USER
            )
            for part in self.content
        ):
            raise TypeError("provider input content must be typed immutable parts")
        result_kind = self.item_kind in {
            FrozenProviderInputItemKind.TOOL_RESULT,
            FrozenProviderInputItemKind.LATE_TOOL_OUTCOME,
        }
        if result_kind != (
            self.tool_result_context is not None
            and self.tool_result_body_text is not None
        ):
            raise ValueError("tool result context union is invalid")
        entry_backed = self.item_kind not in {
            FrozenProviderInputItemKind.CONTEXT_SNAPSHOT,
            FrozenProviderInputItemKind.TOOL_RESULT_CLOSURE,
        }
        if entry_backed != (
            self.source_entry_id is not None
            and self.source_entry_sequence is not None
            and self.source_turn_id is not None
        ):
            raise ValueError("provider input entry attribution union is invalid")
        has_origin = self.item_kind in {
            FrozenProviderInputItemKind.USER,
            FrozenProviderInputItemKind.PLAN_CONTINUATION,
            FrozenProviderInputItemKind.TERMINAL_OBSERVATION,
        }
        if (
            self.item_kind is FrozenProviderInputItemKind.TERMINAL_OBSERVATION
            and self.input_origin is not CanonicalInputOriginKind.CELL_OUTPUT
        ):
            raise ValueError("Cell runtime input requires its real origin")
        if (self.input_origin is CanonicalInputOriginKind.SCHEDULED_TASK) != (
            self.scheduled_input is not None
        ):
            raise ValueError("scheduled origin/provenance union is invalid")
        if has_origin != (self.input_origin is not None):
            raise ValueError("provider input origin union is invalid")
        if (
            not snapshot_kind
            and any(isinstance(part, PromptAnnotationPart) for part in self.content)
            and self.input_origin
            not in {
                CanonicalInputOriginKind.HUMAN_MESSAGE,
                CanonicalInputOriginKind.HUMAN_STEER,
            }
        ):
            raise ValueError("only human USER input can contain annotations")
        images_allowed = (
            (
                self.item_kind is FrozenProviderInputItemKind.USER
                and self.input_origin
                in {
                    CanonicalInputOriginKind.HUMAN_MESSAGE,
                    CanonicalInputOriginKind.HUMAN_STEER,
                    CanonicalInputOriginKind.AGENT_INSTRUCTION,
                    CanonicalInputOriginKind.AGENT_MATERIAL,
                    CanonicalInputOriginKind.AGENT_MESSAGE,
                }
            )
            or result_kind
            or self.item_kind is FrozenProviderInputItemKind.TERMINAL_OBSERVATION
        )
        if not snapshot_kind and content_has_image(self.content) and not images_allowed:
            raise ValueError("provider input origin cannot carry image content")
        if self.source_entry_sequence is not None and self.source_entry_sequence < 0:
            raise ValueError("provider input entry sequence is invalid")
        if self.item_kind is FrozenProviderInputItemKind.CONTEXT_SNAPSHOT and (
            self.source_entry_id is not None
            or self.source_turn_id is not None
            or self.source_entry_sequence is None
        ):
            raise ValueError("context snapshot attribution is invalid")
        call_owner = self.item_kind is (
            FrozenProviderInputItemKind.ASSISTANT_TOOL_REQUEST
        )
        if call_owner != bool(self.tool_calls):
            raise ValueError("assistant tool-call union is invalid")
        call_ids = tuple(call.tool_call_id for call in self.tool_calls)
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("provider tool-call identities are duplicated")
        call_result_kind = result_kind or self.item_kind is (
            FrozenProviderInputItemKind.TOOL_RESULT_CLOSURE
        )
        if call_result_kind != (self.tool_call_id is not None):
            raise ValueError("provider tool-result call identity union is invalid")
        if call_result_kind != (self.tool_request_entry_id is not None):
            raise ValueError("provider tool-result request identity union is invalid")
        if self.tool_call_ordinal is not None and (
            isinstance(self.tool_call_ordinal, bool) or self.tool_call_ordinal < 0
        ):
            raise ValueError("provider tool-result call ordinal is invalid")
        if self.tool_call_arguments is not None and not isinstance(
            self.tool_call_arguments, FrozenJsonObjectFact
        ):
            raise TypeError("provider tool-result arguments must be frozen")
        if (
            not snapshot_kind
            and isinstance(self.content, tuple)
            and content_has_image(self.content)
            and result_kind
            and (self.tool_call_ordinal is None or not self.tool_call_name)
        ):
            raise ValueError("image ToolResult lacks its frozen call source")
        if self.item_kind is FrozenProviderInputItemKind.TOOL_RESULT:
            assert isinstance(self.content, tuple)
            expected_body = "".join(
                part.text for part in self.content if isinstance(part, LLMTextPart)
            )
            if self.tool_result_body_text != expected_body:
                raise ValueError(
                    "ordinary tool result body differs from its public projection"
                )


def provider_input_item_text(item: FrozenProviderInputItem) -> str:
    """Return the legacy renderer input only for strictly text-only content."""

    if not isinstance(item, FrozenProviderInputItem):
        raise TypeError("provider input text accessor requires a frozen item")
    if isinstance(item.content, CompactionSnapshotCarrier):
        raise ValueError("text-only consumer cannot consume a context snapshot")
    return join_text_content(item.content)


@dataclass(frozen=True, slots=True)
class CanonicalModelInputIdentity:
    session_id: str
    turn_id: str
    initial_entry_id: str
    context_binding_revision_id: str
    provider_input_through_sequence: int
    conversation_scope_kind: ModelInputScopeKind
    identity_fingerprint: str

    def __post_init__(self) -> None:
        if (
            not self.session_id
            or not self.turn_id
            or not self.initial_entry_id
            or not self.context_binding_revision_id
            or self.provider_input_through_sequence < 0
        ):
            raise ValueError("canonical model input identity is incomplete")
        expected = canonical_model_input_identity_fingerprint(
            session_id=self.session_id,
            turn_id=self.turn_id,
            initial_entry_id=self.initial_entry_id,
            context_binding_revision_id=self.context_binding_revision_id,
            provider_input_through_sequence=self.provider_input_through_sequence,
            conversation_scope_kind=self.conversation_scope_kind,
        )
        if self.identity_fingerprint != expected:
            raise ValueError("canonical model input identity fingerprint mismatch")


def canonical_model_input_identity_fingerprint(
    *,
    session_id: str,
    turn_id: str,
    initial_entry_id: str,
    context_binding_revision_id: str,
    provider_input_through_sequence: int,
    conversation_scope_kind: ModelInputScopeKind,
) -> str:
    return context_fingerprint(
        "canonical-model-input-identity:v1",
        {
            "session_id": session_id,
            "turn_id": turn_id,
            "initial_entry_id": initial_entry_id,
            "context_binding_revision_id": context_binding_revision_id,
            "provider_input_through_sequence": provider_input_through_sequence,
            "conversation_scope_kind": conversation_scope_kind.value,
        },
    )


@dataclass(frozen=True, slots=True)
class PreparedProviderInputCut:
    session_id: str
    turn_id: str
    context_binding_revision_id: str
    provider_input_through_sequence: int

    def __post_init__(self) -> None:
        if (
            not self.session_id
            or not self.turn_id
            or not self.context_binding_revision_id
            or self.provider_input_through_sequence < 0
        ):
            raise ValueError("prepared provider input cut is incomplete")


class ContextBindingBaseKind(StrEnum):
    FULL_HISTORY = "FULL_HISTORY"
    SNAPSHOT = "SNAPSHOT"


@dataclass(frozen=True, slots=True)
class FrozenContextBindingCompileFact:
    binding_revision_id: str
    revision_ordinal: int
    base_kind: ContextBindingBaseKind
    context_snapshot_id: str | None
    source_through_sequence: int
    context_base_semantic_identity: str
    fact_fingerprint: str

    def __post_init__(self) -> None:
        if (
            not self.binding_revision_id
            or self.revision_ordinal < 0
            or self.source_through_sequence < 0
        ):
            raise ValueError("context binding compile fact is incomplete")
        if (self.base_kind is ContextBindingBaseKind.FULL_HISTORY) != (
            self.context_snapshot_id is None
        ):
            raise ValueError("context binding base union is invalid")
        if not self.context_base_semantic_identity.startswith(SHA256_PREFIX):
            raise ValueError("context base semantic identity is invalid")
        if self.fact_fingerprint != context_binding_compile_fact_fingerprint(self):
            raise ValueError("context binding compile fact fingerprint mismatch")


def context_binding_compile_fact_fingerprint(
    fact: FrozenContextBindingCompileFact,
) -> str:
    return context_fingerprint(
        "pulsara:context-binding-compile-fact:v1",
        {
            "binding_revision_id": fact.binding_revision_id,
            "revision_ordinal": fact.revision_ordinal,
            "base_kind": fact.base_kind.value,
            "context_snapshot_id": fact.context_snapshot_id,
            "source_through_sequence": fact.source_through_sequence,
            "context_base_semantic_identity": fact.context_base_semantic_identity,
        },
    )


@dataclass(frozen=True, slots=True)
class CanonicalModelInputSnapshot:
    identity: CanonicalModelInputIdentity
    items: tuple[FrozenProviderInputItem, ...] = field(repr=False)
    canonical_expanded_bytes: int
    snapshot_fingerprint: str
    closures: tuple[ProviderToolResultClosure, ...] = ()
    late_outcomes: tuple[LateToolOutcomeObservation, ...] = ()

    def __post_init__(self) -> None:
        if self.canonical_expanded_bytes < 0:
            raise ValueError("canonical model input byte count is invalid")
        expected = canonical_model_input_snapshot_fingerprint(
            identity=self.identity,
            items=self.items,
            canonical_expanded_bytes=self.canonical_expanded_bytes,
            closures=self.closures,
            late_outcomes=self.late_outcomes,
        )
        if self.snapshot_fingerprint != expected:
            raise ValueError("canonical model input snapshot fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class FrozenPlanWorkflowCompileFact:
    session_id: str
    workspace_id: str
    turn_id: str
    permission_snapshot_id: str
    permission_snapshot_fingerprint: str
    workflow_id: str
    workflow_ordinal: int
    current_workflow_revision: int
    workflow_status: PlanWorkflowStatus
    entered_by: PlanWorkflowEnteredBy
    resume_permission_mode: PermissionMode
    fact_fingerprint: str

    def __post_init__(self) -> None:
        if self.workflow_status is not PlanWorkflowStatus.ACTIVE:
            raise ValueError("compiled Plan workflow must be active")
        if min(self.workflow_ordinal, self.current_workflow_revision) < 1:
            raise ValueError("compiled Plan workflow revision is invalid")
        if self.fact_fingerprint != plan_workflow_compile_fact_fingerprint(self):
            raise ValueError("compiled Plan workflow fingerprint mismatch")


def plan_workflow_compile_fact_fingerprint(
    fact: FrozenPlanWorkflowCompileFact,
) -> str:
    return context_fingerprint(
        "pulsara:plan-workflow-compile-fact:v1",
        {
            "session_id": fact.session_id,
            "workspace_id": fact.workspace_id,
            "turn_id": fact.turn_id,
            "permission_snapshot_id": fact.permission_snapshot_id,
            "permission_snapshot_fingerprint": fact.permission_snapshot_fingerprint,
            "workflow_id": fact.workflow_id,
            "workflow_ordinal": fact.workflow_ordinal,
            "current_workflow_revision": fact.current_workflow_revision,
            "workflow_status": fact.workflow_status.value,
            "entered_by": fact.entered_by.value,
            "resume_permission_mode": fact.resume_permission_mode.value,
        },
    )


@dataclass(frozen=True, slots=True)
class FrozenPlanHandoffCompileFact:
    session_id: str
    workspace_id: str
    target_turn_id: str
    carrier_entry_id: str
    carrier_entry_sequence: int
    workflow_id: str
    workflow_ordinal: int
    workflow_revision_at_transition: int
    interaction_id: str | None
    handoff_kind: PlanHandoffKind
    workflow_status: PlanWorkflowStatus
    resume_permission_mode: PermissionMode
    transition_semantic_digest: str
    fact_fingerprint: str

    def __post_init__(self) -> None:
        if (
            min(
                self.carrier_entry_sequence,
                self.workflow_ordinal,
                self.workflow_revision_at_transition,
            )
            < 1
        ):
            raise ValueError("compiled Plan handoff sequence is invalid")
        if (
            self.handoff_kind
            in {PlanHandoffKind.ENTERED_PLAN, PlanHandoffKind.EXITED_PLAN}
            and self.interaction_id is not None
        ) or (
            self.handoff_kind
            in {
                PlanHandoffKind.REVISION_REQUESTED,
                PlanHandoffKind.APPROVED_PLAN,
            }
            and self.interaction_id is None
        ):
            raise ValueError("compiled Plan handoff interaction is invalid")
        expected_status = {
            PlanHandoffKind.ENTERED_PLAN: PlanWorkflowStatus.ACTIVE,
            PlanHandoffKind.EXITED_PLAN: PlanWorkflowStatus.EXITED,
            PlanHandoffKind.REVISION_REQUESTED: PlanWorkflowStatus.ACTIVE,
            PlanHandoffKind.APPROVED_PLAN: PlanWorkflowStatus.APPROVED,
            PlanHandoffKind.CANCELLED_PLAN: PlanWorkflowStatus.CANCELLED,
            PlanHandoffKind.FORCE_EXITED_PLAN: PlanWorkflowStatus.FORCE_EXITED,
        }[self.handoff_kind]
        if self.workflow_status is not expected_status:
            raise ValueError("compiled Plan handoff status is invalid")
        if self.fact_fingerprint != plan_handoff_compile_fact_fingerprint(self):
            raise ValueError("compiled Plan handoff fingerprint mismatch")


def plan_handoff_compile_fact_fingerprint(
    fact: FrozenPlanHandoffCompileFact,
) -> str:
    return context_fingerprint(
        "pulsara:plan-handoff-compile-fact:v1",
        {
            "session_id": fact.session_id,
            "workspace_id": fact.workspace_id,
            "target_turn_id": fact.target_turn_id,
            "carrier_entry_id": fact.carrier_entry_id,
            "carrier_entry_sequence": fact.carrier_entry_sequence,
            "workflow_id": fact.workflow_id,
            "workflow_ordinal": fact.workflow_ordinal,
            "workflow_revision_at_transition": (fact.workflow_revision_at_transition),
            "interaction_id": fact.interaction_id,
            "handoff_kind": fact.handoff_kind.value,
            "workflow_status": fact.workflow_status.value,
            "resume_permission_mode": fact.resume_permission_mode.value,
            "transition_semantic_digest": fact.transition_semantic_digest,
        },
    )


@dataclass(frozen=True, slots=True)
class ApprovedPlanMaterializationFact:
    session_id: str
    workspace_id: str
    target_turn_id: str
    workflow_id: str
    interaction_id: str
    assistant_entry_id: str
    tool_call_id: str
    request_semantic_digest: str
    content_identity: PlanDraftContentIdentity
    exact_plan_utf8: bytes = field(repr=False)
    fact_fingerprint: str

    def __post_init__(self) -> None:
        if len(self.exact_plan_utf8) != self.content_identity.plan_utf8_size:
            raise ValueError("approved Plan materialization size mismatch")
        if plan_draft_utf8_digest(self.exact_plan_utf8) != (
            self.content_identity.plan_utf8_digest
        ):
            raise ValueError("approved Plan materialization digest mismatch")
        if self.fact_fingerprint != approved_plan_materialization_fingerprint(self):
            raise ValueError("approved Plan materialization fingerprint mismatch")


def approved_plan_materialization_fingerprint(
    fact: ApprovedPlanMaterializationFact,
) -> str:
    return context_fingerprint(
        "pulsara:approved-plan-materialization-fact:v1",
        {
            "session_id": fact.session_id,
            "workspace_id": fact.workspace_id,
            "target_turn_id": fact.target_turn_id,
            "workflow_id": fact.workflow_id,
            "interaction_id": fact.interaction_id,
            "assistant_entry_id": fact.assistant_entry_id,
            "tool_call_id": fact.tool_call_id,
            "request_semantic_digest": fact.request_semantic_digest,
            "content_identity": {
                "interaction_id": fact.content_identity.interaction_id,
                "assistant_entry_id": fact.content_identity.assistant_entry_id,
                "tool_call_id": fact.content_identity.tool_call_id,
                "request_semantic_digest": (
                    fact.content_identity.request_semantic_digest
                ),
                "plan_utf8_size": fact.content_identity.plan_utf8_size,
                "plan_utf8_digest": fact.content_identity.plan_utf8_digest,
            },
        },
    )


class PreviousTurnOutcomeKind(StrEnum):
    USER_STOPPED = "USER_STOPPED"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    HOST_SESSION_CLOSED = "HOST_SESSION_CLOSED"
    HOST_REPLACED = "HOST_REPLACED"
    PROVIDER_INPUT_CONFLICT = "PROVIDER_INPUT_CONFLICT"
    RESOURCE_BOUNDARY = "RESOURCE_BOUNDARY"
    PLAN_CONTINUATION_FAILED = "PLAN_CONTINUATION_FAILED"
    UNKNOWN_INTERRUPTION = "UNKNOWN_INTERRUPTION"


class AcceptedAssistantDisposition(StrEnum):
    NONE_ACCEPTED = "NONE_ACCEPTED"
    ACCEPTED_PREFIX_PRESENT = "ACCEPTED_PREFIX_PRESENT"


@dataclass(frozen=True, slots=True)
class FrozenPreviousTurnOutcomeCompileFact:
    session_id: str
    workspace_id: str
    current_turn_id: str
    current_scope_kind: ModelInputScopeKind
    predecessor_turn_id: str
    predecessor_initial_entry_sequence: int
    predecessor_terminal_at_utc: str
    outcome_kind: PreviousTurnOutcomeKind
    accepted_assistant_disposition: AcceptedAssistantDisposition
    accepted_assistant_entry_count: int
    definitely_not_dispatched_tool_count: int
    outcome_unknown_tool_count: int
    bounded_tool_name_samples: tuple[str, ...]
    user_input_preserved: Literal[True]
    canonical_entries_preserved: Literal[True]
    fact_fingerprint: str
    terminal_reason: str
    terminal_public_detail: str | None

    def __post_init__(self) -> None:
        if not all(
            (
                self.session_id,
                self.workspace_id,
                self.current_turn_id,
                self.predecessor_turn_id,
            )
        ):
            raise ValueError("previous-turn outcome identity is incomplete")
        if self.predecessor_initial_entry_sequence < 1:
            raise ValueError("previous-turn sequence is invalid")
        parsed = datetime.fromisoformat(
            self.predecessor_terminal_at_utc.replace("Z", "+00:00")
        )
        if canonical_utc_timestamp(parsed) != self.predecessor_terminal_at_utc:
            raise ValueError("previous-turn terminal time is not canonical UTC")
        counts = (
            self.accepted_assistant_entry_count,
            self.definitely_not_dispatched_tool_count,
            self.outcome_unknown_tool_count,
        )
        if min(counts) < 0:
            raise ValueError("previous-turn outcome count is negative")
        expected_disposition = (
            AcceptedAssistantDisposition.ACCEPTED_PREFIX_PRESENT
            if self.accepted_assistant_entry_count
            else AcceptedAssistantDisposition.NONE_ACCEPTED
        )
        if self.accepted_assistant_disposition is not expected_disposition:
            raise ValueError("previous-turn assistant disposition is inconsistent")
        if len(self.bounded_tool_name_samples) > 3:
            raise ValueError("previous-turn tool samples exceed their bound")
        for name in self.bounded_tool_name_samples:
            encoded = name.encode("utf-8")
            if not encoded or len(encoded) > 128:
                raise ValueError("previous-turn tool sample is outside its bound")
        if not self.user_input_preserved or not self.canonical_entries_preserved:
            raise ValueError("previous-turn preservation facts must be true")
        if self.fact_fingerprint != previous_turn_outcome_fingerprint(self):
            raise ValueError("previous-turn outcome fingerprint mismatch")


def previous_turn_outcome_fingerprint(
    fact: FrozenPreviousTurnOutcomeCompileFact,
) -> str:
    return context_fingerprint(
        "pulsara:previous-turn-outcome:v1",
        {
            "session_id": fact.session_id,
            "workspace_id": fact.workspace_id,
            "current_turn_id": fact.current_turn_id,
            "current_scope_kind": fact.current_scope_kind.value,
            "predecessor_turn_id": fact.predecessor_turn_id,
            "predecessor_initial_entry_sequence": (
                fact.predecessor_initial_entry_sequence
            ),
            "predecessor_terminal_at_utc": fact.predecessor_terminal_at_utc,
            "terminal_reason": fact.terminal_reason,
            "terminal_public_detail": fact.terminal_public_detail,
            "outcome_kind": fact.outcome_kind.value,
            "accepted_assistant_disposition": (
                fact.accepted_assistant_disposition.value
            ),
            "accepted_assistant_entry_count": fact.accepted_assistant_entry_count,
            "definitely_not_dispatched_tool_count": (
                fact.definitely_not_dispatched_tool_count
            ),
            "outcome_unknown_tool_count": fact.outcome_unknown_tool_count,
            "bounded_tool_name_samples": fact.bounded_tool_name_samples,
            "user_input_preserved": fact.user_input_preserved,
            "canonical_entries_preserved": fact.canonical_entries_preserved,
        },
    )


@dataclass(frozen=True, slots=True)
class FrozenCanonicalCompileSnapshot:
    canonical_input: CanonicalModelInputSnapshot = field(repr=False)
    context_binding_fact: FrozenContextBindingCompileFact
    run_permission_snapshot: FrozenRunPermissionSnapshot
    plan_workflow_fact: FrozenPlanWorkflowCompileFact | None
    plan_handoff_fact: FrozenPlanHandoffCompileFact | None
    approved_plan_materialization_fact: ApprovedPlanMaterializationFact | None = field(
        repr=False
    )
    previous_turn_outcome_fact: FrozenPreviousTurnOutcomeCompileFact | None
    canonical_read_cut_fingerprint: str
    plan_mode_original_input: tuple[LLMContentPart, ...] | None = field(
        default=None, repr=False
    )

    def __post_init__(self) -> None:
        identity = self.canonical_input.identity
        binding = self.context_binding_fact
        if (
            binding.binding_revision_id != identity.context_binding_revision_id
            or binding.source_through_sequence
            > identity.provider_input_through_sequence
        ):
            raise ValueError("compile context binding does not exact-join")
        permission = self.run_permission_snapshot
        if permission.snapshot_id == "":
            raise ValueError("compile permission snapshot is absent")
        workflow = self.plan_workflow_fact
        plan_bound = permission.overlay is RunPermissionOverlay.PLAN_READ_ONLY
        if plan_bound != (workflow is not None):
            raise ValueError("compile Plan workflow presence does not exact-join")
        if workflow is not None and (
            workflow.session_id != identity.session_id
            or workflow.turn_id != identity.turn_id
            or workflow.permission_snapshot_id != permission.snapshot_id
            or workflow.permission_snapshot_fingerprint
            != permission.snapshot_fingerprint
            or workflow.workflow_id != permission.plan_workflow_id
            or workflow.workflow_ordinal != permission.plan_context_ordinal_at_admission
            or workflow.current_workflow_revision
            < int(permission.plan_workflow_revision_at_admission or 0)
        ):
            raise ValueError("compile Plan workflow does not exact-join")
        handoff = self.plan_handoff_fact
        if handoff is not None:
            expected_item_kind = (
                FrozenProviderInputItemKind.USER
                if handoff.handoff_kind
                in {
                    PlanHandoffKind.CANCELLED_PLAN,
                    PlanHandoffKind.FORCE_EXITED_PLAN,
                    PlanHandoffKind.REVISION_REQUESTED,
                }
                else FrozenProviderInputItemKind.PLAN_CONTINUATION
            )
            matching_items = tuple(
                item
                for item in self.canonical_input.items
                if item.source_entry_id == handoff.carrier_entry_id
                and item.source_entry_sequence == handoff.carrier_entry_sequence
                and item.source_turn_id == identity.turn_id
                and item.item_kind is expected_item_kind
            )
            if (
                handoff.session_id != identity.session_id
                or handoff.target_turn_id != identity.turn_id
                or len(matching_items) != 1
                or (
                    workflow is not None
                    and workflow.workspace_id != handoff.workspace_id
                )
            ):
                raise ValueError("compile Plan handoff does not exact-join")
        approved = self.approved_plan_materialization_fact
        if (approved is not None) != (
            handoff is not None
            and handoff.handoff_kind is PlanHandoffKind.APPROVED_PLAN
        ):
            raise ValueError("approved Plan materialization presence is invalid")
        if approved is not None:
            assert handoff is not None
            content = approved.content_identity
            if (
                approved.session_id != identity.session_id
                or approved.workspace_id != handoff.workspace_id
                or approved.target_turn_id != identity.turn_id
                or approved.workflow_id != handoff.workflow_id
                or approved.interaction_id != handoff.interaction_id
                or approved.assistant_entry_id != content.assistant_entry_id
                or approved.tool_call_id != content.tool_call_id
                or approved.interaction_id != content.interaction_id
                or approved.request_semantic_digest != content.request_semantic_digest
            ):
                raise ValueError("approved Plan materialization does not exact-join")
            matching_items = tuple(
                item
                for item in self.canonical_input.items
                if item.item_kind is FrozenProviderInputItemKind.ASSISTANT_TOOL_REQUEST
                and item.source_entry_id == approved.assistant_entry_id
                and any(
                    call.tool_call_id == approved.tool_call_id
                    for call in item.tool_calls
                )
            )
            if len(matching_items) > 1:
                raise ValueError("approved Plan canonical carrier is duplicated")
            matching_calls = tuple(
                call
                for item in matching_items
                for call in item.tool_calls
                if call.tool_call_id == approved.tool_call_id
            )
            if matching_calls:
                extracted = extract_plan_draft(
                    interaction_id=approved.interaction_id,
                    assistant_entry_id=approved.assistant_entry_id,
                    tool_call_id=approved.tool_call_id,
                    request_semantic_digest=approved.request_semantic_digest,
                    arguments=matching_calls[0].arguments,
                )
                if (
                    extracted.identity != approved.content_identity
                    or extracted.exact_plan_utf8 != approved.exact_plan_utf8
                ):
                    raise ValueError("approved Plan canonical content drifted")
        previous = self.previous_turn_outcome_fact
        if previous is not None and (
            previous.session_id != identity.session_id
            or previous.current_turn_id != identity.turn_id
            or previous.current_scope_kind is not identity.conversation_scope_kind
        ):
            raise ValueError("previous-turn fact does not exact-join input")
        if self.canonical_read_cut_fingerprint != (
            canonical_compile_snapshot_fingerprint(self)
        ):
            raise ValueError("canonical compile snapshot fingerprint mismatch")


def canonical_compile_snapshot_fingerprint(
    snapshot: FrozenCanonicalCompileSnapshot,
) -> str:
    return context_fingerprint(
        "pulsara:canonical-compile-snapshot:v1",
        {
            "canonical_input": snapshot.canonical_input.snapshot_fingerprint,
            "context_binding": snapshot.context_binding_fact.fact_fingerprint,
            "run_permission": (snapshot.run_permission_snapshot.snapshot_fingerprint),
            "plan_mode_original_input": None
            if snapshot.plan_mode_original_input is None
            else llm_content_identity_value(snapshot.plan_mode_original_input),
            "plan_workflow": (
                None
                if snapshot.plan_workflow_fact is None
                else snapshot.plan_workflow_fact.fact_fingerprint
            ),
            "plan_handoff": (
                None
                if snapshot.plan_handoff_fact is None
                else snapshot.plan_handoff_fact.fact_fingerprint
            ),
            "approved_plan": (
                None
                if snapshot.approved_plan_materialization_fact is None
                else snapshot.approved_plan_materialization_fact.fact_fingerprint
            ),
            "previous_turn_outcome": (
                None
                if snapshot.previous_turn_outcome_fact is None
                else snapshot.previous_turn_outcome_fact.fact_fingerprint
            ),
        },
    )


def provider_input_item_fingerprint(item: FrozenProviderInputItem) -> str:
    return context_fingerprint(
        "frozen-provider-input-item:v3-tool-request-owner",
        provider_input_item_leaf(item),
    )


def provider_input_item_leaf(item: FrozenProviderInputItem) -> Mapping[str, object]:
    """Unique stable semantic framing for one canonical provider item."""

    if isinstance(item.content, CompactionSnapshotCarrier):
        # Import within the call so contracts remain the lower-level owner while
        # identity uses the one normal provider projection owned by lowering.
        from pulsara_agent.model_input.lowering import (
            compaction_snapshot_provider_content,
        )

        content = compaction_snapshot_provider_content(item.content)
    else:
        content = prompt_provider_parts(item.content)

    value = {
        "kind": item.item_kind.value,
        "entry_id": item.source_entry_id,
        "sequence": item.source_entry_sequence,
        "turn_id": item.source_turn_id,
        "input_origin": (
            None if item.input_origin is None else item.input_origin.value
        ),
        "cell_output_source": None if item.cell_output_source is None else {
            "cell_id": item.cell_output_source.cell_id,
            "record_sequence": item.cell_output_source.record_sequence,
            "kind": item.cell_output_source.kind,
        },
        "scheduled_input": None
        if item.scheduled_input is None
        else item.scheduled_input.to_dict(),
        "content": None
        if item.item_kind
        in {
            FrozenProviderInputItemKind.TOOL_RESULT,
            FrozenProviderInputItemKind.LATE_TOOL_OUTCOME,
        }
        else cell_output_identity_value(content)
        if item.item_kind is FrozenProviderInputItemKind.TERMINAL_OBSERVATION
        else llm_content_identity_value(content),
        "calls": tuple(
            (call.tool_call_id, call.tool_name, call.arguments)
            for call in item.tool_calls
        ),
        "tool_call_id": item.tool_call_id,
        "tool_request_entry_id": item.tool_request_entry_id,
        "tool_call_ordinal": item.tool_call_ordinal,
        "tool_call_arguments": item.tool_call_arguments,
        "tool_result_context": (
            None
            if item.tool_result_context is None
            else {
                "result_state": item.tool_result_context.result_state,
                "session_id": item.tool_result_context.session_id,
                "result_entry_id": item.tool_result_context.result_entry_id,
                "output_unavailability_reason": (
                    None
                    if item.tool_result_context.output_unavailability_reason is None
                    else item.tool_result_context.output_unavailability_reason.value
                ),
                "timing": item.tool_result_context.timing.fact_fingerprint,
            }
        ),
        "tool_result_body_text": None
        if item.item_kind
        in {
            FrozenProviderInputItemKind.TOOL_RESULT,
            FrozenProviderInputItemKind.LATE_TOOL_OUTCOME,
        }
        else item.tool_result_body_text,
    }
    if item.tool_call_name == "read_video_frames":
        value["tool_call_name"] = item.tool_call_name
    return value


def provider_input_item_logical_utf8_bytes(item: FrozenProviderInputItem) -> int:
    """Quote the complete canonical leaf without inventing another projection."""

    return len(canonical_json_bytes(provider_input_item_leaf(item)))


def compiled_tool_result_source_fingerprint(
    item: FrozenProviderInputItem,
) -> str:
    """Stable join from a canonical ToolResult item to compiler decisions."""

    if (
        item.source_entry_id is None
        or item.tool_call_id is None
        or item.tool_request_entry_id is None
    ):
        raise ValueError("compiled tool-result source lacks canonical identity")
    return context_fingerprint(
        "compiled-tool-result-source:v1",
        {
            "entry_id": item.source_entry_id,
            "sequence": item.source_entry_sequence,
            "tool_request_entry_id": item.tool_request_entry_id,
            "tool_call_id": item.tool_call_id,
        },
    )


def canonical_model_input_snapshot_fingerprint(
    *,
    identity: CanonicalModelInputIdentity,
    items: tuple[FrozenProviderInputItem, ...],
    canonical_expanded_bytes: int,
    closures: tuple[ProviderToolResultClosure, ...],
    late_outcomes: tuple[LateToolOutcomeObservation, ...],
) -> str:
    return context_fingerprint(
        "canonical-model-input-snapshot:v2-expanded-content",
        {
            "identity": identity.identity_fingerprint,
            "items": tuple(provider_input_item_fingerprint(item) for item in items),
            "canonical_expanded_bytes": canonical_expanded_bytes,
            "closures": tuple(
                provider_tool_result_closure_leaf(item) for item in closures
            ),
            "late_outcomes": tuple(
                late_tool_outcome_observation_leaf(item) for item in late_outcomes
            ),
        },
    )


def provider_tool_result_closure_leaf(
    item: ProviderToolResultClosure,
) -> tuple[str, str, str, int]:
    """Unique stable framing for one provider-visible closure leaf."""

    return (
        item.assistant_entry_id,
        item.tool_call_id,
        item.closure_kind.value,
        item.target_provider_input_through_sequence,
    )


def late_tool_outcome_observation_leaf(
    item: LateToolOutcomeObservation,
) -> tuple[str, str, str, int, str]:
    """Unique stable framing for one provider-visible late-outcome leaf."""

    return (
        item.assistant_entry_id,
        item.tool_call_id,
        item.result_entry_id,
        item.result_entry_sequence,
        item.result_state,
    )


@dataclass(frozen=True, slots=True)
class StructuredModelInputCompileRequest:
    context_id: str
    model_call_index: int
    canonical_input: CanonicalModelInputSnapshot = field(repr=False)
    canonical_facts: FrozenCanonicalCompileSnapshot = field(repr=False)
    compile_binding: ModelInputCompileBinding = field(repr=False)
    sources: CollectedContextSources = field(repr=False)
    dispatch_anchor_entry_id: str | None = None

    def __post_init__(self) -> None:
        if not self.context_id or self.model_call_index < 1:
            raise ValueError("compile request identity is invalid")
        identity = self.canonical_input.identity
        if self.canonical_facts.canonical_input.snapshot_fingerprint != (
            self.canonical_input.snapshot_fingerprint
        ):
            raise ValueError("compile request canonical facts do not exact-join")
        if (
            identity.conversation_scope_kind
            is not self.compile_binding.tool_surface.conversation_scope_kind
        ):
            raise ValueError("compile request scope and tool surface differ")
        if self.dispatch_anchor_entry_id is not None and not any(
            item.source_entry_id == self.dispatch_anchor_entry_id
            for item in self.canonical_input.items
        ):
            raise ValueError("compile request dispatch anchor is not canonical")


@dataclass(frozen=True, slots=True)
class CompiledSourceDecision:
    source_kind: ContextSourceKind
    source_instance_fingerprint: str
    channel: ContextChannel
    included: bool


@dataclass(frozen=True, slots=True)
class CompiledToolResultDecision:
    source_entry_fingerprint: str
    current_turn: bool
    result_entry_id: str | None = None
    source_result_entry_id: str | None = None
    native_projection: FrozenJsonObjectFact | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if (self.native_projection is not None) != (
            self.result_entry_id is not None and self.source_result_entry_id is not None
        ):
            raise ValueError("native result decision owner is incomplete")


@dataclass(frozen=True, slots=True)
class ContextCompileReport:
    tool_surface_fingerprint: str
    protected_prefix_message_count: int
    protected_prefix_logical_bytes: int
    protected_prefix_fingerprint: str | None
    omitted_source_count: int
    decision_digest: str

    def __post_init__(self) -> None:
        numeric = (
            self.protected_prefix_message_count,
            self.protected_prefix_logical_bytes,
            self.omitted_source_count,
        )
        if any(value < 0 for value in numeric):
            raise ValueError("compile budget report contains a negative value")
        for value in (
            self.tool_surface_fingerprint,
            self.decision_digest,
        ):
            if not value.startswith(SHA256_PREFIX):
                raise ValueError("compile budget report fingerprint is invalid")
        if self.protected_prefix_fingerprint is not None and not (
            self.protected_prefix_fingerprint.startswith(SHA256_PREFIX)
        ):
            raise ValueError("protected prefix fingerprint is invalid")
        if (self.protected_prefix_message_count == 0) != (
            self.protected_prefix_fingerprint is None
        ):
            raise ValueError("protected prefix summary union is invalid")


@dataclass(frozen=True, slots=True)
class ToolAttachmentSourceMember:
    call_ordinal: int
    source: FrozenProviderInputItem = field(repr=False)

    def __post_init__(self) -> None:
        if isinstance(self.call_ordinal, bool) or self.call_ordinal < 0:
            raise ValueError("tool attachment call ordinal is invalid")
        if self.source.item_kind not in {
            FrozenProviderInputItemKind.TOOL_RESULT,
            FrozenProviderInputItemKind.LATE_TOOL_OUTCOME,
        }:
            raise ValueError("tool attachment member is not a ToolResult source")
        if self.source.tool_call_ordinal != self.call_ordinal:
            raise ValueError("tool attachment member ordinal drifted")
        if not isinstance(self.source.content, tuple) or not content_has_image(
            self.source.content
        ):
            raise ValueError("tool attachment member has no image")


@dataclass(frozen=True, slots=True)
class ToolAttachmentSource:
    assistant_entry_id: str
    members: tuple[ToolAttachmentSourceMember, ...] = field(repr=False)

    def __post_init__(self) -> None:
        if not self.assistant_entry_id or not self.members:
            raise ValueError("tool attachment source is incomplete")
        if tuple(member.call_ordinal for member in self.members) != tuple(
            sorted(member.call_ordinal for member in self.members)
        ) or len({member.call_ordinal for member in self.members}) != len(self.members):
            raise ValueError("tool attachment members are not strictly ordered")
        if any(
            member.source.tool_request_entry_id != self.assistant_entry_id
            for member in self.members
        ):
            raise ValueError("tool attachment members belong to another request")


@dataclass(frozen=True, slots=True)
class FrozenCompiledMessagePlacement:
    message_ordinal: int
    origin_entry_id: str | None
    origin_item_fingerprint: str | None
    within_origin_ordinal: int | None
    role: MessageRole
    tool_attachment_source: ToolAttachmentSource | None = field(
        default=None, repr=False
    )

    def __post_init__(self) -> None:
        if self.message_ordinal < 0:
            raise ValueError("compiled message placement ordinal is invalid")
        combined = self.tool_attachment_source is not None
        if combined:
            if (
                self.origin_entry_id is not None
                or self.origin_item_fingerprint is not None
                or self.within_origin_ordinal is not None
                or self.role is not MessageRole.USER
            ):
                raise ValueError("tool attachment placement source union is invalid")
        elif (
            self.origin_item_fingerprint is None
            or not self.origin_item_fingerprint.startswith(SHA256_PREFIX)
            or self.within_origin_ordinal is None
            or self.within_origin_ordinal < 0
        ):
            raise ValueError("compiled message origin fingerprint is invalid")


def compiled_message_placement_identity_fingerprint(
    placement: FrozenCompiledMessagePlacement,
) -> str:
    return context_fingerprint(
        "pulsara.compiled-message-placement:v2-tool-attachments",
        {
            "ordinal": placement.message_ordinal,
            "entry": placement.origin_entry_id,
            "item": placement.origin_item_fingerprint,
            "within": placement.within_origin_ordinal,
            "role": placement.role.value,
            "tool_attachment": (
                None
                if placement.tool_attachment_source is None
                else {
                    "assistant_entry_id": (
                        placement.tool_attachment_source.assistant_entry_id
                    ),
                    "members": tuple(
                        {
                            "call_ordinal": member.call_ordinal,
                            "source": provider_input_item_leaf(member.source),
                        }
                        for member in placement.tool_attachment_source.members
                    ),
                }
            ),
        },
    )


def compiled_message_placements_fingerprint(
    placements: tuple[FrozenCompiledMessagePlacement, ...],
) -> str:
    return context_fingerprint(
        "pulsara.compiled-message-placements:v1",
        tuple(
            compiled_message_placement_identity_fingerprint(item) for item in placements
        ),
    )


@dataclass(frozen=True, slots=True)
class FrozenCompiledModelInput:
    context_id: str
    canonical_input_identity: CanonicalModelInputIdentity
    system_prompt: str = field(repr=False)
    messages: tuple[LLMMessage, ...] = field(repr=False)
    message_placements: tuple[FrozenCompiledMessagePlacement, ...] = field(repr=False)
    tools: tuple[FrozenToolSpec, ...] = field(repr=False)
    source_decisions: tuple[CompiledSourceDecision, ...]
    tool_result_decisions: tuple[CompiledToolResultDecision, ...]
    compile_report: ContextCompileReport
    diagnostic_codes: tuple[ContextPublicDiagnosticCode, ...]
    source_collection_fingerprint: str
    compiled_semantic_fingerprint: str
    compile_binding_fingerprint: str

    def __post_init__(self) -> None:
        if not self.context_id:
            raise ValueError("compiled model input context identity is empty")
        if len(self.message_placements) != len(self.messages):
            raise ValueError("compiled message placements are not parallel")
        if tuple(item.message_ordinal for item in self.message_placements) != tuple(
            range(len(self.messages))
        ):
            raise ValueError("compiled message placement order is invalid")
        if any(
            item.role is not message.role
            for item, message in zip(
                self.message_placements, self.messages, strict=True
            )
        ):
            raise ValueError("compiled message placement role drifted")
        if (
            len(self.diagnostic_codes)
            > STRUCTURED_MODEL_INPUT_LIMITS.maximum_diagnostics
        ):
            raise ValueError("compiled diagnostics exceed the hard bound")
        if len(self.diagnostic_codes) != len(set(self.diagnostic_codes)):
            raise ValueError("compiled diagnostics are duplicated")
        for value in (
            self.source_collection_fingerprint,
            self.compiled_semantic_fingerprint,
            self.compile_binding_fingerprint,
        ):
            if not value.startswith(SHA256_PREFIX):
                raise ValueError("compiled model input fingerprint is invalid")


@dataclass(frozen=True, slots=True)
class FrozenModelInputSemanticProjection:
    """Non-executable normal-lowering result that may exceed token budget.

    Compaction uses this complete frozen value only to choose an exact semantic
    prefix.  It is deliberately not a ``FrozenCompiledModelInput`` and carries
    no install candidate or provider-open authority.
    """

    canonical_input_identity: CanonicalModelInputIdentity
    system_prompt: str = field(repr=False)
    messages: tuple[LLMMessage, ...] = field(repr=False)
    message_placements: tuple[FrozenCompiledMessagePlacement, ...] = field(repr=False)
    tools: tuple[FrozenToolSpec, ...] = field(repr=False)
    source_decisions: tuple[CompiledSourceDecision, ...]
    tool_result_decisions: tuple[CompiledToolResultDecision, ...]
    diagnostic_codes: tuple[ContextPublicDiagnosticCode, ...]
    source_collection_fingerprint: str
    compile_binding_fingerprint: str

    def __post_init__(self) -> None:
        if len(self.message_placements) != len(self.messages):
            raise ValueError("semantic projection placements are not parallel")
        if tuple(item.message_ordinal for item in self.message_placements) != tuple(
            range(len(self.messages))
        ):
            raise ValueError("semantic projection placement order is invalid")
        if any(
            item.role is not message.role
            for item, message in zip(
                self.message_placements, self.messages, strict=True
            )
        ):
            raise ValueError("semantic projection placement role drifted")
        if len(
            self.diagnostic_codes
        ) > STRUCTURED_MODEL_INPUT_LIMITS.maximum_diagnostics or len(
            self.diagnostic_codes
        ) != len(set(self.diagnostic_codes)):
            raise ValueError("semantic projection diagnostics exceed their bounds")
        for value in (
            self.source_collection_fingerprint,
            self.compile_binding_fingerprint,
        ):
            if not value.startswith(SHA256_PREFIX):
                raise ValueError("semantic projection fingerprint is invalid")


def _llm_message_value(message: LLMMessage) -> dict[str, object]:
    return {
        "role": message.role.value,
        "content": llm_content_identity_value(message.content),
        # Preserve the established fingerprint value after removing the
        # superseded semantic-thinking DTO slot.
        "thinking": (),
        "tool_calls": tuple(
            (call.id, call.name, call.arguments) for call in message.tool_calls
        ),
        "tool_call_id": message.tool_call_id,
        "name": message.name,
        "arguments": message.arguments,
    }


@dataclass(frozen=True, slots=True)
class RuntimeTemporalCapture:
    observed_at_utc: datetime
    local_date: date
    timezone_name: str
    utc_offset_minutes: int

    def __post_init__(self) -> None:
        if (
            self.observed_at_utc.tzinfo is None
            or self.observed_at_utc.utcoffset() is None
            or self.observed_at_utc.utcoffset().total_seconds() != 0
            or not self.timezone_name
            or not -1_440 < self.utc_offset_minutes < 1_440
        ):
            raise ValueError("runtime temporal capture is invalid")


@dataclass(frozen=True, slots=True)
class RuntimeEnvironmentSnapshot:
    workspace_kind: Literal["project", "transient"]
    workspace_root: str
    timezone_name: str
    utc_offset_minutes: int | None

    def __post_init__(self) -> None:
        if (
            self.workspace_kind not in {"project", "transient"}
            or not self.workspace_root
            or not self.timezone_name
            or (
                self.utc_offset_minutes is not None
                and not -1_440 < self.utc_offset_minutes < 1_440
            )
        ):
            raise ValueError("runtime environment snapshot is invalid")


@dataclass(frozen=True, slots=True)
class RuntimeClockSnapshot:
    observed_at_utc: datetime
    local_date: date
    timezone_name: str
    utc_offset_minutes: int

    def __post_init__(self) -> None:
        if (
            self.observed_at_utc.tzinfo is None
            or self.observed_at_utc.utcoffset() is None
            or self.observed_at_utc.utcoffset().total_seconds() != 0
            or not self.timezone_name
            or not -1_440 < self.utc_offset_minutes < 1_440
        ):
            raise ValueError("runtime clock snapshot is invalid")


__all__ = [name for name in globals() if not name.startswith("_")]
