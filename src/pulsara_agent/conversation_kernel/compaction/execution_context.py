"""Compaction hook dispatch and runtime handoff source capture."""

from __future__ import annotations

import asyncio
from time import monotonic
from pulsara_agent.conversation_kernel.context_sources import (
    build_compaction_context_source,
)
from pulsara_agent.conversation_kernel.compaction.contracts import (
    CompactionScope,
    CompactionTrigger,
    PreparedCompactionCanonicalAdoption,
)
from pulsara_agent.conversation_kernel.compaction.runtime_handoff import (
    FrozenCompactionRuntimeHandoff,
)
from pulsara_agent.conversation_kernel.provider_dispatch import (
    PreparedCompactionSourceDispatch,
)
from pulsara_agent.model_input.contracts import (
    ContextSourceAbsentFact,
    ContextSourceAbsenceKind,
    ContextSourceCandidate,
    ContextSourceKind,
)
from pulsara_agent.hooks.contracts import (
    GateDecision,
    HookDispatchEnvelope,
    HookDispatchScopeRef,
    PostCompactInput,
    PostCompactRef,
    PreCompactInput,
    PreCompactRef,
)
from pulsara_agent.hooks.matcher import event_matcher_subject
from pulsara_agent.conversation_kernel.compaction.execution_contracts import (
    CompactionAttemptToken,
)


class CompactionContextOperations:
    """Context capture operations on the coordinator's existing owners."""

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
            session_kind=scope.kind.value,
            input_origin=None,
            session_id=self._writer_lease.guard.session_id,
            cwd=str(self._tools.snapshot_workspace_root()),
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
            session_kind=scope.kind.value,
            input_origin=None,
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

    async def _freeze_compaction_runtime_source(
        self,
        *,
        scope: CompactionScope,
        deadline: float,
    ) -> tuple[
        ContextSourceCandidate | ContextSourceAbsentFact,
        FrozenCompactionRuntimeHandoff | None,
    ]:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("compaction runtime handoff deadline expired")
        handoff = await asyncio.wait_for(
            self._tools.freeze_compaction_runtime_handoff(
                conversation_scope_kind=scope.scope_kind,
                session_id=scope.session_id,
                work_turn_id=scope.turn_id,
            ),
            timeout=remaining,
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
