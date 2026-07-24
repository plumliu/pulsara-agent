from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from tests.conftest import run_end_contract_fields
from tests.support.events import typed_non_transcript_event
from tests.support.runtime_session import in_memory_runtime_session

from pulsara_agent.event import (
    ContextCompactionRequestedEvent,
    EventContext,
    RunEndEvent,
)
from pulsara_agent.primitives.context import (
    ContextEventReferenceFact,
    context_fingerprint,
)
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.runtime_event_vocabulary import (
    ContextCompactionRequestFact,
    PublicationLatchedRunTerminationFact,
    build_runtime_event_deadline_budget,
    ordered_fingerprint_accumulator,
)
from pulsara_agent.runtime.context_input.event_slice import (
    event_reference_from_stored,
)
from pulsara_agent.runtime.mandatory_audit import RuntimeSessionMandatoryAuditOwner
from pulsara_agent.runtime._retry import bounded_none_retry_delay_seconds
from pulsara_agent.host.resume import repair_dangling_runs_for_resume
from pulsara_agent.runtime.publication_maintenance import (
    validate_publication_latched_run_termination_authority,
)
from pulsara_agent.runtime import (
    EventReconciliationRequired,
    EventWriteResult,
)


def test_publication_latch_rejects_before_projection_preparation(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_session = in_memory_runtime_session(tmp_path)
    projection_service = runtime_session.tool_terminal_projection_service
    preparation_called = False

    async def forbidden_preparation(self, events, *, deadline_monotonic=None):
        nonlocal preparation_called
        del events, deadline_monotonic
        assert self is projection_service
        preparation_called = True
        raise AssertionError("latched write reached projection preparation")

    monkeypatch.setattr(
        type(projection_service),
        "prepare_batch",
        forbidden_preparation,
    )
    runtime_session.latch_publication_reconciliation_required()

    async def write() -> None:
        await runtime_session.write_event_with_deadline(
            typed_non_transcript_event(label="publication-latch-preflight"),
            deadline_monotonic=time.monotonic() + 1.0,
        )

    with pytest.raises(
        EventReconciliationRequired,
        match="publication latch rejects ordinary runtime event mutation",
    ):
        asyncio.run(write())

    assert preparation_called is False


def test_accounted_physical_handoff_latches_critical_publication_unavailable(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_session = in_memory_runtime_session(tmp_path)
    terminal_reference = ContextEventReferenceFact(
        runtime_session_id=runtime_session.runtime_session_id,
        event_id="tool-result-end:critical-publication",
        sequence=1,
        event_type="TOOL_RESULT_END",
        payload_fingerprint=context_fingerprint(
            "test-critical-publication-tool-result:v1",
            "tool-result-end:critical-publication",
        ),
    )
    request = build_frozen_fact(
        ContextCompactionRequestFact,
        schema_version="context_compaction_request.v1",
        source="memory_hook_should_compact",
        safe_point="after_tool_results",
        basis_tool_result_terminal_event_references=(terminal_reference,),
        basis_event_ids_accumulator=ordered_fingerprint_accumulator(
            "context-compaction-request-basis:v1",
            (terminal_reference.event_id,),
        ),
    )
    critical_event = ContextCompactionRequestedEvent(
        id="context-compaction-requested:critical-publication",
        **EventContext(
            run_id="run:critical-publication",
            turn_id="turn:critical-publication",
            reply_id="reply:critical-publication",
        ).event_fields(),
        sequence=2,
        request=request,
    )
    write_result = EventWriteResult(
        committed_events=(critical_event,),
        commit_status="committed",
        reducer_high_waters={},
        reconciliation_required=False,
        reducer_errors=(),
        publication_status="unavailable",
        publisher_enqueued_through_sequence=None,
    )

    def confirmed_attempt(
        self,
        stored_events,
        *,
        catch_up_through_sequence,
        state,
        await_delivery,
    ):
        del catch_up_through_sequence, state, await_delivery
        assert self is runtime_session
        assert stored_events == (critical_event,)
        return SimpleNamespace(
            result=write_result,
            delivery_futures=(),
            published_events=(critical_event,),
        )

    monkeypatch.setattr(
        type(runtime_session),
        "_reconcile_confirmed_attempt",
        confirmed_attempt,
    )

    runtime_session._handoff_accounted_business_batch_attempt(
        stored_events=(critical_event,),
        business_events=(critical_event,),
        state=None,
        deadline_monotonic=time.monotonic() + 1.0,
    )

    assert runtime_session.publication_reconciliation_required is True


def test_mandatory_audit_owner_retires_completed_attempt() -> None:
    runtime_session_id = "runtime:test:mandatory-audit-cleanup"
    terminal_reference = ContextEventReferenceFact(
        runtime_session_id=runtime_session_id,
        event_id="tool-result-end:cleanup",
        sequence=1,
        event_type="TOOL_RESULT_END",
        payload_fingerprint=context_fingerprint("test-tool-result:v1", "cleanup"),
    )
    request = build_frozen_fact(
        ContextCompactionRequestFact,
        schema_version="context_compaction_request.v1",
        source="memory_hook_should_compact",
        safe_point="after_tool_results",
        basis_tool_result_terminal_event_references=(terminal_reference,),
        basis_event_ids_accumulator=ordered_fingerprint_accumulator(
            "context-compaction-request-basis:v1",
            (terminal_reference.event_id,),
        ),
    )
    candidate = ContextCompactionRequestedEvent(
        id="context-compaction-requested:cleanup",
        **EventContext(
            run_id="run:cleanup",
            turn_id="turn:cleanup",
            reply_id="reply:cleanup",
        ).event_fields(),
        request=request,
    )

    class SuccessfulRuntimeSession:
        def __init__(self, session_id: str) -> None:
            self.runtime_session_id = session_id

        async def write_event_with_deadline(self, event, **_kwargs):
            stored = event.model_copy(update={"sequence": 1})
            return EventWriteResult(
                committed_events=(stored,),
                commit_status="committed",
                reducer_high_waters={},
                reconciliation_required=False,
                reducer_errors=(),
                publication_status="completed",
                publisher_enqueued_through_sequence=1,
            )

        def resolved_event_write_outcome(self, error):
            raise AssertionError("unexpected write error") from error

        def latch_mandatory_audit_reconciliation_required(self):
            raise AssertionError("unexpected reconciliation latch")

    async def scenario() -> None:
        owner = RuntimeSessionMandatoryAuditOwner(
            SuccessfulRuntimeSession(runtime_session_id)
        )
        receipt = await owner.commit(
            candidate,
            deadline_budget=build_runtime_event_deadline_budget(
                admitted_at_monotonic=time.monotonic(),
                total_timeout_seconds=2.0,
                terminal_reserve_seconds=0.5,
            ),
        )
        assert receipt.status == "full"
        await asyncio.sleep(0)
        assert owner.pending_count == 0
        assert owner._attempts == {}

    asyncio.run(scenario())


def test_none_retry_backoff_is_positive_bounded_and_deadline_aware() -> None:
    now = time.monotonic()
    first = bounded_none_retry_delay_seconds(
        1,
        deadline_monotonic=now + 10.0,
        now_monotonic=now,
    )
    later = bounded_none_retry_delay_seconds(
        10,
        deadline_monotonic=now + 10.0,
        now_monotonic=now,
    )
    near_deadline = bounded_none_retry_delay_seconds(
        10,
        deadline_monotonic=now + 0.005,
        now_monotonic=now,
    )

    assert 0 < first < later <= 0.25
    assert 0 < near_deadline <= 0.005


def test_publication_latched_run_end_exactly_rebinds_source_reference() -> None:
    runtime_session_id = "runtime:test:publication-rebind"
    context = EventContext(
        run_id="run:publication-rebind",
        turn_id="turn:publication-rebind",
        reply_id="reply:publication-rebind",
    )
    source_reference = ContextEventReferenceFact(
        runtime_session_id=runtime_session_id,
        event_id="tool-result-end:publication-rebind",
        sequence=1,
        event_type="TOOL_RESULT_END",
        payload_fingerprint=context_fingerprint(
            "test-tool-result:v1",
            "publication-rebind",
        ),
    )
    request = build_frozen_fact(
        ContextCompactionRequestFact,
        schema_version="context_compaction_request.v1",
        source="memory_hook_should_compact",
        safe_point="after_tool_results",
        basis_tool_result_terminal_event_references=(source_reference,),
        basis_event_ids_accumulator=ordered_fingerprint_accumulator(
            "context-compaction-request-basis:v1",
            (source_reference.event_id,),
        ),
    )
    source = ContextCompactionRequestedEvent(
        id="context-compaction-requested:publication-rebind",
        **context.event_fields(),
        sequence=2,
        request=request,
    )
    exact_reference = event_reference_from_stored(
        source,
        runtime_session_id=runtime_session_id,
    )
    termination = build_frozen_fact(
        PublicationLatchedRunTerminationFact,
        schema_version="publication_latched_run_termination.v1",
        reason="mandatory_runtime_audit_publication_unavailable",
        source_event_references=(exact_reference,),
        source_events_accumulator=ordered_fingerprint_accumulator(
            "publication-latched-run-termination-sources:v1",
            (exact_reference.payload_fingerprint,),
        ),
    )
    run_end = RunEndEvent(
        **run_end_contract_fields(
            context.run_id,
            status="aborted",
            abort_kind="host_teardown",
        ),
        **context.event_fields(),
        sequence=3,
        status="aborted",
        stop_reason="aborted",
        abort_kind="host_teardown",
        publication_latched_termination=termination,
    )

    validate_publication_latched_run_termination_authority(
        run_end,
        runtime_session_id=runtime_session_id,
        resolve_event=lambda event_id: source if event_id == source.id else None,
    )
    conflicting = source.model_copy(update={"metadata": {"tampered": True}})
    with pytest.raises(ValueError, match="not exact"):
        validate_publication_latched_run_termination_authority(
            run_end,
            runtime_session_id=runtime_session_id,
            resolve_event=lambda event_id: (
                conflicting if event_id == source.id else None
            ),
        )


def test_host_reopen_rejects_expired_shared_deadline_before_database_io() -> None:
    class ForbiddenConnectionProvider:
        def connection(self, **_kwargs):
            raise AssertionError("expired reopen reached PostgreSQL")

    with pytest.raises(TimeoutError, match="Host reopen recovery deadline expired"):
        asyncio.run(
            repair_dangling_runs_for_resume(
                connection_provider=ForbiddenConnectionProvider(),
                runtime_session_id="runtime:test:expired-reopen",
                deadline_monotonic=time.monotonic() - 1.0,
            )
        )
