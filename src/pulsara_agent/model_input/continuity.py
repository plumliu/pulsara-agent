"""Pure contracts for Host-scoped provider-input prefix continuity.

The values in this module are provider-neutral and process-local.  They own no
transport, repository, callback, task, writer guard, or durable identity.  A
Host may discard every value on close without changing canonical truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import json

from pulsara_agent.llm.input import LLMMessage, MessageRole
from pulsara_agent.llm.estimator import TokenEstimate
from pulsara_agent.llm.provider_replay import ProviderAssistantReplayFragment
from pulsara_agent.llm.request import FrozenProviderWireInputPlan
from pulsara_agent.model_input.contracts import (
    ContextSourceKind,
    ContextTrustClass,
    FrozenCompiledModelInput,
    FrozenCompiledMessagePlacement,
    FrozenToolSpec,
    ModelInputScopeKind,
)
from pulsara_agent.primitives.context import canonical_json_bytes, context_fingerprint


PROVIDER_MESSAGE_LOWERING_CONTRACT = (
    "pulsara.provider-message-lowering.prefix-continuity.v3-tool-result-full"
)
FULL_HISTORY_CONTEXT_BASE_IDENTITY = context_fingerprint(
    "pulsara:context-base-semantic-identity:v1",
    {"kind": "FULL_HISTORY", "lowering": PROVIDER_MESSAGE_LOWERING_CONTRACT},
)
MAXIMUM_PROVIDER_INPUT_EPOCH_BYTES = 64 << 20
NO_PROVIDER_ASSISTANT_REPLAY_CONTRACT_FINGERPRINT = context_fingerprint(
    "pulsara.provider-assistant-replay-profile:v1", {"kind": "NONE"}
)


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
class ProviderInputEpochCompatibility:
    compiler_contract_version: str
    base_system_semantic_fingerprint: str
    tool_surface_fingerprint: str
    model_target_fingerprint: str
    estimator_fingerprint: str
    provider_message_lowering_contract: str
    context_base_semantic_identity: str
    provider_assistant_replay_contract_fingerprint: str = (
        NO_PROVIDER_ASSISTANT_REPLAY_CONTRACT_FINGERPRINT
    )

    def __post_init__(self) -> None:
        if not self.compiler_contract_version or not self.provider_message_lowering_contract:
            raise ValueError("provider-input epoch compatibility is incomplete")
        for value, name in (
            (self.base_system_semantic_fingerprint, "base system"),
            (self.tool_surface_fingerprint, "tool surface"),
            (self.model_target_fingerprint, "model target"),
            (self.estimator_fingerprint, "estimator"),
            (self.context_base_semantic_identity, "context base"),
            (
                self.provider_assistant_replay_contract_fingerprint,
                "provider assistant replay contract",
            ),
        ):
            _fingerprint(value, name)


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
        if len(self.ordered_item_fingerprints) > self.through_sequence + 1:
            raise ValueError("canonical frontier item count exceeds its sequence cut")
        for value in self.ordered_item_fingerprints:
            _fingerprint(value, "canonical item")

    def require_prefix_of(self, successor: "ProcessLocalCanonicalFrontier") -> None:
        if self.context_base_semantic_identity != successor.context_base_semantic_identity:
            raise ValueError("canonical context base changed inside an epoch")
        if successor.through_sequence < self.through_sequence:
            raise ValueError("canonical frontier sequence moved backwards")
        if successor.ordered_item_fingerprints[: len(self.ordered_item_fingerprints)] != (
            self.ordered_item_fingerprints
        ):
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
    if message.role is not MessageRole.USER or len(message.content) != 1:
        raise ValueError("runtime observation must be one user-role JSON message")
    try:
        value = json.loads(message.content[0])
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


class ProviderInputEpochResetReason(StrEnum):
    COLD_HOST_BOOTSTRAP = "COLD_HOST_BOOTSTRAP"
    BASE_SYSTEM_CHANGED = "BASE_SYSTEM_CHANGED"
    TOOL_SURFACE_CHANGED = "TOOL_SURFACE_CHANGED"
    MODEL_TARGET_CHANGED = "MODEL_TARGET_CHANGED"
    PROVIDER_LOWERING_CHANGED = "PROVIDER_LOWERING_CHANGED"
    CONTEXT_BINDING_REWRITE = "CONTEXT_BINDING_REWRITE"
    EXPLICIT_TEST_RESET = "EXPLICIT_TEST_RESET"


def _message_value(message: LLMMessage) -> object:
    return {
        "role": message.role.value,
        "content": message.content,
        "thinking": message.thinking,
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


def provider_input_logical_utf8_bytes(
    *, system_prompt: str, tools: tuple[FrozenToolSpec, ...], messages: tuple[LLMMessage, ...]
) -> int:
    values: list[str] = [system_prompt]
    values.extend(item.canonical_bytes.decode("utf-8") for item in tools)
    for message in messages:
        values.extend(message.content)
        values.extend(message.thinking)
        for call in message.tool_calls:
            values.extend((call.id, call.name, call.arguments))
        values.extend(
            value
            for value in (message.tool_call_id, message.name, message.arguments)
            if value is not None
        )
    return sum(len(item.encode("utf-8")) for item in values)


@dataclass(frozen=True, slots=True)
class FrozenProviderInputEpochView:
    scope: ProviderInputContinuityScope
    epoch_nonce: str
    epoch_revision: int
    compatibility: ProviderInputEpochCompatibility
    system_prompt: str = field(repr=False)
    tools: tuple[FrozenToolSpec, ...] = field(repr=False)
    messages: tuple[LLMMessage, ...] = field(repr=False)
    message_placements: tuple[FrozenCompiledMessagePlacement, ...] = field(
        repr=False
    )
    wire_input_plan: FrozenProviderWireInputPlan = field(repr=False)
    canonical_frontier: ProcessLocalCanonicalFrontier
    source_heads: tuple[ProcessLocalSourceHead, ...]
    final_estimate: TokenEstimate
    logical_utf8_bytes: int
    semantic_prefix_fingerprint: str
    assistant_replay_fragments: tuple[ProviderAssistantReplayFragment, ...] = field(
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
        if self.logical_utf8_bytes != provider_input_logical_utf8_bytes(
            system_prompt=self.system_prompt, tools=self.tools, messages=self.messages
        ):
            raise ValueError("provider-input epoch logical size mismatch")
        if self.logical_utf8_bytes > MAXIMUM_PROVIDER_INPUT_EPOCH_BYTES:
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
            "provider_input_item_fingerprint": (
                anchor.provider_input_item_fingerprint
            ),
            "provider_group_boundary_fingerprint": (
                anchor.provider_group_boundary_fingerprint
            ),
        }
    return {
        "kind": "NO_NEW_TRIGGER",
        "predecessor_frontier_fingerprint": (
            anchor.predecessor_frontier_fingerprint
        ),
    }


@dataclass(frozen=True, slots=True)
class FrozenProviderInputAppendPlanningInput:
    scope: ProviderInputContinuityScope
    predecessor: ProviderInputAdmissionPredecessorKind
    predecessor_view: FrozenProviderInputEpochView | None = field(repr=False)
    dispatch_anchor: ProviderInputDispatchAnchor
    canonical_delta_fingerprints: tuple[str, ...]
    planning_fingerprint: str

    def __post_init__(self) -> None:
        if (self.predecessor is ProviderInputAdmissionPredecessorKind.EMPTY) != (
            self.predecessor_view is None
        ):
            raise ValueError("provider-input planning predecessor union is invalid")
        for value in self.canonical_delta_fingerprints:
            _fingerprint(value, "canonical delta")
        expected = provider_input_append_planning_fingerprint(
            scope=self.scope,
            predecessor=self.predecessor,
            predecessor_view=self.predecessor_view,
            dispatch_anchor=self.dispatch_anchor,
            canonical_delta_fingerprints=self.canonical_delta_fingerprints,
        )
        if self.planning_fingerprint != expected:
            raise ValueError("provider-input planning fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class PreparedProviderInputAppendCandidate:
    scope: ProviderInputContinuityScope
    epoch_nonce: str
    expected_epoch_revision: int
    predecessor_prefix_fingerprint: str | None
    dispatch_anchor: ProviderInputDispatchAnchor
    resulting_compiled_input: FrozenCompiledModelInput = field(repr=False)
    wire_input_plan: FrozenProviderWireInputPlan = field(repr=False)
    resulting_canonical_frontier: ProcessLocalCanonicalFrontier
    resulting_source_heads: tuple[ProcessLocalSourceHead, ...]
    appended_message_count: int
    reset_reason: ProviderInputEpochResetReason | None
    compatibility: ProviderInputEpochCompatibility
    planning_fingerprint: str
    candidate_fingerprint: str

    def __post_init__(self) -> None:
        if not self.epoch_nonce or self.expected_epoch_revision < 0:
            raise ValueError("provider-input append epoch identity is invalid")
        if self.predecessor_prefix_fingerprint is not None:
            _fingerprint(self.predecessor_prefix_fingerprint, "predecessor prefix")
        if self.appended_message_count < 0:
            raise ValueError("provider-input append message count is invalid")
        expected = prepared_provider_input_append_candidate_fingerprint(
            scope=self.scope,
            epoch_nonce=self.epoch_nonce,
            expected_epoch_revision=self.expected_epoch_revision,
            predecessor_prefix_fingerprint=self.predecessor_prefix_fingerprint,
            dispatch_anchor=self.dispatch_anchor,
            resulting_compiled_input=self.resulting_compiled_input,
            wire_input_plan=self.wire_input_plan,
            resulting_canonical_frontier=self.resulting_canonical_frontier,
            resulting_source_heads=self.resulting_source_heads,
            appended_message_count=self.appended_message_count,
            reset_reason=self.reset_reason,
            compatibility=self.compatibility,
            planning_fingerprint=self.planning_fingerprint,
        )
        if self.candidate_fingerprint != expected:
            raise ValueError("provider-input append candidate fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class FrozenProviderInputAppendCompileResult:
    compiled_input: FrozenCompiledModelInput = field(repr=False)
    canonical_frontier: ProcessLocalCanonicalFrontier
    source_heads: tuple[ProcessLocalSourceHead, ...]
    appended_message_count: int
    reset_reason: ProviderInputEpochResetReason | None

    def __post_init__(self) -> None:
        if self.appended_message_count < 0:
            raise ValueError("compiled append message count is invalid")


@dataclass(frozen=True, slots=True)
class ProcessLocalProviderInputInstallPermit:
    scope: ProviderInputContinuityScope
    epoch_nonce: str
    epoch_revision: int
    candidate_fingerprint: str
    execution_fingerprint: str
    permit_nonce: str

    def __post_init__(self) -> None:
        if not self.epoch_nonce or not self.permit_nonce or self.epoch_revision < 1:
            raise ValueError("provider-input install permit is incomplete")
        _fingerprint(self.candidate_fingerprint, "append candidate")
        _fingerprint(self.execution_fingerprint, "model execution")


def provider_input_append_planning_fingerprint(
    *,
    scope: ProviderInputContinuityScope,
    predecessor: ProviderInputAdmissionPredecessorKind,
    predecessor_view: FrozenProviderInputEpochView | None,
    dispatch_anchor: ProviderInputDispatchAnchor,
    canonical_delta_fingerprints: tuple[str, ...],
) -> str:
    return context_fingerprint(
        "pulsara:provider-input-append-planning:v2-wire-proof",
        {
            "scope": (
                scope.session_id,
                scope.scope_kind.value,
                scope.scope_subagent_task_id,
            ),
            "predecessor": predecessor.value,
            "predecessor_prefix": (
                None
                if predecessor_view is None
                else predecessor_view.semantic_prefix_fingerprint
            ),
            "predecessor_revision": (
                None if predecessor_view is None else predecessor_view.epoch_revision
            ),
            "anchor": provider_input_dispatch_anchor_value(dispatch_anchor),
            "delta": canonical_delta_fingerprints,
        },
    )


def prepared_provider_input_append_candidate_fingerprint(
    *,
    scope: ProviderInputContinuityScope,
    epoch_nonce: str,
    expected_epoch_revision: int,
    predecessor_prefix_fingerprint: str | None,
    dispatch_anchor: ProviderInputDispatchAnchor,
    resulting_compiled_input: FrozenCompiledModelInput,
    wire_input_plan: FrozenProviderWireInputPlan,
    resulting_canonical_frontier: ProcessLocalCanonicalFrontier,
    resulting_source_heads: tuple[ProcessLocalSourceHead, ...],
    appended_message_count: int,
    reset_reason: ProviderInputEpochResetReason | None,
    compatibility: ProviderInputEpochCompatibility,
    planning_fingerprint: str,
) -> str:
    return context_fingerprint(
        "pulsara:prepared-provider-input-append:v2-wire-proof",
        {
            "scope": (
                scope.session_id,
                scope.scope_kind.value,
                scope.scope_subagent_task_id,
            ),
            "epoch_nonce": epoch_nonce,
            "expected_revision": expected_epoch_revision,
            "predecessor": predecessor_prefix_fingerprint,
            "anchor": provider_input_dispatch_anchor_value(dispatch_anchor),
            "compiled": resulting_compiled_input.compiled_semantic_fingerprint,
            "wire_plan": wire_input_plan.plan_fingerprint,
            "wire_quote": wire_input_plan.quote.quote_fingerprint,
            "frontier": {
                "binding_revision": (
                    resulting_canonical_frontier.latest_context_binding_revision_id
                ),
                "context_base": (
                    resulting_canonical_frontier.context_base_semantic_identity
                ),
                "through": resulting_canonical_frontier.through_sequence,
                "items": resulting_canonical_frontier.ordered_item_fingerprints,
            },
            "source_heads": tuple(
                {
                    "source": item.source_kind.value,
                    "presence": item.presence.value,
                    "semantic": item.semantic_fingerprint,
                    "observation": item.installed_observation_fingerprint,
                    "turn": item.last_emitted_turn_id,
                    "call": item.last_emitted_model_call_index,
                }
                for item in resulting_source_heads
            ),
            "appended": appended_message_count,
            "reset": None if reset_reason is None else reset_reason.value,
            "compatibility": {
                "compiler": compatibility.compiler_contract_version,
                "base": compatibility.base_system_semantic_fingerprint,
                "tools": compatibility.tool_surface_fingerprint,
                "target": compatibility.model_target_fingerprint,
                "estimator": compatibility.estimator_fingerprint,
                "lowering": compatibility.provider_message_lowering_contract,
                "context_base": compatibility.context_base_semantic_identity,
                "replay_contract": (
                    compatibility.provider_assistant_replay_contract_fingerprint
                ),
            },
            "planning": planning_fingerprint,
        },
    )


__all__ = [
    "FULL_HISTORY_CONTEXT_BASE_IDENTITY",
    "FrozenProviderInputAppendPlanningInput",
    "FrozenProviderInputAppendCompileResult",
    "FrozenProviderInputEpochView",
    "MAXIMUM_PROVIDER_INPUT_EPOCH_BYTES",
    "NewTriggerAnchor",
    "NoNewTriggerAnchor",
    "PROVIDER_MESSAGE_LOWERING_CONTRACT",
    "ProviderRuntimeObservation",
    "PreparedProviderInputAppendCandidate",
    "ProcessLocalCanonicalFrontier",
    "ProcessLocalProviderInputInstallPermit",
    "ProcessLocalSourceHead",
    "ProviderInputAdmissionPredecessorKind",
    "ProviderInputContinuityScope",
    "ProviderInputDispatchAnchor",
    "ProviderInputEpochCompatibility",
    "ProviderInputEpochResetReason",
    "RuntimeObservation",
    "SourceObservationLifecycle",
    "SourceObservationPresence",
    "decode_runtime_observation",
    "encode_runtime_observation",
    "provider_input_logical_utf8_bytes",
    "provider_input_append_planning_fingerprint",
    "provider_input_dispatch_anchor_value",
    "provider_input_prefix_fingerprint",
    "prepared_provider_input_append_candidate_fingerprint",
]
