"""Pure contracts for Host-scoped provider-input prefix continuity.

The values in this module are provider-neutral and process-local.  They own no
transport, repository, callback, task, writer guard, or durable identity.  A
Host may discard every value on close without changing canonical truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import json

from pulsara_agent.llm.input import (
    LLMMessage,
    LLMTextPart,
    MessageRole,
    llm_content_identity_value,
    llm_content_logical_bytes,
)
from pulsara_agent.llm.estimator import TokenEstimate
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
    FrozenMcpRouteProjection,
    FrozenNativeToolProjectionSet,
    FrozenToolCapabilityExposurePlan,
    frozen_tool_spec_fingerprint,
)
from pulsara_agent.model_input.contracts import (
    CompiledToolResultDecision,
    ContextSourceKind,
    ContextTrustClass,
    FrozenCompiledModelInput,
    FrozenCompiledMessagePlacement,
    FrozenModelInputSemanticProjection,
    FrozenToolSpec,
    ModelInputScopeKind,
)
from pulsara_agent.primitives.context import canonical_json_bytes, context_fingerprint
from pulsara_agent.primitives.model_call import ModelCallPurpose

PROVIDER_MESSAGE_LOWERING_CONTRACT = (
    "pulsara.provider-message-lowering.prefix-continuity.v11-tool-guidance"
)
FULL_HISTORY_CONTEXT_BASE_IDENTITY = context_fingerprint(
    "pulsara:context-base-semantic-identity:v1",
    {"kind": "FULL_HISTORY", "lowering": PROVIDER_MESSAGE_LOWERING_CONTRACT},
)
MAXIMUM_PROVIDER_INPUT_EPOCH_BYTES = 64 << 20
_EMPTY_BOOTSTRAP_AUTHORITY_SEAL = object()
_EXPLICIT_MODEL_SWITCH_AUTHORITY_SEAL = object()
_DIRECT_SWITCH_ADMISSION_SEAL = object()


def _fingerprint(value: str, name: str) -> None:
    if not value.startswith("sha256:") or len(value) != 71:
        raise ValueError(f"{name} is not a canonical SHA-256 fingerprint")


@dataclass(frozen=True, slots=True)
class ProviderInputContinuityScope:
    session_id: str
    scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None

    def __post_init__(self) -> None:
        if not self.session_id:
            raise ValueError("provider-input continuity session is empty")
        if (self.scope_kind is ModelInputScopeKind.ROOT) != (
            self.scope_subagent_task_id is None
        ):
            raise ValueError("provider-input continuity scope union is invalid")


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


class SourceObservationLifecycle(StrEnum):
    SNAPSHOT = "SNAPSHOT"
    CLEARED = "CLEARED"
    UNAVAILABLE = "UNAVAILABLE"
    TURN = "TURN"
    ACTIVATION = "ACTIVATION"
    CALL = "CALL"
    ONE_SHOT = "ONE_SHOT"


class SourceObservationPresence(StrEnum):
    VALUE = "VALUE"
    CLEARED = "CLEARED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class RuntimeObservation:
    source_kind: ContextSourceKind
    trust_class: ContextTrustClass
    lifecycle: SourceObservationLifecycle
    presence: SourceObservationPresence
    contract_version: str
    body: str = field(repr=False)

    def __post_init__(self) -> None:
        if self.source_kind is ContextSourceKind.BASE_SYSTEM:
            raise ValueError("base system is not a runtime observation")
        if not self.contract_version:
            raise ValueError("runtime observation contract is empty")
        self.body.encode("utf-8")
        if self.presence is SourceObservationPresence.VALUE and self.lifecycle in {
            SourceObservationLifecycle.CLEARED,
            SourceObservationLifecycle.UNAVAILABLE,
        }:
            raise ValueError("runtime observation lifecycle/presence differs")
        if self.presence is SourceObservationPresence.CLEARED and (
            self.lifecycle is not SourceObservationLifecycle.CLEARED or self.body
        ):
            raise ValueError("cleared runtime observation must have an empty body")
        if self.presence is SourceObservationPresence.UNAVAILABLE and (
            self.lifecycle is not SourceObservationLifecycle.UNAVAILABLE or self.body
        ):
            raise ValueError("unavailable runtime observation must have an empty body")


@dataclass(frozen=True, slots=True)
class ProviderRuntimeObservation:
    """Exact provider-visible projection; internal contract proofs stay local."""

    source_kind: ContextSourceKind
    trust_class: ContextTrustClass
    lifecycle: SourceObservationLifecycle
    presence: SourceObservationPresence
    body: str = field(repr=False)

    def __post_init__(self) -> None:
        # Reuse the closed lifecycle/presence validator without exposing its
        # internal contract member on the provider wire.
        RuntimeObservation(
            source_kind=self.source_kind,
            trust_class=self.trust_class,
            lifecycle=self.lifecycle,
            presence=self.presence,
            contract_version="provider-projection-only",
            body=self.body,
        )


def _provider_runtime_observation_message(
    observation: ProviderRuntimeObservation,
) -> LLMMessage:
    payload = {
        "pulsara_runtime_observation": {
            "body": observation.body,
            "lifecycle": observation.lifecycle.value,
            "presence": observation.presence.value,
            "source": observation.source_kind.value,
            "trust": observation.trust_class.value,
        }
    }
    return LLMMessage.user(canonical_json_bytes(payload).decode("utf-8"))


def encode_runtime_observation(
    *,
    source_kind: ContextSourceKind,
    trust_class: ContextTrustClass,
    lifecycle: SourceObservationLifecycle,
    presence: SourceObservationPresence,
    contract_version: str,
    body: str,
) -> LLMMessage:
    observation = RuntimeObservation(
        source_kind=source_kind,
        trust_class=trust_class,
        lifecycle=lifecycle,
        presence=presence,
        contract_version=contract_version,
        body=body,
    )
    return _provider_runtime_observation_message(
        ProviderRuntimeObservation(
            source_kind=observation.source_kind,
            trust_class=observation.trust_class,
            lifecycle=observation.lifecycle,
            presence=observation.presence,
            body=observation.body,
        )
    )


def decode_runtime_observation(message: LLMMessage) -> ProviderRuntimeObservation:
    if (
        message.role is not MessageRole.USER
        or len(message.content) != 1
        or not isinstance(message.content[0], LLMTextPart)
    ):
        raise ValueError("runtime observation must be one user-role JSON message")
    try:
        value = json.loads(message.content[0].text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("runtime observation JSON is invalid") from exc
    if not isinstance(value, dict) or set(value) != {"pulsara_runtime_observation"}:
        raise ValueError("runtime observation top-level contract is invalid")
    payload = value["pulsara_runtime_observation"]
    required = {"body", "lifecycle", "presence", "source", "trust"}
    if not isinstance(payload, dict) or set(payload) != required:
        raise ValueError("runtime observation member contract is invalid")
    if not all(isinstance(payload[name], str) for name in required):
        raise ValueError("runtime observation members must be strings")
    result = ProviderRuntimeObservation(
        source_kind=ContextSourceKind(payload["source"]),
        trust_class=ContextTrustClass(payload["trust"]),
        lifecycle=SourceObservationLifecycle(payload["lifecycle"]),
        presence=SourceObservationPresence(payload["presence"]),
        body=payload["body"],
    )
    if _provider_runtime_observation_message(result) != message:
        raise ValueError("runtime observation is not canonically encoded")
    return result


@dataclass(frozen=True, slots=True)
class ProcessLocalSourceHead:
    source_kind: ContextSourceKind
    presence: SourceObservationPresence
    semantic_fingerprint: str
    installed_observation_fingerprint: str
    last_emitted_turn_id: str | None
    last_emitted_model_call_index: int

    def __post_init__(self) -> None:
        _fingerprint(self.semantic_fingerprint, "source semantic")
        _fingerprint(self.installed_observation_fingerprint, "source observation")
        if self.last_emitted_model_call_index < 1:
            raise ValueError("source-head call index is invalid")


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
    source_heads: tuple[ProcessLocalSourceHead, ...]
    final_estimate: TokenEstimate
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
        if len(self.final_estimate.message_tokens_by_index) != len(self.messages):
            raise ValueError("provider-input epoch token breakdown is invalid")
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
        if self.logical_bytes > MAXIMUM_PROVIDER_INPUT_EPOCH_BYTES:
            raise ValueError("provider-input epoch exceeds its hard bound")
        expected = provider_input_prefix_fingerprint(
            system_prompt=self.system_prompt, tools=self.tools, messages=self.messages
        )
        if self.semantic_prefix_fingerprint != expected:
            raise ValueError("provider-input epoch prefix fingerprint mismatch")
        kinds = tuple(item.source_kind for item in self.source_heads)
        if len(kinds) != len(set(kinds)):
            raise ValueError("provider-input source heads are duplicated")
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

    @property
    def mcp_route_projection(self) -> FrozenMcpRouteProjection:
        return self.tool_exposure_plan.mcp_catalog_route_projection


@dataclass(frozen=True, slots=True)
class InstalledEpochRuntimeCohort:
    """One atomic installed semantic prefix and its transport-free target."""

    view: FrozenProviderInputEpochView
    target_bundle: FrozenEpochModelTargetBundle


@dataclass(frozen=True, slots=True, init=False)
class PreparedEmptyScopeBootstrapAuthority:
    scope: ProviderInputContinuityScope
    call_target: FrozenEpochModelCallTarget
    authority_nonce: str
    _preparation_basis: object = field(repr=False, compare=False)
    _owner_seal: object = field(repr=False, compare=False)

    def __init__(
        self,
        *,
        scope: ProviderInputContinuityScope,
        call_target: FrozenEpochModelCallTarget,
        authority_nonce: str,
        preparation_basis: object,
        _owner_seal: object,
    ) -> None:
        if (
            _owner_seal is not _EMPTY_BOOTSTRAP_AUTHORITY_SEAL
            or not authority_nonce
            or preparation_basis is None
        ):
            raise ValueError("empty bootstrap authority nonce is empty")
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "call_target", call_target)
        object.__setattr__(self, "authority_nonce", authority_nonce)
        object.__setattr__(self, "_preparation_basis", preparation_basis)
        object.__setattr__(self, "_owner_seal", _owner_seal)


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


@dataclass(frozen=True, slots=True)
class EmptyScopeColdStart:
    bootstrap_authority: PreparedEmptyScopeBootstrapAuthority
    seed: "CanonicalColdContinuationSeed | SubagentInitialSeed" = field(  # noqa: F821
        repr=False
    )

    @property
    def destination(self) -> FrozenEpochModelCallTarget:
        return self.bootstrap_authority.call_target

    def __post_init__(self) -> None:
        if self.bootstrap_authority.scope.session_id != self.destination.session_id:
            raise ValueError("empty bootstrap transition scope drifted")


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


def _issue_empty_scope_bootstrap_authority(
    *,
    scope: ProviderInputContinuityScope,
    call_target: FrozenEpochModelCallTarget,
    authority_nonce: str,
    preparation_basis: object,
) -> PreparedEmptyScopeBootstrapAuthority:
    return PreparedEmptyScopeBootstrapAuthority(
        scope=scope,
        call_target=call_target,
        authority_nonce=authority_nonce,
        preparation_basis=preparation_basis,
        _owner_seal=_EMPTY_BOOTSTRAP_AUTHORITY_SEAL,
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
    resulting_source_heads: tuple[ProcessLocalSourceHead, ...]
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

    @property
    def mcp_route_projection(self) -> FrozenMcpRouteProjection:
        return self.tool_exposure_plan.mcp_catalog_route_projection


@dataclass(frozen=True, slots=True)
class FrozenProviderInputAppendCompileResult:
    compiled_input: FrozenCompiledModelInput = field(repr=False)
    canonical_frontier: ProcessLocalCanonicalFrontier
    source_heads: tuple[ProcessLocalSourceHead, ...]
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
    "MAXIMUM_PROVIDER_INPUT_EPOCH_BYTES",
    "NewTriggerAnchor",
    "NoNewTriggerAnchor",
    "PROVIDER_MESSAGE_LOWERING_CONTRACT",
    "ProviderRuntimeObservation",
    "PreparedProviderInputAppendCandidate",
    "PreparedEmptyScopeBootstrapAuthority",
    "ProcessLocalCanonicalFrontier",
    "ProcessLocalProviderInputInstallPermit",
    "ProcessLocalSourceHead",
    "ProviderInputAdmissionPredecessorKind",
    "ProviderInputContinuityScope",
    "ProviderInputDispatchAnchor",
    "ProviderInputEpochTransition",
    "RuntimeObservation",
    "SourceObservationLifecycle",
    "SourceObservationPresence",
    "decode_runtime_observation",
    "encode_runtime_observation",
    "provider_input_logical_bytes",
    "provider_input_dispatch_anchor_value",
    "provider_input_prefix_fingerprint",
]
