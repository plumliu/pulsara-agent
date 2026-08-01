from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from threading import Event, Timer
from time import monotonic

import pytest

from tests.support.model_stream import make_text_block_segment_event
from tests.support.runtime_session import in_memory_runtime_session

from pulsara_agent.event import EventContext
from pulsara_agent.event import RunErrorEvent, RunStartEvent
from pulsara_agent.memory.artifacts.archive import InMemoryArchiveStore
from pulsara_agent.ports.terminal_presentation import (
    PresentationHistoryPageReadLimits,
)
from pulsara_agent.primitives.prompt_queue import (
    PromptQueueArtifactPreparationHoldFact,
)
from pulsara_agent.primitives.presentation_history import (
    AuditCell,
    UserPromptCell,
    build_default_history_materialization_policy,
    build_default_placement_key_contract,
    build_presentation_history_placement_key,
)
from pulsara_agent.primitives._context_base import ContextEventReferenceFact
from pulsara_agent.runtime.authority_materialization import (
    build_default_transcript_event_domain_registry_binding,
)
from pulsara_agent.runtime.authority_materialization.transcript_reducer import (
    TranscriptProjectionDocumentRegistry,
    TranscriptProjectionStateStore,
)
from pulsara_agent.runtime.terminal_presentation.observation import (
    UiCommittedEventTap,
    UiOperationalActivityStore,
)
from pulsara_agent.ports.stored_event import build_encoder_stored_event_pair
from pulsara_agent.runtime.terminal_presentation.history_capacity import (
    PresentationHistoryCapacityOwner,
    presentation_run_growth_source_fingerprint,
)
from pulsara_agent.runtime.terminal_presentation.history_checkpoint import (
    _checkpoint_receipt,
)
from pulsara_agent.runtime.terminal_application.artifact_hold import (
    InMemoryPromptQueueArtifactStorage,
)
from pulsara_agent.runtime.terminal_presentation.policy import (
    PresentationPurposePolicyRegistry,
    build_default_audit_extractor_binding,
    build_default_presentation_purpose_policy_registry,
)
from pulsara_agent.runtime.terminal_presentation.projection import (
    PresentationHistoryProjectionOwner,
)
from pulsara_agent.runtime.terminal_presentation.service import (
    TerminalPresentationCloseBlocked,
)
from tests.conftest import run_start_permission_fields


CTX = EventContext(
    run_id="run:presentation-foundation",
    turn_id="turn:presentation-foundation",
    reply_id="reply:presentation-foundation",
)


def _segment(label: str):
    return make_text_block_segment_event(
        **CTX.event_fields(),
        block_id=f"text:{label}",
        delta=label,
    )


def test_committed_tap_bootstrap_catches_up_then_enters_live_mode(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path)

    async def run() -> None:
        await runtime.write_event(_segment("one"))

    asyncio.run(run())

    tap = runtime.ui_committed_event_tap
    bootstrap = tap.begin_bootstrap(snapshot_through_sequence=0)
    assert bootstrap.status == "ready"
    assert bootstrap.frozen_ring_head_sequence == 1
    assert tuple(
        (item.source_first_sequence, item.source_last_sequence)
        for item in bootstrap.retained_entries
    ) == ((1, 1),)

    tap.mark_live(bootstrap.subscriber_id, through_sequence=1)
    assert tap.snapshot_subscriber(bootstrap.subscriber_id).status == "live"

    asyncio.run(runtime.write_event(_segment("two")))
    live = tap.snapshot_subscriber(bootstrap.subscriber_id)
    assert tuple(item.source_last_sequence for item in live.pending_entries) == (2,)
    tap.acknowledge(bootstrap.subscriber_id, through_sequence=2)
    assert not tap.snapshot_subscriber(bootstrap.subscriber_id).pending_entries


def test_committed_tap_overflow_requires_range_catch_up(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    tap = runtime.ui_committed_event_tap

    async def run() -> None:
        await runtime.write_event(_segment("one"))
        await runtime.write_event(_segment("two"))

    asyncio.run(run())
    entries = tap.begin_bootstrap(snapshot_through_sequence=0).retained_entries
    assert len(entries) == 2

    bounded = UiCommittedEventTap(
        runtime_session_id=runtime.runtime_session_id,
        max_ring_entries=1,
        max_ring_bytes=16 * 1024 * 1024,
    )
    assert bounded.offer_nowait(entries[0])
    assert bounded.offer_nowait(entries[1])
    bootstrap = bounded.begin_bootstrap(snapshot_through_sequence=0)
    assert bootstrap.status == "range_catch_up_required"
    assert tuple(item.source_first_sequence for item in bootstrap.retained_entries) == (
        2,
    )


def test_committed_tap_ring_eviction_preserves_existing_subscriber_buffer(
    tmp_path,
) -> None:
    runtime = in_memory_runtime_session(tmp_path)

    async def run() -> None:
        await runtime.write_event(_segment("one"))
        await runtime.write_event(_segment("two"))

    asyncio.run(run())
    entries = runtime.ui_committed_event_tap.begin_bootstrap(
        snapshot_through_sequence=0
    ).retained_entries
    bounded = UiCommittedEventTap(
        runtime_session_id=runtime.runtime_session_id,
        max_ring_entries=1,
        max_ring_bytes=16 * 1024 * 1024,
        max_subscriber_entries=2,
    )
    assert bounded.offer_nowait(entries[0])
    bootstrap = bounded.begin_bootstrap(snapshot_through_sequence=0)
    assert bootstrap.status == "ready"
    assert bounded.offer_nowait(entries[1])

    subscriber = bounded.snapshot_subscriber(bootstrap.subscriber_id)
    assert subscriber.status == "catching_up"
    assert tuple(item.source_last_sequence for item in subscriber.pending_entries) == (
        1,
        2,
    )


def test_presentation_bootstrap_uses_bounded_range_when_ring_floor_advanced(
    tmp_path,
) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    runtime.ui_committed_event_tap = UiCommittedEventTap(
        runtime_session_id=runtime.runtime_session_id,
        max_ring_entries=1,
        max_ring_bytes=16 * 1024 * 1024,
    )

    async def run() -> None:
        await runtime.write_event(_segment("one"))
        await runtime.write_event(_segment("two"))
        service = runtime.terminal_presentation_foundation_service
        service.start_background_if_possible()
        for _ in range(200):
            if service.snapshot().active_head.through_authority_sequence == 2:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError(service.reconciliation_reason)
        assert service.reconciliation_reason is None
        assert service.projection_owner.through_sequence == 2
        await service.stop_admission_and_drain(deadline_monotonic=monotonic() + 2.0)

    asyncio.run(run())


def test_transcript_fold_is_independent_of_physical_batch_grouping(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path)

    async def run() -> None:
        await runtime.write_event(_segment("one"))
        await runtime.write_event(_segment("two"))

    asyncio.run(run())
    live_snapshot = runtime.transcript_projection_state_store.snapshot()

    restored = TranscriptProjectionStateStore(
        runtime_session_id=runtime.runtime_session_id,
        documents=TranscriptProjectionDocumentRegistry(),
    )
    proof = runtime.event_log.read_joined_raw_range(
        source_kind="reopen_restore",
        from_sequence_exclusive=0,
        through_sequence=2,
        max_events=2,
        max_payload_bytes=1024 * 1024,
    )
    assert proof is not None
    restored.fold_restored_range(proof)
    restored_snapshot = restored.snapshot()

    assert restored_snapshot == live_snapshot
    assert restored.stable_entries() == (
        runtime.transcript_projection_state_store.stable_entries()
    )


def test_tap_validation_failure_never_escapes_writer(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path)

    def broken_offer(*_args, **_kwargs):
        raise RuntimeError("synthetic UI observer failure")

    runtime.ui_committed_event_tap.offer_committed_nowait = broken_offer  # type: ignore[method-assign]
    result = asyncio.run(runtime.write_event(_segment("committed")))
    assert result.commit_status == "committed"
    assert result.committed_events[0].sequence == 1


def test_stored_event_pair_rejects_same_identity_with_different_payload(
    tmp_path,
) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    result = asyncio.run(
        runtime.write_event(RunErrorEvent(**CTX.event_fields(), message="original"))
    )
    receipt = result.stored_batch_receipt
    assert receipt is not None
    forged = receipt.owned_stored_events[0].model_copy(update={"message": "forged"})

    with pytest.raises(ValueError, match="payload mismatch"):
        build_encoder_stored_event_pair(forged, receipt.raw_stored_envelopes[0])


@pytest.mark.parametrize(
    ("first_disposition", "confirmation_kind"),
    (
        ("none", "predecessor_unchanged"),
        ("unknown", "unavailable"),
        ("conflict", "conflict"),
    ),
)
def test_checkpoint_non_full_retries_the_exact_stable_candidate(
    tmp_path, monkeypatch, first_disposition, confirmation_kind
) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    service = runtime.terminal_presentation_foundation_service
    checkpoint_owner = runtime.presentation_history_checkpoint_owner
    original = checkpoint_owner.commit_prepared_attempt
    seen_attempts: list[str] = []

    def fail_none_once(attempt, *, deadline_monotonic=None):
        seen_attempts.append(attempt.attempt_fingerprint)
        if len(seen_attempts) == 1:
            return _checkpoint_receipt(
                disposition=first_disposition,
                candidate_fingerprint=attempt.commit_candidate_fingerprint,
                installed_checkpoint=None,
                installed_root_identity=None,
                confirmation_kind=confirmation_kind,
            )
        return original(attempt, deadline_monotonic=deadline_monotonic)

    monkeypatch.setattr(checkpoint_owner, "commit_prepared_attempt", fail_none_once)

    async def run() -> None:
        service.start_background_if_possible()
        await runtime.write_event(_segment("stable-retry"))
        for _ in range(300):
            if service.snapshot().active_head.through_authority_sequence == 1:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError(service.reconciliation_reason)
        await service.stop_admission_and_drain(deadline_monotonic=monotonic() + 2.0)

    asyncio.run(run())
    assert len(seen_attempts) >= 2
    assert len(set(seen_attempts)) == 1


def test_tap_gap_during_checkpoint_rebootstraps_from_durable_high_water(
    tmp_path, monkeypatch
) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    runtime.ui_committed_event_tap = UiCommittedEventTap(
        runtime_session_id=runtime.runtime_session_id,
        max_ring_entries=1,
        max_ring_bytes=16 * 1024 * 1024,
        max_subscriber_entries=1,
    )
    service = runtime.terminal_presentation_foundation_service
    checkpoint_owner = runtime.presentation_history_checkpoint_owner
    original = checkpoint_owner.commit_prepared_attempt
    physical_started = Event()
    release_physical = Event()
    blocked_once = False

    def block_first_commit(attempt, *, deadline_monotonic=None):
        nonlocal blocked_once
        if not blocked_once:
            blocked_once = True
            physical_started.set()
            if not release_physical.wait(timeout=2.0):
                raise TimeoutError("checkpoint probe was not released")
        return original(attempt, deadline_monotonic=deadline_monotonic)

    monkeypatch.setattr(checkpoint_owner, "commit_prepared_attempt", block_first_commit)

    async def run() -> None:
        service.start_background_if_possible()
        await runtime.write_event(_segment("gap-one"))
        assert await asyncio.to_thread(physical_started.wait, 1.0)
        await runtime.write_event(_segment("gap-two"))
        await runtime.write_event(_segment("gap-three"))
        assert runtime.ui_committed_event_tap.latest_observed_sequence == 3
        release_physical.set()
        for _ in range(400):
            if service.snapshot().active_head.through_authority_sequence == 3:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError(service.reconciliation_reason)
        subscriber_id = service._subscriber_id
        assert subscriber_id is not None
        subscriber = runtime.ui_committed_event_tap.snapshot_subscriber(subscriber_id)
        assert subscriber.status == "live"
        assert subscriber.last_consumed_sequence == 3
        await service.stop_admission_and_drain(deadline_monotonic=monotonic() + 2.0)

    asyncio.run(run())


def test_presentation_close_blocks_until_executor_operation_physically_exits(
    tmp_path,
) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    service = runtime.terminal_presentation_foundation_service
    physical_started = Event()
    release_physical = Event()

    def blocking_io() -> str:
        physical_started.set()
        release_physical.wait()
        return "done"

    async def run() -> None:
        waiter = asyncio.create_task(
            service.execute_bounded_io(
                operation_name="presentation-close-probe",
                operation=blocking_io,
                deadline_monotonic=monotonic() + 5.0,
            )
        )
        assert await asyncio.to_thread(physical_started.wait, 1.0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        with pytest.raises(TerminalPresentationCloseBlocked):
            await service.stop_admission_and_drain(
                deadline_monotonic=monotonic() + 0.02
            )
        assert service.io_service.pending_count() == 1
        release_physical.set()
        await service.stop_admission_and_drain(deadline_monotonic=monotonic() + 2.0)

    asyncio.run(run())


def test_history_page_blocking_store_read_does_not_block_event_loop(
    tmp_path, monkeypatch
) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    service = runtime.terminal_presentation_foundation_service
    physical_started = Event()
    release_physical = Event()

    def blocking_read_page(**_kwargs):
        physical_started.set()
        release_physical.wait()
        return "page"

    monkeypatch.setattr(service.viewport_service, "read_page", blocking_read_page)

    async def run() -> None:
        Timer(0.15, release_physical.set).start()
        page = asyncio.create_task(
            service.read_history_page_async(
                cursor=None,
                direction="before",
                limits=None,
                absolute_deadline=monotonic() + 2.0,
            )
        )
        assert await asyncio.to_thread(physical_started.wait, 1.0)
        ticks = 0
        while not page.done():
            ticks += 1
            await asyncio.sleep(0.01)
        assert await page == "page"
        assert ticks >= 5
        await service.stop_admission_and_drain(deadline_monotonic=monotonic() + 2.0)

    asyncio.run(run())


def test_history_tree_archive_read_uses_absolute_deadline_and_physically_exits(
    tmp_path, monkeypatch
) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    service = runtime.terminal_presentation_foundation_service
    initial_high_water = service.snapshot().active_head.through_authority_sequence
    run_start = RunStartEvent(
        id=f"run_start:deadline:{CTX.run_id}",
        **CTX.event_fields(),
        **run_start_permission_fields(
            CTX.run_id,
            user_input="deadline-bound presentation",
            turn_id=CTX.turn_id,
            reply_id=CTX.reply_id,
            ledger_runtime_session_id=runtime.runtime_session_id,
            mcp_installation_owner_runtime_session_id=runtime.runtime_session_id,
        ),
        user_input_chars=len("deadline-bound presentation"),
    )
    original_get_text = runtime.archive.get_text
    physical_exited = Event()
    observed_deadlines: list[float] = []
    reservation = service.reserve_ordinary_growth(
        admission_kind="run_activation",
        source_authority_fingerprint=presentation_run_growth_source_fingerprint(
            runtime_session_id=runtime.runtime_session_id,
            run_id=CTX.run_id,
        ),
        owner_kind="host_run",
        owner_id=CTX.run_id,
        owner_generation=1,
    )

    def deadline_bound_get_text(
        _archive,
        blob_id: str,
        *,
        session_id: str | None = None,
        deadline_monotonic: float | None = None,
    ) -> str:
        if not blob_id.startswith("artifact:presentation-history-node:"):
            return original_get_text(
                blob_id,
                session_id=session_id,
                deadline_monotonic=deadline_monotonic,
            )
        assert deadline_monotonic is not None
        observed_deadlines.append(deadline_monotonic)
        # Emulate a database statement timeout firing just before the shared
        # absolute operation deadline.  No release Event is involved: the
        # physical worker must terminate solely because the deadline arrived.
        remaining = deadline_monotonic - monotonic() - 0.02
        if remaining > 0:
            Event().wait(remaining)
        physical_exited.set()
        raise TimeoutError("synthetic artifact statement timeout")

    async def run() -> None:
        service.start_background_if_possible()
        await runtime.write_event(run_start)
        for _ in range(300):
            if (
                service.snapshot().active_head.through_authority_sequence
                > initial_high_water
            ):
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError(service.reconciliation_reason)

        viewport = service.snapshot()
        cursor = viewport.latest_root_cursor_pair.after_cursor
        assert cursor is not None
        # Force the next tree traversal through the durable artifact store rather
        # than the update owner's process-local prepared-node cache.
        runtime.presentation_history_checkpoint_owner.tree._prepared.clear()
        monkeypatch.setattr(
            type(runtime.archive),
            "get_text",
            deadline_bound_get_text,
        )
        deadline = monotonic() + 0.15
        outcome = await service.read_history_page_async(
            cursor=cursor,
            direction="before",
            limits=PresentationHistoryPageReadLimits(
                maximum_entries=16,
                maximum_canonical_bytes=1024 * 1024,
                maximum_rendered_bytes=1024 * 1024,
                maximum_node_reads=64,
                maximum_tree_height=16,
            ),
            absolute_deadline=deadline,
        )
        assert outcome.disposition == "reconciliation_required"
        assert outcome.fault_code == "PRESENTATION_PAGE_DEADLINE_EXPIRED"
        assert physical_exited.is_set()
        assert observed_deadlines == [deadline]
        service.terminalize_ordinary_growth(
            reservation.growth_reservation_id,
            outcome="settled",
        )
        await service.stop_admission_and_drain(deadline_monotonic=monotonic() + 2.0)

    asyncio.run(run())


def test_checkpoint_path_copy_forwards_absolute_deadline_to_tree_reads(
    tmp_path, monkeypatch
) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    service = runtime.terminal_presentation_foundation_service
    initial_high_water = service.snapshot().active_head.through_authority_sequence
    run_start = RunStartEvent(
        id=f"run_start:checkpoint-deadline:{CTX.run_id}",
        **CTX.event_fields(),
        **run_start_permission_fields(
            CTX.run_id,
            user_input="checkpoint deadline",
            turn_id=CTX.turn_id,
            reply_id=CTX.reply_id,
            ledger_runtime_session_id=runtime.runtime_session_id,
            mcp_installation_owner_runtime_session_id=runtime.runtime_session_id,
        ),
        user_input_chars=len("checkpoint deadline"),
    )
    reservation = service.reserve_ordinary_growth(
        admission_kind="run_activation",
        source_authority_fingerprint=presentation_run_growth_source_fingerprint(
            runtime_session_id=runtime.runtime_session_id,
            run_id=CTX.run_id,
        ),
        owner_kind="host_run",
        owner_id=CTX.run_id,
        owner_generation=1,
    )
    original_get_text = runtime.archive.get_text
    observed_tree_deadlines: list[float | None] = []

    def recording_get_text(
        _archive,
        blob_id: str,
        *,
        session_id: str | None = None,
        deadline_monotonic: float | None = None,
    ) -> str:
        if blob_id.startswith("artifact:presentation-history-node:"):
            observed_tree_deadlines.append(deadline_monotonic)
        return original_get_text(
            blob_id,
            session_id=session_id,
            deadline_monotonic=deadline_monotonic,
        )

    async def run() -> None:
        service.start_background_if_possible()
        await runtime.write_event(run_start)
        for _ in range(300):
            if (
                service.snapshot().active_head.through_authority_sequence
                > initial_high_water
            ):
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError(service.reconciliation_reason)

        runtime.presentation_history_checkpoint_owner.tree._prepared.clear()
        monkeypatch.setattr(
            type(runtime.archive),
            "get_text",
            recording_get_text,
        )
        await runtime.write_event(
            RunErrorEvent(
                id=f"run_error:checkpoint-deadline:{CTX.run_id}",
                **CTX.event_fields(),
                message="checkpoint path-copy deadline probe",
            )
        )
        target_high_water = runtime.ui_committed_event_tap.latest_observed_sequence
        for _ in range(300):
            if (
                service.snapshot().active_head.through_authority_sequence
                >= target_high_water
            ):
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError(service.reconciliation_reason)

        assert observed_tree_deadlines
        assert all(deadline is not None for deadline in observed_tree_deadlines)
        service.terminalize_ordinary_growth(
            reservation.growth_reservation_id,
            outcome="settled",
        )
        await service.stop_admission_and_drain(deadline_monotonic=monotonic() + 2.0)

    asyncio.run(run())


def test_placement_key_has_exact_fixed_width_and_order() -> None:
    contract = build_default_placement_key_contract()
    before = build_presentation_history_placement_key(
        contract=contract,
        canonical_spine_left_coordinate=None,
        canonical_spine_right_coordinate=1,
        relative_position_kind="before_leaf",
        source_ledger_sequence_or_zero=1,
        relative_local_ordinal=0,
        stable_source_id="audit:run-start",
    )
    canonical = build_presentation_history_placement_key(
        contract=contract,
        canonical_spine_left_coordinate=1,
        canonical_spine_right_coordinate=1,
        relative_position_kind="canonical_leaf",
        source_ledger_sequence_or_zero=0,
        relative_local_ordinal=0,
        stable_source_id="anchor:user",
    )
    assert len(before.canonical_comparable_key_bytes) == 75
    assert len(canonical.canonical_comparable_key_bytes) == 75
    assert (
        before.canonical_comparable_key_bytes < canonical.canonical_comparable_key_bytes
    )


def test_run_start_projects_lifecycle_audit_before_canonical_user(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    run_start = RunStartEvent(
        id=f"run_start:test:{CTX.run_id}",
        **CTX.event_fields(),
        **run_start_permission_fields(
            CTX.run_id,
            user_input="hello presentation",
            turn_id=CTX.turn_id,
            reply_id=CTX.reply_id,
            ledger_runtime_session_id=runtime.runtime_session_id,
            mcp_installation_owner_runtime_session_id=runtime.runtime_session_id,
        ),
        user_input_chars=len("hello presentation"),
    )
    asyncio.run(runtime.write_event(run_start))
    tap_entry = runtime.ui_committed_event_tap.begin_bootstrap(
        snapshot_through_sequence=0
    ).retained_entries[0]
    contract = build_default_presentation_purpose_policy_registry(
        transcript_domains=build_default_transcript_event_domain_registry_binding()
    )
    owner = PresentationHistoryProjectionOwner(
        runtime_session_id=runtime.runtime_session_id,
        placement_contract=build_default_placement_key_contract(),
        purpose_policy=PresentationPurposePolicyRegistry(contract),
        audit_extractor=build_default_audit_extractor_binding(),
        transcript_documents=runtime.transcript_projection_document_registry,
        archive=runtime.archive,
    )
    result = owner.apply_committed_tap_entry(tap_entry)
    snapshot = owner.snapshot()

    # Live tail folding advances authority only; a confirmed checkpoint/root
    # swap is the sole owner of the client-visible projection revision.
    assert result.resulting_projection_revision == 0
    assert len(snapshot.ordered_tail_segments) == tap_entry.source_last_sequence
    assert sum(item.mutation_count for item in snapshot.ordered_tail_segments) == 2
    assert isinstance(snapshot.ordered_entries[0].cell, AuditCell)
    assert snapshot.ordered_entries[0].cell.audit_kind == "run_lifecycle"
    assert isinstance(snapshot.ordered_entries[1].cell, UserPromptCell)
    assert snapshot.ordered_entries[1].cell.content_blocks[0].text == (
        "hello presentation"
    )


def test_expired_prepared_artifact_hold_releases_only_without_queue_reference() -> None:
    archive = InMemoryArchiveStore()
    storage = InMemoryPromptQueueArtifactStorage(archive)
    deadline = monotonic() + 1.0
    abandoned = storage.prepare(
        runtime_session_id="runtime:artifact-hold",
        owner_client_submission_identity="submission:abandoned",
        text="a" * 20_000,
        deadline_monotonic=deadline,
    )
    consumed = storage.prepare(
        runtime_session_id="runtime:artifact-hold",
        owner_client_submission_identity="submission:consumed",
        text="b" * 20_000,
        deadline_monotonic=deadline,
    )
    storage.apply_accept_in_memory(
        runtime_session_id="runtime:artifact-hold",
        queue_item_id="queue:consumed",
        content=consumed,
    )

    released = storage.release_expired_prepared(
        runtime_session_id="runtime:artifact-hold",
        expired_before_utc=(datetime.now(UTC) + timedelta(days=2)).isoformat(),
        maximum_holds=16,
        deadline_monotonic=deadline,
    )

    assert released == (abandoned.preparation_id,)
    abandoned_hold = PromptQueueArtifactPreparationHoldFact.model_validate(
        archive.prompt_queue_artifact_holds[abandoned.preparation_id]
    )
    consumed_hold = PromptQueueArtifactPreparationHoldFact.model_validate(
        archive.prompt_queue_artifact_holds[consumed.preparation_id]
    )
    assert abandoned_hold.state == "RELEASED"
    assert abandoned_hold.consuming_queue_item_id is None
    assert consumed_hold.state == "CONSUMED"
    assert consumed_hold.consuming_queue_item_id == "queue:consumed"


def test_operational_activity_store_coalesces_retires_and_reports_gap() -> None:
    store = UiOperationalActivityStore(
        runtime_session_id="runtime:operational",
        maximum_change_entries=2,
    )
    for text in ("first", "second", "third"):
        assert store.offer_nowait(
            activity_kind="model_activity",
            owner_kind="model_call",
            owner_id="call:1",
            owner_generation=1,
            coalesce_key="model:call:1",
            replacement_semantics="expire_at_terminal",
            public_text=text,
        )
    snapshot = store.snapshot()
    assert len(snapshot.ordered_activity_cells) == 1
    assert snapshot.ordered_activity_cells[0].bounded_public_text == "third"
    assert (
        store.read_after(operational_generation=1, operational_cursor=0).status == "gap"
    )
    recent = store.read_after(operational_generation=1, operational_cursor=1)
    assert recent.status == "next"
    assert tuple(item.operational_cursor for item in recent.ordered_changes) == (2, 3)

    assert store.retire_nowait(
        coalesce_key="model:call:1",
        owner_kind="model_call",
        owner_id="call:1",
        owner_generation=1,
        reason="durable_terminal",
    )
    assert not store.snapshot().ordered_activity_cells
    removal = store.read_after(operational_generation=1, operational_cursor=3)
    assert removal.status == "next"
    assert removal.ordered_changes[0].removal_reason == "durable_terminal"


def test_history_capacity_checkpoint_restores_exact_remaining_run_reserve() -> None:
    runtime_session_id = "runtime:history-capacity"
    run_id = "run:history-capacity"
    policy = build_default_history_materialization_policy()
    owner = PresentationHistoryCapacityOwner(
        runtime_session_id=runtime_session_id,
        materialization_policy=policy,
    )
    source = presentation_run_growth_source_fingerprint(
        runtime_session_id=runtime_session_id,
        run_id=run_id,
    )
    quote = owner.derive_quote(
        admission_kind="run_activation",
        source_authority_fingerprint=source,
    )
    decision = owner.decide(
        quote=quote,
        confirmed_entry_count=0,
        current_tail_worst_case_entry_count=0,
    )
    reservation = owner.reserve(
        quote=quote,
        decision=decision,
        owner_kind="host_run",
        owner_id=run_id,
        owner_generation=1,
    )
    owner.bind_run_start_source(
        reservation.growth_reservation_id,
        source_run_start_event_reference=ContextEventReferenceFact(
            runtime_session_id=runtime_session_id,
            event_id="run_start:history-capacity",
            sequence=7,
            event_type="RUN_START",
            payload_fingerprint="sha256:" + "1" * 64,
        ),
    )
    settled = owner.settle_growth(
        reservation.growth_reservation_id,
        positive_entry_growth=3,
    )
    checkpoint = owner.checkpoint_snapshot(through_authority_sequence=9)

    restored = PresentationHistoryCapacityOwner(
        runtime_session_id=runtime_session_id,
        materialization_policy=policy,
    )
    restored.restore_checkpoint(checkpoint)
    rebound = restored.rebind_active_owner(
        owner_kind="host_run",
        owner_id=run_id,
    )

    assert rebound.growth_reservation_id == reservation.growth_reservation_id
    assert rebound.owner_generation == 2
    assert rebound.settled_materialized_entry_count == 3
    assert rebound.remaining_unmaterialized_entry_count == (
        quote.maximum_new_history_entries - 3
    )
    assert rebound.previous_reservation_fingerprint == settled.reservation_fingerprint
