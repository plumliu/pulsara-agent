"""Pure contracts for Host-scoped provider-input prefix continuity.

The values in this module are provider-neutral and process-local.  They own no
transport, repository, callback, task, writer guard, or durable identity.  A
Host may discard every value on close without changing canonical truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Mapping

from pulsara_agent.llm.input import (
    LLMMessage,
    llm_content_identity_value,
    llm_content_logical_bytes,
)
from pulsara_agent.llm.frozen_target import (
    FrozenEpochModelCallTarget,
    FrozenEpochModelTargetBundle,
)
from pulsara_agent.llm.provider_replay import ProviderAssistantReplayFragment
from pulsara_agent.llm.request import (
    FrozenProviderWireInputPlan,
    FrozenProviderWireInputQuote,
    FrozenProviderWireMaterialization,
)
from pulsara_agent.capability.contracts import (
    FrozenCapabilityDispatchCut,
    FrozenNativeToolProjectionSet,
    FrozenToolCapabilityExposurePlan,
    frozen_tool_spec_fingerprint,
)
from pulsara_agent.model_input.contracts import (
    CompiledToolResultDecision,
    ContextSourceKind,
    FrozenCompiledModelInput,
    FrozenCompiledMessagePlacement,
    FrozenModelInputSemanticProjection,
    FrozenToolSpec,
    ModelInputScopeKind,
)
from pulsara_agent.primitives.context import (
    canonical_json_bytes,
    context_fingerprint,
    FrozenJsonObjectFact,
    freeze_json,
    thaw_json,
)

if TYPE_CHECKING:
    from pulsara_agent.conversation_kernel.capability_composition import (
        PreparedSkillCatalogSourceSnapshot,
        PreparedMcpInspectionInput,
    )
    from pulsara_agent.conversation_kernel.mcp.contracts import McpCatalogSnapshot
    from pulsara_agent.conversation_kernel.memory.contracts import (
        MemoryObservationState,
    )
    from pulsara_agent.capability.types import MinimalSkillRef
from pulsara_agent.primitives.model_call import ModelCallPurpose

FULL_HISTORY_CONTEXT_BASE_IDENTITY = context_fingerprint(
    "pulsara:context-base-semantic-identity:v1",
    {"kind": "FULL_HISTORY"},
)
_EMPTY_SCOPE_COLD_START_SEAL = object()
_EXPLICIT_MODEL_SWITCH_AUTHORITY_SEAL = object()
_DIRECT_SWITCH_ADMISSION_SEAL = object()


def _fingerprint(value: str, name: str) -> None:
    if not value.startswith("sha256:") or len(value) != 71:
        raise ValueError(f"{name} is not a canonical SHA-256 fingerprint")


@dataclass(frozen=True, slots=True)
class ProviderInputContinuityScope:
    session_id: str
    scope_kind: ModelInputScopeKind = field(compare=False)

    def __post_init__(self) -> None:
        if not self.session_id:
            raise ValueError("provider-input continuity session is empty")


@dataclass(frozen=True, slots=True)
class ProcessLocalCanonicalFrontier:
    latest_context_binding_revision_id: str
    context_base_semantic_identity: str
    through_sequence: int
    ordered_item_fingerprints: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.latest_context_binding_revision_id or self.through_sequence < 0:
            raise ValueError("canonical frontier identity is invalid")
        _fingerprint(self.context_base_semantic_identity, "context base")
        # The cut counts transcript entries, not lowered input items. One
        # entry can expose multiple blocks; interrupted calls also need
        # derived closure items. Prefix equality below protects the actual
        # ordered input independently of the canonical sequence cut.
        for value in self.ordered_item_fingerprints:
            _fingerprint(value, "canonical item")

    def require_prefix_of(self, successor: "ProcessLocalCanonicalFrontier") -> None:
        if (
            self.context_base_semantic_identity
            != successor.context_base_semantic_identity
        ):
            raise ValueError("canonical context base changed inside an epoch")
        if successor.through_sequence < self.through_sequence:
            raise ValueError("canonical frontier sequence moved backwards")
        if successor.ordered_item_fingerprints[
            : len(self.ordered_item_fingerprints)
        ] != (self.ordered_item_fingerprints):
            raise ValueError("canonical provider-input prefix was rewritten")


@dataclass(frozen=True, slots=True)
class RuntimeObservationItem:
    text: str
    target: FrozenJsonObjectFact | Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text:
            raise ValueError("runtime observation text must be non-empty")
        if self.target is not None and not isinstance(
            self.target, FrozenJsonObjectFact
        ):
            target = freeze_json(self.target)
            if not isinstance(target, FrozenJsonObjectFact):
                raise ValueError("runtime observation target must be an object")
            object.__setattr__(self, "target", target)


def encode_runtime_observation(
    *, source_kind: ContextSourceKind, body: tuple[RuntimeObservationItem, ...]
) -> LLMMessage:
    if not body or source_kind in {
        ContextSourceKind.BASE_SYSTEM,
        ContextSourceKind.SKILL_CATALOG,
        ContextSourceKind.COMPACTION_RUNTIME_HANDOFF,
    }:
        raise ValueError("runtime observation requires its source and non-empty body")
    values = tuple(
        {
            "text": item.text,
            **({} if item.target is None else {"target": thaw_json(item.target)}),
        }
        for item in body
    )
    return LLMMessage.user(
        canonical_json_bytes(
            {
                "pulsara_runtime_observation": {
                    "source": source_kind.value,
                    "body": values,
                }
            }
        ).decode("utf-8")
    )


@dataclass(frozen=True, slots=True)
class InstalledMcpObservationSnapshot:
    """Actually announced catalog state with last successful tool facts."""

    catalog_snapshot: McpCatalogSnapshot
    inspection_inputs: tuple[PreparedMcpInspectionInput, ...]


@dataclass(frozen=True, slots=True)
class InstalledObservationState:
    """Exactly the domain snapshots represented in this installed context."""

    last_turn_id: str | None = None
    reread_skills: tuple[MinimalSkillRef, ...] = ()
    skill_catalog: PreparedSkillCatalogSourceSnapshot | None = field(
        default=None, repr=False
    )
    mcp_catalog: InstalledMcpObservationSnapshot | None = field(
        default=None, repr=False
    )
    memory: MemoryObservationState | None = field(default=None, repr=False)


def _message_value(message: LLMMessage) -> object:
    return {
        "role": message.role.value,
        "content": llm_content_identity_value(message.content),
        # Preserve the established fingerprint value after removing the
        # superseded semantic-thinking DTO slot.
        "thinking": (),
        "tool_calls": tuple(
            (item.id, item.name, item.arguments) for item in message.tool_calls
        ),
        "tool_call_id": message.tool_call_id,
        "name": message.name,
        "arguments": message.arguments,
    }


def provider_input_prefix_fingerprint(
    *,
    system_prompt: str,
    tools: tuple[FrozenToolSpec, ...],
    messages: tuple[LLMMessage, ...],
) -> str:
    return context_fingerprint(
        "pulsara:provider-input-semantic-prefix:v2-wire-proof",
        {
            "system": system_prompt,
            "tools": tuple(item.canonical_bytes.decode("utf-8") for item in tools),
            "messages": tuple(_message_value(item) for item in messages),
        },
    )


def provider_input_logical_bytes(
    *,
    system_prompt: str,
    tools: tuple[FrozenToolSpec, ...],
    messages: tuple[LLMMessage, ...],
) -> int:
    values: list[str] = [system_prompt]
    values.extend(item.canonical_bytes.decode("utf-8") for item in tools)
    content_bytes = 0
    for message in messages:
        content_bytes += llm_content_logical_bytes(message.content)
        for call in message.tool_calls:
            values.extend((call.id, call.name, call.arguments))
        values.extend(
            value
            for value in (message.tool_call_id, message.name, message.arguments)
            if value is not None
        )
    return content_bytes + sum(len(item.encode("utf-8")) for item in values)


@dataclass(frozen=True, slots=True)
class FrozenProviderInputEpochView:
    scope: ProviderInputContinuityScope
    epoch_nonce: str
    epoch_revision: int
    system_prompt: str = field(repr=False)
    tools: tuple[FrozenToolSpec, ...] = field(repr=False)
    messages: tuple[LLMMessage, ...] = field(repr=False)
    message_placements: tuple[FrozenCompiledMessagePlacement, ...] = field(repr=False)
    wire_input_plan: FrozenProviderWireInputPlan = field(repr=False)
    tool_exposure_plan: FrozenToolCapabilityExposurePlan = field(repr=False)
    canonical_frontier: ProcessLocalCanonicalFrontier
    observation_state: InstalledObservationState
    logical_bytes: int
    semantic_prefix_fingerprint: str
    assistant_replay_fragments: tuple[ProviderAssistantReplayFragment, ...] = field(
        default=(), repr=False
    )
    tool_result_decisions: tuple[CompiledToolResultDecision, ...] = field(
        default=(), repr=False
    )

    def __post_init__(self) -> None:
        if not self.epoch_nonce or self.epoch_revision < 1:
            raise ValueError("provider-input epoch revision is invalid")
        if len(self.message_placements) != len(self.messages):
            raise ValueError("provider-input epoch placements are not parallel")
        _fingerprint(
            self.wire_input_plan.compiled_semantic_fingerprint,
            "compiled semantic input",
        )
        direct_native_projection_set = self.tool_exposure_plan.direct_projection_set
        if (
            tuple(item.name for item in self.tools)
            != tuple(
                item.provider_name
                for item in direct_native_projection_set.tool_versions
            )
            or any(
                frozen_tool_spec_fingerprint(spec)
                != projection.canonical_tool_spec_fingerprint
                for spec, projection in zip(
                    self.tools,
                    direct_native_projection_set.projections,
                    strict=True,
                )
            )
            or self.wire_input_plan.materialization.tool_items
            != tuple(
                item.wire_tool for item in direct_native_projection_set.projections
            )
        ):
            raise ValueError("installed native tool proof drifted")
        if self.logical_bytes != provider_input_logical_bytes(
            system_prompt=self.system_prompt, tools=self.tools, messages=self.messages
        ):
            raise ValueError("provider-input epoch logical size mismatch")
        expected = provider_input_prefix_fingerprint(
            system_prompt=self.system_prompt, tools=self.tools, messages=self.messages
        )
        if self.semantic_prefix_fingerprint != expected:
            raise ValueError("provider-input epoch prefix fingerprint mismatch")
        fragment_entries = tuple(
            item.assistant_entry_id for item in self.assistant_replay_fragments
        )
        if len(fragment_entries) != len(set(fragment_entries)):
            raise ValueError("provider-input replay fragments are duplicated")

    @property
    def capability_dispatch_cut(self) -> FrozenCapabilityDispatchCut:
        return self.tool_exposure_plan.dispatch_view.parent_dispatch_cut

    @property
    def direct_native_projection_set(self) -> FrozenNativeToolProjectionSet:
        return self.tool_exposure_plan.direct_projection_set


@dataclass(frozen=True, slots=True)
class InstalledEpochRuntimeCohort:
    """One atomic installed semantic prefix and its transport-free target."""

    view: FrozenProviderInputEpochView
    target_bundle: FrozenEpochModelTargetBundle


@dataclass(frozen=True, slots=True, init=False)
class FrozenDirectSwitchAdmission:
    destination: FrozenEpochModelCallTarget
    semantic_projection: FrozenModelInputSemanticProjection = field(repr=False)
    wire_materialization: FrozenProviderWireMaterialization = field(repr=False)
    quote: FrozenProviderWireInputQuote

    def __init__(
        self,
        *,
        destination: FrozenEpochModelCallTarget,
        semantic_projection: FrozenModelInputSemanticProjection,
        wire_materialization: FrozenProviderWireMaterialization,
        quote: FrozenProviderWireInputQuote,
        _seal: object,
    ) -> None:
        if (
            _seal is not _DIRECT_SWITCH_ADMISSION_SEAL
            or destination.purpose is not ModelCallPurpose.AGENT_MODEL_LOOP
            or semantic_projection.canonical_input_identity.session_id
            != destination.session_id
            or semantic_projection.canonical_input_identity.turn_id
            != destination.turn_id
            or quote.wire_api != destination.target_bundle.target_fact.wire_api
            or quote.effective_input_budget_tokens
            != destination.input_budget.effective_input_budget_tokens
        ):
            raise ValueError("direct switch admission does not exact-join")
        object.__setattr__(self, "destination", destination)
        object.__setattr__(self, "semantic_projection", semantic_projection)
        object.__setattr__(self, "wire_materialization", wire_materialization)
        object.__setattr__(self, "quote", quote)


def _issue_frozen_direct_switch_admission(
    *,
    destination: FrozenEpochModelCallTarget,
    semantic_projection: FrozenModelInputSemanticProjection,
    wire_input_plan: FrozenProviderWireInputPlan,
) -> FrozenDirectSwitchAdmission:
    """Consume no authority; freeze the exact admitted Tier-1 pure result."""

    return FrozenDirectSwitchAdmission(
        destination=destination,
        semantic_projection=semantic_projection,
        wire_materialization=wire_input_plan.materialization,
        quote=wire_input_plan.quote,
        _seal=_DIRECT_SWITCH_ADMISSION_SEAL,
    )


@dataclass(frozen=True, slots=True)
class InstalledEpochAppend:
    predecessor: InstalledEpochRuntimeCohort


@dataclass(frozen=True, slots=True, init=False)
class EmptyScopeColdStart:
    """Continuity-issued transition carrying the exact empty preparation."""

    scope: ProviderInputContinuityScope
    destination: FrozenEpochModelCallTarget
    seed: "CanonicalColdContinuationSeed" = field(  # noqa: F821
        repr=False
    )
    _preparation_basis: object = field(repr=False, compare=False)

    def __init__(
        self,
        *,
        scope: ProviderInputContinuityScope,
        destination: FrozenEpochModelCallTarget,
        seed: "CanonicalColdContinuationSeed",  # noqa: F821
        preparation_basis: object,
        _seal: object,
    ) -> None:
        if _seal is not _EMPTY_SCOPE_COLD_START_SEAL:
            raise TypeError("empty cold start is continuity-issued")
        if preparation_basis is None:
            raise ValueError("empty cold start lacks its preparation basis")
        if scope.session_id != destination.session_id:
            raise ValueError("empty bootstrap transition scope drifted")
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "destination", destination)
        object.__setattr__(self, "seed", seed)
        object.__setattr__(self, "_preparation_basis", preparation_basis)


@dataclass(frozen=True, slots=True)
class ExplicitModelSwitchColdStart:
    predecessor: InstalledEpochRuntimeCohort
    admission: FrozenDirectSwitchAdmission = field(repr=False)
    _authority: object = field(repr=False, compare=False)

    @property
    def destination(self) -> FrozenEpochModelCallTarget:
        return self.admission.destination

    def __post_init__(self) -> None:
        if (
            self._authority is not _EXPLICIT_MODEL_SWITCH_AUTHORITY_SEAL
            or self.predecessor.target_bundle == self.destination.target_bundle
            or self.predecessor.view.scope.session_id != self.destination.session_id
        ):
            raise ValueError("explicit model switch authority is invalid")


_ADOPTED_COMPACTION_SUCCESSOR_SEAL = object()


@dataclass(frozen=True, slots=True, init=False)
class AdoptedCompactionSuccessor:
    predecessor: InstalledEpochRuntimeCohort
    destination: FrozenEpochModelCallTarget
    seed: "AdoptedCompactionContinuationSeed" = field(repr=False)  # noqa: F821

    def __init__(
        self,
        *,
        predecessor: InstalledEpochRuntimeCohort,
        destination: FrozenEpochModelCallTarget,
        seed: "AdoptedCompactionContinuationSeed",  # noqa: F821
        _seal: object,
    ) -> None:
        if _seal is not _ADOPTED_COMPACTION_SUCCESSOR_SEAL:
            raise TypeError("adopted compaction successor is continuity-issued")
        object.__setattr__(self, "predecessor", predecessor)
        object.__setattr__(self, "destination", destination)
        object.__setattr__(self, "seed", seed)


def _issue_adopted_compaction_successor(
    *,
    predecessor: InstalledEpochRuntimeCohort,
    destination: FrozenEpochModelCallTarget,
    seed: "AdoptedCompactionContinuationSeed",  # noqa: F821
) -> AdoptedCompactionSuccessor:
    return AdoptedCompactionSuccessor(
        predecessor=predecessor,
        destination=destination,
        seed=seed,
        _seal=_ADOPTED_COMPACTION_SUCCESSOR_SEAL,
    )


ProviderInputEpochTransition = (
    InstalledEpochAppend
    | EmptyScopeColdStart
    | ExplicitModelSwitchColdStart
    | AdoptedCompactionSuccessor
)


def _issue_empty_scope_cold_start(
    *,
    scope: ProviderInputContinuityScope,
    destination: FrozenEpochModelCallTarget,
    seed: "CanonicalColdContinuationSeed",  # noqa: F821
    preparation_basis: object,
) -> EmptyScopeColdStart:
    return EmptyScopeColdStart(
        scope=scope,
        destination=destination,
        seed=seed,
        preparation_basis=preparation_basis,
        _seal=_EMPTY_SCOPE_COLD_START_SEAL,
    )


def _issue_explicit_model_switch_transition(
    *,
    predecessor: InstalledEpochRuntimeCohort,
    admission: FrozenDirectSwitchAdmission,
) -> ExplicitModelSwitchColdStart:
    return ExplicitModelSwitchColdStart(
        predecessor=predecessor,
        admission=admission,
        _authority=_EXPLICIT_MODEL_SWITCH_AUTHORITY_SEAL,
    )


class ProviderInputAdmissionPredecessorKind(StrEnum):
    EMPTY = "EMPTY"
    INSTALLED = "INSTALLED"


@dataclass(frozen=True, slots=True)
class NewTriggerAnchor:
    source_entry_id: str
    provider_input_item_fingerprint: str
    provider_group_boundary_fingerprint: str

    def __post_init__(self) -> None:
        if not self.source_entry_id:
            raise ValueError("provider-input trigger entry is empty")
        _fingerprint(self.provider_input_item_fingerprint, "trigger item")
        _fingerprint(self.provider_group_boundary_fingerprint, "trigger boundary")


@dataclass(frozen=True, slots=True)
class NoNewTriggerAnchor:
    predecessor_frontier_fingerprint: str | None

    def __post_init__(self) -> None:
        if self.predecessor_frontier_fingerprint is not None:
            _fingerprint(self.predecessor_frontier_fingerprint, "predecessor frontier")


ProviderInputDispatchAnchor = NewTriggerAnchor | NoNewTriggerAnchor


def provider_input_dispatch_anchor_value(
    anchor: ProviderInputDispatchAnchor,
) -> object:
    if isinstance(anchor, NewTriggerAnchor):
        return {
            "kind": "NEW_TRIGGER",
            "source_entry_id": anchor.source_entry_id,
            "provider_input_item_fingerprint": (anchor.provider_input_item_fingerprint),
            "provider_group_boundary_fingerprint": (
                anchor.provider_group_boundary_fingerprint
            ),
        }
    return {
        "kind": "NO_NEW_TRIGGER",
        "predecessor_frontier_fingerprint": (anchor.predecessor_frontier_fingerprint),
    }


@dataclass(frozen=True, slots=True)
class FrozenProviderInputAppendPlanningInput:
    planning_nonce: str
    scope: ProviderInputContinuityScope
    predecessor: ProviderInputAdmissionPredecessorKind
    predecessor_view: FrozenProviderInputEpochView | None = field(repr=False)
    dispatch_anchor: ProviderInputDispatchAnchor
    canonical_delta_fingerprints: tuple[str, ...]
    _empty_preparation_reservation: object | None = field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if (self.predecessor is ProviderInputAdmissionPredecessorKind.EMPTY) != (
            self.predecessor_view is None
        ):
            raise ValueError("provider-input planning predecessor union is invalid")
        if not self.planning_nonce:
            raise ValueError("provider-input planning nonce is empty")
        for value in self.canonical_delta_fingerprints:
            _fingerprint(value, "canonical delta")


@dataclass(frozen=True, slots=True)
class PreparedProviderInputAppendCandidate:
    planning: FrozenProviderInputAppendPlanningInput = field(repr=False)
    transition: ProviderInputEpochTransition = field(repr=False)
    call_target: FrozenEpochModelCallTarget = field(repr=False)
    epoch_nonce: str
    expected_epoch_revision: int
    resulting_compiled_input: FrozenCompiledModelInput = field(repr=False)
    wire_input_plan: FrozenProviderWireInputPlan = field(repr=False)
    tool_exposure_plan: FrozenToolCapabilityExposurePlan = field(repr=False)
    resulting_canonical_frontier: ProcessLocalCanonicalFrontier
    resulting_observation_state: InstalledObservationState
    appended_message_count: int
    _preparation_basis: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.epoch_nonce or self.expected_epoch_revision < 0:
            raise ValueError("provider-input append epoch identity is invalid")
        if self.appended_message_count < 0:
            raise ValueError("provider-input append message count is invalid")
        if (
            self.resulting_compiled_input.tools
            != self.tool_exposure_plan.direct_tool_surface.tool_specs
            or self.wire_input_plan.materialization.tool_items
            != tuple(
                item.wire_tool
                for item in self.tool_exposure_plan.direct_projection_set.projections
            )
        ):
            raise ValueError("provider-input append candidate capability plan drifted")
        if isinstance(self.transition, InstalledEpochAppend):
            if (
                self.call_target.target_bundle
                != self.transition.predecessor.target_bundle
                or self.epoch_nonce != self.transition.predecessor.view.epoch_nonce
                or self.expected_epoch_revision
                != self.transition.predecessor.view.epoch_revision
            ):
                raise ValueError("installed append transition drifted")
        elif self.call_target is not self.transition.destination:
            raise ValueError("new epoch transition destination drifted")

    @property
    def scope(self) -> ProviderInputContinuityScope:
        return self.planning.scope

    @property
    def dispatch_anchor(self) -> ProviderInputDispatchAnchor:
        return self.planning.dispatch_anchor

    @property
    def capability_dispatch_cut(self) -> FrozenCapabilityDispatchCut:
        return self.tool_exposure_plan.dispatch_view.parent_dispatch_cut

    @property
    def direct_native_projection_set(self) -> FrozenNativeToolProjectionSet:
        return self.tool_exposure_plan.direct_projection_set


@dataclass(frozen=True, slots=True)
class FrozenProviderInputAppendCompileResult:
    compiled_input: FrozenCompiledModelInput = field(repr=False)
    canonical_frontier: ProcessLocalCanonicalFrontier
    observation_state: InstalledObservationState
    appended_message_count: int

    def __post_init__(self) -> None:
        if self.appended_message_count < 0:
            raise ValueError("compiled append message count is invalid")


@dataclass(frozen=True, slots=True)
class FrozenProviderInputAppendSemanticProjection:
    """Read-only prospective append; never a continuity install candidate."""

    projected_input: FrozenModelInputSemanticProjection = field(repr=False)
    canonical_frontier: ProcessLocalCanonicalFrontier
    appended_message_count: int

    def __post_init__(self) -> None:
        identity = self.projected_input.canonical_input_identity
        if (
            self.appended_message_count < 0
            or self.appended_message_count > len(self.projected_input.messages)
            or self.canonical_frontier.latest_context_binding_revision_id
            != identity.context_binding_revision_id
            or self.canonical_frontier.through_sequence
            != identity.provider_input_through_sequence
        ):
            raise ValueError("projected append message count is invalid")


@dataclass(frozen=True, slots=True)
class ProcessLocalProviderInputInstallPermit:
    scope: ProviderInputContinuityScope
    epoch_nonce: str
    epoch_revision: int
    permit_nonce: str

    def __post_init__(self) -> None:
        if not self.epoch_nonce or not self.permit_nonce or self.epoch_revision < 1:
            raise ValueError("provider-input install permit is incomplete")


__all__ = [
    "AdoptedCompactionSuccessor",
    "EmptyScopeColdStart",
    "ExplicitModelSwitchColdStart",
    "FULL_HISTORY_CONTEXT_BASE_IDENTITY",
    "FrozenDirectSwitchAdmission",
    "FrozenProviderInputAppendPlanningInput",
    "FrozenProviderInputAppendCompileResult",
    "FrozenProviderInputAppendSemanticProjection",
    "FrozenProviderInputEpochView",
    "InstalledEpochAppend",
    "InstalledEpochRuntimeCohort",
    "NewTriggerAnchor",
    "NoNewTriggerAnchor",
    "PreparedProviderInputAppendCandidate",
    "ProcessLocalCanonicalFrontier",
    "ProcessLocalProviderInputInstallPermit",
    "ProviderInputAdmissionPredecessorKind",
    "ProviderInputContinuityScope",
    "ProviderInputDispatchAnchor",
    "ProviderInputEpochTransition",
    "encode_runtime_observation",
    "RuntimeObservationItem",
    "InstalledObservationState",
    "InstalledMcpObservationSnapshot",
    "provider_input_logical_bytes",
    "provider_input_dispatch_anchor_value",
    "provider_input_prefix_fingerprint",
]
