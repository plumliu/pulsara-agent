"""One-shot primary-model execution for a Round 5B semantic handoff."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from enum import StrEnum
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
    ProviderPrefixCutProof,
    compaction_summary_message_prefix_fingerprint,
)
from pulsara_agent.conversation_kernel.compaction.prompt import (
    SUMMARY_REQUEST,
    summary_request_fingerprint,
)
from pulsara_agent.conversation_kernel.direct_model import DirectKernelModelPort
from pulsara_agent.llm.input import LLMMessage, LLMToolCall, MessageRole, ToolSpec
from pulsara_agent.llm.estimator import TokenEstimate
from pulsara_agent.llm.request import FrozenProviderWireInputPlan, LLMContext
from pulsara_agent.llm.resolution import ResolvedModelCall
from pulsara_agent.llm.validation import validate_model_context_for_call
from pulsara_agent.model_input.compiler import COMPILER_CONTRACT_VERSION
from pulsara_agent.model_input.continuity import provider_input_logical_utf8_bytes
from pulsara_agent.model_input.contracts import (
    ContextCompileBudgetReport,
    FrozenCompiledMessagePlacement,
    FrozenCompiledModelInput,
    ModelInputCompileBinding,
    compiled_message_placements_fingerprint,
    frozen_compiled_model_input_fingerprint,
)
from pulsara_agent.model_input.provider_replay import (
    FrozenSelectedDurableProviderReplayHydration,
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
class PreparedCompactionSummarySemantic:
    call: ResolvedModelCall = field(repr=False)
    source_view: FrozenCompactionSourceView = field(repr=False)
    prefix_proof: ProviderPrefixCutProof
    compile_binding: ModelInputCompileBinding = field(repr=False)
    native_projection_set: FrozenNativeToolProjectionSet = field(repr=False)
    compiled_input: FrozenCompiledModelInput = field(repr=False)
    semantic_fingerprint: str


class PreparedCompactionSummaryCall:
    """Sealed one-shot transport authority with no tool/runtime authority."""

    def __init__(
        self,
        *,
        semantic: PreparedCompactionSummarySemantic,
        wire_input_plan: FrozenProviderWireInputPlan,
    ) -> None:
        call = semantic.call
        compiled = semantic.compiled_input
        if (
            call.fact.purpose is not ModelCallPurpose.CONTEXT_COMPACTION_SUMMARY
            or wire_input_plan.compiled_semantic_fingerprint
            != compiled.compiled_semantic_fingerprint
            or wire_input_plan.message_placements_fingerprint
            != compiled.message_placements_fingerprint
            or wire_input_plan.resolved_target_semantic_fingerprint
            != call.target.fact.target_fingerprint
            or wire_input_plan.materialization.tool_items
            != tuple(
                item.wire_tool
                for item in semantic.native_projection_set.projections
            )
        ):
            raise ValueError("prepared summary call does not exact-join")
        tools = tuple(_thaw_tool(item) for item in compiled.tools)
        context = LLMContext(
            messages=compiled.messages,
            context_id=compiled.context_id,
            resolved_model_call_id=call.resolved_model_call_id,
            target_fingerprint=call.target.fact.target_fingerprint,
            model_call_index=compiled.canonical_input_identity.provider_input_through_sequence
            + 1,
            tools=tools,
            system_prompt=compiled.system_prompt,
            compiler_estimated_input_tokens=(
                compiled.final_estimate.total_input_tokens
            ),
            provider_wire_input_plan=wire_input_plan,
            tool_choice_none=True,
        )
        validation = validate_model_context_for_call(call=call, context=context)
        if validation.estimate != compiled.final_estimate:
            raise ValueError("summary pre-send estimate differs from its compile")
        self._semantic = semantic
        self._wire_input_plan = wire_input_plan
        self._context = context
        self._state = _SummaryCallState.PREPARED
        self._lock = Lock()
        self.request_fingerprint = context_fingerprint(
            "pulsara.prepared-compaction-summary-call.v1",
            {
                "purpose": ModelCallPurpose.CONTEXT_COMPACTION_SUMMARY.value,
                "tool_choice": "none",
                "call": call.fact,
                "source_view": semantic.source_view.source_view_fingerprint,
                "prefix": semantic.prefix_proof.proof_fingerprint,
                "semantic": semantic.semantic_fingerprint,
                "wire": wire_input_plan.plan_fingerprint,
                "estimate": _estimate_value(compiled.final_estimate),
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
        call = self._semantic.call
        execution = call.target.transport.open_stream(
            call=call,
            context=self._context,
        )
        assembler = ProviderStreamAssembler(
            session_id=(
                self._semantic.source_view.canonical_dispatch_read.compile_snapshot.canonical_input.identity.session_id
            ),
            turn_id=(
                self._semantic.source_view.canonical_dispatch_read.compile_snapshot.canonical_input.identity.turn_id
            ),
            live_bus=_NullLiveBus(),  # type: ignore[arg-type]
            proposed_entry_id=f"compaction-summary:{self.request_fingerprint[7:39]}",
            conversation_scope_kind=(
                self._semantic.source_view.canonical_dispatch_read.compile_snapshot.canonical_input.identity.conversation_scope_kind.value
            ),
            scope_subagent_task_id=(
                self._semantic.source_view.canonical_dispatch_read.compile_snapshot.canonical_input.identity.scope_subagent_task_id
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
            if terminal.terminal_kind is ProviderNormalizedTerminalKind.OUTPUT_INCOMPLETE:
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
                    arguments=canonical_json_bytes(
                        thaw_json(item.arguments)
                    ).decode("utf-8"),
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
                raise RuntimeError("summary provider physical operation did not exit")
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
    source_compiled_input: FrozenCompiledModelInput,
    prefix_proof: ProviderPrefixCutProof,
    native_projection_set: FrozenNativeToolProjectionSet,
) -> PreparedCompactionSummarySemantic:
    if (
        call.fact.purpose is not ModelCallPurpose.CONTEXT_COMPACTION_SUMMARY
        or call.target.fact != source_view.normal_compile_binding.target_fact
        or source_compiled_input.system_prompt
        != source_view.materialized_system_prompt()
        or source_compiled_input.messages != source_view.materialized_messages()
        or source_compiled_input.tools
        != source_view.normal_compile_binding.tool_surface.tool_specs
        or prefix_proof.source_view_fingerprint
        != source_view.source_view_fingerprint
    ):
        raise ValueError("summary semantic inputs do not exact-join")
    count = prefix_proof.summary_prefix_message_count
    prefix = source_compiled_input.messages[:count]
    if compaction_summary_message_prefix_fingerprint(
        prefix
    ) != prefix_proof.summary_prefix_messages_fingerprint:
        raise ValueError("summary prefix proof changed")
    messages = prefix + (LLMMessage.user(SUMMARY_REQUEST),)
    placements = source_compiled_input.message_placements[:count] + (
        _synthetic_summary_placement(
            message_ordinal=count,
            proof=prefix_proof,
        ),
    )
    binding = source_view.normal_compile_binding
    estimate = binding.estimator.estimate_frozen_input(
        system_prompt=source_compiled_input.system_prompt,
        messages=messages,
        tools=source_compiled_input.tools,
    )
    if estimate.total_input_tokens > binding.effective_input_budget_tokens:
        raise ValueError("summary slice exceeds the resolved input budget")
    prefix_bytes = provider_input_logical_utf8_bytes(
        system_prompt="", tools=(), messages=prefix
    )
    report = ContextCompileBudgetReport(
        compiler_contract_version=COMPILER_CONTRACT_VERSION,
        estimator_fingerprint=binding.estimator_fingerprint,
        target_fingerprint=binding.target_fact.target_fingerprint,
        tool_surface_fingerprint=binding.tool_surface.surface_fingerprint,
        effective_input_budget_tokens=binding.effective_input_budget_tokens,
        system_tokens=estimate.system_tokens,
        message_tokens=estimate.message_tokens,
        tool_tokens=estimate.tool_tokens,
        envelope_tokens=estimate.envelope_tokens,
        total_input_tokens=estimate.total_input_tokens,
        protected_transcript_tokens=sum(
            estimate.message_tokens_by_index[:count]
        ),
        protected_prefix_message_count=count,
        protected_prefix_logical_utf8_bytes=prefix_bytes,
        protected_prefix_fingerprint=(
            prefix_proof.summary_prefix_messages_fingerprint
        ),
        context_source_tokens=source_compiled_input.budget_report.context_source_tokens,
        degraded_source_count=(
            source_compiled_input.budget_report.degraded_source_count
        ),
        omitted_source_count=(
            source_compiled_input.budget_report.omitted_source_count
        ),
        degraded_tool_result_count=(
            source_compiled_input.budget_report.degraded_tool_result_count
        ),
        omitted_tool_result_body_count=(
            source_compiled_input.budget_report.omitted_tool_result_body_count
        ),
        decision_digest=context_fingerprint(
            "pulsara.compaction-summary-compile-decisions.v1",
            {
                "source": source_compiled_input.compiled_semantic_fingerprint,
                "prefix": prefix_proof.proof_fingerprint,
                "prompt": summary_request_fingerprint(),
            },
        ),
    )
    context_id = context_fingerprint(
        "pulsara.compaction-summary-context-id.v1",
        {
            "call": call.resolved_model_call_id,
            "source": source_view.source_view_fingerprint,
            "prefix": prefix_proof.proof_fingerprint,
        },
    )
    placement_fingerprint = compiled_message_placements_fingerprint(placements)
    values = {
        "context_id": context_id,
        "canonical_input_identity": source_compiled_input.canonical_input_identity,
        "system_prompt": source_compiled_input.system_prompt,
        "messages": messages,
        "message_placements": placements,
        "message_placements_fingerprint": placement_fingerprint,
        "tools": source_compiled_input.tools,
        "final_estimate": estimate,
        "source_decisions": source_compiled_input.source_decisions,
        "tool_result_decisions": source_compiled_input.tool_result_decisions,
        "budget_report": report,
        "diagnostic_codes": source_compiled_input.diagnostic_codes,
        "source_collection_fingerprint": (
            source_compiled_input.source_collection_fingerprint
        ),
        "compile_binding_fingerprint": binding.binding_fingerprint,
    }
    compiled = FrozenCompiledModelInput(
        **values,
        compiled_semantic_fingerprint=frozen_compiled_model_input_fingerprint(
            **{
                key: value
                for key, value in values.items()
                if key
                not in {
                    "message_placements",
                    "message_placements_fingerprint",
                }
            }
        ),
    )
    semantic_fingerprint = context_fingerprint(
        "pulsara.prepared-compaction-summary-semantic.v1",
        {
            "call": call.fact,
            "source": source_view.source_view_fingerprint,
            "prefix": prefix_proof.proof_fingerprint,
            "native": native_projection_set.projection_set_fingerprint,
            "compiled": compiled.compiled_semantic_fingerprint,
        },
    )
    return PreparedCompactionSummarySemantic(
        call=call,
        source_view=source_view,
        prefix_proof=prefix_proof,
        compile_binding=binding,
        native_projection_set=native_projection_set,
        compiled_input=compiled,
        semantic_fingerprint=semantic_fingerprint,
    )


def finalize_compaction_summary_call(
    semantic: PreparedCompactionSummarySemantic,
    *,
    replay_hydration: FrozenSelectedDurableProviderReplayHydration | None,
    predecessor_summary_wire_plan: FrozenProviderWireInputPlan | None = None,
) -> PreparedCompactionSummaryCall:
    plan = DirectKernelModelPort.plan_compaction_wire_input(
        summary_call=semantic.call,
        compile_binding=semantic.compile_binding,
        native_projection_set=semantic.native_projection_set,
        compiled_input=semantic.compiled_input,
        predecessor_view=semantic.source_view.predecessor_epoch_view,
        replay_hydration=replay_hydration,
    )
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
    return PreparedCompactionSummaryCall(
        semantic=semantic,
        wire_input_plan=plan,
    )


_SUMMARY_TOOL_DENIAL = (
    "Context compaction cannot execute tools. Return the requested "
    "<summary> block directly without any tool calls."
)


def prepare_compaction_summary_repair_semantic(
    initial: PreparedCompactionSummarySemantic,
    *,
    tool_calls: tuple[LLMToolCall, ...],
) -> PreparedCompactionSummarySemantic:
    """Append one provider-valid ephemeral denial group to the same request."""

    if not tool_calls:
        raise ValueError("summary repair requires at least one denied tool call")
    compiled = initial.compiled_input
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
        tuple(
            (item.id, item.name, item.arguments)
            for item in tool_calls
        ),
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
    binding = initial.compile_binding
    estimate = binding.estimator.estimate_frozen_input(
        system_prompt=compiled.system_prompt,
        messages=messages,
        tools=compiled.tools,
    )
    if estimate.total_input_tokens > binding.effective_input_budget_tokens:
        raise ValueError("summary repair exceeds the resolved input budget")
    report = replace(
        compiled.budget_report,
        system_tokens=estimate.system_tokens,
        message_tokens=estimate.message_tokens,
        tool_tokens=estimate.tool_tokens,
        envelope_tokens=estimate.envelope_tokens,
        total_input_tokens=estimate.total_input_tokens,
        decision_digest=context_fingerprint(
            "pulsara.compaction-summary-repair-decisions.v1",
            {
                "initial": initial.semantic_fingerprint,
                "tool_calls": tool_calls_fingerprint,
            },
        ),
    )
    placement_fingerprint = compiled_message_placements_fingerprint(
        frozen_placements
    )
    values = {
        "context_id": context_fingerprint(
            "pulsara.compaction-summary-repair-context-id.v1",
            {
                "initial": initial.semantic_fingerprint,
                "tool_calls": tool_calls_fingerprint,
            },
        ),
        "canonical_input_identity": compiled.canonical_input_identity,
        "system_prompt": compiled.system_prompt,
        "messages": messages,
        "message_placements": frozen_placements,
        "message_placements_fingerprint": placement_fingerprint,
        "tools": compiled.tools,
        "final_estimate": estimate,
        "source_decisions": compiled.source_decisions,
        "tool_result_decisions": compiled.tool_result_decisions,
        "budget_report": report,
        "diagnostic_codes": compiled.diagnostic_codes,
        "source_collection_fingerprint": compiled.source_collection_fingerprint,
        "compile_binding_fingerprint": compiled.compile_binding_fingerprint,
    }
    repaired = FrozenCompiledModelInput(
        **values,
        compiled_semantic_fingerprint=frozen_compiled_model_input_fingerprint(
            **{
                key: value
                for key, value in values.items()
                if key
                not in {
                    "message_placements",
                    "message_placements_fingerprint",
                }
            }
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
        source_view=initial.source_view,
        prefix_proof=initial.prefix_proof,
        compile_binding=initial.compile_binding,
        native_projection_set=initial.native_projection_set,
        compiled_input=repaired,
        semantic_fingerprint=semantic_fingerprint,
    )


def _require_summary_wire_prefix(
    semantic: PreparedCompactionSummarySemantic,
    plan: FrozenProviderWireInputPlan,
) -> None:
    predecessor = semantic.source_view.predecessor_epoch_view
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
    overlap = min(len(actual_prefix), len(old_items))
    if actual_prefix[:overlap] != old_items[:overlap]:
        raise ValueError("summary actual wire input rewrote the installed prefix")


def _synthetic_summary_placement(
    *,
    message_ordinal: int,
    proof: ProviderPrefixCutProof,
) -> FrozenCompiledMessagePlacement:
    return _ephemeral_summary_placement(
        message_ordinal=message_ordinal,
        role=MessageRole.USER,
        domain="request",
        identity={
            "prompt": summary_request_fingerprint(),
            "text": SUMMARY_REQUEST,
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
    fingerprint = context_fingerprint(
        "pulsara.compiled-message-placement:v1",
        {
            "ordinal": message_ordinal,
            "entry": None,
            "item": origin,
            "within": 0,
            "role": role.value,
        },
    )
    return FrozenCompiledMessagePlacement(
        message_ordinal=message_ordinal,
        origin_entry_id=None,
        origin_item_fingerprint=origin,
        within_origin_ordinal=0,
        role=role,
        placement_fingerprint=fingerprint,
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


def _estimate_value(estimate: TokenEstimate) -> dict[str, object]:
    return {
        "system": estimate.system_tokens,
        "messages": estimate.message_tokens,
        "message_by_index": estimate.message_tokens_by_index,
        "tools": estimate.tool_tokens,
        "envelope": estimate.envelope_tokens,
        "total": estimate.total_input_tokens,
    }


__all__ = [
    "PreparedCompactionSummaryCall",
    "PreparedCompactionSummarySemantic",
    "RawCompactionSummaryResponse",
    "finalize_compaction_summary_call",
    "prepare_compaction_summary_repair_semantic",
    "prepare_compaction_summary_semantic",
]
