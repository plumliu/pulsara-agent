"""Child-only RunStart commit owner.

Host runs are committed by ``HostSession``. A subagent child has no Host safe
point, so its parent-owned child driver is the only child ``RunStart`` owner.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
import asyncio

from pulsara_agent.event import ContextWindowOpenedEvent, EventContext, RunStartEvent
from pulsara_agent.primitives.run_entry import DurableRunExistence
from pulsara_agent.primitives.run_lifecycle import RunStopReason
from pulsara_agent.runtime.run_entry import (
    AgentRunDraft,
    CommittedSubagentRunEntry,
    install_run_working_set,
    prepare_agent_run_draft,
)
from pulsara_agent.primitives.permission import (
    parse_permission_mode,
    preset_permission_policy_fact,
)
from pulsara_agent.primitives.run_boundary import PlanWorkflowStateFact
from pulsara_agent.runtime.session import (
    EventPublicationAfterCommitError,
)
from pulsara_agent.runtime.state import LoopState
from pulsara_agent.message import Msg
from pulsara_agent.runtime.run_entry import PreparedSubagentRunEntry

if TYPE_CHECKING:
    from pulsara_agent.runtime.agent import AgentRuntime


@dataclass(frozen=True, slots=True)
class CommittedSubagentRunEntryBundle:
    draft: AgentRunDraft
    committed: CommittedSubagentRunEntry
    stored_events: tuple[RunStartEvent | ContextWindowOpenedEvent, ...]


class SubagentRunEntryCommitUntrusted(RuntimeError):
    def __init__(
        self,
        *,
        durable_run_existence: DurableRunExistence,
        child_runtime_session_id: str,
        child_run_id: str,
    ) -> None:
        self.durable_run_existence = durable_run_existence
        self.child_runtime_session_id = child_runtime_session_id
        self.child_run_id = child_run_id
        super().__init__(
            "child RunStart commit requires reconciliation: "
            f"{durable_run_existence.value}"
        )


class SubagentRunEntryDriver:
    """The sole production committer for a child ``RunStartEvent``."""

    async def prepare_and_commit(
        self,
        *,
        child_agent: AgentRuntime,
        state: LoopState,
        prepared: PreparedSubagentRunEntry,
        prior_messages: list[Msg] | None = None,
    ) -> CommittedSubagentRunEntryBundle:
        draft = await prepare_agent_run_draft(
            child_agent,
            state,
            run_model_target=prepared.run_model_target,
            permission_snapshot=prepared.permission_snapshot,
            current_user_message=prepared.current_user_message,
            run_start_event_id=prepared.run_start_event_id,
            terminal_run_end_event_id=prepared.terminal_run_end_event_id,
            capability_basis=prepared.capability_basis.fact,
            frozen_execution_surface=prepared.frozen_execution_surface,
            new_run_boundary=None,
            subagent_run_entry=prepared.entry_fact,
            long_horizon=prepared.long_horizon,
            child_rollout_subaccount=prepared.child_rollout_subaccount,
            host_run_ingress=None,
            host_ingress_admission_proof=None,
            prior_messages=prior_messages,
        )
        candidate = draft.run_start_event
        if candidate.run_entry_kind != "subagent_child":
            raise RuntimeError("SubagentRunEntryDriver requires a child RunStart")
        entry = candidate.subagent_run_entry
        if entry is None:
            raise RuntimeError("child RunStart is missing SubagentRunEntryFact")
        original_error: BaseException | None = None
        window_open = ContextWindowOpenedEvent(
            id=prepared.long_horizon.contract.initial_window_open_event_id,
            **EventContext(
                run_id=state.run_id,
                turn_id=state.turn_id,
                reply_id=state.reply_id,
            ).event_fields(),
            window=prepared.long_horizon.initial_window,
            opening_batch_id=prepared.long_horizon.opening_batch_id,
        )
        candidates = (candidate, window_open)
        try:
            stored = tuple(
                await child_agent.runtime_session.emit_many(candidates, state=state)
            )
            publication_status = "completed"
        except EventPublicationAfterCommitError as exc:
            stored = exc.result.committed_events
            publication_status = "failed_after_commit"
            original_error = exc
        except BaseException as exc:
            outcome = child_agent.runtime_session.resolved_event_write_outcome(exc)
            if outcome.status == "unknown":
                raise SubagentRunEntryCommitUntrusted(
                    durable_run_existence=DurableRunExistence.UNKNOWN,
                    child_runtime_session_id=(
                        child_agent.runtime_session.runtime_session_id
                    ),
                    child_run_id=state.run_id,
                ) from exc
            if outcome.status == "none":
                child_agent.runtime_session.transcript_projection_checkpoint_service.discard_prepared_run_seed(
                    state.run_id
                )
                raise
            stored = outcome.committed_events
            publication_status = "failed_after_commit"
            original_error = exc
        business = tuple(event for event in stored if event.id in {item.id for item in candidates})
        if (
            len(business) != 2
            or not isinstance(business[0], RunStartEvent)
            or not isinstance(business[1], ContextWindowOpenedEvent)
        ):
            raise RuntimeError("child RunStart commit returned an invalid batch")
        run_start = business[0]
        if run_start.sequence is None:
            raise RuntimeError("child RunStart commit is missing sequence")
        committed = CommittedSubagentRunEntry(
            run_start_event=run_start,
            run_start_sequence=run_start.sequence,
            committed_through_sequence=stored[-1].sequence or run_start.sequence,
            publication_status=publication_status,
            subagent_run_id=entry.subagent_run_id,
        )
        child_agent.runtime_session.transcript_projection_checkpoint_service.adopt_committed_run_seed(
            run_start
        )
        install_run_working_set(
            state,
            committed,
            plan_snapshot=PlanWorkflowStateFact(
                workflow_id=None,
                active=False,
                pending_entry_audit=False,
                revision=0,
                entered_event_id=None,
                entered_event_sequence=None,
                entry_run_id=None,
                entry_turn_id=None,
                entry_reply_id=None,
                stored_default_permission=preset_permission_policy_fact(
                    parse_permission_mode(run_start.permission_mode)
                ),
                accepted_plan_artifact_id=None,
            ),
            capability_resolve_basis=prepared.capability_basis,
            frozen_execution_surface=prepared.frozen_execution_surface,
        )
        if original_error is not None:
            if isinstance(original_error, asyncio.CancelledError):
                current = asyncio.current_task()
                if current is not None:
                    current.uncancel()
            if state.stop_request is not None:
                await child_agent.abort_run(
                    state,
                    reason=state.stop_request.reason,
                )
            else:
                await child_agent.fail_committed_run(
                    state,
                    stop_reason=RunStopReason.RUNTIME_PUBLICATION_FAILURE,
                    error_message="child RunStart acknowledgement/publication failed",
                )
            raise original_error
        return CommittedSubagentRunEntryBundle(
            draft=draft,
            committed=committed,
            stored_events=(run_start, stored[1]),
        )


__all__ = [
    "CommittedSubagentRunEntryBundle",
    "SubagentRunEntryCommitUntrusted",
    "SubagentRunEntryDriver",
]
