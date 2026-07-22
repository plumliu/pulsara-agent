from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from types import SimpleNamespace
from threading import Event, Thread
import time

import pytest

from pulsara_agent.event import (
    EventContext,
    RunErrorEvent,
    TerminalProcessCompletedEvent,
    TerminalProcessObservationDeliveryDispositionEvent,
)
from pulsara_agent.host.ingress import (
    HostIngressCapacityError,
    HostIngressCoordinator,
    HostIngressWaitingUserError,
)
from pulsara_agent.host.session import HostSession, HostSessionLifecycle
from pulsara_agent.primitives import context_fingerprint
from pulsara_agent.primitives.context_source import ContextArtifactReferenceFact
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.terminal_observation import (
    ArtifactTerminalObservationCoverageFact,
    InlineTerminalObservationCoverageFact,
    TerminalOutputCursorFact,
    TerminalProcessCompletionSemanticFact,
    TerminalProcessMonitorCompletionObservationSemanticFact,
    TerminalProcessMonitorDeliveryPolicyFact,
    TerminalProcessMonitorProgressLimiterStateFact,
    TerminalProcessMonitorProgressObservationSemanticFact,
    TerminalProcessObservationReceiptFact,
    TerminalProcessObservationSemanticFact,
    advance_progress_limiter,
    build_running_terminal_process_state,
    build_terminal_lifecycle_outcome,
    progress_limiter_decision,
    terminal_receipt_dominates_observation,
)
from pulsara_agent.runtime.terminal.monitor import (
    TERMINAL_MONITOR_CHECKPOINT_KIND,
    TERMINAL_MONITOR_CHECKPOINT_SCHEMA_VERSION,
    TerminalMonitorContractError,
    TerminalMonitorCoordinator,
    TerminalMonitorRecoveryBlocked,
    TerminalMonitorStore,
    _FiringOwner,
    default_monitor_conditions,
    default_monitor_delivery_policy,
    default_monitor_lifetime,
    initial_monitor_core_state,
    monitor_lifetime_expired,
    resulting_disposition_core_state,
    resulting_observation_core_state,
    resulting_receipt_core_state,
)
from pulsara_agent.runtime.context_input.event_slice import (
    event_reference_from_stored,
)
from pulsara_agent.runtime.terminal.notification import (
    HostIngressNotificationProjectionStore,
    TERMINAL_NOTIFICATION_CHECKPOINT_KIND,
    TERMINAL_NOTIFICATION_CHECKPOINT_SCHEMA_VERSION,
    TerminalNotificationAccountCoordinator,
)
from pulsara_agent.event_log import InMemoryEventLog, RawRuntimeProjectionCheckpoint
from pulsara_agent.runtime.event_write_service import PendingRuntimeEventWriteError
from pulsara_agent.runtime.session import RuntimeSession
from pulsara_agent.runtime.terminal.output import SanitizedOutputJournal
from pulsara_agent.runtime.terminal.process import (
    ProcessRegistry,
    snapshot_process_for_monitor_registration,
)
from pulsara_agent.runtime.terminal.ui_stream import (
    TerminalMonitorEventChannel,
    TerminalMonitorUIReconnectCursor,
)
from pulsara_agent.tools.base import ToolRuntimeContext


def _utc(seconds: int) -> str:
    return (
        (datetime(2026, 7, 21, tzinfo=timezone.utc) + timedelta(seconds=seconds))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _run(coro):
    return asyncio.run(coro)


def test_tm2_human_wins_when_human_and_runtime_cross_selection_barrier() -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        order: list[str] = []

        async def barrier() -> None:
            entered.set()
            await release.wait()

        coordinator = HostIngressCoordinator(
            host_session_id="host:test",
            permission_policy_fingerprint=context_fingerprint("permission:v1", {}),
            selection_barrier=barrier,
        )

        async def runner(owner) -> str:
            order.append(owner.kind)
            return owner.kind

        runtime = asyncio.create_task(
            coordinator.submit(kind="runtime", payload="runtime", runner=runner)
        )
        await entered.wait()
        human = asyncio.create_task(
            coordinator.submit(kind="human", payload="human", runner=runner)
        )
        await asyncio.sleep(0)
        release.set()

        assert await human == "human"
        assert await runtime == "runtime"
        assert order == ["human", "runtime"]
        await coordinator.finish_close()

    _run(scenario())


def test_tm2_queued_cancel_withdraws_but_preparing_cancel_only_detaches() -> None:
    async def scenario() -> None:
        coordinator = HostIngressCoordinator(
            host_session_id="host:test",
            permission_policy_fingerprint=context_fingerprint("permission:v1", {}),
        )
        entered = asyncio.Event()
        release = asyncio.Event()
        completed: list[str] = []

        async def blocking(owner) -> str:
            entered.set()
            await release.wait()
            completed.append(owner.ingress_id)
            return "done"

        preparing = asyncio.create_task(
            coordinator.submit(
                kind="human",
                payload="first",
                ingress_id="ingress:preparing",
                runner=blocking,
            )
        )
        await entered.wait()
        queued = asyncio.create_task(
            coordinator.submit(
                kind="runtime",
                payload="second",
                ingress_id="ingress:queued",
                runner=blocking,
            )
        )
        await asyncio.sleep(0)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        preparing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await preparing
        release.set()
        for _ in range(20):
            if completed:
                break
            await asyncio.sleep(0.01)
        assert completed == ["ingress:preparing"]
        await coordinator.finish_close()

    _run(scenario())


def test_tm2_waiting_user_durably_defers_instead_of_retrying_dispatch(
    monkeypatch,
) -> None:
    pending = SimpleNamespace(wake_chain_id="wake-chain:waiting-user")
    store = SimpleNamespace(
        pending_notifications=lambda **_kwargs: (pending,),
        autonomy_chain_snapshot=lambda _chain_id: SimpleNamespace(
            attribution=SimpleNamespace(
                resolved_policy=SimpleNamespace(
                    maximum_notifications_per_autonomous_ingress=8
                )
            )
        ),
    )
    deferred: list[tuple[tuple[object, ...], str]] = []

    async def record_defer(self, selected, *, reason):
        del self
        deferred.append((selected, reason))

    monkeypatch.setattr(
        HostSession,
        "_defer_terminal_notifications",
        record_defer,
    )
    session = object.__new__(HostSession)
    session.wiring = SimpleNamespace(
        runtime_wiring=SimpleNamespace(
            runtime_session=SimpleNamespace(
                terminal_notification_store=store,
                reconciliation_required=False,
            )
        )
    )
    session._lifecycle = HostSessionLifecycle.OPEN
    session._ingress_coordinator = SimpleNamespace(
        state_fact=lambda: SimpleNamespace(lifecycle_state="waiting_user")
    )
    session._terminal_notification_dispatch_error = None
    session._terminal_notification_dispatch_task = None

    _run(session._dispatch_terminal_notifications())

    assert deferred == [((pending,), "host_waiting_user")]
    assert session._terminal_notification_dispatch_error is None


def test_tm2_open_activation_dispatches_already_recovered_notification(
    monkeypatch,
) -> None:
    dispatches: list[HostSession] = []
    store = SimpleNamespace(
        pending_notifications=lambda **_kwargs: (SimpleNamespace(),)
    )

    def record_dispatch(self):
        dispatches.append(self)

    monkeypatch.setattr(
        HostSession,
        "_ensure_terminal_notification_dispatch",
        record_dispatch,
    )
    session = object.__new__(HostSession)
    session.wiring = SimpleNamespace(
        runtime_wiring=SimpleNamespace(
            runtime_session=SimpleNamespace(terminal_notification_store=store)
        )
    )
    session._terminal_notification_dispatch_enabled = False
    session._host_event_loop = None

    async def activate() -> None:
        session.activate_terminal_notification_dispatch_after_open()

    _run(activate())

    assert session._terminal_notification_dispatch_enabled is True
    assert dispatches == [session]


def test_tm2_capacity_rejection_precedes_ordinal_and_replan_keeps_ordinal() -> None:
    async def scenario() -> None:
        barrier_entered = asyncio.Event()
        barrier_release = asyncio.Event()

        async def barrier() -> None:
            barrier_entered.set()
            await barrier_release.wait()

        coordinator = HostIngressCoordinator(
            host_session_id="host:test",
            maximum_queued_ingress=1,
            permission_policy_fingerprint=context_fingerprint("permission:v1", 1),
            selection_barrier=barrier,
        )
        seen_ordinals: list[int] = []
        cleanup_count = 0

        async def cleanup(_owner) -> None:
            nonlocal cleanup_count
            cleanup_count += 1

        async def runner(owner) -> str:
            assert owner.accepted_ingress_ordinal is not None
            seen_ordinals.append(owner.accepted_ingress_ordinal)
            proof = coordinator.admission_proof(
                owner,
                ingress_fact_fingerprint=context_fingerprint("ingress:v1", "one"),
            )
            if owner.replan_count == 0:
                await coordinator.update_permission_policy(
                    context_fingerprint("permission:v1", 2)
                )
            coordinator.validate_precommit(owner, proof)
            return "committed"

        accepted = asyncio.create_task(
            coordinator.submit(
                kind="runtime",
                payload="one",
                runner=runner,
                replan_cleanup=cleanup,
            )
        )
        await barrier_entered.wait()
        with pytest.raises(HostIngressCapacityError):
            await coordinator.submit(kind="human", payload="overflow", runner=runner)
        barrier_release.set()
        assert await accepted == "committed"
        assert seen_ordinals == [1, 1]
        assert cleanup_count == 1
        await coordinator.finish_close()

    _run(scenario())


def test_tm2_waiting_user_accepts_only_matching_resume() -> None:
    async def scenario() -> None:
        coordinator = HostIngressCoordinator(
            host_session_id="host:test",
            permission_policy_fingerprint=context_fingerprint("permission:v1", {}),
        )

        async def suspend(_owner) -> str:
            await coordinator.mark_waiting_user(resume_match_key="resume:expected")
            return "waiting"

        assert (
            await coordinator.submit(kind="human", payload="ask", runner=suspend)
            == "waiting"
        )
        with pytest.raises(HostIngressWaitingUserError):
            await coordinator.submit(
                kind="human", payload="new", runner=lambda _owner: asyncio.sleep(0)
            )
        with pytest.raises(HostIngressWaitingUserError):
            await coordinator.submit(
                kind="resume",
                payload="wrong",
                resume_match_key="resume:wrong",
                runner=lambda _owner: asyncio.sleep(0),
            )
        assert (
            await coordinator.submit(
                kind="resume",
                payload="answer",
                resume_match_key="resume:expected",
                runner=lambda _owner: asyncio.sleep(0, result="resumed"),
            )
            == "resumed"
        )
        await coordinator.finish_close()

    _run(scenario())


def test_tm2_physical_commit_guard_linearizes_permission_revision() -> None:
    async def scenario() -> None:
        coordinator = HostIngressCoordinator(
            host_session_id="host:test",
            permission_policy_fingerprint=context_fingerprint("permission:v1", 1),
        )
        guard_entered = Event()
        release_guard = Event()
        order: list[str] = []

        async def runner(owner) -> str:
            ingress_fingerprint = context_fingerprint("ingress:v1", "guarded")
            proof = coordinator.admission_proof(
                owner,
                ingress_fact_fingerprint=ingress_fingerprint,
            )
            event = SimpleNamespace(
                host_ingress_admission_proof=proof,
                host_run_ingress=SimpleNamespace(fact_fingerprint=ingress_fingerprint),
            )

            def commit() -> None:
                with coordinator.run_start_commit_guard(event):
                    order.append("commit_entered")
                    guard_entered.set()
                    assert release_guard.wait(1)
                    order.append("commit_finished")

            writer = Thread(target=commit)
            writer.start()
            assert await asyncio.to_thread(guard_entered.wait, 1)

            def release_later() -> None:
                time.sleep(0.05)
                release_guard.set()

            releaser = Thread(target=release_later)
            releaser.start()
            await coordinator.update_permission_policy(
                context_fingerprint("permission:v1", 2)
            )
            order.append("permission_updated")
            writer.join(1)
            releaser.join(1)
            assert not writer.is_alive()
            return "done"

        assert (
            await coordinator.submit(
                kind="human",
                payload="guarded",
                runner=runner,
            )
            == "done"
        )
        assert order == [
            "commit_entered",
            "commit_finished",
            "permission_updated",
        ]
        await coordinator.finish_close()

    _run(scenario())


def test_tm5_monitor_permission_action_matrix_is_explicit() -> None:
    from pulsara_agent.capability.call_classifier import (
        TERMINAL_MONITOR_OBSERVE_ACTIONS,
        TERMINAL_PROCESS_OBSERVE_ACTIONS,
    )
    from pulsara_agent.runtime.permission import (
        TERMINAL_MONITOR_READ_ONLY_ACTIONS,
        TERMINAL_PROCESS_READ_ONLY_ACTIONS,
    )

    assert TERMINAL_PROCESS_OBSERVE_ACTIONS == {"list", "log", "poll", "wait"}
    assert TERMINAL_PROCESS_READ_ONLY_ACTIONS == {"list", "log", "poll", "wait"}
    assert TERMINAL_MONITOR_OBSERVE_ACTIONS == {"list"}
    assert TERMINAL_MONITOR_READ_ONLY_ACTIONS == {"list"}


def _monitor_registration(journal: SanitizedOutputJournal):
    from pulsara_agent.primitives.terminal_observation import (
        TerminalProcessMonitorPolicyFact,
        TerminalProcessMonitorRegistrationSemanticFact,
    )

    policy = build_frozen_fact(
        TerminalProcessMonitorPolicyFact,
        schema_version="terminal_process_monitor_policy.v1",
        conditions=default_monitor_conditions(
            min_new_output_chars=1,
            quiet_period_ms=0,
            heartbeat_interval_seconds=None,
        ),
        delivery=default_monitor_delivery_policy(),
        lifetime=default_monitor_lifetime(),
    )
    return build_frozen_fact(
        TerminalProcessMonitorRegistrationSemanticFact,
        schema_version="terminal_process_monitor_registration_semantic.v1",
        monitor_id="monitor:test",
        initial_baseline_cursor=journal.initial_cursor,
        policy=policy,
    )


def _progress_observation(*, delta, ordinal: int = 1):
    return build_frozen_fact(
        TerminalProcessMonitorProgressObservationSemanticFact,
        schema_version="terminal_process_monitor_progress_observation_semantic.v1",
        monitor_id="monitor:test",
        observation_kind="output_progress",
        observation_ordinal=ordinal,
        process_state=build_running_terminal_process_state(),
        output_authority=delta,
    )


def _receipt(*, start, end, coverage, observed_state=None):
    semantic = build_frozen_fact(
        TerminalProcessObservationSemanticFact,
        schema_version="terminal_process_observation_semantic.v1",
        requested_start_cursor=start,
        observed_start_cursor=start,
        observed_end_cursor=end,
        output_coverage=coverage,
        observed_state=observed_state or build_running_terminal_process_state(),
    )
    return build_frozen_fact(
        TerminalProcessObservationReceiptFact,
        schema_version="terminal_process_observation_receipt.v1",
        observation_semantic=semantic,
        action_kind="log",
        origin_tool_call_id="call:log",
        completion_event_reference=None,
    )


def test_tm1_terminal_supersede_uses_consumed_cursor_not_observation_cursor() -> None:
    journal = SanitizedOutputJournal(process_id="process:test")
    registration = _monitor_registration(journal)
    before = initial_monitor_core_state(registration)
    journal.append(b"a" * 100)
    progress_delta, _ = journal.snapshot_since(
        before.last_consumed_cursor, max_chars=200
    )
    progress = _progress_observation(delta=progress_delta)
    pending = resulting_observation_core_state(
        before=before,
        observation=progress,
        observed_at_utc=_utc(0),
        delivery_policy=registration.policy.delivery,
    )
    assert pending.last_observation_cursor.sanitized_char_offset == 100
    assert pending.last_consumed_cursor.sanitized_char_offset == 0

    journal.append(b"b" * 50)
    final_delta, _ = journal.snapshot_since(
        pending.last_consumed_cursor,
        max_chars=200,
    )

    assert final_delta.requested_start_cursor.sanitized_char_offset == 0
    assert final_delta.end_cursor.sanitized_char_offset == 150
    assert final_delta.output_preview == "a" * 100 + "b" * 50


def test_tm3_receipt_requires_full_visible_interval_and_accepts_exact_artifact() -> (
    None
):
    journal = SanitizedOutputJournal(process_id="process:test")
    journal.append(b"a" * 100)
    pending_delta, _ = journal.snapshot_since(journal.initial_cursor, max_chars=200)
    pending = _progress_observation(delta=pending_delta)
    journal.append(b"b" * 10)
    end = journal.end_cursor
    tail_start = build_frozen_fact(
        TerminalOutputCursorFact,
        schema_version="terminal_output_cursor.v1",
        stream_identity=end.stream_identity,
        sanitized_char_offset=90,
        sanitized_utf8_byte_offset=90,
        canonical_prefix_sha256=f"sha256:{sha256(('a' * 90).encode()).hexdigest()}",
        sanitizer_contract_fingerprint=end.sanitizer_contract_fingerprint,
    )
    tail_coverage = build_frozen_fact(
        InlineTerminalObservationCoverageFact,
        schema_version="inline_terminal_observation_coverage.v1",
        covered_start_cursor=tail_start,
        covered_end_cursor=end,
        visible_content_sha256=f"sha256:{sha256(('a' * 10 + 'b' * 10).encode()).hexdigest()}",
    )
    assert not terminal_receipt_dominates_observation(
        receipt=_receipt(start=tail_start, end=end, coverage=tail_coverage),
        pending=pending,
    )

    full_coverage = build_frozen_fact(
        InlineTerminalObservationCoverageFact,
        schema_version="inline_terminal_observation_coverage.v1",
        covered_start_cursor=journal.initial_cursor,
        covered_end_cursor=end,
        visible_content_sha256=f"sha256:{sha256(('a' * 100 + 'b' * 10).encode()).hexdigest()}",
    )
    assert terminal_receipt_dominates_observation(
        receipt=_receipt(
            start=journal.initial_cursor,
            end=end,
            coverage=full_coverage,
        ),
        pending=pending,
    )

    artifact_ref = build_frozen_fact(
        ContextArtifactReferenceFact,
        schema_version="context_artifact_reference.v1",
        artifact_id="artifact:terminal:test",
        media_type="text/plain; charset=utf-8",
        content_sha256=f"sha256:{sha256(('a' * 100 + 'b' * 10).encode()).hexdigest()}",
        content_bytes=110,
        artifact_contract_fingerprint=context_fingerprint("artifact-contract:v1", {}),
    )
    artifact_coverage = build_frozen_fact(
        ArtifactTerminalObservationCoverageFact,
        schema_version="artifact_terminal_observation_coverage.v1",
        covered_start_cursor=journal.initial_cursor,
        covered_end_cursor=end,
        artifact_reference=artifact_ref,
        covered_range_content_sha256=artifact_ref.content_sha256,
        artifact_codec_contract_fingerprint=context_fingerprint("codec:v1", "utf-8"),
    )
    assert terminal_receipt_dominates_observation(
        receipt=_receipt(
            start=journal.initial_cursor,
            end=end,
            coverage=artifact_coverage,
        ),
        pending=pending,
    )


def test_tm3_receipt_advances_pending_monitor_without_losing_covered_prefix() -> None:
    journal = SanitizedOutputJournal(process_id="process:test")
    registration = _monitor_registration(journal)
    before = initial_monitor_core_state(registration)
    journal.append(b"a" * 100)
    progress_delta, _ = journal.snapshot_since(
        before.last_consumed_cursor, max_chars=200
    )
    progress = _progress_observation(delta=progress_delta)
    pending = resulting_observation_core_state(
        before=before,
        observation=progress,
        observed_at_utc=_utc(0),
        delivery_policy=registration.policy.delivery,
    )

    journal.append(b"b" * 10)
    end = journal.end_cursor
    coverage = build_frozen_fact(
        InlineTerminalObservationCoverageFact,
        schema_version="inline_terminal_observation_coverage.v1",
        covered_start_cursor=journal.initial_cursor,
        covered_end_cursor=end,
        visible_content_sha256=(
            f"sha256:{sha256(('a' * 100 + 'b' * 10).encode()).hexdigest()}"
        ),
    )
    receipt = _receipt(
        start=journal.initial_cursor,
        end=end,
        coverage=coverage,
    )
    receipt_applied = resulting_receipt_core_state(
        before=pending,
        receipt=receipt,
        pending=progress,
    )

    assert receipt_applied.last_observation_cursor == end
    assert receipt_applied.last_consumed_cursor == end
    assert receipt_applied.last_committed_observation_ordinal == 1
    assert receipt_applied.lifecycle_state == "active_pending_delivery"

    delivered = resulting_disposition_core_state(
        before=receipt_applied,
        observation=progress,
        delivery_policy=registration.policy.delivery,
        consumed_through_cursor=end,
    )
    assert delivered.last_observation_cursor == end
    assert delivered.last_consumed_cursor == end
    assert delivered.lifecycle_state == "active_ready"


def test_tm3_receipt_without_pending_observation_advances_durable_baseline() -> None:
    journal = SanitizedOutputJournal(process_id="process:test")
    registration = _monitor_registration(journal)
    before = initial_monitor_core_state(registration)
    journal.append(b"already observed")
    end = journal.end_cursor
    coverage = build_frozen_fact(
        InlineTerminalObservationCoverageFact,
        schema_version="inline_terminal_observation_coverage.v1",
        covered_start_cursor=journal.initial_cursor,
        covered_end_cursor=end,
        visible_content_sha256=(f"sha256:{sha256(b'already observed').hexdigest()}"),
    )

    after = resulting_receipt_core_state(
        before=before,
        receipt=_receipt(
            start=journal.initial_cursor,
            end=end,
            coverage=coverage,
        ),
        pending=None,
    )

    assert after.state_revision == before.state_revision + 1
    assert after.last_observation_cursor == end
    assert after.last_consumed_cursor == end
    assert after.last_committed_observation_ordinal == 0
    assert after.lifecycle_state == "active_ready"


def test_tm3_sequential_completion_heads_reuse_one_durable_slot() -> None:
    runtime_session_id = "runtime:slot-reuse"
    store = HostIngressNotificationProjectionStore(
        runtime_session_id=runtime_session_id,
        maximum_completion_process_heads=1,
    )
    coordinator = TerminalNotificationAccountCoordinator(
        runtime_session_id=runtime_session_id,
        store=store,
    )
    sequence = 0

    for index in range(4):
        process_id = f"process:slot-reuse:{index}"
        journal = SanitizedOutputJournal(process_id=process_id)
        process = SimpleNamespace(process_id=process_id, output=journal)
        prepared = coordinator.prepare_completion_reservation(
            process=process,
            tool_result_end_event_id=f"tool_result_end:slot-reuse:{index}",
        )
        cause = RunErrorEvent(
            id=f"slot-reuse-cause:{index}",
            run_id=f"run:slot-reuse:{index}",
            turn_id=f"turn:slot-reuse:{index}",
            reply_id=f"reply:slot-reuse:{index}",
            message="slot reuse fixture",
            code="slot_reuse_fixture",
        )
        created = coordinator.freeze_created_event(
            prepared=prepared,
            cause_events=(cause,),
        ).model_copy(update={"sequence": sequence + 1})
        store.apply_committed((created,))

        outcome = build_terminal_lifecycle_outcome(
            status="success",
            exit_code=0,
            kill_reason=None,
        )
        completion_semantic = build_frozen_fact(
            TerminalProcessCompletionSemanticFact,
            schema_version="terminal_process_completion_semantic.v1",
            terminal_output_cursor=journal.end_cursor,
            outcome=outcome,
        )
        completion = TerminalProcessCompletedEvent(
            id=f"terminal_process_completed:slot-reuse:{index}",
            run_id=cause.run_id,
            turn_id=cause.turn_id,
            reply_id=cause.reply_id,
            completion_semantic=completion_semantic,
            terminal_session_id="default",
            command="true",
            cwd="/tmp",
            duration_seconds=0,
            output_recovery_reference=journal.recovery_reference(),
            owner_host_session_id="host:slot-reuse",
            owner_conversation_id="conversation:slot-reuse",
            origin_runtime_session_id=runtime_session_id,
            origin_run_entry_kind="host_main_run",
        ).model_copy(update={"sequence": sequence + 2})
        store.apply_committed((completion,))

        disposition = TerminalProcessObservationDeliveryDispositionEvent(
            id=f"terminal_notification_disposition:slot-reuse:{index}",
            run_id=cause.run_id,
            turn_id=cause.turn_id,
            reply_id=cause.reply_id,
            observation_source_references=(
                event_reference_from_stored(
                    completion,
                    runtime_session_id=runtime_session_id,
                ),
            ),
            outcome="session_closed",
        ).model_copy(update={"sequence": sequence + 3})
        store.apply_committed((disposition,))
        release = coordinator.freeze_released_event(
            reservation_id=prepared.reservation.reservation_id,
            cause_events=(disposition,),
        ).model_copy(update={"sequence": sequence + 4})
        store.apply_committed((release,))
        coordinator.on_committed((release,))
        sequence += 4

        assert store.account_snapshot().active_completion_reservations == ()
        assert store.projection_snapshot().process_heads == ()
        journal.close(destroy_spool=True)


def test_tm3_projection_checkpoints_restore_without_ledger_iteration() -> None:
    class NoFullScanEventLog(InMemoryEventLog):
        def iter(self, **_kwargs):
            raise AssertionError(
                "terminal projection restore must not iterate the ledger"
            )

    runtime_session_id = "runtime:checkpoint-restore"
    event_log = NoFullScanEventLog(runtime_session_id=runtime_session_id)
    notification = HostIngressNotificationProjectionStore(
        runtime_session_id=runtime_session_id
    )
    monitor = TerminalMonitorStore(runtime_session_id=runtime_session_id)

    for kind, version, payload, through in (
        (
            TERMINAL_NOTIFICATION_CHECKPOINT_KIND,
            TERMINAL_NOTIFICATION_CHECKPOINT_SCHEMA_VERSION,
            notification.checkpoint_payload(),
            notification.through_sequence,
        ),
        (
            TERMINAL_MONITOR_CHECKPOINT_KIND,
            TERMINAL_MONITOR_CHECKPOINT_SCHEMA_VERSION,
            monitor.checkpoint_payload(),
            monitor.through_sequence,
        ),
    ):
        json_round_tripped_payload = json.loads(json.dumps(payload))
        ledger_prefix = event_log.read_raw_ledger_prefix(through_sequence=through)
        event_log.write_runtime_projection_checkpoint(
            RawRuntimeProjectionCheckpoint(
                projection_kind=kind,
                through_sequence=through,
                projection_schema_version=version,
                ledger_prefix=ledger_prefix,
                validation_base_through_sequence=0,
                validation_base_state_payload=json_round_tripped_payload,
                state_payload=json_round_tripped_payload,
                payload_fingerprint=RuntimeSession._runtime_projection_checkpoint_fingerprint(
                    projection_kind=kind,
                    through_sequence=through,
                    projection_schema_version=version,
                    ledger_prefix=ledger_prefix,
                    validation_base_through_sequence=0,
                    validation_base_state_payload=json_round_tripped_payload,
                    state_payload=json_round_tripped_payload,
                ),
            )
        )

    session = object.__new__(RuntimeSession)
    session.event_log = event_log
    session.runtime_session_id = runtime_session_id
    deadline = time.monotonic() + 3.0

    restored_notification = session._restore_terminal_notification_projection(
        deadline_monotonic=deadline
    )
    restored_monitor = session._restore_terminal_monitor_projection(
        deadline_monotonic=deadline
    )

    assert (
        restored_notification.checkpoint_payload() == notification.checkpoint_payload()
    )
    assert restored_monitor.checkpoint_payload() == monitor.checkpoint_payload()


def test_tm3_projection_checkpoint_cannot_skip_committed_reservation() -> None:
    runtime_session_id = "runtime:checkpoint-forgery"
    event_log = InMemoryEventLog(runtime_session_id=runtime_session_id)
    base_store = HostIngressNotificationProjectionStore(
        runtime_session_id=runtime_session_id
    )
    coordinator = TerminalNotificationAccountCoordinator(
        runtime_session_id=runtime_session_id,
        store=base_store,
    )
    journal = SanitizedOutputJournal(process_id="process:checkpoint-forgery")
    process = SimpleNamespace(
        process_id="process:checkpoint-forgery",
        output=journal,
    )
    prepared = coordinator.prepare_completion_reservation(
        process=process,
        tool_result_end_event_id="tool_result_end:checkpoint-forgery",
    )
    cause = RunErrorEvent(
        id="run_error:checkpoint-forgery",
        run_id="run:checkpoint-forgery",
        turn_id="turn:checkpoint-forgery",
        reply_id="reply:checkpoint-forgery",
        message="checkpoint forgery fixture",
        code="checkpoint_forgery_fixture",
    )
    reservation = event_log.append(
        coordinator.freeze_created_event(
            prepared=prepared,
            cause_events=(cause,),
        )
    )
    through_sequence = reservation.sequence or 0
    forged_store = HostIngressNotificationProjectionStore(
        runtime_session_id=runtime_session_id
    )
    forged_store.through_sequence = through_sequence
    forged_payload = forged_store.checkpoint_payload()
    base_payload = base_store.checkpoint_payload()
    ledger_prefix = event_log.read_raw_ledger_prefix(through_sequence=through_sequence)
    checkpoint = RawRuntimeProjectionCheckpoint(
        projection_kind=TERMINAL_NOTIFICATION_CHECKPOINT_KIND,
        through_sequence=through_sequence,
        projection_schema_version=(TERMINAL_NOTIFICATION_CHECKPOINT_SCHEMA_VERSION),
        ledger_prefix=ledger_prefix,
        validation_base_through_sequence=0,
        validation_base_state_payload=base_payload,
        state_payload=forged_payload,
        payload_fingerprint=RuntimeSession._runtime_projection_checkpoint_fingerprint(
            projection_kind=TERMINAL_NOTIFICATION_CHECKPOINT_KIND,
            through_sequence=through_sequence,
            projection_schema_version=(TERMINAL_NOTIFICATION_CHECKPOINT_SCHEMA_VERSION),
            ledger_prefix=ledger_prefix,
            validation_base_through_sequence=0,
            validation_base_state_payload=base_payload,
            state_payload=forged_payload,
        ),
    )
    event_log.write_runtime_projection_checkpoint(checkpoint)

    session = object.__new__(RuntimeSession)
    session.event_log = event_log
    session.runtime_session_id = runtime_session_id
    with pytest.raises(ValueError, match="reducer transition is untrusted"):
        session._restore_terminal_notification_projection(
            deadline_monotonic=time.monotonic() + 3.0
        )
    journal.close(destroy_spool=True)


def test_tm3_projection_checkpoint_cannot_forge_notification_genesis() -> None:
    runtime_session_id = "runtime:checkpoint-forged-genesis"
    event_log = InMemoryEventLog(runtime_session_id=runtime_session_id)
    unrelated = event_log.append(
        RunErrorEvent(
            id="run_error:checkpoint-forged-genesis",
            run_id="run:checkpoint-forged-genesis",
            turn_id="turn:checkpoint-forged-genesis",
            reply_id="reply:checkpoint-forged-genesis",
            message="checkpoint forged genesis fixture",
            code="checkpoint_forged_genesis_fixture",
        )
    )
    through_sequence = unrelated.sequence or 0

    forged_base_store = HostIngressNotificationProjectionStore(
        runtime_session_id=runtime_session_id,
        maximum_completion_process_heads=99,
        maximum_active_monitor_slots=77,
    )
    forged_base_payload = forged_base_store.checkpoint_payload()
    forged_base_store.through_sequence = through_sequence
    forged_result_payload = forged_base_store.checkpoint_payload()
    ledger_prefix = event_log.read_raw_ledger_prefix(through_sequence=through_sequence)
    checkpoint = RawRuntimeProjectionCheckpoint(
        projection_kind=TERMINAL_NOTIFICATION_CHECKPOINT_KIND,
        through_sequence=through_sequence,
        projection_schema_version=(TERMINAL_NOTIFICATION_CHECKPOINT_SCHEMA_VERSION),
        ledger_prefix=ledger_prefix,
        validation_base_through_sequence=0,
        validation_base_state_payload=forged_base_payload,
        state_payload=forged_result_payload,
        payload_fingerprint=RuntimeSession._runtime_projection_checkpoint_fingerprint(
            projection_kind=TERMINAL_NOTIFICATION_CHECKPOINT_KIND,
            through_sequence=through_sequence,
            projection_schema_version=(TERMINAL_NOTIFICATION_CHECKPOINT_SCHEMA_VERSION),
            ledger_prefix=ledger_prefix,
            validation_base_through_sequence=0,
            validation_base_state_payload=forged_base_payload,
            state_payload=forged_result_payload,
        ),
    )
    event_log.write_runtime_projection_checkpoint(checkpoint)

    session = object.__new__(RuntimeSession)
    session.event_log = event_log
    session.runtime_session_id = runtime_session_id
    with pytest.raises(ValueError, match="checkpoint genesis is untrusted"):
        session._restore_terminal_notification_projection(
            deadline_monotonic=time.monotonic() + 3.0
        )


def test_tm3_restart_preserves_pending_progress_until_disposition() -> None:
    pending_source = RunErrorEvent(
        id="terminal_monitor_observation:restart-pending:1",
        run_id="run:restart-pending",
        turn_id="turn:restart-pending",
        reply_id="reply:restart-pending",
        message="restart pending fixture",
        code="restart_pending_fixture",
    ).model_copy(update={"sequence": 7})
    record = SimpleNamespace(
        core_state=SimpleNamespace(lifecycle_state="active_pending_delivery"),
        pending_observation_event=SimpleNamespace(id=pending_source.id),
        registration_event=SimpleNamespace(
            registration_semantic=SimpleNamespace(monitor_id="monitor:restart-pending")
        ),
    )

    class RecordingCoordinator(TerminalMonitorCoordinator):
        def __init__(self):
            super().__init__(
                runtime_session=SimpleNamespace(),
                store=SimpleNamespace(snapshots=lambda: (record,)),
            )
            self.started: list[str] = []

        def _start_post_restart_delivery_terminalization(
            self,
            *,
            monitor_id,
            disposition,
        ) -> None:
            del disposition
            self.started.append(monitor_id)

    coordinator = RecordingCoordinator()
    coordinator.recover_after_restart(deadline_monotonic=time.monotonic() + 1)

    assert coordinator.started == []
    assert coordinator._restart_pending_delivery == {
        pending_source.id: "monitor:restart-pending"
    }

    disposition = TerminalProcessObservationDeliveryDispositionEvent(
        id="terminal_notification_disposition:restart-pending",
        run_id=pending_source.run_id,
        turn_id=pending_source.turn_id,
        reply_id=pending_source.reply_id,
        observation_source_references=(
            event_reference_from_stored(
                pending_source,
                runtime_session_id="runtime:restart-pending",
            ),
        ),
        outcome="session_closed",
    ).model_copy(update={"sequence": 8})
    coordinator.on_committed((disposition,))

    assert coordinator.started == ["monitor:restart-pending"]
    assert coordinator._restart_pending_delivery == {}


def test_tm3_checkpoint_persistence_reuses_current_writer_deadline() -> None:
    deadline = time.monotonic() + 3.0
    event_log = InMemoryEventLog(runtime_session_id="runtime:deadline")
    stored = event_log.append(
        RunErrorEvent(
            id="run_error:checkpoint-deadline",
            run_id="run:checkpoint-deadline",
            turn_id="turn:checkpoint-deadline",
            reply_id="reply:checkpoint-deadline",
            message="deadline fixture",
            code="checkpoint_deadline_fixture",
        )
    )
    calls: list[tuple[str, float | None]] = []

    class RecordingEventLog:
        def read_raw_ledger_prefix(self, *, through_sequence, deadline_monotonic):
            calls.append(("read", deadline_monotonic))
            return event_log.read_raw_ledger_prefix(through_sequence=through_sequence)

        def write_runtime_projection_checkpoint(
            self,
            checkpoint,
            *,
            deadline_monotonic,
        ):
            calls.append(("write", deadline_monotonic))
            event_log.write_runtime_projection_checkpoint(checkpoint)

    session = object.__new__(RuntimeSession)
    session.event_log = RecordingEventLog()
    session.event_write_service = SimpleNamespace(
        current_deadline_monotonic=lambda: deadline
    )
    base_payload = {"through_sequence": 0}
    session._terminal_monitor_checkpoint_head = (0, base_payload)
    session._persist_runtime_projection_checkpoint(
        projection_kind=TERMINAL_MONITOR_CHECKPOINT_KIND,
        projection_schema_version=TERMINAL_MONITOR_CHECKPOINT_SCHEMA_VERSION,
        through_sequence=stored.sequence or 0,
        state_payload={"through_sequence": stored.sequence or 0},
    )

    assert calls == [("read", deadline), ("write", deadline)]


def test_tm3_restart_recovery_none_is_bounded_by_absolute_deadline() -> None:
    attempts = 0

    def reject(_events, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise PendingRuntimeEventWriteError("synthetic NONE")

    coordinator = TerminalMonitorCoordinator(
        runtime_session=SimpleNamespace(write_events_from_thread=reject),
        store=SimpleNamespace(),
    )
    owner = _FiringOwner(
        monitor_id="monitor:recovery-deadline",
        stable_candidates=(
            RunErrorEvent(
                id="run_error:recovery-deadline",
                run_id="run:recovery-deadline",
                turn_id="turn:recovery-deadline",
                reply_id="reply:recovery-deadline",
                message="synthetic restart recovery candidate",
                code="synthetic_restart_recovery_candidate",
            ),
        ),
        source_state_fingerprint="sha256:" + "a" * 64,
    )
    coordinator._firing[owner.monitor_id] = owner

    with pytest.raises(TerminalMonitorRecoveryBlocked, match="returned NONE"):
        coordinator._commit_firing(
            owner,
            deadline_monotonic=time.monotonic() + 0.05,
        )

    assert attempts == 1
    assert coordinator._firing[owner.monitor_id] is owner


def test_tm3_registration_snapshot_and_baseline_share_one_journal_read(
    tmp_path,
) -> None:
    registry = ProcessRegistry()
    context = EventContext(
        run_id="run:registration-baseline",
        turn_id="turn:registration-baseline",
        reply_id="reply:registration-baseline",
    )
    try:
        process, yielded = registry.exec_with_yield(
            terminal_session_id="default",
            command="sleep 30",
            cwd=tmp_path,
            max_output_chars=1024,
            yield_time_ms=0,
            owner_host_session_id="host:registration-baseline",
            origin_event_context=context,
            origin_tool_call_id="call:registration-baseline",
            origin_runtime_session_id="runtime:registration-baseline",
            origin_run_entry_kind="host_main_run",
        )
        assert yielded
        process.output.append(b"before-registration\n")

        initial = snapshot_process_for_monitor_registration(
            process,
            max_output_chars=1024,
        )
        baseline = initial.observation_semantic.observed_end_cursor
        process.output.append(b"after-registration\n")
        later, _ = process.output.snapshot_since(baseline, max_chars=1024)

        assert initial.output == "before-registration\n"
        assert later.output_preview == "after-registration\n"
        assert initial.observation_semantic.observed_end_cursor == baseline
        assert baseline.sanitized_char_offset == len(initial.output)
    finally:
        registry.shutdown()


def test_tm3_current_monitor_inventory_is_bounded_and_excludes_history(
    monkeypatch,
) -> None:
    records = tuple(
        SimpleNamespace(
            registration_event=SimpleNamespace(
                registration_semantic=SimpleNamespace(monitor_id=f"monitor:{index}")
            ),
            core_state=SimpleNamespace(
                lifecycle_state="terminated" if index == 0 else "active_ready"
            ),
        )
        for index in range(5)
    )
    monkeypatch.setattr(TerminalMonitorStore, "snapshots", lambda _self: records)
    store = TerminalMonitorStore(runtime_session_id="runtime:inventory")

    current, omitted = store.current_snapshots(maximum_items=2)

    assert tuple(
        item.registration_event.registration_semantic.monitor_id for item in current
    ) == ("monitor:1", "monitor:2")
    assert omitted == 2


def test_tm4_sliding_window_boundary_clock_rollback_and_restart_are_stable() -> None:
    policy = build_frozen_fact(
        TerminalProcessMonitorDeliveryPolicyFact,
        schema_version="terminal_process_monitor_delivery_policy.v1",
        max_output_chars=100,
        minimum_progress_observation_interval_seconds=5,
        maximum_pending_progress_observations=1,
        maximum_committed_progress_observations=119,
        progress_observation_rate_window_seconds=60,
        maximum_progress_observations_per_rate_window=1,
    )
    initial = build_frozen_fact(
        TerminalProcessMonitorProgressLimiterStateFact,
        schema_version="terminal_process_monitor_progress_limiter_state.v1",
        retained_progress_observed_at_utc=(),
        last_committed_progress_observed_at_utc=None,
        delivery_policy_fingerprint=policy.delivery_policy_fingerprint,
    )
    first = advance_progress_limiter(
        previous=initial,
        policy=policy,
        observed_at_utc=_utc(60),
    )
    assert first is not None
    restored = TerminalProcessMonitorProgressLimiterStateFact.model_validate(
        first.model_dump(mode="json")
    )
    rolled_back, retry_at = progress_limiter_decision(
        previous=restored,
        policy=policy,
        observed_at_utc=_utc(30),
    )
    assert rolled_back is None
    assert retry_at == _utc(120)
    boundary = advance_progress_limiter(
        previous=restored,
        policy=policy,
        observed_at_utc=_utc(120),
    )
    assert boundary is not None
    assert boundary.retained_progress_observed_at_utc == (_utc(120),)


def test_tm4_progress_cap_enters_completion_only_but_terminal_is_reserved() -> None:
    journal = SanitizedOutputJournal(process_id="process:test")
    registration = _monitor_registration(journal)
    journal.append(b"progress")
    delta, _ = journal.snapshot_since(journal.initial_cursor, max_chars=100)
    observation = _progress_observation(delta=delta, ordinal=119)
    limiter = build_frozen_fact(
        TerminalProcessMonitorProgressLimiterStateFact,
        schema_version="terminal_process_monitor_progress_limiter_state.v1",
        retained_progress_observed_at_utc=(_utc(0),),
        last_committed_progress_observed_at_utc=_utc(0),
        delivery_policy_fingerprint=(
            registration.policy.delivery.delivery_policy_fingerprint
        ),
    )
    pending = build_frozen_fact(
        type(initial_monitor_core_state(registration)),
        schema_version="terminal_process_monitor_core_state.v1",
        monitor_id="monitor:test",
        state_revision=119,
        lifecycle_state="active_pending_delivery",
        last_observation_cursor=delta.end_cursor,
        last_consumed_cursor=journal.initial_cursor,
        last_committed_observation_ordinal=119,
        committed_progress_observation_count=119,
        progress_limiter_state=limiter,
        pending_observation_semantic_fingerprint=(
            observation.observation_semantic_fingerprint
        ),
        terminal_reason=None,
    )
    completion_only = resulting_disposition_core_state(
        before=pending,
        observation=observation,
        delivery_policy=registration.policy.delivery,
    )
    assert completion_only.lifecycle_state == "active_completion_only"

    outcome = build_terminal_lifecycle_outcome(
        status="success", exit_code=0, kill_reason=None
    )
    # Build the nested fact through its registered factory rather than relying
    # on Pydantic to fill its own fingerprint.
    from pulsara_agent.primitives.terminal_observation import (
        TerminalProcessCompletionSemanticFact,
    )

    completion_semantic = build_frozen_fact(
        TerminalProcessCompletionSemanticFact,
        schema_version="terminal_process_completion_semantic.v1",
        terminal_output_cursor=delta.end_cursor,
        outcome=outcome,
    )
    completion = build_frozen_fact(
        TerminalProcessMonitorCompletionObservationSemanticFact,
        schema_version="terminal_process_monitor_completion_observation_semantic.v1",
        monitor_id="monitor:test",
        observation_kind="process_completed",
        observation_ordinal=120,
        completion_semantic=completion_semantic,
        output_authority=delta,
    )
    terminal = resulting_observation_core_state(
        before=completion_only,
        observation=completion,
        observed_at_utc=_utc(120),
        delivery_policy=registration.policy.delivery,
    )
    assert terminal.lifecycle_state == "terminal_pending_delivery"


def test_tm4_ten_hour_lifetime_boundary_uses_virtual_time() -> None:
    lifetime = default_monitor_lifetime()
    assert lifetime.maximum_duration_seconds == 36_000
    assert not monitor_lifetime_expired(
        expires_at_utc=_utc(36_000),
        observed_at_utc=_utc(35_999),
    )
    assert monitor_lifetime_expired(
        expires_at_utc=_utc(36_000),
        observed_at_utc=_utc(36_000),
    )


def test_tm4_child_caller_is_rejected_before_session_authority_lookup() -> None:
    coordinator = TerminalMonitorCoordinator(
        runtime_session=SimpleNamespace(),
        store=SimpleNamespace(),
    )
    with pytest.raises(
        TerminalMonitorContractError,
        match="terminal_monitor_child_registration_unsupported",
    ):
        coordinator.prepare_registration(
            process_id="process:child",
            origin_tool_call_id="call:monitor-child",
            runtime_context=ToolRuntimeContext(
                runtime_session_id="runtime:child",
                event_context=EventContext(
                    run_id="run:child",
                    turn_id="turn:child",
                    reply_id="reply:child",
                ),
                run_entry_kind="subagent_child",
            ),
            conditions=default_monitor_conditions(
                min_new_output_chars=1,
                quiet_period_ms=0,
                heartbeat_interval_seconds=None,
            ),
            delivery=default_monitor_delivery_policy(),
            lifetime=default_monitor_lifetime(),
        )


def test_tm4_main_cannot_monitor_child_origin_or_cross_ledger_process(
    tmp_path,
) -> None:
    registry = ProcessRegistry()
    context = EventContext(
        run_id="run:origin",
        turn_id="turn:origin",
        reply_id="reply:origin",
    )
    try:
        child, child_yielded = registry.exec_with_yield(
            terminal_session_id="default",
            command="sleep 30",
            cwd=tmp_path,
            max_output_chars=1024,
            yield_time_ms=0,
            owner_host_session_id="host:test",
            origin_event_context=context,
            origin_tool_call_id="call:child-process",
            origin_runtime_session_id="runtime:child",
            origin_run_entry_kind="subagent_child",
        )
        assert child_yielded
        with pytest.raises(
            ValueError,
            match="terminal_monitor_child_origin_process_unsupported",
        ):
            registry.monitorable_process(
                child.process_id,
                owner_host_session_id="host:test",
                origin_runtime_session_id="runtime:child",
            )

        foreign, foreign_yielded = registry.exec_with_yield(
            terminal_session_id="default",
            command="sleep 30",
            cwd=tmp_path,
            max_output_chars=1024,
            yield_time_ms=0,
            owner_host_session_id="host:test",
            origin_event_context=context,
            origin_tool_call_id="call:foreign-process",
            origin_runtime_session_id="runtime:other",
            origin_run_entry_kind="host_main_run",
        )
        assert foreign_yielded
        with pytest.raises(
            ValueError,
            match="terminal_monitor_cross_ledger_process_unsupported",
        ):
            registry.monitorable_process(
                foreign.process_id,
                owner_host_session_id="host:test",
                origin_runtime_session_id="runtime:main",
            )
    finally:
        registry.shutdown()


def test_tm5_ui_slow_subscriber_gets_gap_without_blocking_journal() -> None:
    async def scenario() -> None:
        channel = TerminalMonitorEventChannel(
            projection_revision=lambda: 0,
            event_resolver=lambda _event_id: None,
            maximum_replay_events=2,
            maximum_subscriber_queue=1,
        )
        journal = SanitizedOutputJournal(process_id="process:ui")
        channel.bind_journal(
            monitor_id="monitor:ui",
            baseline_cursor=journal.initial_cursor,
        )
        subscription = channel.subscribe()
        journal.append(b"one\n")
        channel.publish_journal(journal)
        journal.append(b"two\n")
        channel.publish_journal(journal)
        await asyncio.sleep(0)

        event = await asyncio.wait_for(subscription.__anext__(), timeout=1)
        assert event.replay_gap is True
        assert event.payload["gap_reason"] == "subscriber_backpressure"
        assert journal.end_cursor.sanitized_char_offset == 8
        await subscription.aclose()
        channel.close()

    _run(scenario())


def test_tm5_ui_reconnect_reports_retained_window_gap_then_replays_tail() -> None:
    async def scenario() -> None:
        channel = TerminalMonitorEventChannel(
            projection_revision=lambda: 0,
            event_resolver=lambda _event_id: None,
            maximum_replay_events=2,
            maximum_subscriber_queue=4,
        )
        journal = SanitizedOutputJournal(process_id="process:ui-reconnect")
        channel.bind_journal(
            monitor_id="monitor:ui-reconnect",
            baseline_cursor=journal.initial_cursor,
        )
        reconnect_cursor = TerminalMonitorUIReconnectCursor(
            stream_identity=journal.stream_identity,
            terminal_cursor=journal.initial_cursor,
            notification_projection_revision=0,
        )

        for chunk in (b"one\n", b"two\n", b"three\n"):
            journal.append(chunk)
            channel.publish_journal(journal)

        subscription = channel.subscribe(reconnect_cursor=reconnect_cursor)
        gap = await asyncio.wait_for(subscription.__anext__(), timeout=1)
        second = await asyncio.wait_for(subscription.__anext__(), timeout=1)
        third = await asyncio.wait_for(subscription.__anext__(), timeout=1)

        assert gap.replay_gap is True
        assert gap.payload["gap_reason"] == "retained_replay_window_exceeded"
        assert [second.payload["output"], third.payload["output"]] == [
            "two\n",
            "three\n",
        ]
        assert second.replay_gap is False
        assert third.replay_gap is False
        await subscription.aclose()
        channel.close()
        journal.close(destroy_spool=True)

    _run(scenario())
