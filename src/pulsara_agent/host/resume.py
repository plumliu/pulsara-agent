"""Conversation resume recovery helpers.

V1 resume reopens a durable runtime session in a new HostSession.  It cannot
recover in-process coroutines, terminal managers, or suspended LoopState, so any
durable run that was left ``running`` by a dead host must be terminalized before
the next prompt is rebuilt.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from psycopg.rows import dict_row

from pulsara_agent.event import (
    ContextWindowClosedEvent,
    RolloutBudgetAccountClosedEvent,
    RunEndEvent,
    RunStartEvent,
)
from pulsara_agent.event_log import PostgresEventLog
from pulsara_agent.memory.artifacts.postgres_archive import PostgresArtifactStore
from pulsara_agent.runtime.tool_artifacts import PostgresToolResultArtifactIndex
from pulsara_agent.llm.recovery import ModelStreamRecoveryService
from pulsara_agent.llm.control_recovery import (
    ModelCallControlDispositionRecoveryService,
)
from pulsara_agent.primitives.long_horizon import ContextWindowCloseReason
from pulsara_agent.primitives.run_lifecycle import (
    RunStopReason,
    RunTerminalizationKind,
)
from pulsara_agent.runtime.recovery import AbortKind
from pulsara_agent.runtime.long_horizon.rollout import apply_rollout_event
from pulsara_agent.runtime.mcp.recovery import (
    terminalize_reopened_mcp_input_required,
)
from pulsara_agent.runtime.session import RuntimeSession
from time import monotonic

from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane,
    VerifiedPostgresConnectionProviderProtocol,
)


RESUME_RECOVERED_STOP_REASON = "resume_recovered_interrupted"


@dataclass(frozen=True, slots=True)
class DanglingRunRepairResult:
    runtime_session_id: str
    repaired_run_ids: tuple[str, ...]
    skipped_run_ids: tuple[str, ...]
    projection_rows_updated: int
    recovered_model_call_ids: tuple[str, ...]
    recovered_model_control_call_ids: tuple[str, ...]

    @property
    def repaired_count(self) -> int:
        return len(self.repaired_run_ids)


async def repair_dangling_runs_for_resume(
    *,
    connection_provider: VerifiedPostgresConnectionProviderProtocol,
    runtime_session_id: str,
    workspace_root: str | None = None,
    deadline_monotonic: float,
) -> DanglingRunRepairResult:
    """Append host-teardown ``RUN_END`` events for canonical running runs.

    The repair is intentionally canonical-event-first.  Directly updating
    ``runs.status`` would make inspector summaries look tidy but would not give
    ``rebuild_prior_messages()`` the recovery fact it needs to filter unfinished
    tool calls and explain the interruption to the model.
    """

    _require_reopen_deadline(deadline_monotonic)
    log = PostgresEventLog(
        connection_provider=connection_provider,
        runtime_session_id=runtime_session_id,
        workspace_root=workspace_root,
    )
    archive = PostgresArtifactStore(connection_provider)
    model_recovery = ModelStreamRecoveryService(
        event_log=log,
        archive=archive,
    ).repair_incomplete_model_streams(deadline_monotonic=deadline_monotonic)
    recovered_model_call_ids = tuple(
        item.resolved_model_call_id for item in model_recovery.repaired
    )
    control_recovery = ModelCallControlDispositionRecoveryService(
        event_log=log,
        archive=archive,
    ).repair_missing_dispositions(deadline_monotonic=deadline_monotonic)
    recovered_model_control_call_ids = tuple(
        item.resolved_model_call_id for item in control_recovery.recovered
    )
    running = _running_runs_with_latest_context(
        connection_provider,
        runtime_session_id,
        deadline_monotonic=deadline_monotonic,
    )
    if not running:
        return DanglingRunRepairResult(
            runtime_session_id=runtime_session_id,
            repaired_run_ids=(),
            skipped_run_ids=(),
            projection_rows_updated=log.repair_run_projection(
                deadline_monotonic=deadline_monotonic
            ),
            recovered_model_call_ids=recovered_model_call_ids,
            recovered_model_control_call_ids=recovered_model_control_call_ids,
        )

    runtime_session = RuntimeSession(
        Path(workspace_root or "."),
        runtime_session_id=runtime_session_id,
        event_log=log,
        archive=archive,
        tool_result_artifacts=PostgresToolResultArtifactIndex(
            connection_provider
        ),
        reopen_deadline_monotonic=deadline_monotonic,
    )
    state_store = runtime_session.long_horizon_state_store
    repaired: list[str] = []
    skipped: list[str] = []
    try:
        for row in running:
            _require_reopen_deadline(deadline_monotonic)
            run_id = str(row["run_id"])
            turn_id = row.get("turn_id")
            reply_id = row.get("reply_id")
            if not isinstance(turn_id, str) or not isinstance(reply_id, str):
                skipped.append(run_id)
                continue
            # Idempotency guard: another resume/open may have repaired it after the
            # initial SELECT but before this append.
            if _run_has_end_event(
                connection_provider,
                runtime_session_id,
                run_id,
                deadline_monotonic=deadline_monotonic,
            ):
                skipped.append(run_id)
                continue
            starts = [
                event
                for event in log.iter(
                    run_id=run_id,
                    deadline_monotonic=deadline_monotonic,
                )
                if isinstance(event, RunStartEvent)
            ]
            if len(starts) != 1:
                skipped.append(run_id)
                continue
            started = starts[0]
            if started.child_rollout_subaccount is not None:
                raise RuntimeError(
                    "Host resume cannot terminalize a child-native runtime ledger"
                )
            active_mcp = (
                runtime_session.mcp_input_required_lifecycle_store.active_for_run(
                    run_id
                )
            )
            closure_reference = None
            if active_mcp:
                recovered_closure = (
                    await terminalize_reopened_mcp_input_required(
                        runtime_session,
                        run_id=run_id,
                        closure_reason="session_reopen_lease_unavailable",
                        deadline_monotonic=deadline_monotonic,
                    )
                )
                closure_reference = recovered_closure.closure_event_reference
            window_state = state_store.window_state(run_id)
            account = state_store.rollout_account(
                started.long_horizon.rollout_account_id
            )
            account_state = state_store.rollout_state(
                started.long_horizon.rollout_account_id
            )
            if (
                window_state is None
                or window_state.active_window_id is None
                or account is None
                or account_state is None
            ):
                raise RuntimeError(
                    "dangling run is missing its required window/account opening batch"
                )
            if account_state.active_reservations:
                raise RuntimeError(
                    "dangling run recovery requires reservation recovery before RunEnd"
                )
            window = window_state.windows[window_state.active_window_id]
            projection_state = state_store.projection_state(window.window_id)
            if projection_state is None:
                raise RuntimeError("dangling run is missing projection state")
            next_sequence = log.next_sequence(
                deadline_monotonic=deadline_monotonic
            )
            metadata = {
                "recovered_by": "resume",
                "resume_stop_reason": RESUME_RECOVERED_STOP_REASON,
            }
            window_close = ContextWindowClosedEvent(
                id=window.stable_close_event_id,
                run_id=run_id,
                turn_id=turn_id,
                reply_id=reply_id,
                window_id=window.window_id,
                window_generation=window.generation,
                close_reason=ContextWindowCloseReason.RECOVERED_INTERRUPTED,
                final_projection_generation=projection_state.projection_generation,
                final_projection_state_fingerprint=(
                    projection_state.state_semantic_fingerprint
                ),
                source_through_sequence=next_sequence - 1,
                next_window_id=None,
                compaction_terminal_event_id=None,
                metadata=metadata,
            )
            _, state_before_close = apply_rollout_event(
                account=account,
                state=account_state,
                event=window_close.model_copy(update={"sequence": next_sequence}),
            )
            assert state_before_close is not None
            account_close = RolloutBudgetAccountClosedEvent(
                id=f"rollout_budget_account_closed:{account.account_id}",
                run_id=run_id,
                turn_id=turn_id,
                reply_id=reply_id,
                account_id=account.account_id,
                final_state_fingerprint=state_before_close.state_fingerprint,
                charged_milliunits=state_before_close.charged_milliunits,
                model_call_count=state_before_close.model_call_count,
                tool_call_count=state_before_close.tool_call_count,
                active_reservation_count=0,
                run_end_event_id=started.terminal_run_end_event_id,
                metadata=metadata,
            )
            run_end = RunEndEvent(
                id=started.terminal_run_end_event_id,
                run_id=run_id,
                turn_id=turn_id,
                reply_id=reply_id,
                status="aborted",
                stop_reason=RunStopReason.ABORTED,
                terminalization_kind=RunTerminalizationKind.RECOVERED_INTERRUPTED,
                abort_kind=AbortKind.HOST_TEARDOWN.value,
                mcp_input_required_closure_event_reference=closure_reference,
                metadata=metadata,
            )
            result = await runtime_session.write_events_with_deadline(
                (window_close, account_close, run_end),
                deadline_monotonic=deadline_monotonic,
                expected_last_sequence=next_sequence - 1,
            )
            if (
                result.publication_status == "unavailable"
                or result.publication_errors
            ):
                raise RuntimeError(
                    "dangling run recovery publication is unavailable"
                )
            repaired.append(run_id)
    finally:
        runtime_session.close()

    return DanglingRunRepairResult(
        runtime_session_id=runtime_session_id,
        repaired_run_ids=tuple(repaired),
        skipped_run_ids=tuple(skipped),
        projection_rows_updated=log.repair_run_projection(
            deadline_monotonic=deadline_monotonic
        ),
        recovered_model_call_ids=recovered_model_call_ids,
        recovered_model_control_call_ids=recovered_model_control_call_ids,
    )


def _running_runs_with_latest_context(
    connection_provider: VerifiedPostgresConnectionProviderProtocol,
    runtime_session_id: str,
    *,
    deadline_monotonic: float,
) -> list[dict[str, Any]]:
    _require_reopen_deadline(deadline_monotonic)
    with connection_provider.connection(
        lane=PostgresConnectionLane.HOST_CONTROL,
        row_factory=dict_row,
        deadline_monotonic=deadline_monotonic,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                select
                    runs.id as run_id,
                    latest.turn_id,
                    latest.reply_id
                from runs
                left join lateral (
                    select turn_id, reply_id
                    from agent_events
                    where session_id = runs.session_id and run_id = runs.id
                    order by sequence desc
                    limit 1
                ) latest on true
                where runs.session_id = %s
                  and runs.status = 'running'
                  and not exists (
                    select 1
                    from agent_events as ended
                    where ended.session_id = runs.session_id
                      and ended.run_id = runs.id
                      and ended.event_type = 'RUN_END'
                  )
                order by runs.started_at asc, runs.id asc
                """,
                (runtime_session_id,),
            )
            return list(cursor.fetchall())


def _run_has_end_event(
    connection_provider: VerifiedPostgresConnectionProviderProtocol,
    runtime_session_id: str,
    run_id: str,
    *,
    deadline_monotonic: float,
) -> bool:
    _require_reopen_deadline(deadline_monotonic)
    with connection_provider.connection(
        lane=PostgresConnectionLane.HOST_CONTROL,
        deadline_monotonic=deadline_monotonic,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                select 1
                from agent_events
                where session_id = %s and run_id = %s and event_type = 'RUN_END'
                limit 1
                """,
                (runtime_session_id, run_id),
            )
            return cursor.fetchone() is not None


def _require_reopen_deadline(deadline_monotonic: float) -> None:
    if monotonic() >= deadline_monotonic:
        raise TimeoutError("Host reopen recovery deadline expired")
