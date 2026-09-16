"""Process-local Plan QUESTION waiter and Host continuation ownership.

The canonical repository owns every accepted Plan fact.  This module only
bridges a same-Host running coroutine to a canonical QUESTION resolution and
keeps automatic continuation tasks alive when an origin request detaches.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
from typing import Awaitable, Callable, Protocol

from jsonschema import ValidationError, validators

from pulsara_agent.capability.builtin_catalog import builtin_tool_catalog_entry
from pulsara_agent.conversation_kernel.assembler import CompletedToolCallBlock
from pulsara_agent.conversation_kernel.contracts import WriterLease
from pulsara_agent.conversation_kernel.execution_watchdogs import (
    KernelExecutionDeadlineFactory,
    KernelWatchdogOwner,
)
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.llm.input import LLMTextPart

from pulsara_agent.conversation_kernel.repository import (
    AcceptedPlanToolBatch,
    AcceptedPlanResolution,
    ConversationKernelConflict,
    ConversationKernelRepository,
    PlanToolBatchDisposition,
    PlanToolControlKind,
    PreparedPlanBatchCall,
    PreparedPlanToolBatch,
)
from pulsara_agent.conversation_kernel.tool_surface import (
    ProcessLocalToolSurfaceBorrow,
)
from pulsara_agent.conversation_kernel.tool_contracts import (
    AcceptedCanonicalToolResultSettlement,
    PreparedResolvedToolInvocation,
)
from pulsara_agent.conversation_kernel.workspace import SessionWorkspaceResolver
from pulsara_agent.model_input.contracts import (
    FrozenCanonicalCompileSnapshot,
    FrozenProviderInputItem,
    FrozenProviderInputItemKind,
)
from pulsara_agent.model_input.lowering import project_tool_result_public_value
from pulsara_agent.primitives.context import canonical_json_bytes, thaw_json
from pulsara_agent.primitives.plan_workflow import (
    PlanInteractionBinding,
    PlanQuestionContent,
    extract_plan_draft,
    extract_plan_entry_reason,
    extract_plan_question,
)
from pulsara_agent.ports.tool_execution import thaw_tool_json_object
from pulsara_agent.hooks.context import HookContextOwner
from pulsara_agent.hooks.contracts import (
    FrozenHookDefinitionView,
    GateDecision,
    HookDispatchEnvelope,
    HookDispatchScopeRef,
    PostToolRef,
    PostToolUseInput,
    PreToolRef,
    PreToolUseInput,
    external_permission_mode,
)
from pulsara_agent.hooks.dispatcher import KernelHookDispatcher
from pulsara_agent.hooks.matcher import event_matcher_subject, tool_matcher_subject


@dataclass(frozen=True, slots=True)
class PlanQuestionWaiter:
    interaction_id: str
    origin_turn_id: str
    waiter_generation: int
    _future: asyncio.Future[AcceptedPlanResolution]


@dataclass(frozen=True, slots=True)
class OpenPlanQuestion:
    interaction_id: str
    origin_turn_id: str
    question: PlanQuestionContent
    waiter_generation: int


class KernelPlanInteractionCoordinator:
    """One dormant-before-write QUESTION waiter per active Plan workflow."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._waiter: PlanQuestionWaiter | None = None
        self._open: OpenPlanQuestion | None = None
        self._generation = 0
        self._closed = False

    async def prepare_question(
        self, *, interaction_id: str, origin_turn_id: str
    ) -> PlanQuestionWaiter:
        async with self._lock:
            if self._closed:
                raise RuntimeError("Plan interaction coordinator is closed")
            if self._waiter is not None:
                raise RuntimeError("another Plan question waiter is active")
            self._generation += 1
            waiter = PlanQuestionWaiter(
                interaction_id=interaction_id,
                origin_turn_id=origin_turn_id,
                waiter_generation=self._generation,
                _future=asyncio.get_running_loop().create_future(),
            )
            self._waiter = waiter
            return waiter

    async def publish_open(
        self, waiter: PlanQuestionWaiter, question: PlanQuestionContent
    ) -> OpenPlanQuestion:
        async with self._lock:
            self._require_waiter(waiter)
            opened = OpenPlanQuestion(
                waiter.interaction_id,
                waiter.origin_turn_id,
                question,
                waiter.waiter_generation,
            )
            # Resolution may commit after OPEN FULL but before the runner
            # promotes its dormant waiter.  Canonical ANSWERED wins; do not
            # synthesize a stale process-local Opened view in that window.
            self._open = None if waiter._future.done() else opened
            return opened

    async def current_open(self) -> OpenPlanQuestion | None:
        async with self._lock:
            return self._open

    async def settle(
        self, *, interaction_id: str, resolution: AcceptedPlanResolution
    ) -> bool:
        async with self._lock:
            waiter = self._waiter
            if waiter is None or waiter.interaction_id != interaction_id:
                return False
            if not waiter._future.done():
                waiter._future.set_result(resolution)
            self._open = None
            return True

    async def wait(self, waiter: PlanQuestionWaiter) -> AcceptedPlanResolution:
        # Deliberately no operation timeout: waiting for a human is not a
        # physical provider/tool operation deadline.
        try:
            return await asyncio.shield(waiter._future)
        finally:
            async with self._lock:
                if self._waiter is waiter and waiter._future.done():
                    self._waiter = None
                    self._open = None

    async def abandon(self, waiter: PlanQuestionWaiter, error: BaseException) -> None:
        async with self._lock:
            if self._waiter is not waiter:
                return
            if not waiter._future.done():
                waiter._future.set_exception(error)
            self._waiter = None
            self._open = None

    async def abort_current(self, error: BaseException) -> None:
        """Settle the sole same-Host waiter after canonical interruption."""

        async with self._lock:
            waiter = self._waiter
            self._waiter = None
            self._open = None
            if waiter is not None and not waiter._future.done():
                waiter._future.set_exception(error)

    async def aclose(self) -> None:
        async with self._lock:
            self._closed = True
            waiter = self._waiter
            self._waiter = None
            self._open = None
            if waiter is not None and not waiter._future.done():
                waiter._future.set_exception(
                    RuntimeError("Host closed while a Plan question was open")
                )

    def _require_waiter(self, waiter: PlanQuestionWaiter) -> None:
        if self._waiter is not waiter:
            raise RuntimeError("Plan question waiter authority changed")


class ContinuationAdmissionPhase(StrEnum):
    ADMITTING = "ADMITTING"
    TERMINALIZING = "TERMINALIZING"


@dataclass(slots=True)
class ContinuationAdmissionAttempt:
    """Host-owned process-local task; request cancellation only detaches."""

    attempt_id: str
    turn_id: str
    semantic_candidate_fingerprint: str
    task: asyncio.Task[object]
    phase: ContinuationAdmissionPhase = ContinuationAdmissionPhase.ADMITTING


class ContinuationAdmissionOwner:
    def __init__(self) -> None:
        self._attempts: dict[str, ContinuationAdmissionAttempt] = {}
        self._closing = False

    def start(
        self,
        *,
        attempt_id: str,
        turn_id: str,
        semantic_candidate_fingerprint: str,
        run: Callable[[], Awaitable[object]],
        before_start: Callable[[], None] | None = None,
    ) -> ContinuationAdmissionAttempt:
        # Host methods and close all run on the owning event loop.  Installation
        # deliberately has no await point: once a command is admitted, caller
        # cancellation can only detach from the already-owned physical task.
        existing = self._attempts.get(attempt_id)
        if existing is not None:
            if (
                existing.turn_id != turn_id
                or existing.semantic_candidate_fingerprint
                != semantic_candidate_fingerprint
            ):
                raise ConversationKernelConflict(
                    "continuation attempt semantic identity conflicts"
                )
            return existing
        if self._closing:
            raise RuntimeError("continuation admission owner is closing")
        if not semantic_candidate_fingerprint:
            raise ValueError("continuation semantic candidate fingerprint is absent")
        if before_start is not None:
            before_start()
        task = asyncio.create_task(run(), name=f"kernel-plan-continuation:{turn_id}")
        attempt = ContinuationAdmissionAttempt(
            attempt_id,
            turn_id,
            semantic_candidate_fingerprint,
            task,
        )
        self._attempts[attempt_id] = attempt
        task.add_done_callback(lambda completed: self._retire(attempt_id, completed))
        return attempt

    def mark_terminalizing(
        self, *, attempt_id: str, task: asyncio.Task[object]
    ) -> None:
        attempt = self._attempts.get(attempt_id)
        if attempt is None or attempt.task is not task:
            raise RuntimeError("continuation terminalization owner changed")
        attempt.phase = ContinuationAdmissionPhase.TERMINALIZING

    def _retire(self, attempt_id: str, task: asyncio.Task[object]) -> None:
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            pass
        current = self._attempts.get(attempt_id)
        if current is not None and current.task is task:
            self._attempts.pop(attempt_id, None)

    async def drain(self) -> None:
        """Join the currently admitted finite operations without closing admission."""

        while self._attempts:
            attempts = tuple(self._attempts.values())
            await asyncio.gather(
                *(attempt.task for attempt in attempts), return_exceptions=True
            )
            # Let done callbacks retire the exact snapshot before deciding
            # whether another already-admitted attempt must also be joined.
            await asyncio.sleep(0)

    async def aclose(self) -> None:
        self._closing = True
        attempts = tuple(self._attempts.values())
        # Admission tasks own bounded repository write/confirmation.  Close
        # detaches callers but drains those physical owners; it must not cancel
        # them after a canonical continuation may already be FULL.
        if attempts:
            await asyncio.gather(
                *(attempt.task for attempt in attempts), return_exceptions=True
            )


class AutomaticPlanContinuationPort(Protocol):
    async def __call__(
        self,
        candidate: PreparedPlanToolBatch,
        deadline_monotonic: float,
    ) -> AcceptedPlanToolBatch: ...


def _stable_id(prefix: str, *parts: str) -> str:
    return f"{prefix}:{sha256(chr(0).join(parts).encode()).hexdigest()}"


def _json_digest(value) -> str:
    return "sha256:" + sha256(canonical_json_bytes(value)).hexdigest()


class PlanToolBatchCoordinator:
    """Own one complete Plan control batch and its human waiter settlement."""

    def __init__(
        self,
        *,
        repository: ConversationKernelRepository,
        writer_lease: WriterLease,
        io_owner: KernelSessionIO,
        interactions: KernelPlanInteractionCoordinator | None,
        automatic_continuation: AutomaticPlanContinuationPort | None,
        workspace_resolver: SessionWorkspaceResolver,
        deadline_factory: KernelExecutionDeadlineFactory,
        hook_dispatcher: KernelHookDispatcher | None = None,
        hook_context_owner: HookContextOwner | None = None,
        hook_scope: HookDispatchScopeRef | None = None,
    ) -> None:
        self._repository = repository
        self._writer_lease = writer_lease
        self._io = io_owner
        self._plan_interactions = interactions
        self._automatic_plan_continuation = automatic_continuation
        self._workspace_resolver = workspace_resolver
        self._deadlines = deadline_factory
        self._hooks = hook_dispatcher
        self._hook_context = hook_context_owner
        self._hook_scope = hook_scope

    async def _dispatch_pre(
        self,
        *,
        view: FrozenHookDefinitionView,
        prepared: PreparedResolvedToolInvocation,
        turn_id: str,
        assistant_entry_id: str,
        tool_call_id: str,
        model: str,
        cwd: str,
        canonical_facts: FrozenCanonicalCompileSnapshot,
        deadline: float,
    ):
        if self._hooks is None or self._hook_scope is None:
            raise RuntimeError("Plan Hook dispatch is not composed")
        subject = tool_matcher_subject(prepared.requested_tool_name)
        public_input = PreToolUseInput(
            session_id=self._writer_lease.guard.session_id,
            cwd=cwd,
            model=model,
            turn_id=turn_id,
            tool_name=subject.external_primary,
            tool_use_id=tool_call_id,
            tool_input=thaw_json(prepared.resolved_arguments),
            permission_mode=external_permission_mode(
                canonical_facts.run_permission_snapshot.effective_mode.value,
                active_plan_workflow=(
                    canonical_facts.run_permission_snapshot.plan_workflow_id
                    is not None
                ),
            ),
        )
        causal_ref = PreToolRef(
            turn_id,
            assistant_entry_id,
            tool_call_id,
            prepared.canonical_tool_name,
        )
        outcome = await self._hooks.dispatch(
            HookDispatchEnvelope(
                view,
                self._hook_scope,
                public_input,
                causal_ref,
                deadline,
            ),
            matcher_subject=event_matcher_subject(
                public_input.event_type, tool=subject
            ),
        )
        return outcome, causal_ref

    async def _dispatch_post(
        self,
        *,
        settlement: AcceptedCanonicalToolResultSettlement,
        model: str,
        cwd: str,
        canonical_facts: FrozenCanonicalCompileSnapshot,
        continuity_epoch_nonce: object,
        context_allowed: bool,
    ) -> None:
        if self._hooks is None or self._hook_scope is None:
            return
        view = self._hooks.capture_view()
        subject = tool_matcher_subject(settlement.tool_name)
        item = FrozenProviderInputItem(
            item_kind=FrozenProviderInputItemKind.TOOL_RESULT,
            source_entry_id=settlement.result_entry_id,
            source_entry_sequence=settlement.accepted_entry_sequence,
            source_turn_id=settlement.turn_id,
            content=(
                settlement.canonical_content.parts
                if settlement.canonical_content is not None
                else (LLMTextPart(settlement.public_projection.canonical_body),)
            ),
            tool_call_id=settlement.tool_call_id,
            tool_request_entry_id=settlement.assistant_entry_id,
            tool_result_context=settlement.public_projection.metadata,
            tool_result_body_text=settlement.public_projection.canonical_body,
            tool_result_delivery=settlement.public_projection.delivery,
            tool_call_ordinal=settlement.call_ordinal,
            tool_call_arguments=settlement.public_arguments,
        )
        projected = project_tool_result_public_value(
            item, artifact_read_available=True
        )
        causal_ref = PostToolRef(
            settlement.turn_id,
            settlement.tool_call_id,
            settlement.result_id,
            settlement.result_entry_id,
            settlement.result_state,
            continuity_epoch_nonce,
        )
        public_input = PostToolUseInput(
            session_id=self._writer_lease.guard.session_id,
            cwd=cwd,
            model=model,
            turn_id=settlement.turn_id,
            tool_name=subject.external_primary,
            tool_use_id=settlement.tool_call_id,
            tool_input=thaw_json(settlement.public_arguments),
            tool_response=projected.value,
            permission_mode=external_permission_mode(
                canonical_facts.run_permission_snapshot.effective_mode.value,
                active_plan_workflow=(
                    canonical_facts.run_permission_snapshot.plan_workflow_id
                    is not None
                ),
            ),
        )
        outcome = await self._hooks.dispatch(
            HookDispatchEnvelope(
                view,
                self._hook_scope,
                public_input,
                causal_ref,
                self._deadlines.deadline(
                    KernelWatchdogOwner.NONTERMINAL_TOOL_INVOCATION
                ),
            ),
            matcher_subject=event_matcher_subject(
                public_input.event_type, tool=subject
            ),
        )
        if context_allowed and self._hook_context is not None:
            self._hook_context.accept_sync(
                scope=self._hook_scope,
                causal_ref=causal_ref,
                entries=outcome.context_entries,
            )

    def _canonical_deadline(self) -> float:
        return self._deadlines.deadline(KernelWatchdogOwner.FOREGROUND_CANONICAL)

    async def _resolved_workspace_id(self) -> str:
        return await self._workspace_resolver.resolve(
            deadline=self._canonical_deadline()
        )

    async def accept_batch(
        self,
        *,
        calls: tuple[CompletedToolCallBlock, ...],
        selected_call_index: int,
        assistant_entry_id: str,
        canonical_facts: FrozenCanonicalCompileSnapshot,
        surface_borrow: ProcessLocalToolSurfaceBorrow,
        deadline: float,
        hook_model: str = "pulsara-model",
        hook_cwd: str = ".",
        continuity_epoch_nonce: object | None = None,
    ) -> AcceptedPlanToolBatch:
        selected = calls[selected_call_index]
        kind = {
            "enter_plan": PlanToolControlKind.ENTER,
            "ask_plan_question": PlanToolControlKind.QUESTION,
            "exit_plan": PlanToolControlKind.DRAFT,
        }[selected.tool_name]
        workflow_fact = canonical_facts.plan_workflow_fact
        if kind is PlanToolControlKind.ENTER:
            if workflow_fact is None:
                workflow_id = _stable_id(
                    "plan-workflow",
                    self._writer_lease.guard.session_id,
                    assistant_entry_id,
                    selected.tool_call_id,
                )
                expected_revision = None
            else:
                workflow_id = workflow_fact.workflow_id
                expected_revision = workflow_fact.current_workflow_revision
        else:
            workflow_id = (
                workflow_fact.workflow_id
                if workflow_fact is not None
                else _stable_id(
                    "plan-unavailable-workflow",
                    self._writer_lease.guard.session_id,
                    assistant_entry_id,
                    selected.tool_call_id,
                )
            )
            expected_revision = (
                None
                if workflow_fact is None
                else workflow_fact.current_workflow_revision
            )
        provisional_interaction_id = (
            None
            if kind is PlanToolControlKind.ENTER
            else _stable_id(
                "plan-interaction",
                workflow_id,
                assistant_entry_id,
                selected.tool_call_id,
            )
        )
        catalog_entry = builtin_tool_catalog_entry(selected.tool_name)
        catalog_binding = catalog_entry.binding_contract.base
        request_binding = PlanInteractionBinding(
            catalog_binding.contract_id,
            catalog_binding.contract_version,
            catalog_binding.binding_fingerprint,
        )
        disposition = PlanToolBatchDisposition.APPLY
        # A Plan call owns the complete batch even when its frozen surface was
        # revoked or its arguments are invalid.  Classify those conditions
        # before constructing any workflow/interaction subject so the
        # repository can install one closed no-attempt result for every call.
        try:
            advertised_execution_binding = surface_borrow.execution_binding(
                selected.tool_name
            )
            advertised_spec = next(
                (
                    item
                    for item in surface_borrow.prepared.model_surface.tool_specs
                    if item.name == selected.tool_name
                ),
                None,
            )
            if (
                advertised_spec is None
                or advertised_execution_binding.descriptor_fingerprint
                != advertised_spec.descriptor_fingerprint
                or advertised_spec.descriptor_fingerprint
                != catalog_entry.descriptor.fingerprint()
            ):
                disposition = PlanToolBatchDisposition.TOOL_UNAVAILABLE
        except (KeyError, RuntimeError):
            disposition = PlanToolBatchDisposition.TOOL_UNAVAILABLE
        if disposition is PlanToolBatchDisposition.APPLY:
            schema_source = catalog_entry.descriptor.input_schema
            if schema_source is None:
                disposition = PlanToolBatchDisposition.TOOL_UNAVAILABLE
            else:
                schema = thaw_tool_json_object(schema_source)
                try:
                    validator = validators.validator_for(schema)
                    validator.check_schema(schema)
                except Exception:
                    disposition = PlanToolBatchDisposition.TOOL_UNAVAILABLE
                else:
                    try:
                        raw_arguments = thaw_json(selected.arguments)
                        if not isinstance(raw_arguments, dict):
                            raise ValidationError("arguments must be an object")
                        validator(schema).validate(raw_arguments)
                    except ValidationError:
                        disposition = PlanToolBatchDisposition.INVALID_ARGUMENTS
        if disposition is PlanToolBatchDisposition.APPLY:
            try:
                if kind is PlanToolControlKind.ENTER:
                    extract_plan_entry_reason(
                        binding=request_binding,
                        arguments=selected.arguments,
                    )
                elif kind is PlanToolControlKind.QUESTION:
                    assert provisional_interaction_id is not None
                    extract_plan_question(
                        interaction_id=provisional_interaction_id,
                        binding=request_binding,
                        arguments=selected.arguments,
                    )
                else:
                    assert provisional_interaction_id is not None
                    extract_plan_draft(
                        interaction_id=provisional_interaction_id,
                        assistant_entry_id=assistant_entry_id,
                        tool_call_id=selected.tool_call_id,
                        binding=request_binding,
                        request_semantic_digest=_json_digest(selected.arguments),
                        arguments=selected.arguments,
                    )
            except ValueError:
                disposition = PlanToolBatchDisposition.INVALID_ARGUMENTS
        if disposition is PlanToolBatchDisposition.APPLY:
            if kind is not PlanToolControlKind.ENTER and workflow_fact is None:
                disposition = PlanToolBatchDisposition.TOOL_UNAVAILABLE
            elif (
                kind is PlanToolControlKind.QUESTION and self._plan_interactions is None
            ):
                disposition = PlanToolBatchDisposition.TOOL_UNAVAILABLE
            elif (
                kind is PlanToolControlKind.ENTER
                and workflow_fact is None
                and self._automatic_plan_continuation is None
            ):
                disposition = PlanToolBatchDisposition.TOOL_UNAVAILABLE
        pre_context_reservation = None
        if (
            disposition is PlanToolBatchDisposition.APPLY
            and self._hooks is not None
            and self._hook_scope is not None
        ):
            selected_prepared = PreparedResolvedToolInvocation(
                selected.tool_name,
                selected.tool_name,
                selected.tool_name,
                None,
                selected.arguments,
            )
            pre_outcome, pre_causal_ref = await self._dispatch_pre(
                view=self._hooks.capture_view(),
                prepared=selected_prepared,
                turn_id=canonical_facts.canonical_input.identity.turn_id,
                assistant_entry_id=assistant_entry_id,
                tool_call_id=selected.tool_call_id,
                model=hook_model,
                cwd=hook_cwd,
                canonical_facts=canonical_facts,
                deadline=deadline,
            )
            if self._hook_context is not None:
                pre_context_reservation = self._hook_context.prepare_sync(
                    scope=self._hook_scope,
                    causal_ref=pre_causal_ref,
                    entries=pre_outcome.context_entries,
                )
            if pre_outcome.decision is GateDecision.BLOCK:
                disposition = PlanToolBatchDisposition.HOOK_BLOCKED
        apply_control = disposition is PlanToolBatchDisposition.APPLY
        interaction_id = provisional_interaction_id if apply_control else None
        continuation_turn_id = (
            _stable_id("plan-continuation-turn", workflow_id, selected.tool_call_id)
            if apply_control
            and kind is PlanToolControlKind.ENTER
            and workflow_fact is None
            else None
        )
        continuation_entry_id = (
            _stable_id("plan-continuation-entry", workflow_id, selected.tool_call_id)
            if apply_control
            and kind is PlanToolControlKind.ENTER
            and workflow_fact is None
            else None
        )
        continuation_revision_id = (
            _stable_id("context-revision", continuation_turn_id or "", "0")
            if continuation_turn_id is not None
            else None
        )
        prepared_calls: list[PreparedPlanBatchCall] = []
        for index, call in enumerate(calls):
            selected_question = (
                apply_control
                and index == selected_call_index
                and kind is PlanToolControlKind.QUESTION
            )
            prepared_calls.append(
                PreparedPlanBatchCall(
                    block_id=call.block_id,
                    tool_call_id=call.tool_call_id,
                    tool_name=call.tool_name,
                    arguments=call.arguments,
                    result_id=(
                        None
                        if selected_question
                        else _stable_id(
                            "tool-result", assistant_entry_id, call.tool_call_id
                        )
                    ),
                    result_entry_id=(
                        None
                        if selected_question
                        else _stable_id(
                            "tool-result-entry", assistant_entry_id, call.tool_call_id
                        )
                    ),
                )
            )
        candidate = PreparedPlanToolBatch(
            session_id=self._writer_lease.guard.session_id,
            workspace_id=await self._resolved_workspace_id(),
            origin_turn_id=canonical_facts.canonical_input.identity.turn_id,
            assistant_entry_id=assistant_entry_id,
            selected_call_ordinal=selected_call_index,
            control_kind=kind,
            selected_arguments=selected.arguments,
            request_binding=request_binding,
            permission_snapshot=canonical_facts.run_permission_snapshot,
            workflow_id=workflow_id,
            expected_workflow_revision=expected_revision,
            interaction_id=interaction_id,
            continuation_turn_id=continuation_turn_id,
            continuation_entry_id=continuation_entry_id,
            continuation_context_binding_revision_id=continuation_revision_id,
            calls=tuple(prepared_calls),
            occurred_at=datetime.now(timezone.utc),
            actor_id="plan-runtime",
            idempotent_existing=(
                apply_control
                and kind is PlanToolControlKind.ENTER
                and workflow_fact is not None
            ),
            selected_disposition=disposition,
        )
        waiter: PlanQuestionWaiter | None = None
        if apply_control and kind is PlanToolControlKind.QUESTION:
            assert self._plan_interactions is not None
            assert interaction_id is not None
            waiter = await self._plan_interactions.prepare_question(
                interaction_id=interaction_id,
                origin_turn_id=candidate.origin_turn_id,
            )
        try:
            if (
                apply_control
                and kind is PlanToolControlKind.ENTER
                and not candidate.idempotent_existing
            ):
                assert self._automatic_plan_continuation is not None
                # The Host callback installs its own continuation task before
                # its first await.  Calling it in the ROOT run-chain task is
                # essential: an extra shield-created wrapper would become the
                # observed origin task and could never exact-join Host's ROOT
                # slot.  The callback itself shields the installed owner.
                outcome = await self._automatic_plan_continuation(candidate, deadline)
            else:
                try:
                    outcome = await self._io.run(
                        self._repository.accept_plan_tool_batch,
                        self._writer_lease.guard,
                        candidate=candidate,
                        deadline_monotonic=deadline,
                    )
                except Exception:
                    outcome = await self._io.run(
                        self._repository.confirm_plan_tool_batch_winner,
                        candidate=candidate,
                        deadline_monotonic=self._canonical_deadline(),
                    )
                    if outcome is None:
                        raise
            if waiter is not None:
                context_allowed = not outcome.origin_turn_completed
                if pre_context_reservation is not None:
                    if context_allowed:
                        pre_context_reservation.commit()
                    else:
                        pre_context_reservation.retire()
                if self._hooks is not None:
                    if continuity_epoch_nonce is None:
                        raise RuntimeError("Plan PostTool lost continuity epoch")
                    for settlement in outcome.tool_result_settlements:
                        await self._dispatch_post(
                            settlement=settlement,
                            model=hook_model,
                            cwd=hook_cwd,
                            canonical_facts=canonical_facts,
                            continuity_epoch_nonce=continuity_epoch_nonce,
                            context_allowed=context_allowed,
                        )
                if outcome.question is None:
                    raise RuntimeError("accepted Plan question lacks typed content")
                await self._plan_interactions.publish_open(waiter, outcome.question)
                resolution = await self._plan_interactions.wait(waiter)
                if resolution.tool_result_settlement is None:
                    raise RuntimeError(
                        "answered Plan question lost its ToolResult settlement"
                    )
                await self._dispatch_post(
                    settlement=resolution.tool_result_settlement,
                    model=hook_model,
                    cwd=hook_cwd,
                    canonical_facts=canonical_facts,
                    continuity_epoch_nonce=continuity_epoch_nonce,
                    context_allowed=True,
                )
            else:
                context_allowed = not outcome.origin_turn_completed
                if pre_context_reservation is not None:
                    if context_allowed:
                        pre_context_reservation.commit()
                    else:
                        pre_context_reservation.retire()
                if self._hooks is not None:
                    if continuity_epoch_nonce is None:
                        raise RuntimeError("Plan PostTool lost continuity epoch")
                    for settlement in outcome.tool_result_settlements:
                        await self._dispatch_post(
                            settlement=settlement,
                            model=hook_model,
                            cwd=hook_cwd,
                            canonical_facts=canonical_facts,
                            continuity_epoch_nonce=continuity_epoch_nonce,
                            context_allowed=context_allowed,
                        )
            return outcome
        except BaseException as error:
            if pre_context_reservation is not None:
                pre_context_reservation.retire()
            if waiter is not None and self._plan_interactions is not None:
                await self._plan_interactions.abandon(waiter, error)
            raise


__all__ = [
    "AutomaticPlanContinuationPort",
    "PlanToolBatchCoordinator",
    "ContinuationAdmissionAttempt",
    "ContinuationAdmissionOwner",
    "ContinuationAdmissionPhase",
    "KernelPlanInteractionCoordinator",
    "OpenPlanQuestion",
    "PlanQuestionWaiter",
]
