"""One-shot primary-model execution for a Round 5B semantic handoff."""

from __future__ import annotations

from pulsara_agent.identity import derive_id, random_id

import asyncio
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Literal
from threading import Lock

from pulsara_agent.capability.contracts import FrozenNativeToolProjectionSet
from pulsara_agent.conversation_kernel.assembler import (
    CompletedDataBlock,
    CompletedTextBlock,
    CompletedToolCallBlock,
    ProviderStreamAssembler,
)
from pulsara_agent.conversation_kernel.compaction.contracts import (
    FrozenCompactionSourceView,
    CompactionSourceCompatibility,
    ProviderPrefixCutProof,
    compaction_summary_message_prefix_fingerprint,
)
from pulsara_agent.conversation_kernel.compaction.planner import (
    DestinationDialogueProjection,
)
from pulsara_agent.conversation_kernel.compaction.prompt import (
    summary_request_fingerprint,
)
from pulsara_agent.conversation_kernel.provider_dispatch import (
    PreparedWireMeasurementDecision,
)
from pulsara_agent.conversation_kernel.direct_model import (
    provider_wire_profile_fingerprint,
)
from pulsara_agent.llm.input import (
    LLMMessage,
    LLMToolCall,
    MessageRole,
    ToolSpec,
    llm_content_identity_value,
)
from pulsara_agent.llm.request import (
    MAXIMUM_PROVIDER_WIRE_INPUT_BYTES,
    FrozenProviderWireInputPlan,
    LLMContext,
    provider_wire_input_plan_identity_fingerprint,
)
from pulsara_agent.llm.resolution import ResolvedModelCall
from pulsara_agent.llm.frozen_target import (
    FrozenEpochModelCallTarget,
    _freeze_provider_physical_call_target,
)
from pulsara_agent.llm.provider_open import (
    CompactionSummaryPromotionAuthority,
    CompactionSummaryProviderOpenPermit,
    _issue_compaction_summary_provider_open_permit,
)
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.llm.adapters.openai.client import OpenAITransportTimeoutPolicy
from pulsara_agent.llm.validation import validate_model_context_shape_for_call
from pulsara_agent.model_input.contracts import (
    CanonicalModelInputIdentity,
    FrozenCompiledMessagePlacement,
    FrozenModelInputSemanticProjection,
    FrozenToolSpec,
    ModelInputCompileBinding,
    compiled_message_placements_fingerprint,
    provider_input_item_fingerprint,
    FrozenProviderInputItemKind,
)
from pulsara_agent.model_input.continuity import decode_runtime_observation
from pulsara_agent.model_input.provider_replay import (
    FrozenCanonicalProviderDispatchRead,
)
from pulsara_agent.ports.provider_stream import (
    ProviderModelExecutionFailed,
    ProviderModelOutputIncomplete,
    ProviderNormalizedTerminalKind,
    ProviderPhysicalCompletionStatus,
    ProviderStreamTerminal,
)
from pulsara_agent.primitives.context import (
    canonical_json_bytes,
    context_fingerprint,
    thaw_json,
)
from pulsara_agent.primitives.model_call import ModelCallPurpose


class _SummaryCallState(StrEnum):
    PREPARED = "PREPARED"
    OPENING = "OPENING"
    CLOSED = "CLOSED"
    DISCARDED = "DISCARDED"


@dataclass(frozen=True, slots=True)
class RawCompactionSummaryResponse:
    text: str
    tool_calls: tuple[LLMToolCall, ...]


@dataclass(frozen=True, slots=True)
class PreparedCompactionSummarySemanticInput:
    """Ephemeral structural input; semantic over-budget is intentionally legal."""

    context_id: str
    canonical_input_identity: CanonicalModelInputIdentity = field(repr=False)
    system_prompt: str = field(repr=False)
    messages: tuple[LLMMessage, ...] = field(repr=False)
    message_placements: tuple[FrozenCompiledMessagePlacement, ...] = field(repr=False)
    tools: tuple[FrozenToolSpec, ...] = field(repr=False)
    compile_binding_fingerprint: str
    compiled_semantic_fingerprint: str

    def __post_init__(self) -> None:
        if not self.context_id or len(self.message_placements) != len(self.messages):
            raise ValueError("summary semantic input shape is invalid")
        if tuple(item.message_ordinal for item in self.message_placements) != tuple(
            range(len(self.messages))
        ):
            raise ValueError("summary semantic placement order is invalid")
        if any(
            placement.role is not message.role
            for placement, message in zip(
                self.message_placements, self.messages, strict=True
            )
        ):
            raise ValueError("summary semantic placement role drifted")
        for value in (
            self.compile_binding_fingerprint,
            self.compiled_semantic_fingerprint,
        ):
            if not value.startswith("sha256:"):
                raise ValueError("summary semantic fingerprint is invalid")


class _CompactionSummarySourceSeal:
    pass


_COMPACTION_SUMMARY_SOURCE_SEAL = _CompactionSummarySourceSeal()


@dataclass(frozen=True, slots=True)
class CompactionSummarySourceProof:
    """Frozen auxiliary read membership, separate from the adopted replacement cut."""

    source_view: FrozenCompactionSourceView = field(repr=False)
    source_projection: FrozenModelInputSemanticProjection = field(repr=False)
    prefix_proof: ProviderPrefixCutProof
    read_through_sequence: int
    retained_entry_ids: tuple[str, ...]
    source_message_indices: tuple[int, ...]
    reference_message_indices: tuple[int, ...]
    input_mode: Literal["APPEND", "INDEPENDENT", "PROJECTED"]
    projection: DestinationDialogueProjection | None = field(repr=False)
    _seal: _CompactionSummarySourceSeal = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        source = self.source_projection
        canonical = (
            self.source_view.canonical_dispatch_read.compile_snapshot.canonical_input
        )
        if (
            self._seal is not _COMPACTION_SUMMARY_SOURCE_SEAL
            or source.canonical_input_identity != canonical.identity
            or source.messages != self.source_view.materialized_messages()
            or source.system_prompt != self.source_view.materialized_system_prompt()
            or self.prefix_proof.source_view_fingerprint
            != self.source_view.source_view_fingerprint
            or not self.prefix_proof.source_through_sequence
            <= self.read_through_sequence
            <= canonical.identity.provider_input_through_sequence
            or tuple(sorted(set(self.source_message_indices)))
            != self.source_message_indices
            or any(
                not 0 <= index < len(source.messages)
                for index in self.source_message_indices
            )
            or not set(self.reference_message_indices)
            <= set(self.source_message_indices)
            or (self.input_mode == "PROJECTED") != (self.projection is not None)
        ):
            raise ValueError("compaction summary read does not join its frozen source")
        if self.input_mode == "APPEND":
            predecessor = self.source_view.predecessor_epoch_view
            selected = tuple(
                source.messages[index] for index in self.source_message_indices
            )
            if predecessor is None or (
                source.system_prompt != predecessor.system_prompt
                or source.tools != predecessor.tools
                or selected[: len(predecessor.messages)] != predecessor.messages
            ):
                raise ValueError(
                    "append summary does not retain the installed provider prefix"
                )


def _summary_source_messages(
    proof: CompactionSummarySourceProof,
) -> tuple[tuple[LLMMessage, ...], tuple[FrozenCompiledMessagePlacement, ...]]:
    source = proof.source_projection
    selected = proof.source_message_indices
    if proof.projection is None:
        pairs = [
            (source.messages[index], source.message_placements[index])
            for index in selected
        ]
    else:
        observations = []
        for index in selected:
            message = source.messages[index]
            if source.message_placements[index].origin_entry_id is not None:
                continue
            try:
                decode_runtime_observation(message)
            except ValueError:
                continue
            observations.append((message, source.message_placements[index]))
        observation_indices = {
            placement.message_ordinal for _, placement in observations
        }
        references = [
            (source.messages[index], source.message_placements[index])
            for index in proof.reference_message_indices
            if index not in observation_indices
        ]
        projection_message = LLMMessage(
            role=MessageRole.USER, content=proof.projection.content
        )
        pairs = [
            *observations,
            (
                projection_message,
                _ephemeral_summary_placement(
                    message_ordinal=len(observations),
                    role=MessageRole.USER,
                    domain="history-projection",
                    identity={
                        "source": proof.source_view.source_view_fingerprint,
                        "content": llm_content_identity_value(proof.projection.content),
                    },
                ),
            ),
            *references,
        ]
    return (
        tuple(message for message, _ in pairs),
        tuple(
            replace(placement, message_ordinal=index)
            for index, (_, placement) in enumerate(pairs)
        ),
    )


@dataclass(frozen=True, slots=True)
class PreparedCompactionSummarySemantic:
    call: ResolvedModelCall = field(repr=False)
    source_proof: CompactionSummarySourceProof = field(repr=False)
    compile_binding: ModelInputCompileBinding = field(repr=False)
    native_projection_set: FrozenNativeToolProjectionSet = field(repr=False)
    summary_request: str = field(repr=False)
    semantic_input: PreparedCompactionSummarySemanticInput = field(repr=False)
    semantic_fingerprint: str

    def __post_init__(self) -> None:
        compiled = self.semantic_input
        binding = self.compile_binding
        source = self.source_proof.source_projection
        messages, placements = _summary_source_messages(self.source_proof)
        request_placement = _synthetic_summary_placement(
            message_ordinal=len(messages),
            proof=self.source_proof.prefix_proof,
            summary_request=self.summary_request,
        )
        expected_messages = (*messages, LLMMessage.user(self.summary_request))
        expected_placements = (*placements, request_placement)
        if (
            self.call.fact.purpose is not ModelCallPurpose.CONTEXT_COMPACTION_SUMMARY
            or self.call.target.fact != binding.target_fact
            or compiled.compile_binding_fingerprint != binding.binding_fingerprint
            or compiled.tools != binding.tool_surface.tool_specs
            or source.tools != compiled.tools
            or compiled.system_prompt != source.system_prompt
            or compiled.canonical_input_identity != source.canonical_input_identity
            or not self.summary_request
            or compiled.messages[: len(expected_messages)] != expected_messages
            or compiled.message_placements[: len(expected_placements)]
            != expected_placements
            or not self.semantic_fingerprint.startswith("sha256:")
        ):
            raise ValueError("summary semantic carrier does not exact-join")

    @property
    def canonical_read(self) -> FrozenCanonicalProviderDispatchRead:
        return self.source_proof.source_view.canonical_dispatch_read

    @property
    def tool_choice(self) -> str:
        return "auto"


class PreparedCompactionSummaryCall:
    """Sealed one-shot transport authority with no tool/runtime authority."""

    def __init__(
        self,
        *,
        semantic: PreparedCompactionSummarySemantic,
        wire_input_plan: FrozenProviderWireInputPlan,
        model_runtime: ModelRuntime,
        purpose_permit: CompactionSummaryProviderOpenPermit,
    ) -> None:
        call = semantic.call
        compiled = semantic.semantic_input
        if (
            call.fact.purpose is not ModelCallPurpose.CONTEXT_COMPACTION_SUMMARY
            or wire_input_plan.compiled_semantic_fingerprint
            != compiled.compiled_semantic_fingerprint
            or wire_input_plan.message_placements_fingerprint
            != compiled_message_placements_fingerprint(compiled.message_placements)
            or wire_input_plan.materialization.tool_items
            != tuple(
                item.wire_tool for item in semantic.native_projection_set.projections
            )
        ):
            raise ValueError("prepared summary call does not exact-join")
        tools = tuple(_thaw_tool(item) for item in compiled.tools)
        context = LLMContext(
            messages=compiled.messages,
            context_id=compiled.context_id,
            resolved_model_call_id=call.resolved_model_call_id,
            model_call_index=compiled.canonical_input_identity.provider_input_through_sequence
            + 1,
            tools=tools,
            system_prompt=compiled.system_prompt,
            provider_wire_input_plan=wire_input_plan,
            tool_choice="auto",
        )
        validate_model_context_shape_for_call(call=call, context=context)
        self._semantic = semantic
        self._wire_input_plan = wire_input_plan
        self._context = context
        self._model_runtime = model_runtime
        self._purpose_permit = purpose_permit
        self._state = _SummaryCallState.PREPARED
        self._lock = Lock()
        self.request_fingerprint = context_fingerprint(
            "pulsara.prepared-compaction-summary-call.v1",
            {
                "purpose": ModelCallPurpose.CONTEXT_COMPACTION_SUMMARY.value,
                "tool_choice": "auto",
                "call": call.fact,
                "source": _summary_source_identity_value(semantic.source_proof),
                "semantic": semantic.semantic_fingerprint,
                "wire": provider_wire_input_plan_identity_fingerprint(
                    wire_input_plan,
                    target_fact=call.target.fact,
                ),
            },
        )

    @property
    def semantic(self) -> PreparedCompactionSummarySemantic:
        return self._semantic

    @property
    def wire_input_plan(self) -> FrozenProviderWireInputPlan:
        return self._wire_input_plan

    async def open_once(self) -> RawCompactionSummaryResponse:
        with self._lock:
            if self._state is not _SummaryCallState.PREPARED:
                raise RuntimeError("compaction summary call is not openable")
            self._state = _SummaryCallState.OPENING
        prepared_call = self._semantic.call
        borrowed = self._model_runtime.borrow_transport(self._purpose_permit)
        if borrowed.call.fact != prepared_call.fact:
            borrowed.close()
            raise RuntimeError("borrowed compaction summary call drifted")
        execution = borrowed.open_stream(context=self._context)
        assembler = ProviderStreamAssembler(
            session_id=(
                self._semantic.canonical_read.compile_snapshot.canonical_input.identity.session_id
            ),
            turn_id=(
                self._semantic.canonical_read.compile_snapshot.canonical_input.identity.turn_id
            ),
            live_bus=_NullLiveBus(),  # type: ignore[arg-type]
            proposed_entry_id=derive_id(
                "entry", "compaction-summary", self.request_fingerprint
            ),
            conversation_scope_kind=(
                self._semantic.canonical_read.compile_snapshot.canonical_input.identity.conversation_scope_kind.value
            ),
        )
        terminal: ProviderStreamTerminal | None = None
        response: RawCompactionSummaryResponse | None = None
        try:
            while True:
                item = await execution.read_next()
                if item is None:
                    break
                if isinstance(item, ProviderStreamTerminal):
                    terminal = item
                    break
                assembler.apply(item)
            if terminal is None:
                raise RuntimeError("summary stream lacks an explicit terminal")
            if (
                terminal.terminal_kind
                is ProviderNormalizedTerminalKind.OUTPUT_INCOMPLETE
            ):
                assert terminal.incomplete_reason is not None
                raise ProviderModelOutputIncomplete(terminal.incomplete_reason)
            if terminal.terminal_kind is ProviderNormalizedTerminalKind.PROVIDER_ERROR:
                assert terminal.error is not None
                raise ProviderModelExecutionFailed(terminal.error)
            completed = assembler.complete()
            if any(isinstance(item, CompletedDataBlock) for item in completed.blocks):
                raise RuntimeError("summary response contains non-text data")
            tool_calls = tuple(
                LLMToolCall(
                    id=item.tool_call_id,
                    name=item.tool_name,
                    arguments=canonical_json_bytes(thaw_json(item.arguments)).decode(
                        "utf-8"
                    ),
                )
                for item in completed.blocks
                if isinstance(item, CompletedToolCallBlock)
            )
            text = "".join(
                item.text
                for item in completed.blocks
                if isinstance(item, CompletedTextBlock)
            )
            response = RawCompactionSummaryResponse(text=text, tool_calls=tool_calls)
        except BaseException:
            with self._lock:
                self._state = _SummaryCallState.DISCARDED
            raise
        finally:
            try:
                drain = asyncio.create_task(
                    _drain_summary_execution(execution),
                    name=f"kernel-compaction-summary-drain:{self.request_fingerprint[7:23]}",
                )
                try:
                    completion = await asyncio.shield(drain)
                except asyncio.CancelledError:
                    completion = await drain
                    raise
                if completion.status is not ProviderPhysicalCompletionStatus.COMPLETED:
                    with self._lock:
                        self._state = _SummaryCallState.DISCARDED
                    raise RuntimeError(
                        "summary provider physical operation did not exit"
                    )
            finally:
                borrowed.close()
        assert response is not None
        with self._lock:
            self._state = _SummaryCallState.CLOSED
        return response

    def discard(self) -> None:
        with self._lock:
            if self._state is _SummaryCallState.PREPARED:
                self._state = _SummaryCallState.DISCARDED
            elif self._state is not _SummaryCallState.DISCARDED:
                raise RuntimeError("started summary call cannot be discarded")


def prepare_compaction_summary_semantic(
    *,
    call: ResolvedModelCall,
    source_view: FrozenCompactionSourceView,
    source_projection: FrozenModelInputSemanticProjection,
    prefix_proof: ProviderPrefixCutProof,
    native_projection_set: FrozenNativeToolProjectionSet,
    summary_request: str,
    read_through_sequence: int,
    retained_entry_ids: tuple[str, ...],
    projection: DestinationDialogueProjection | None = None,
) -> PreparedCompactionSummarySemantic:
    """Read H+R (or P(H)+raw R), replacing only the independently proven H cut."""

    if call.target.fact != source_view.normal_compile_binding.target_fact:
        raise ValueError("summary target changed from its frozen source")
    canonical = source_view.canonical_dispatch_read.compile_snapshot.canonical_input
    by_entry = {
        item.source_entry_id: item
        for item in canonical.items
        if item.source_entry_id is not None
    }
    retained = frozenset(retained_entry_ids)
    snapshot_sequences = {
        provider_input_item_fingerprint(item): item.source_entry_sequence
        for item in canonical.items
        if item.item_kind is FrozenProviderInputItemKind.CONTEXT_SNAPSHOT
    }
    selected = []
    references = []
    for index, placement in enumerate(source_projection.message_placements):
        attachment = placement.tool_attachment_source
        if attachment is not None:
            sequences = tuple(
                member.source.source_entry_sequence for member in attachment.members
            )
            if any(
                sequence is None or sequence > read_through_sequence
                for sequence in sequences
            ):
                continue
            is_reference = any(
                sequence > prefix_proof.source_through_sequence
                for sequence in sequences
            )
        elif placement.origin_entry_id is not None:
            item = by_entry.get(placement.origin_entry_id)
            if (
                item is None
                or item.source_entry_sequence is None
                or item.source_entry_sequence > read_through_sequence
            ):
                continue
            is_reference = (
                item.source_entry_id in retained
                or item.source_entry_sequence > prefix_proof.source_through_sequence
            )
        else:
            sequence = snapshot_sequences.get(placement.origin_item_fingerprint)
            if sequence is not None and sequence > read_through_sequence:
                continue
            # Runtime observations describe current owners, never historical task execution.
            is_reference = sequence is None
        selected.append(index)
        if is_reference:
            references.append(index)
    selected_indices = tuple(selected)
    predecessor = source_view.predecessor_epoch_view
    selected_messages = tuple(
        source_projection.messages[index] for index in selected_indices
    )
    mode = "PROJECTED" if projection is not None else "INDEPENDENT"
    if (
        projection is None
        and predecessor is not None
        and (
            source_view.compatibility is CompactionSourceCompatibility.COMPATIBLE_APPEND
            and predecessor.wire_input_plan.wire_api
            == call.target.model_profile.route_wire_profile.wire_api
            and predecessor.wire_input_plan.route_wire_profile_fingerprint
            == provider_wire_profile_fingerprint(call)
            and source_projection.system_prompt == predecessor.system_prompt
            and source_projection.tools == predecessor.tools
            and selected_messages[: len(predecessor.messages)] == predecessor.messages
        )
    ):
        mode = "APPEND"
    proof = CompactionSummarySourceProof(
        source_view=source_view,
        source_projection=source_projection,
        prefix_proof=prefix_proof,
        read_through_sequence=read_through_sequence,
        retained_entry_ids=retained_entry_ids,
        source_message_indices=selected_indices,
        reference_message_indices=tuple(references),
        input_mode=mode,
        projection=projection,
        _seal=_COMPACTION_SUMMARY_SOURCE_SEAL,
    )
    messages, placements = _summary_source_messages(proof)
    if projection is None:
        source_to_request = {
            source_index: index for index, source_index in enumerate(selected_indices)
        }
        reference_ordinals = tuple(source_to_request[index] for index in references)
        history_ordinals = tuple(
            index for index in range(len(messages)) if index not in reference_ordinals
        )
        scope = (
            "Summary scope: conversation message ordinals start at zero. "
            f"Summarize only historical message ordinals {history_ordinals}. "
            f"Message ordinals {reference_ordinals} are exact retained reference material. "
            "Use their corrections to understand history, without replacing their original messages."
        )
    else:
        scope = (
            "Summary scope: summarize only the Runtime-authored historical projection. "
            "Other messages are exact retained references or current observations. "
            "Use reference corrections to understand history; they remain available separately."
        )
    summary_request = scope + "\n\n" + summary_request
    request_placement = _synthetic_summary_placement(
        message_ordinal=len(messages),
        proof=prefix_proof,
        summary_request=summary_request,
    )
    messages = (*messages, LLMMessage.user(summary_request))
    placements = (*placements, request_placement)
    binding = source_view.normal_compile_binding
    context_id = random_id("context")
    values = dict(
        context_id=context_id,
        canonical_input_identity=source_projection.canonical_input_identity,
        system_prompt=source_projection.system_prompt,
        messages=messages,
        message_placements=placements,
        tools=source_projection.tools,
        compile_binding_fingerprint=binding.binding_fingerprint,
    )
    compiled = PreparedCompactionSummarySemanticInput(
        **values,
        compiled_semantic_fingerprint=context_fingerprint(
            "pulsara.compaction-summary-semantic-input.v2",
            {
                "context": context_id,
                "canonical": source_projection.canonical_input_identity.identity_fingerprint,
                "messages": compaction_summary_message_prefix_fingerprint(messages),
                "placements": compiled_message_placements_fingerprint(placements),
                "binding": binding.binding_fingerprint,
            },
        ),
    )
    return PreparedCompactionSummarySemantic(
        call=call,
        source_proof=proof,
        compile_binding=binding,
        native_projection_set=native_projection_set,
        summary_request=summary_request,
        semantic_input=compiled,
        semantic_fingerprint=context_fingerprint(
            "pulsara.prepared-compaction-summary-semantic.v2",
            {
                "call": call.fact,
                "source": _summary_source_identity_value(proof),
                "native": native_projection_set.projection_set_fingerprint,
                "compiled": compiled.compiled_semantic_fingerprint,
            },
        ),
    )


def promote_compaction_summary_call(
    semantic: PreparedCompactionSummarySemantic,
    *,
    decision: PreparedWireMeasurementDecision,
    predecessor_summary_wire_plan: FrozenProviderWireInputPlan | None = None,
    model_runtime: ModelRuntime,
    timeout_policy: OpenAITransportTimeoutPolicy,
    successor_destination: FrozenEpochModelCallTarget,
    promotion_authority: CompactionSummaryPromotionAuthority,
) -> PreparedCompactionSummaryCall:
    if decision.candidate is not semantic:
        raise ValueError("summary wire decision belongs to another candidate")
    plan = decision.wire_input_plan
    if plan is None:
        raise ValueError("summary wire decision is not executable")
    if (
        plan.quote is not decision.quote
        or plan.wire_api
        != semantic.call.target.model_profile.route_wire_profile.wire_api
        or plan.route_wire_profile_fingerprint
        != provider_wire_profile_fingerprint(semantic.call)
        or decision.quote.estimator_fingerprint
        != semantic.compile_binding.estimator_fingerprint
        or decision.quote.effective_input_budget_tokens
        != semantic.compile_binding.effective_input_budget_tokens
        or decision.quote.budget_input_tokens
        > decision.quote.effective_input_budget_tokens
        or decision.quote.final_wire_utf8_bytes > MAXIMUM_PROVIDER_WIRE_INPUT_BYTES
    ):
        raise ValueError("summary wire decision failed hard admission")
    if predecessor_summary_wire_plan is None:
        _require_summary_wire_prefix(semantic, plan)
    else:
        old = predecessor_summary_wire_plan.materialization
        new = plan.materialization
        if (
            old.root_policy_value != new.root_policy_value
            or old.tool_items != new.tool_items
            or new.ordered_input_items[: len(old.ordered_input_items)]
            != old.ordered_input_items
            or len(new.ordered_input_items) <= len(old.ordered_input_items)
        ):
            raise ValueError("summary repair rewrote its first request")
    call_target = _freeze_provider_physical_call_target(
        target=semantic.call.target,
        call=semantic.call,
        maximum_input_tokens=semantic.compile_binding.effective_input_budget_tokens,
    )
    permit = _issue_compaction_summary_provider_open_permit(
        summary_call_target=call_target,
        successor_destination=successor_destination,
        resolved_model_call_id=semantic.call.resolved_model_call_id,
        timeout_policy=timeout_policy,
        promotion_authority=promotion_authority,
        semantic=semantic,
        decision=decision,
    )
    return PreparedCompactionSummaryCall(
        semantic=semantic,
        wire_input_plan=plan,
        model_runtime=model_runtime,
        purpose_permit=permit,
    )


_SUMMARY_TOOL_DENIAL = (
    "The requested tool was not executed and returned no information. Do not "
    "retry it or call any other tool. For this response, the current checkpoint "
    "request supersedes earlier requests to perform task work. Return the requested "
    "semantic handoff now as plain text and preserve the actual task status. The "
    "fact that this summary-only call did not execute a tool is not evidence that "
    "the user's task is queued, deferred, blocked, or waiting for a later turn."
)


def prepare_compaction_summary_repair_semantic(
    initial: PreparedCompactionSummarySemantic,
    *,
    tool_calls: tuple[LLMToolCall, ...],
) -> PreparedCompactionSummarySemantic:
    """Append one provider-valid ephemeral denial group to the same request."""

    if not tool_calls:
        raise ValueError("summary repair requires at least one denied tool call")
    compiled = initial.semantic_input
    suffix = (
        LLMMessage.assistant_turn(tool_calls=tool_calls),
        *tuple(
            LLMMessage.tool_result(_SUMMARY_TOOL_DENIAL, tool_call_id=item.id)
            for item in tool_calls
        ),
    )
    messages = compiled.messages + suffix
    placements = list(compiled.message_placements)
    tool_calls_fingerprint = context_fingerprint(
        "pulsara.compaction-summary-repair-tool-calls.v1",
        tuple((item.id, item.name, item.arguments) for item in tool_calls),
    )
    for message in suffix:
        ordinal = len(placements)
        placements.append(
            _ephemeral_summary_placement(
                message_ordinal=ordinal,
                role=message.role,
                domain="repair",
                identity={
                    "initial": initial.semantic_fingerprint,
                    "message": compaction_summary_message_prefix_fingerprint(
                        (message,)
                    ),
                    "ordinal": ordinal,
                },
            )
        )
    frozen_placements = tuple(placements)
    values = {
        "context_id": random_id("context"),
        "canonical_input_identity": compiled.canonical_input_identity,
        "system_prompt": compiled.system_prompt,
        "messages": messages,
        "message_placements": frozen_placements,
        "tools": compiled.tools,
        "compile_binding_fingerprint": compiled.compile_binding_fingerprint,
    }
    repaired = PreparedCompactionSummarySemanticInput(
        **values,
        compiled_semantic_fingerprint=context_fingerprint(
            "pulsara.compaction-summary-repair-semantic-input.v1",
            {
                "context": values["context_id"],
                "canonical": compiled.canonical_input_identity.identity_fingerprint,
                "messages": compaction_summary_message_prefix_fingerprint(messages),
                "placements": compiled_message_placements_fingerprint(
                    frozen_placements
                ),
                "binding": compiled.compile_binding_fingerprint,
            },
        ),
    )
    semantic_fingerprint = context_fingerprint(
        "pulsara.prepared-compaction-summary-repair-semantic.v1",
        {
            "initial": initial.semantic_fingerprint,
            "compiled": repaired.compiled_semantic_fingerprint,
            "tool_calls": tool_calls_fingerprint,
        },
    )
    return PreparedCompactionSummarySemantic(
        call=initial.call,
        source_proof=initial.source_proof,
        compile_binding=initial.compile_binding,
        native_projection_set=initial.native_projection_set,
        summary_request=initial.summary_request,
        semantic_input=repaired,
        semantic_fingerprint=semantic_fingerprint,
    )


def _require_summary_wire_prefix(
    semantic: PreparedCompactionSummarySemantic,
    plan: FrozenProviderWireInputPlan,
) -> None:
    proof = semantic.source_proof
    if proof.input_mode != "APPEND":
        return
    predecessor = proof.source_view.predecessor_epoch_view
    if predecessor is None:
        return
    old = predecessor.wire_input_plan.materialization
    new = plan.materialization
    if (
        old.root_policy_value != new.root_policy_value
        or old.tool_items != new.tool_items
        or not new.ordered_input_items
    ):
        raise ValueError("summary wire root or tools changed within an epoch")
    # The synthetic request lowers to one final user item on both supported APIs.
    actual_prefix = new.ordered_input_items[:-1]
    old_items = old.ordered_input_items
    if (
        len(actual_prefix) < len(old_items)
        or actual_prefix[: len(old_items)] != old_items
    ):
        raise ValueError("summary actual wire input rewrote the installed prefix")


def _summary_source_identity_value(proof: CompactionSummarySourceProof) -> object:
    return {
        "mode": proof.input_mode,
        "source": proof.source_view.source_view_fingerprint,
        "replacement": proof.prefix_proof.proof_fingerprint,
        "read_through_sequence": proof.read_through_sequence,
        "retained_entry_ids": proof.retained_entry_ids,
        "read_messages": proof.source_message_indices,
        "reference_messages": proof.reference_message_indices,
        "projection": None
        if proof.projection is None
        else llm_content_identity_value(proof.projection.content),
    }


def _synthetic_summary_placement(
    *,
    message_ordinal: int,
    proof: ProviderPrefixCutProof,
    summary_request: str,
) -> FrozenCompiledMessagePlacement:
    return _ephemeral_summary_placement(
        message_ordinal=message_ordinal,
        role=MessageRole.USER,
        domain="request",
        identity={
            "prompt": summary_request_fingerprint(summary_request),
            "text": summary_request,
            "prefix": proof.proof_fingerprint,
        },
    )


def _ephemeral_summary_placement(
    *,
    message_ordinal: int,
    role: MessageRole,
    domain: str,
    identity: object,
) -> FrozenCompiledMessagePlacement:
    origin = context_fingerprint(
        f"pulsara.compaction-summary-ephemeral-{domain}-item.v1",
        identity,
    )
    return FrozenCompiledMessagePlacement(
        message_ordinal=message_ordinal,
        origin_entry_id=None,
        origin_item_fingerprint=origin,
        within_origin_ordinal=0,
        role=role,
    )


def _thaw_tool(item: object) -> ToolSpec:
    parameters = thaw_json(item.parameters)  # type: ignore[attr-defined]
    if not isinstance(parameters, dict):
        raise TypeError("summary tool schema is not an object")
    return ToolSpec(
        item.name,  # type: ignore[attr-defined]
        item.description,  # type: ignore[attr-defined]
        parameters,
    )


async def _drain_summary_execution(execution: object):
    await execution.aclose()  # type: ignore[attr-defined]
    return await execution.wait_physical_completion()  # type: ignore[attr-defined]


class _NullLiveBus:
    def offer_nowait(self, **values: object) -> None:
        del values


__all__ = [
    "CompactionSummarySourceProof",
    "PreparedCompactionSummaryCall",
    "PreparedCompactionSummarySemantic",
    "PreparedCompactionSummarySemanticInput",
    "RawCompactionSummaryResponse",
    "promote_compaction_summary_call",
    "prepare_compaction_summary_repair_semantic",
    "prepare_compaction_summary_semantic",
]
