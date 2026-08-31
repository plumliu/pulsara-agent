"""Round 5B compaction execution and canonical adoption coordinator."""

from __future__ import annotations

import asyncio

from dataclasses import dataclass, field as dataclass_field, replace

from datetime import datetime, timezone
from hashlib import sha256
from typing import Protocol


from time import monotonic


from pulsara_agent.conversation_kernel.assembler import (
    MAXIMUM_COMPLETED_ASSISTANT_MESSAGE_UTF8_BYTES,
)


from pulsara_agent.conversation_kernel.blob import (
    CanonicalContentPublisher,
)

from pulsara_agent.conversation_kernel.context_sources import (
    build_compaction_context_source,
)

from pulsara_agent.conversation_kernel.compaction.contracts import (
    COMPACTION_MODEL_CONTRACT,
    COMPACTION_SNAPSHOT_COMPILER_CONTRACT,
    COMPACTION_SUMMARY_PROMPT_CONTRACT,
    CONTEXT_SNAPSHOT_CODEC,
    CONTEXT_SNAPSHOT_MEDIA_TYPE,
    CompactionCanonicalAdoptionFactoryInput,
    CompactionCanonicalWritePreconditions,
    CompactionContinuationMode,
    CompactionAttemptPhase,
    CompactionConfirmationKind,
    CompactionDisposition,
    CompactionOutcome,
    CompactionScope,
    CompactionTargetBranch,
    CompactionTrigger,
    ExpectedCompactionPredecessorRevision,
    FrozenCompactionCanonicalRead,
    FrozenCompactionActiveRequest,
    FrozenCompactionSourceView,
    PreparedCompactionCanonicalAdoption,
    manual_compaction_stable_suffix,
    build_prepared_compaction_canonical_adoption,
    canonical_compaction_range_digest,
    freeze_compaction_canonical_range,
)

from pulsara_agent.conversation_kernel.compaction.model_call import (
    finalize_compaction_summary_call,
    prepare_compaction_summary_repair_semantic,
    prepare_compaction_summary_semantic,
)

from pulsara_agent.conversation_kernel.compaction.planner import (
    build_synthetic_compaction_dispatch_read,
    CompactionPlanningError,
    CompactionReclaimUnavailable,
    crosses_compaction_resource_headroom,
    enumerate_complete_tool_groups,
    estimate_unavoidable_compaction_successor_tokens,
    freeze_compaction_continuation,
    freeze_compaction_source_view,
    freeze_tail_and_prefix,
    select_recent_human_messages,
    should_trigger_compaction,
    validate_compaction_reclaim,
)

from pulsara_agent.conversation_kernel.compaction.prompt import (
    build_compaction_snapshot_carrier,
    compaction_summary_request,
    freeze_compaction_summary_output,
)

from pulsara_agent.conversation_kernel.compaction.runtime import (
    HostCompactionRuntimeOwner,
    ManualCompactionRequest,
)

from pulsara_agent.conversation_kernel.compaction.runtime_handoff import (
    FrozenCompactionRuntimeHandoff,
)

from pulsara_agent.conversation_kernel.cold_epoch import (
    CompactionContinuationSeed,
)


from pulsara_agent.conversation_kernel.input_continuity import (
    HostProviderInputContinuityOwner,
)

from pulsara_agent.conversation_kernel.contracts import (
    CanonicalContent,
    TurnStatus,
    WriterLease,
)


from pulsara_agent.conversation_kernel.io import KernelSessionIO

from pulsara_agent.conversation_kernel.execution_watchdogs import (
    KernelExecutionDeadlineFactory,
    KernelWatchdogOwner,
)


from pulsara_agent.conversation_kernel.workspace import SessionWorkspaceResolver
from pulsara_agent.conversation_kernel.memory.contracts import (
    MemoryUsePolicy,
)


from pulsara_agent.conversation_kernel.tool_contracts import (
    ToolSurfacePlanningPort,
)

from pulsara_agent.conversation_kernel.repository import (
    ConversationKernelRepository,
    ConversationKernelConflict,
    StaleHostWriter,
)


from pulsara_agent.conversation_kernel.provider_dispatch import (
    KernelModelPort,
    PreparedCompactionSourceDispatch,
    PreparedProviderDispatch,
    PreparedProviderHeadroomAdmission,
    ProviderDispatchCoordinator,
)


from pulsara_agent.conversation_kernel.reader import (
    CanonicalProviderInputReader,
)

from pulsara_agent.conversation_kernel.safe_point import (
    ProviderSafePointCoordinator,
)


from pulsara_agent.model_input.compiler import (
    StructuredModelInputCompiler,
)


from pulsara_agent.model_input.contracts import (
    ContextSourceAbsentFact,
    ContextSourceAbsenceKind,
    ContextSourceCandidate,
    ContextSourceKind,
    ModelInputScopeKind,
    ModelInputCompileFailureKind,
    StructuredModelInputCompileError,
)

from pulsara_agent.model_input.continuity import (
    ProviderInputContinuityScope,
    provider_input_logical_utf8_bytes,
)

from pulsara_agent.primitives.run_permission import FrozenRunPermissionSnapshot
from pulsara_agent.hooks.context import PendingHookContextReservation
from pulsara_agent.hooks.contracts import (
    GateDecision,
    HookDispatchEnvelope,
    HookDispatchScopeRef,
    PostCompactInput,
    PostCompactRef,
    PreCompactInput,
    PreCompactRef,
)
from pulsara_agent.hooks.dispatcher import KernelHookDispatcher
from pulsara_agent.hooks.matcher import event_matcher_subject


def _stable_id(prefix: str, *parts: str) -> str:
    return f"{prefix}:{sha256(chr(0).join(parts).encode()).hexdigest()}"


def _can_retry_compaction_with_smaller_tail(error: BaseException) -> bool:
    if isinstance(error, CompactionPlanningError):
        return True
    if not isinstance(error, StructuredModelInputCompileError):
        return False
    return error.kind in {
        ModelInputCompileFailureKind.COMPILE_WORKING_SET_EXCEEDED,
        ModelInputCompileFailureKind.PROTECTED_TRANSCRIPT_EXCEEDS_BUDGET,
        ModelInputCompileFailureKind.PREFIX_EPOCH_BUDGET_EXHAUSTED,
        ModelInputCompileFailureKind.REQUIRED_CONTEXT_EXCEEDS_BUDGET,
        ModelInputCompileFailureKind.FULL_REQUIRED_TOOL_RESULT_NOT_INLINEABLE,
        ModelInputCompileFailureKind.FULL_REQUIRED_TOOL_RESULT_EXCEEDS_INPUT_BUDGET,
    }


@dataclass(frozen=True, slots=True)
class CompactionExecutionResult:
    outcome: CompactionOutcome
    successor_dispatch: PreparedProviderDispatch | None = dataclass_field(
        default=None, repr=False
    )
    active_continuation_blocked_reason: str | None = None

    def __post_init__(self) -> None:
        if self.active_continuation_blocked_reason is not None and (
            self.outcome.disposition is not CompactionDisposition.COMPACTED
            or self.successor_dispatch is not None
        ):
            raise ValueError("compaction continuation block carrier is invalid")


def _already_compact_result(turn_id: str) -> CompactionExecutionResult:
    return CompactionExecutionResult(
        CompactionOutcome(
            CompactionDisposition.NOT_NEEDED,
            turn_id,
            None,
            None,
            "CONTEXT_ALREADY_COMPACT",
        )
    )


class _PostAdoptionCompactionFailure(BaseException):
    """Carry an already-FULL compaction winner until its error is re-raised."""

    def __init__(self, outcome: CompactionOutcome, error: BaseException) -> None:
        if (
            outcome.disposition is not CompactionDisposition.COMPACTED
            or outcome.snapshot_id is None
            or outcome.revision_ordinal is None
        ):
            raise ValueError("post-adoption failure lacks its canonical winner")
        super().__init__(type(error).__name__)
        self.outcome = outcome
        self.error = error


@dataclass(frozen=True, slots=True)
class CompactionAttemptToken:
    session_id: str
    turn_id: str
    scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    trigger: CompactionTrigger


@dataclass(frozen=True, slots=True)
class PreparedCompactSessionStartFacts:
    attempt_token: CompactionAttemptToken = dataclass_field(repr=False, compare=False)
    adopted_snapshot_id: str
    adopted_binding_revision_id: str
    scope: CompactionScope
    turn_id: str
    model_id: str
    permission_snapshot: FrozenRunPermissionSnapshot = dataclass_field(repr=False)
    deadline_monotonic: float = dataclass_field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            self.scope.scope_kind is not ModelInputScopeKind.ROOT
            or self.scope.scope_subagent_task_id is not None
            or self.scope.turn_id != self.turn_id
            or self.attempt_token.session_id != self.scope.session_id
            or self.attempt_token.turn_id != self.turn_id
            or self.attempt_token.scope_kind is not ModelInputScopeKind.ROOT
            or self.attempt_token.scope_subagent_task_id is not None
            or not self.adopted_snapshot_id
            or not self.adopted_binding_revision_id
            or not self.model_id
            or not self.permission_snapshot.snapshot_id
            or self.deadline_monotonic <= 0
        ):
            raise ValueError("compact SessionStart facts do not exact-join")


@dataclass(frozen=True, slots=True)
class PreparedCompactSessionStart:
    proceed: bool
    reason: str | None = None
    reservation: PendingHookContextReservation | None = dataclass_field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if not self.proceed and self.reservation is not None:
            raise ValueError("blocked compact SessionStart cannot own context")


class SessionStartCompactPort(Protocol):
    async def __call__(
        self, facts: PreparedCompactSessionStartFacts
    ) -> PreparedCompactSessionStart: ...


class SessionStartCompactBoundaryPort(Protocol):
    async def arm_compact_boundary(
        self,
        *,
        attempt_token: CompactionAttemptToken,
        adopted_snapshot_id: str,
        adopted_binding_revision_id: str,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class PreparedPrecompileCompaction:
    """Exact non-executable source retained across the decision/attempt seam."""

    dispatch: PreparedCompactionSourceDispatch = dataclass_field(repr=False)
    canonical_read: FrozenCompactionCanonicalRead = dataclass_field(repr=False)
    source_view: FrozenCompactionSourceView = dataclass_field(repr=False)
    attempt_token: CompactionAttemptToken = dataclass_field(repr=False, compare=False)

    def close(self) -> None:
        self.dispatch.handle.close()


@dataclass(frozen=True, slots=True)
class PrecompileCompactionDecision:
    """Closed result of the exact projection before an ordinary compile."""

    compaction: PreparedPrecompileCompaction | None = dataclass_field(
        default=None, repr=False
    )
    ordinary_admission: PreparedProviderHeadroomAdmission | None = dataclass_field(
        default=None, repr=False
    )

    def __post_init__(self) -> None:
        if self.compaction is not None and self.ordinary_admission is not None:
            raise ValueError("precompile decision owns two continuations")


class CompactionCoordinator:
    """Own active/idle compaction planning, adoption and successor installation."""

    def __init__(
        self,
        *,
        owner: HostCompactionRuntimeOwner | None,
        repository: ConversationKernelRepository,
        writer_lease: WriterLease,
        model: KernelModelPort,
        tools: ToolSurfacePlanningPort,
        provider_dispatch: ProviderDispatchCoordinator,
        input_reader: CanonicalProviderInputReader,
        io_owner: KernelSessionIO,
        compiler: StructuredModelInputCompiler,
        continuity_owner: HostProviderInputContinuityOwner,
        safe_point: ProviderSafePointCoordinator,
        content_publisher: CanonicalContentPublisher,
        workspace_resolver: SessionWorkspaceResolver,
        deadline_factory: KernelExecutionDeadlineFactory,
        hook_dispatcher: KernelHookDispatcher | None = None,
        hook_root_scope: HookDispatchScopeRef | None = None,
    ) -> None:
        self._compaction_owner = owner
        self._repository = repository
        self._writer_lease = writer_lease
        self._model = model
        self._tools = tools
        self._provider_dispatch = provider_dispatch
        self._input_reader = input_reader
        self._io = io_owner
        self._compiler = compiler
        self._continuity = continuity_owner
        self._safe_point = safe_point
        self._content_publisher = content_publisher
        self._workspace_resolver = workspace_resolver
        self._deadlines = deadline_factory
        self._hook_dispatcher = hook_dispatcher
        self._hook_root_scope = hook_root_scope

    def _canonical_deadline(self) -> float:
        return self._deadlines.deadline(KernelWatchdogOwner.FOREGROUND_CANONICAL)

    async def take_manual(
        self,
        *,
        scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
        turn_id: str,
    ) -> ManualCompactionRequest | None:
        if self._compaction_owner is None:
            return None
        return await self._compaction_owner.take_manual(
            scope_kind=scope_kind,
            scope_subagent_task_id=scope_subagent_task_id,
            turn_id=turn_id,
        )

    async def prepare_precompile_admission(
        self,
        *,
        turn_id: str,
        model_call_index: int,
        deadline: float,
    ) -> PreparedProviderHeadroomAdmission:
        return await self._provider_dispatch.prepare_headroom_admission(
            turn_id=turn_id,
            model_call_index=model_call_index,
            deadline=deadline,
        )

    def automatic_allowed(
        self,
        *,
        scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
    ) -> bool:
        return self._compaction_owner is not None and (
            self._compaction_owner.automatic_allowed(
                scope_kind=scope_kind,
                scope_subagent_task_id=scope_subagent_task_id,
            )
        )

    async def _resolved_workspace_id(self) -> str:
        return await self._workspace_resolver.resolve(
            deadline=self._canonical_deadline()
        )

    async def _content(
        self,
        value: bytes,
        *,
        deadline: float,
        media_type: str = "text/plain",
        codec: str = "utf-8",
    ) -> CanonicalContent:
        return await self._io.run(
            self._content_publisher.materialize,
            session_id=self._writer_lease.guard.session_id,
            content=value,
            media_type=media_type,
            codec=codec,
            deadline_monotonic=deadline,
        )

    @staticmethod
    def _hook_trigger(trigger: CompactionTrigger) -> str:
        return "manual" if trigger is CompactionTrigger.MANUAL else "auto"

    async def _dispatch_pre_compact(
        self,
        *,
        dispatch: PreparedCompactionSourceDispatch,
        scope: HookDispatchScopeRef | None,
        attempt_token: CompactionAttemptToken,
        turn_id: str,
        trigger: CompactionTrigger,
        deadline: float,
    ) -> tuple[bool, str | None]:
        dispatcher = self._hook_dispatcher
        if dispatcher is None or scope is None:
            return True, None
        trigger_name = self._hook_trigger(trigger)
        public_input = PreCompactInput(
            session_id=self._writer_lease.guard.session_id,
            cwd=str(self._tools.snapshot_terminal_cwd()),
            model=dispatch.prepared_call.call.target.fact.model_id,
            turn_id=turn_id,
            trigger=trigger_name,
        )
        outcome = await dispatcher.dispatch(
            HookDispatchEnvelope(
                dispatcher.capture_view(),
                scope,
                public_input,
                PreCompactRef(
                    attempt_token,
                    attempt_token.scope_kind.value,
                    turn_id,
                    trigger_name,
                ),
                deadline,
            ),
            matcher_subject=event_matcher_subject(
                public_input.event_type, trigger=trigger_name
            ),
        )
        return outcome.decision is GateDecision.PROCEED, outcome.reason

    async def _dispatch_post_compact(
        self,
        *,
        model_id: str,
        cwd: str,
        scope: HookDispatchScopeRef | None,
        attempt_token: CompactionAttemptToken,
        candidate: PreparedCompactionCanonicalAdoption,
        turn_id: str,
        trigger: CompactionTrigger,
        deadline: float,
    ) -> tuple[bool, str | None]:
        dispatcher = self._hook_dispatcher
        if dispatcher is None or scope is None:
            return True, None
        trigger_name = self._hook_trigger(trigger)
        public_input = PostCompactInput(
            session_id=self._writer_lease.guard.session_id,
            cwd=cwd,
            model=model_id,
            turn_id=turn_id,
            trigger=trigger_name,
        )
        outcome = await dispatcher.dispatch(
            HookDispatchEnvelope(
                dispatcher.capture_view(),
                scope,
                public_input,
                PostCompactRef(
                    attempt_token,
                    candidate.snapshot.snapshot_id,
                    candidate.binding.binding_revision_id,
                ),
                deadline,
            ),
            matcher_subject=event_matcher_subject(
                public_input.event_type, trigger=trigger_name
            ),
        )
        return outcome.decision is GateDecision.PROCEED, outcome.reason

    async def execute_active(
        self,
        *,
        turn_id: str,
        model_call_index: int,
        inherited_memory_use_policy: MemoryUsePolicy,
        trigger: CompactionTrigger,
        force: bool,
        manual_request: ManualCompactionRequest | None,
        scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
        prepared_source: PreparedPrecompileCompaction | None = None,
        hook_scope: HookDispatchScopeRef | None = None,
        session_start_compact_port: SessionStartCompactPort | None = None,
        session_start_boundary_port: SessionStartCompactBoundaryPort | None = None,
    ) -> CompactionExecutionResult:
        return await self._execute_active(
            turn_id=turn_id,
            model_call_index=model_call_index,
            inherited_memory_use_policy=inherited_memory_use_policy,
            trigger=trigger,
            force=force,
            manual_request=manual_request,
            scope_kind=scope_kind,
            scope_subagent_task_id=scope_subagent_task_id,
            prepared_source=prepared_source,
            hook_scope=hook_scope,
            session_start_compact_port=session_start_compact_port,
            session_start_boundary_port=session_start_boundary_port,
        )

    async def prepare_precompile(
        self,
        *,
        turn_id: str,
        model_call_index: int,
        inherited_memory_use_policy: MemoryUsePolicy,
        trigger: CompactionTrigger,
        scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
        headroom_admission: PreparedProviderHeadroomAdmission,
    ) -> PrecompileCompactionDecision:
        owner = self._compaction_owner
        if owner is None:
            headroom_admission.close()
            return PrecompileCompactionDecision()
        try:
            scope = CompactionScope(
                session_id=self._writer_lease.guard.session_id,
                workspace_id=await self._resolved_workspace_id(),
                turn_id=turn_id,
                scope_kind=scope_kind,
                scope_subagent_task_id=scope_subagent_task_id,
            )
        except BaseException:
            headroom_admission.close()
            raise

        async def operation() -> PreparedPrecompileCompaction | None:
            deadline = monotonic() + owner.policy.planning_attempt_seconds
            dispatch = await self._provider_dispatch.prepare_compaction_source(
                turn_id=turn_id,
                model_call_index=model_call_index,
                inherited_memory_use_policy=inherited_memory_use_policy,
                deadline=deadline,
                existing_handle=headroom_admission.handle,
                headroom_preflight_override=headroom_admission.preflight,
                prepared_target_override=headroom_admission.prepared_target,
            )
            try:
                canonical_read = await self._io.run(
                    self._input_reader.read_frozen_compaction_cut,
                    dispatch.handle.cut,
                    deadline_monotonic=deadline,
                )
                if (
                    canonical_read.scope != scope
                    or canonical_read.turn_status != "RUNNING"
                ):
                    raise CompactionPlanningError(
                        "precompile compaction target changed at admission"
                    )
                preflight = headroom_admission.preflight
                canonical_range = canonical_read.safe_head_range
                if (
                    preflight.effective_materialization_lineage_floor
                    != canonical_range.effective_materialization_lineage_floor
                ):
                    raise CompactionPlanningError(
                        "compaction headroom quote differs from its frozen source"
                    )
                source_view = freeze_compaction_source_view(
                    canonical_read=canonical_read,
                    compile_binding=dispatch.prepared_call.compile_binding,
                    semantic_projection=dispatch.projection,
                    predecessor_epoch_view=dispatch.planning.predecessor_view,
                )
                if not should_trigger_compaction(
                    source_view=source_view,
                    policy=owner.policy,
                    force=False,
                ):
                    return None
                return PreparedPrecompileCompaction(
                    dispatch=dispatch,
                    canonical_read=canonical_read,
                    source_view=source_view,
                    attempt_token=CompactionAttemptToken(
                        scope.session_id,
                        turn_id,
                        scope_kind,
                        scope_subagent_task_id,
                        trigger,
                    ),
                )
            except BaseException:
                dispatch.handle.close()
                raise

        try:
            prepared = await owner.run_fenced(
                scope=scope,
                trigger=trigger,
                operation=operation,
            )
            if prepared is None:
                return PrecompileCompactionDecision(
                    ordinary_admission=headroom_admission
                )
            return PrecompileCompactionDecision(compaction=prepared)
        except (asyncio.CancelledError, StaleHostWriter):
            headroom_admission.close()
            raise
        except BaseException:
            headroom_admission.close()
            owner.record_automatic_failure(
                scope_kind=scope_kind,
                scope_subagent_task_id=scope_subagent_task_id,
            )
            return PrecompileCompactionDecision()

    async def _execute_active(
        self,
        *,
        turn_id: str,
        model_call_index: int,
        inherited_memory_use_policy: MemoryUsePolicy,
        trigger: CompactionTrigger,
        force: bool,
        manual_request: ManualCompactionRequest | None,
        scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
        prepared_source: PreparedPrecompileCompaction | None,
        hook_scope: HookDispatchScopeRef | None,
        session_start_compact_port: SessionStartCompactPort | None,
        session_start_boundary_port: SessionStartCompactBoundaryPort | None,
    ) -> CompactionExecutionResult:
        owner = self._compaction_owner
        if owner is None:
            if prepared_source is not None:
                prepared_source.close()
            return CompactionExecutionResult(
                CompactionOutcome(
                    CompactionDisposition.NOT_NEEDED,
                    turn_id,
                    None,
                    None,
                    "COMPACTION_DISABLED",
                )
            )
        if manual_request is not None and (
            manual_request.scope_kind is not scope_kind
            or manual_request.scope_subagent_task_id != scope_subagent_task_id
        ):
            raise RuntimeError("manual compaction belongs to another scope")
        scope_task_id = scope_subagent_task_id
        attempt_token = (
            prepared_source.attempt_token
            if prepared_source is not None
            else CompactionAttemptToken(
                self._writer_lease.guard.session_id,
                turn_id,
                scope_kind,
                scope_subagent_task_id,
                trigger,
            )
        )
        try:
            provisional_scope = CompactionScope(
                session_id=self._writer_lease.guard.session_id,
                workspace_id=await self._resolved_workspace_id(),
                turn_id=turn_id,
                scope_kind=scope_kind,
                scope_subagent_task_id=scope_task_id,
            )
        except BaseException:
            if prepared_source is not None:
                prepared_source.close()
            raise

        async def operation() -> CompactionExecutionResult:
            return await self._execute_compaction_fenced(
                turn_id=turn_id,
                model_call_index=model_call_index,
                inherited_memory_use_policy=inherited_memory_use_policy,
                trigger=trigger,
                force=force,
                expected_scope=provisional_scope,
                post_adoption_branch=CompactionTargetBranch.ACTIVE_INSTALLATION,
                stable_command_id=(
                    None if manual_request is None else manual_request.command_id
                ),
                prepared_source=prepared_source,
                attempt_token=attempt_token,
                pre_compact_dispatched=False,
                hook_scope=(
                    hook_scope
                    if hook_scope is not None
                    else self._hook_root_scope
                    if scope_kind is ModelInputScopeKind.ROOT
                    else None
                ),
                session_start_compact_port=session_start_compact_port,
                session_start_boundary_port=session_start_boundary_port,
            )

        try:
            outcome = await owner.run_fenced(
                scope=provisional_scope,
                trigger=trigger,
                operation=operation,
            )
        except _PostAdoptionCompactionFailure as failure:
            if manual_request is not None:
                await asyncio.shield(
                    owner.settle_manual(manual_request, failure.outcome)
                )
            raise failure.error.with_traceback(failure.error.__traceback__)
        except asyncio.CancelledError:
            if manual_request is not None:
                await asyncio.shield(
                    owner.settle_manual(
                        manual_request,
                        CompactionOutcome(
                            CompactionDisposition.FAILED,
                            turn_id,
                            None,
                            None,
                            "COMPACTION_CANCELLED",
                        ),
                    )
                )
            raise
        except StaleHostWriter:
            if manual_request is not None:
                await owner.settle_manual(
                    manual_request,
                    CompactionOutcome(
                        CompactionDisposition.FAILED,
                        turn_id,
                        None,
                        None,
                        "WRITER_REPLACED",
                    ),
                )
            raise
        except BaseException:
            execution = CompactionExecutionResult(
                CompactionOutcome(
                    CompactionDisposition.FAILED,
                    turn_id,
                    None,
                    None,
                    "COMPACTION_PLANNING_FAILED",
                )
            )
        else:
            execution = outcome
        finally:
            if prepared_source is not None:
                prepared_source.close()
        outcome = execution.outcome
        if manual_request is not None:
            await owner.settle_manual(manual_request, outcome)
        elif outcome.disposition is CompactionDisposition.FAILED and not (
            outcome.public_code or ""
        ).startswith("HOOK_PRE_COMPACT_BLOCKED"):
            owner.record_automatic_failure(
                scope_kind=scope_kind,
                scope_subagent_task_id=scope_task_id,
            )
        return execution

    def dispatch_crosses_threshold(self, dispatch: PreparedProviderDispatch) -> bool:
        owner = self._compaction_owner
        if owner is None or not owner.policy.automatic_enabled:
            return False
        compiled = dispatch.append_result.compiled_input
        budget = dispatch.prepared_call.compile_binding.effective_input_budget_tokens
        if compiled.final_estimate.total_input_tokens >= int(
            budget * owner.policy.auto_trigger_ratio
        ):
            return True
        canonical = dispatch.canonical_facts.canonical_input
        preflight = dispatch.compaction_headroom_preflight
        if preflight is not None:
            identity = canonical.identity
            if (
                preflight.session_id != identity.session_id
                or preflight.turn_id != identity.turn_id
                or preflight.context_binding_revision_id
                != identity.context_binding_revision_id
                or preflight.provider_input_through_sequence
                != identity.provider_input_through_sequence
                or preflight.scope_kind is not identity.conversation_scope_kind
                or preflight.scope_subagent_task_id != identity.scope_subagent_task_id
            ):
                raise RuntimeError(
                    "compaction headroom preflight differs from provider cut"
                )
            item_count = preflight.post_base_item_count
            canonical_bytes = preflight.post_base_canonical_utf8_bytes
        else:
            item_count = len(canonical.items)
            canonical_bytes = canonical.canonical_utf8_bytes
        return crosses_compaction_resource_headroom(
            post_base_item_count=item_count,
            post_base_canonical_utf8_bytes=canonical_bytes,
            continuity_epoch_logical_utf8_bytes=provider_input_logical_utf8_bytes(
                system_prompt=compiled.system_prompt,
                tools=compiled.tools,
                messages=compiled.messages,
            ),
        )

    async def _freeze_compaction_runtime_source(
        self,
        *,
        scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
        maximum_utf8_bytes: int,
    ) -> tuple[
        ContextSourceCandidate | ContextSourceAbsentFact,
        FrozenCompactionRuntimeHandoff | None,
    ]:
        handoff = await self._tools.freeze_compaction_runtime_handoff(
            conversation_scope_kind=scope_kind,
            scope_subagent_task_id=scope_subagent_task_id,
            maximum_utf8_bytes=maximum_utf8_bytes,
        )
        if handoff is None:
            source = build_compaction_context_source(
                kind=ContextSourceKind.COMPACTION_RUNTIME_HANDOFF,
                texts=None,
                absence_kind=ContextSourceAbsenceKind.EXPLICIT_EMPTY,
            )
        else:
            source = build_compaction_context_source(
                kind=ContextSourceKind.COMPACTION_RUNTIME_HANDOFF,
                texts=(handoff.full_text, handoff.compact_text),
                domain_identity=handoff.source_fingerprint,
            )
        return source, handoff

    async def _execute_compaction_fenced(
        self,
        *,
        turn_id: str,
        model_call_index: int,
        inherited_memory_use_policy: MemoryUsePolicy,
        trigger: CompactionTrigger,
        force: bool,
        expected_scope: CompactionScope,
        post_adoption_branch: CompactionTargetBranch,
        stable_command_id: str | None,
        maximum_retained_tool_groups: int | None = None,
        prepared_source: PreparedPrecompileCompaction | None = None,
        attempt_token: CompactionAttemptToken | None = None,
        pre_compact_dispatched: bool = False,
        hook_scope: HookDispatchScopeRef | None = None,
        session_start_compact_port: SessionStartCompactPort | None = None,
        session_start_boundary_port: SessionStartCompactBoundaryPort | None = None,
    ) -> CompactionExecutionResult:
        owner = self._compaction_owner
        if owner is None:
            raise RuntimeError("compaction lacks its Host owner")
        if attempt_token is None:
            attempt_token = CompactionAttemptToken(
                expected_scope.session_id,
                turn_id,
                expected_scope.scope_kind,
                expected_scope.scope_subagent_task_id,
                trigger,
            )
        deadline = monotonic() + owner.policy.planning_attempt_seconds
        if prepared_source is None:
            dispatch = await self._provider_dispatch.prepare_compaction_source(
                turn_id=turn_id,
                model_call_index=model_call_index,
                inherited_memory_use_policy=inherited_memory_use_policy,
                deadline=deadline,
                allow_terminal_compaction=(
                    post_adoption_branch is CompactionTargetBranch.IDLE_BASE_ONLY
                ),
            )
            try:
                compaction_read = await self._io.run(
                    self._input_reader.read_frozen_compaction_cut,
                    dispatch.handle.cut,
                    deadline_monotonic=deadline,
                )
                source_view = freeze_compaction_source_view(
                    canonical_read=compaction_read,
                    compile_binding=dispatch.prepared_call.compile_binding,
                    semantic_projection=dispatch.projection,
                    predecessor_epoch_view=dispatch.planning.predecessor_view,
                )
            except BaseException:
                dispatch.handle.close()
                raise
        else:
            if post_adoption_branch is not CompactionTargetBranch.ACTIVE_INSTALLATION:
                raise ValueError(
                    "precompile source is only valid for active compaction"
                )
            dispatch = prepared_source.dispatch
            compaction_read = prepared_source.canonical_read
            source_view = prepared_source.source_view
        dry_dispatch: PreparedProviderDispatch | None = None
        try:
            expected_statuses = (
                {"RUNNING"}
                if post_adoption_branch is CompactionTargetBranch.ACTIVE_INSTALLATION
                else {"COMPLETED", "INTERRUPTED"}
            )
            if (
                compaction_read.scope != expected_scope
                or compaction_read.turn_status not in expected_statuses
            ):
                raise CompactionPlanningError("compaction target changed at admission")
            if not should_trigger_compaction(
                source_view=source_view,
                policy=owner.policy,
                force=force,
            ):
                return CompactionExecutionResult(
                    CompactionOutcome(
                        CompactionDisposition.NOT_NEEDED,
                        turn_id,
                        None,
                        None,
                        "BELOW_TRIGGER",
                    )
                )
            if not pre_compact_dispatched:
                pre_compact_dispatched = True
                proceed, reason = await self._dispatch_pre_compact(
                    dispatch=dispatch,
                    scope=hook_scope,
                    attempt_token=attempt_token,
                    turn_id=turn_id,
                    trigger=trigger,
                    deadline=deadline,
                )
                if not proceed:
                    return CompactionExecutionResult(
                        CompactionOutcome(
                            CompactionDisposition.FAILED,
                            turn_id,
                            None,
                            None,
                            "HOOK_PRE_COMPACT_BLOCKED"
                            + (f":{reason}" if reason else ""),
                        )
                    )
            groups = enumerate_complete_tool_groups(compaction_read)
            semantic = None
            tail = None
            prefix = None
            recent = None
            selected_continuation: (
                tuple[
                    CompactionContinuationMode,
                    FrozenCompactionActiveRequest | None,
                ]
                | None
            ) = None
            selected_retained_count: int | None = None
            summary_call = self._model.resolve_compaction_summary_call(
                active_prepared_call=dispatch.prepared_call
            )
            summary_request = compaction_summary_request()
            maximum_retained = min(
                owner.policy.maximum_retained_tool_groups,
                len(groups),
            )
            if maximum_retained_tool_groups is not None:
                maximum_retained = min(
                    maximum_retained,
                    maximum_retained_tool_groups,
                )
            for retained_count in range(
                maximum_retained,
                -1,
                -1,
            ):
                if monotonic() >= deadline:
                    raise TimeoutError("compaction planning deadline expired")
                try:
                    candidate_tail, candidate_prefix = freeze_tail_and_prefix(
                        source_view=source_view,
                        complete_tool_groups=groups,
                        retained_group_count=retained_count,
                        source_projection=dispatch.projection.projected_input,
                        summary_request=summary_request,
                        deadline_monotonic=deadline,
                    )
                    canonical = (
                        compaction_read.dispatch_read.compile_snapshot.canonical_input
                    )
                    tail_range = freeze_compaction_canonical_range(
                        scope=compaction_read.scope,
                        effective_materialization_lineage_floor=(
                            candidate_tail.source_through_sequence
                        ),
                        source_through_sequence=(source_view.exact_safe_canonical_head),
                        ordered_items=canonical.items,
                        closures=canonical.closures,
                        late_outcomes=canonical.late_outcomes,
                    )
                    if tail_range.canonical_utf8_bytes > (
                        owner.policy.maximum_retained_tail_utf8_bytes
                    ):
                        continue
                    candidate_continuation = freeze_compaction_continuation(
                        source_view=source_view,
                        target_branch=post_adoption_branch,
                        source_through_sequence=(
                            candidate_prefix.source_through_sequence
                        ),
                    )
                    candidate_active_request = candidate_continuation[1]
                    candidate_recent = select_recent_human_messages(
                        canonical_read=compaction_read,
                        source_through_sequence=(
                            candidate_prefix.source_through_sequence
                        ),
                        policy=owner.policy,
                        excluded_entry_id=(
                            None
                            if candidate_active_request is None
                            else candidate_active_request.entry_id
                        ),
                    )
                    unavoidable_tokens = (
                        estimate_unavoidable_compaction_successor_tokens(
                            source_view=source_view,
                            tail=candidate_tail,
                            recent_user_messages=candidate_recent,
                            continuation_mode=candidate_continuation[0],
                            active_request=candidate_active_request,
                            deadline_monotonic=deadline,
                        )
                    )
                    validate_compaction_reclaim(
                        source_tokens=(
                            source_view.provider_projection.final_estimate.total_input_tokens
                        ),
                        successor_tokens=unavoidable_tokens,
                        hard_input_budget_tokens=(
                            source_view.normal_compile_binding.effective_input_budget_tokens
                        ),
                        policy=owner.policy,
                        force=force,
                        enforce_soft_target=True,
                    )
                    candidate_semantic = prepare_compaction_summary_semantic(
                        call=summary_call,
                        source_view=source_view,
                        source_projection=dispatch.projection.projected_input,
                        prefix_proof=candidate_prefix,
                        native_projection_set=(
                            dispatch.tool_exposure_plan.direct_projection_set
                        ),
                        summary_request=summary_request,
                    )
                except (CompactionPlanningError, ValueError):
                    continue
                semantic = candidate_semantic
                tail = candidate_tail
                prefix = candidate_prefix
                recent = candidate_recent
                selected_continuation = candidate_continuation
                selected_retained_count = retained_count
                break
            if (
                semantic is None
                or tail is None
                or prefix is None
                or recent is None
                or selected_continuation is None
                or selected_retained_count is None
            ):
                return CompactionExecutionResult(
                    CompactionOutcome(
                        CompactionDisposition.NOT_NEEDED,
                        turn_id,
                        None,
                        None,
                        "CONTEXT_ALREADY_COMPACT",
                    )
                )
            replay_hydration = await self._io.run(
                self._input_reader.hydrate_selected_provider_replays,
                dispatch_read=compaction_read.dispatch_read,
                compiled_input=semantic.compiled_input,
                replay_target=self._model.replay_target_for_resolved_call(summary_call),
                deadline_monotonic=deadline,
            )
            prepared_summary = finalize_compaction_summary_call(
                semantic,
                replay_hydration=replay_hydration,
            )
            owner.advance_phase(
                scope_kind=expected_scope.scope_kind,
                scope_subagent_task_id=expected_scope.scope_subagent_task_id,
                phase=CompactionAttemptPhase.STREAMING,
            )
            # The provider transport owns connect/write/read-idle watchdogs.
            # A progressing summary stream has no independent total deadline,
            # matching ordinary foreground model execution.
            raw = await prepared_summary.open_once()
            if raw.tool_calls:
                # Tool calls are never dispatched.  One independent repair is
                # permitted; it appends a provider-valid ephemeral denial group
                # to the exact first request rather than replaying that request.
                owner.advance_phase(
                    scope_kind=expected_scope.scope_kind,
                    scope_subagent_task_id=expected_scope.scope_subagent_task_id,
                    phase=CompactionAttemptPhase.REPAIRING,
                )
                repair_semantic = prepare_compaction_summary_repair_semantic(
                    semantic,
                    tool_calls=raw.tool_calls,
                )
                repair = finalize_compaction_summary_call(
                    repair_semantic,
                    replay_hydration=replay_hydration,
                    predecessor_summary_wire_plan=(prepared_summary.wire_input_plan),
                )
                raw = await repair.open_once()
                if raw.tool_calls:
                    raise CompactionPlanningError(
                        "summary model attempted tools after one repair"
                    )
            summary = freeze_compaction_summary_output(
                raw.text,
                maximum_utf8_bytes=(MAXIMUM_COMPLETED_ASSISTANT_MESSAGE_UTF8_BYTES),
            )
            owner.advance_phase(
                scope_kind=expected_scope.scope_kind,
                scope_subagent_task_id=expected_scope.scope_subagent_task_id,
                phase=CompactionAttemptPhase.VALIDATED,
            )
            # Successor capability/source planning is a new bounded operation;
            # it receives one fresh absolute deadline that is then shared by
            # dry assembly and the post-FULL exact re-read/install proof.
            successor_deadline = monotonic() + owner.policy.planning_attempt_seconds
            carrier = build_compaction_snapshot_carrier(
                summary=summary,
                recent_user_messages=tuple(item.text for item in recent),
                continuation_mode=selected_continuation[0],
                active_request=selected_continuation[1],
            )
            content = await self._content(
                carrier.body,
                deadline=self._canonical_deadline(),
                media_type=CONTEXT_SNAPSHOT_MEDIA_TYPE,
                codec=CONTEXT_SNAPSHOT_CODEC,
            )
            canonical = compaction_read.dispatch_read.compile_snapshot.canonical_input
            boundary_range = freeze_compaction_canonical_range(
                scope=compaction_read.scope,
                effective_materialization_lineage_floor=(
                    compaction_read.lineage_base.effective_materialization_lineage_floor
                ),
                source_through_sequence=prefix.source_through_sequence,
                ordered_items=canonical.items,
                closures=canonical.closures,
                late_outcomes=canonical.late_outcomes,
            )
            source_digest = canonical_compaction_range_digest(
                compaction_read.lineage_base, boundary_range
            )
            stable_suffix = (
                manual_compaction_stable_suffix(
                    session_id=expected_scope.session_id,
                    command_id=stable_command_id,
                )
                if stable_command_id is not None
                else _stable_id(
                    "compaction",
                    expected_scope.session_id,
                    expected_scope.turn_id,
                    trigger.value,
                    source_digest,
                ).rsplit(":", 1)[-1]
            )
            lineage = compaction_read.lineage_base
            candidate = build_prepared_compaction_canonical_adoption(
                CompactionCanonicalAdoptionFactoryInput(
                    scope=compaction_read.scope,
                    target_branch=post_adoption_branch,
                    expected_turn_status=compaction_read.turn_status,
                    predecessor=ExpectedCompactionPredecessorRevision(
                        binding_revision_id=lineage.binding_revision_id,
                        revision_ordinal=lineage.binding_revision_ordinal,
                        base_kind=(
                            "FULL_HISTORY"
                            if lineage.snapshot_id is None
                            else "SNAPSHOT"
                        ),
                        context_snapshot_id=lineage.snapshot_id,
                        source_through_sequence=(
                            lineage.persisted_revision_genesis_marker
                        ),
                    ),
                    snapshot_id=f"context-snapshot:{stable_suffix}",
                    binding_revision_id=f"context-binding:{stable_suffix}",
                    event_id=f"event:{stable_suffix}",
                    source_through_sequence=prefix.source_through_sequence,
                    source_digest=source_digest,
                    snapshot_content=content,
                    compiler_contract=COMPACTION_SNAPSHOT_COMPILER_CONTRACT,
                    prompt_contract=COMPACTION_SUMMARY_PROMPT_CONTRACT,
                    model_contract=COMPACTION_MODEL_CONTRACT,
                    occurred_at=datetime.now(timezone.utc),
                    actor_id=self._writer_lease.guard.writer_owner_id,
                )
            )
            preconditions = CompactionCanonicalWritePreconditions(
                scope=compaction_read.scope,
                expected_turn_status=compaction_read.turn_status,
                expected_safe_head=source_view.exact_safe_canonical_head,
                provider_safe=True,
            )
            synthetic_read = build_synthetic_compaction_dispatch_read(
                canonical_read=compaction_read,
                source_through_sequence=prefix.source_through_sequence,
                snapshot_id=candidate.snapshot.snapshot_id,
                binding_revision_id=candidate.binding.binding_revision_id,
                binding_revision_ordinal=candidate.binding.revision_ordinal,
                snapshot_body=carrier.body,
                snapshot_content_digest=content.digest,
                snapshot_content_size=content.size,
                snapshot_content_media_type=content.media_type,
                snapshot_content_codec=content.codec,
                snapshot_blob_id=getattr(content, "blob_id", None),
            )
            (
                runtime_source,
                _runtime_handoff,
            ) = await self._freeze_compaction_runtime_source(
                scope_kind=expected_scope.scope_kind,
                scope_subagent_task_id=(expected_scope.scope_subagent_task_id),
                maximum_utf8_bytes=(owner.policy.maximum_runtime_handoff_utf8_bytes),
            )
            owner.advance_phase(
                scope_kind=expected_scope.scope_kind,
                scope_subagent_task_id=(expected_scope.scope_subagent_task_id),
                phase=CompactionAttemptPhase.ADOPTION_PREPARED,
            )
            # Every compaction candidate crosses the same cold-base assembly
            # path. Lifecycle continuation is interpreted only after adoption.
            seed = CompactionContinuationSeed(
                dispatch_read=synthetic_read,
                binding_rewrite_identity=(candidate.binding.binding_revision_id),
                protected_tail_selection_fingerprint=(
                    tail.protected_tail_selection_fingerprint
                ),
            )
            try:
                old_bytes = compaction_read.dispatch_read.compile_snapshot.canonical_input.canonical_utf8_bytes
                new_bytes = (
                    synthetic_read.compile_snapshot.canonical_input.canonical_utf8_bytes
                )
                if new_bytes >= old_bytes:
                    raise CompactionReclaimUnavailable(
                        "compaction candidate does not reclaim canonical input"
                    )
                source_estimate = (
                    source_view.provider_projection.final_estimate.total_input_tokens
                )
                dry_dispatch = await self._provider_dispatch.prepare(
                    turn_id=turn_id,
                    model_call_index=model_call_index,
                    inherited_memory_use_policy=inherited_memory_use_policy,
                    deadline=successor_deadline,
                    allow_steers=False,
                    canonical_read_override=synthetic_read,
                    expected_source_read=compaction_read.dispatch_read,
                    force_empty_capability_predecessor=True,
                    cold_seed_override=seed,
                    existing_handle=dispatch.handle,
                    compaction_source_replacements=(runtime_source,),
                    compaction_retained_skill_read=compaction_read,
                    include_hook_context=False,
                )
                candidate_estimate = dry_dispatch.append_result.compiled_input.final_estimate.total_input_tokens
                target_budget = dry_dispatch.prepared_call.compile_binding.effective_input_budget_tokens
                validate_compaction_reclaim(
                    source_tokens=source_estimate,
                    successor_tokens=candidate_estimate,
                    hard_input_budget_tokens=target_budget,
                    policy=owner.policy,
                    force=force,
                    enforce_soft_target=True,
                )
            except (
                CompactionPlanningError,
                StructuredModelInputCompileError,
            ) as exc:
                if (
                    selected_retained_count <= 0
                    or not _can_retry_compaction_with_smaller_tail(exc)
                ):
                    if isinstance(exc, CompactionReclaimUnavailable):
                        return _already_compact_result(turn_id)
                    raise
                # Both lifecycle branches use the same candidate-shrink search.
                # Nothing has been canonically adopted, so discard exact dry
                # resources and re-freeze a current cut with one fewer retained
                # tool group. Physical summary calls are never replayed and no
                # tool is dispatched by this branch.
                if dry_dispatch is not None:
                    dry_dispatch.handle.close()
                    dry_dispatch.close_surface_borrow()
                    dry_dispatch = None
                dispatch.handle.close()
                return await self._execute_compaction_fenced(
                    turn_id=turn_id,
                    model_call_index=model_call_index,
                    inherited_memory_use_policy=inherited_memory_use_policy,
                    trigger=trigger,
                    force=force,
                    expected_scope=expected_scope,
                    post_adoption_branch=post_adoption_branch,
                    stable_command_id=stable_command_id,
                    maximum_retained_tool_groups=(selected_retained_count - 1),
                    attempt_token=attempt_token,
                    pre_compact_dispatched=pre_compact_dispatched,
                    hook_scope=hook_scope,
                    session_start_compact_port=session_start_compact_port,
                    session_start_boundary_port=session_start_boundary_port,
                )
            owner.advance_phase(
                scope_kind=expected_scope.scope_kind,
                scope_subagent_task_id=expected_scope.scope_subagent_task_id,
                phase=CompactionAttemptPhase.SETTLING,
            )
            settlement_dispatch = dry_dispatch
            dry_dispatch = None
            settlement = owner.start_settlement(
                self._complete_compaction_settlement(
                    turn_id=turn_id,
                    model_call_index=model_call_index,
                    inherited_memory_use_policy=inherited_memory_use_policy,
                    force=force,
                    expected_scope=expected_scope,
                    target_branch=post_adoption_branch,
                    successor_deadline=successor_deadline,
                    candidate=candidate,
                    preconditions=preconditions,
                    dry_dispatch=settlement_dispatch,
                    source_tokens=(
                        source_view.provider_projection.final_estimate.total_input_tokens
                    ),
                    protected_tail_selection_fingerprint=(
                        tail.protected_tail_selection_fingerprint
                    ),
                    compaction_read=compaction_read,
                    trigger=trigger,
                    attempt_token=attempt_token,
                    hook_scope=hook_scope,
                    hook_model_id=dispatch.prepared_call.call.target.fact.model_id,
                    hook_cwd=(
                        str(self._tools.snapshot_terminal_cwd())
                        if self._hook_dispatcher is not None
                        else ""
                    ),
                    session_start_compact_port=session_start_compact_port,
                    session_start_boundary_port=session_start_boundary_port,
                ),
                name=f"kernel-compaction-settlement:{candidate.snapshot.snapshot_id}",
            )
            try:
                return await asyncio.shield(settlement)
            except asyncio.CancelledError:
                await settlement
                raise
        finally:
            dispatch.handle.close()
            if dry_dispatch is not None:
                dry_dispatch.handle.close()
                dry_dispatch.close_surface_borrow()

    async def _complete_compaction_settlement(
        self,
        *,
        turn_id: str,
        model_call_index: int,
        inherited_memory_use_policy: MemoryUsePolicy,
        force: bool,
        expected_scope: CompactionScope,
        target_branch: CompactionTargetBranch,
        successor_deadline: float,
        candidate: PreparedCompactionCanonicalAdoption,
        preconditions: CompactionCanonicalWritePreconditions,
        dry_dispatch: PreparedProviderDispatch | None,
        source_tokens: int,
        protected_tail_selection_fingerprint: str,
        compaction_read: FrozenCompactionCanonicalRead,
        trigger: CompactionTrigger,
        attempt_token: CompactionAttemptToken,
        hook_scope: HookDispatchScopeRef | None,
        hook_model_id: str,
        hook_cwd: str,
        session_start_compact_port: SessionStartCompactPort | None,
        session_start_boundary_port: SessionStartCompactBoundaryPort | None,
    ) -> CompactionExecutionResult:
        """Drain canonical FULL and its exact process-local branch settlement."""

        owner = self._compaction_owner
        if owner is None:
            raise RuntimeError("compaction settlement lost its Host owner")
        continuity_scope = ProviderInputContinuityScope(
            session_id=expected_scope.session_id,
            scope_kind=expected_scope.scope_kind,
            scope_subagent_task_id=expected_scope.scope_subagent_task_id,
        )
        successor_dispatch: PreparedProviderDispatch | None = None
        adopted_outcome: CompactionOutcome | None = None
        try:
            confirmation = await self._settle_compaction_adoption(
                candidate=candidate,
                preconditions=preconditions,
            )
            if confirmation.kind is not CompactionConfirmationKind.FULL:
                raise ConversationKernelConflict("compaction adoption did not settle")
            adopted_outcome = CompactionOutcome(
                CompactionDisposition.COMPACTED,
                turn_id,
                candidate.snapshot.snapshot_id,
                confirmation.revision_ordinal,
                (
                    "COMPACTED_CONTINUATION_UNAVAILABLE"
                    if target_branch is CompactionTargetBranch.ACTIVE_INSTALLATION
                    else "COMPACTED_POST_ADOPTION_FAILURE"
                ),
            )
            if (
                expected_scope.scope_kind is ModelInputScopeKind.ROOT
                and session_start_boundary_port is not None
            ):
                await session_start_boundary_port.arm_compact_boundary(
                    attempt_token=attempt_token,
                    adopted_snapshot_id=candidate.snapshot.snapshot_id,
                    adopted_binding_revision_id=(candidate.binding.binding_revision_id),
                )
            post_proceed, post_reason = await self._dispatch_post_compact(
                model_id=hook_model_id,
                cwd=hook_cwd,
                scope=hook_scope,
                attempt_token=attempt_token,
                candidate=candidate,
                turn_id=turn_id,
                trigger=trigger,
                deadline=successor_deadline,
            )
            if target_branch is CompactionTargetBranch.ACTIVE_INSTALLATION:
                if dry_dispatch is None:
                    raise RuntimeError("active compaction lost its dry assembly")
                status = await self._io.run(
                    self._repository.read_turn_status,
                    session_id=expected_scope.session_id,
                    turn_id=turn_id,
                    deadline_monotonic=successor_deadline,
                )
                if status is not TurnStatus.RUNNING:
                    self._continuity.discard_scope(continuity_scope)
                    owner.reset_automatic_failures(
                        scope_kind=expected_scope.scope_kind,
                        scope_subagent_task_id=(expected_scope.scope_subagent_task_id),
                    )
                    return CompactionExecutionResult(
                        CompactionOutcome(
                            CompactionDisposition.COMPACTED,
                            turn_id,
                            candidate.snapshot.snapshot_id,
                            confirmation.revision_ordinal,
                            "HISTORICAL_COMPACTION_WINNER",
                        )
                    )
                if not post_proceed:
                    self._continuity.discard_scope(continuity_scope)
                    owner.reset_automatic_failures(
                        scope_kind=expected_scope.scope_kind,
                        scope_subagent_task_id=(expected_scope.scope_subagent_task_id),
                    )
                    return CompactionExecutionResult(
                        CompactionOutcome(
                            CompactionDisposition.COMPACTED,
                            turn_id,
                            candidate.snapshot.snapshot_id,
                            confirmation.revision_ordinal,
                            "COMPACTED_CONTINUATION_BLOCKED",
                        ),
                        active_continuation_blocked_reason=(
                            post_reason or "PostCompact Hook blocked continuation"
                        ),
                    )
                rotated = await self._io.run(
                    self._safe_point.rotate_provider_input,
                    dry_dispatch.handle,
                    turn_id=turn_id,
                    deadline_monotonic=successor_deadline,
                )
                dry_dispatch.close_surface_borrow()
                dry_dispatch = None
                actual_read = await self._provider_dispatch.read_dispatch_read(
                    rotated.cut, deadline=successor_deadline
                )
                actual_facts = actual_read.compile_snapshot
                actual_identity = actual_facts.canonical_input.identity
                actual_binding = actual_facts.context_binding_fact
                if (
                    actual_identity.turn_id != turn_id
                    or actual_identity.conversation_scope_kind
                    is not expected_scope.scope_kind
                    or actual_identity.scope_subagent_task_id
                    != expected_scope.scope_subagent_task_id
                    or actual_binding.binding_revision_id
                    != candidate.binding.binding_revision_id
                    or actual_binding.context_snapshot_id
                    != candidate.snapshot.snapshot_id
                ):
                    rotated.close()
                    raise ConversationKernelConflict(
                        "post-adoption cut does not name the adopted compaction winner"
                    )
                (
                    current_runtime_source,
                    _current_runtime_handoff,
                ) = await self._freeze_compaction_runtime_source(
                    scope_kind=expected_scope.scope_kind,
                    scope_subagent_task_id=(expected_scope.scope_subagent_task_id),
                    maximum_utf8_bytes=(
                        owner.policy.maximum_runtime_handoff_utf8_bytes
                    ),
                )
                prepared_base = await self._provider_dispatch.prepare(
                    turn_id=turn_id,
                    model_call_index=model_call_index,
                    inherited_memory_use_policy=inherited_memory_use_policy,
                    deadline=successor_deadline,
                    allow_steers=False,
                    canonical_read_override=actual_read,
                    expected_source_read=actual_read,
                    force_empty_capability_predecessor=True,
                    cold_seed_override=CompactionContinuationSeed(
                        dispatch_read=actual_read,
                        binding_rewrite_identity=(
                            candidate.binding.binding_revision_id
                        ),
                        protected_tail_selection_fingerprint=(
                            protected_tail_selection_fingerprint
                        ),
                    ),
                    existing_handle=rotated,
                    compaction_source_replacements=(current_runtime_source,),
                    compaction_retained_skill_read=compaction_read,
                    include_hook_context=False,
                )
                if not isinstance(prepared_base, PreparedProviderDispatch):
                    raise RuntimeError("final compaction base is not installable")
                dry_dispatch = prepared_base
                current_estimate = dry_dispatch.append_result.compiled_input.final_estimate.total_input_tokens
                current_budget = dry_dispatch.prepared_call.compile_binding.effective_input_budget_tokens
                validate_compaction_reclaim(
                    source_tokens=source_tokens,
                    successor_tokens=current_estimate,
                    hard_input_budget_tokens=current_budget,
                    policy=owner.policy,
                    force=force,
                    enforce_soft_target=True,
                )
                compact_start: PreparedCompactSessionStart | None = None
                if expected_scope.scope_kind is ModelInputScopeKind.ROOT:
                    if session_start_compact_port is not None:
                        compact_start = await session_start_compact_port(
                            PreparedCompactSessionStartFacts(
                                attempt_token=attempt_token,
                                adopted_snapshot_id=candidate.snapshot.snapshot_id,
                                adopted_binding_revision_id=(
                                    candidate.binding.binding_revision_id
                                ),
                                scope=expected_scope,
                                turn_id=turn_id,
                                model_id=(
                                    dry_dispatch.prepared_call.call.target.fact.model_id
                                ),
                                permission_snapshot=(
                                    dry_dispatch.canonical_facts.run_permission_snapshot
                                ),
                                deadline_monotonic=successor_deadline,
                            )
                        )
                        if not compact_start.proceed:
                            self._continuity.discard_scope(continuity_scope)
                            owner.reset_automatic_failures(
                                scope_kind=expected_scope.scope_kind,
                                scope_subagent_task_id=None,
                            )
                            return CompactionExecutionResult(
                                CompactionOutcome(
                                    CompactionDisposition.COMPACTED,
                                    turn_id,
                                    candidate.snapshot.snapshot_id,
                                    confirmation.revision_ordinal,
                                    "COMPACTED_CONTINUATION_BLOCKED",
                                ),
                                active_continuation_blocked_reason=(
                                    compact_start.reason
                                    or "compact SessionStart Hook blocked continuation"
                                ),
                            )
                elif session_start_compact_port is not None:
                    raise RuntimeError("child compaction received a ROOT start port")
                pending_start = (
                    None if compact_start is None else compact_start.reservation
                )
                try:
                    try:
                        hook_sibling = (
                            await self._provider_dispatch.prepare_hook_context_sibling(
                                dry_dispatch,
                                model_call_index=model_call_index,
                                deadline=successor_deadline,
                            )
                        )
                    except (
                        StructuredModelInputCompileError,
                        TimeoutError,
                        ValueError,
                    ):
                        hook_sibling = None
                    if hook_sibling is not None:
                        dry_dispatch = hook_sibling
                finally:
                    if pending_start is not None:
                        pending_start.retire()
                try:
                    installed_open = (
                        await self._provider_dispatch.install_provider_open(
                            dispatch=dry_dispatch,
                            turn_id=turn_id,
                            model_call_index=model_call_index,
                            deadline=successor_deadline,
                        )
                    )
                finally:
                    if dry_dispatch.hook_context_reservation is not None:
                        dry_dispatch.hook_context_reservation.retire()
                dry_dispatch = replace(
                    dry_dispatch,
                    installed_provider_open=installed_open,
                )
                successor_dispatch = dry_dispatch
                dry_dispatch = None
                owner.advance_phase(
                    scope_kind=expected_scope.scope_kind,
                    scope_subagent_task_id=(expected_scope.scope_subagent_task_id),
                    phase=CompactionAttemptPhase.ACTIVE_EPOCH_INSTALLED,
                )
            else:
                self._continuity.discard_scope(continuity_scope)
                owner.advance_phase(
                    scope_kind=expected_scope.scope_kind,
                    scope_subagent_task_id=(expected_scope.scope_subagent_task_id),
                    phase=CompactionAttemptPhase.IDLE_BASE_ADOPTED,
                )
            owner.reset_automatic_failures(
                scope_kind=expected_scope.scope_kind,
                scope_subagent_task_id=expected_scope.scope_subagent_task_id,
            )
            return CompactionExecutionResult(
                CompactionOutcome(
                    CompactionDisposition.COMPACTED,
                    turn_id,
                    candidate.snapshot.snapshot_id,
                    confirmation.revision_ordinal,
                    "COMPACTED",
                ),
                successor_dispatch=(
                    successor_dispatch
                    if target_branch is CompactionTargetBranch.ACTIVE_INSTALLATION
                    else None
                ),
            )
        except BaseException as error:
            if adopted_outcome is None:
                raise
            raise _PostAdoptionCompactionFailure(adopted_outcome, error) from error
        finally:
            if dry_dispatch is not None:
                dry_dispatch.handle.close()
                dry_dispatch.close_surface_borrow()

    async def compact_idle_turn(
        self,
        *,
        turn_id: str,
        command_id: str,
        force: bool,
        scope_kind: ModelInputScopeKind = ModelInputScopeKind.ROOT,
        scope_subagent_task_id: str | None = None,
        session_start_boundary_port: SessionStartCompactBoundaryPort | None = None,
    ) -> CompactionOutcome:
        """Compact the latest terminal exact-scope turn without a runner epoch."""

        owner = self._compaction_owner
        if owner is None or not owner.policy.manual_enabled:
            return CompactionOutcome(
                CompactionDisposition.FAILED,
                turn_id,
                None,
                None,
                "COMPACTION_DISABLED",
            )
        scope = CompactionScope(
            session_id=self._writer_lease.guard.session_id,
            workspace_id=await self._resolved_workspace_id(),
            turn_id=turn_id,
            scope_kind=scope_kind,
            scope_subagent_task_id=scope_subagent_task_id,
        )

        async def operation() -> CompactionExecutionResult:
            return await self._execute_compaction_fenced(
                turn_id=turn_id,
                model_call_index=1,
                inherited_memory_use_policy=MemoryUsePolicy.ENABLED,
                trigger=CompactionTrigger.MANUAL,
                force=force,
                expected_scope=scope,
                post_adoption_branch=CompactionTargetBranch.IDLE_BASE_ONLY,
                stable_command_id=command_id,
                hook_scope=(
                    self._hook_root_scope
                    if scope_kind is ModelInputScopeKind.ROOT
                    else None
                ),
                session_start_boundary_port=session_start_boundary_port,
            )

        try:
            execution = await owner.run_fenced(
                scope=scope,
                trigger=CompactionTrigger.MANUAL,
                operation=operation,
            )
            if execution.successor_dispatch is not None:
                execution.successor_dispatch.handle.close()
                execution.successor_dispatch.close_surface_borrow()
                raise RuntimeError("idle compaction produced an active successor")
            return execution.outcome
        except _PostAdoptionCompactionFailure as failure:
            return failure.outcome
        except asyncio.CancelledError:
            raise
        except StaleHostWriter:
            raise
        except BaseException:
            return CompactionOutcome(
                CompactionDisposition.FAILED,
                turn_id,
                None,
                None,
                "COMPACTION_FAILED",
            )

    async def _settle_compaction_adoption(
        self,
        *,
        candidate,
        preconditions: CompactionCanonicalWritePreconditions,
    ):
        delay = 0.05
        while True:
            try:
                confirmation = await self._io.run(
                    self._repository.confirm_context_snapshot_adoption,
                    candidate=candidate,
                    deadline_monotonic=self._canonical_deadline(),
                )
            except StaleHostWriter:
                raise
            except BaseException:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 0.5)
                continue
            if confirmation.kind is CompactionConfirmationKind.FULL:
                return confirmation
            if confirmation.kind is CompactionConfirmationKind.CONFLICT:
                raise ConversationKernelConflict(
                    "context compaction has a conflicting winner"
                )
            try:
                written = await self._io.run(
                    self._repository.adopt_context_snapshot,
                    self._writer_lease.guard,
                    candidate=candidate,
                    preconditions=preconditions,
                    deadline_monotonic=self._canonical_deadline(),
                )
            except StaleHostWriter:
                raise
            except BaseException:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 0.5)
                continue
            if written.kind is CompactionConfirmationKind.FULL:
                return written
            if written.kind is CompactionConfirmationKind.CONFLICT:
                raise ConversationKernelConflict(
                    "context compaction has a conflicting winner"
                )
            await asyncio.sleep(0)


__all__ = ["CompactionCoordinator", "CompactionExecutionResult"]
