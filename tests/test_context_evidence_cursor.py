from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from threading import Event, RLock, Thread

import pytest
from tests.conftest import emit_test_accepted_model_reply
from tests.support.runtime_session import in_memory_runtime_session

from pulsara_agent.event import CustomEvent, EventContext, PlanExitResolvedEvent
from pulsara_agent.event_log.protocol import (
    RawStoredEventEnvelope,
    RawTranscriptDomainDeltaSnapshot,
)
from pulsara_agent.primitives import context_fingerprint
from pulsara_agent.runtime.authority_materialization import (
    CursorResidentBudgetLimits,
    CursorResidentBudgetManager,
    PersistentTranscriptSemanticEnvelopeVector,
    ProjectionEvidenceCursorOutcome,
    TranscriptProjectionMaterializationEquivalenceBinding,
    TranscriptProjectionMaterializationMismatchCode,
    VerifiedTranscriptProjectionDocumentView,
    ValidatedCursorSnapshotFactory,
    build_materialization_equivalence_contract,
    compose_verified_transcript_sparse_read_proof,
)
from pulsara_agent.runtime.authority_materialization.cursor_resident_budget import (
    estimate_cursor_resident_charge,
)
from pulsara_agent.runtime.authority_materialization.transcript_restore import (
    restore_transcript_projection_from_base,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_evidence_cursor_remains_process_local_memoization() -> None:
    durable_paths = (
        REPO_ROOT / "src/pulsara_agent/event",
        REPO_ROOT / "src/pulsara_agent/primitives",
        REPO_ROOT / "src/pulsara_agent/event_log/serialization.py",
        REPO_ROOT / "src/pulsara_agent/storage/postgres_schema.py",
    )
    violations: list[str] = []
    for root in durable_paths:
        paths = sorted(root.rglob("*.py")) if root.is_dir() else (root,)
        for path in paths:
            source = path.read_text(encoding="utf-8")
            if (
                "evidence_cursor" in source
                or "VerifiedTranscriptProjectionCursor" in source
                or "CursorResident" in source
            ):
                violations.append(path.relative_to(REPO_ROOT).as_posix())
    assert violations == []


def test_evidence_cursor_same_high_water_and_delta_extension(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    context = EventContext(
        run_id="run:evidence-cursor",
        turn_id="turn:evidence-cursor",
        reply_id="reply:evidence-cursor",
    )

    async def scenario() -> None:
        await emit_test_accepted_model_reply(
            runtime,
            event_context=context,
            assistant_text="cursor baseline",
        )
        service = runtime.transcript_projection_checkpoint_service
        high_water = runtime.event_log.next_sequence() - 1
        seeded = await service.prepare_projection_evidence(
            requested_through_sequence=high_water
        )

        reads: list[tuple[int, int]] = []
        event_log_type = type(runtime.event_log)
        original = event_log_type.read_transcript_domain_delta

        def recording_read(self, *, after_sequence, through_sequence, **kwargs):
            if self is runtime.event_log:
                reads.append((after_sequence, through_sequence))
            return original(
                self,
                after_sequence=after_sequence,
                through_sequence=through_sequence,
                **kwargs,
            )

        monkeypatch.setattr(
            event_log_type,
            "read_transcript_domain_delta",
            recording_read,
        )
        same = await service.prepare_projection_evidence(
            requested_through_sequence=high_water
        )
        assert same.cursor_outcome is (
            ProjectionEvidenceCursorOutcome.SAME_HIGH_WATER_HIT
        )
        assert reads == []
        assert isinstance(
            same.document_registry,
            VerifiedTranscriptProjectionDocumentView,
        )
        equivalence = TranscriptProjectionMaterializationEquivalenceBinding(
            build_materialization_equivalence_contract(
                message_artifact_contract_fingerprint=(
                    runtime.transcript_projection_materialization_contracts.normalized_message_content.contract_fingerprint
                ),
                terminal_document_contract_fingerprint=(
                    runtime.terminal_projection_contracts.document.contract_fingerprint
                ),
            )
        ).compare(left=seeded, right=same)
        assert equivalence.equivalent is True
        assert equivalence.mismatch_code is None
        drifted_base = same.projection_base.model_copy(
            update={
                "fact_fingerprint": context_fingerprint(
                    "deliberately-different-projection-base",
                    {},
                )
            }
        )
        base_mismatch = TranscriptProjectionMaterializationEquivalenceBinding(
            build_materialization_equivalence_contract(
                message_artifact_contract_fingerprint=(
                    runtime.transcript_projection_materialization_contracts.normalized_message_content.contract_fingerprint
                ),
                terminal_document_contract_fingerprint=(
                    runtime.terminal_projection_contracts.document.contract_fingerprint
                ),
            )
        ).compare(
            left=seeded,
            right=replace(same, projection_base=drifted_base),
        )
        assert base_mismatch.mismatch_code is (
            TranscriptProjectionMaterializationMismatchCode.PROJECTION_AUTHORITY
        )

        await runtime.emit(
            CustomEvent(
                **context.event_fields(),
                name="cursor-non-semantic-suffix",
                value={"step": 1},
            )
        )
        next_high_water = runtime.event_log.next_sequence() - 1
        extended = await service.prepare_projection_evidence(
            requested_through_sequence=next_high_water
        )
        assert extended.cursor_outcome is (
            ProjectionEvidenceCursorOutcome.DELTA_EXTENSION
        )
        assert reads == [(high_water, next_high_water)]
        assert extended.semantic_delta_events == same.semantic_delta_events
        assert (
            extended.domain_completeness_proof.prefix_through.ledger_through_sequence
            == next_high_water
        )

        await runtime.emit(
            PlanExitResolvedEvent(
                **context.event_fields(),
                exit_request_id="exit:cursor",
                tool_call_id="tool:cursor",
                decision="approve",
            )
        )
        semantic_high_water = runtime.event_log.next_sequence() - 1
        decode_count = 0
        original_decode = RawStoredEventEnvelope.decode_owned

        def recording_decode(self, *args, **kwargs):
            nonlocal decode_count
            decode_count += 1
            return original_decode(self, *args, **kwargs)

        monkeypatch.setattr(
            RawStoredEventEnvelope,
            "decode_owned",
            recording_decode,
        )
        semantic_extension = await service.prepare_projection_evidence(
            requested_through_sequence=semantic_high_water
        )
        assert semantic_extension.cursor_outcome is (
            ProjectionEvidenceCursorOutcome.DELTA_EXTENSION
        )
        assert reads[-1] == (next_high_water, semantic_high_water)
        assert decode_count == 1

    asyncio.run(scenario())
    runtime.close()


def test_evidence_cursor_resident_rejection_keeps_exact_evidence(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    context = EventContext(
        run_id="run:evidence-cursor-rejection",
        turn_id="turn:evidence-cursor-rejection",
        reply_id="reply:evidence-cursor-rejection",
    )

    async def scenario() -> None:
        await emit_test_accepted_model_reply(
            runtime,
            event_context=context,
            assistant_text="resident rejection remains correct",
        )
        service = runtime.transcript_projection_checkpoint_service
        handle = service._verified_evidence_cursor_handle  # noqa: SLF001
        if handle is not None:
            service._discard_cursor(handle)  # noqa: SLF001
        service._cursor_resident_budget = CursorResidentBudgetManager(  # noqa: SLF001
            CursorResidentBudgetLimits(
                max_resident_charge_bytes=1,
                max_resident_chunks=1,
                max_resident_cursors=1,
            )
        )
        high_water = runtime.event_log.next_sequence() - 1
        evidence = await service.prepare_projection_evidence(
            requested_through_sequence=high_water
        )
        assert evidence.cursor_outcome is (
            ProjectionEvidenceCursorOutcome.RESIDENT_ADMISSION_REJECTED
        )
        assert service._verified_evidence_cursor_handle is None  # noqa: SLF001
        assert evidence.domain_completeness_proof.through_sequence == high_water

    asyncio.run(scenario())
    runtime.close()


def test_cursor_factory_guard_and_process_lru_are_exact_handle_safe(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    context = EventContext(
        run_id="run:evidence-cursor-guard",
        turn_id="turn:evidence-cursor-guard",
        reply_id="reply:evidence-cursor-guard",
    )

    async def prepare_cursor():
        await emit_test_accepted_model_reply(
            runtime,
            event_context=context,
            assistant_text="guarded cursor",
        )
        high_water = runtime.event_log.next_sequence() - 1
        await runtime.transcript_projection_checkpoint_service.prepare_projection_evidence(
            requested_through_sequence=high_water
        )
        handle = (
            runtime.transcript_projection_checkpoint_service._verified_evidence_cursor_handle  # noqa: SLF001
        )
        assert handle is not None
        return handle.cursor

    cursor = asyncio.run(prepare_cursor())
    reducer_snapshot = runtime.transcript_projection_state_store.evidence_snapshot()
    with pytest.raises(ValueError, match="validated factory"):
        ValidatedCursorSnapshotFactory.validate_for_use(
            replace(cursor, _factory_guard=object()),
            active_generation=cursor.generation,
            active_base_identity=cursor.base_identity,
            event_domain_binding=runtime.authority_materialization_contracts.event_domain,
            reducer_snapshot=reducer_snapshot,
        )
    token = ValidatedCursorSnapshotFactory.validate_for_use(
        cursor,
        active_generation=cursor.generation,
        active_base_identity=cursor.base_identity,
        event_domain_binding=runtime.authority_materialization_contracts.event_domain,
        reducer_snapshot=reducer_snapshot,
    )
    empty_delta = RawTranscriptDomainDeltaSnapshot.build(
        runtime_session_id=runtime.runtime_session_id,
        before=cursor.delta_after,
        after=cursor.delta_after,
        semantic_events=(),
        registry_contract_fingerprint=(
            runtime.authority_materialization_contracts.event_domain.contract.registry_contract_fingerprint
        ),
    )
    with pytest.raises(ValueError, match="token frozen identity"):
        compose_verified_transcript_sparse_read_proof(
            previous=replace(
                token,
                reducer_snapshot_fingerprint=context_fingerprint(
                    "deliberately-drifted-reducer-snapshot",
                    {},
                ),
            ),
            new_delta=empty_delta,
            next_semantic_envelopes=cursor.semantic_envelopes,
            binding=runtime.authority_materialization_contracts.event_domain,
        )

    charge = estimate_cursor_resident_charge(cursor)
    manager = CursorResidentBudgetManager(
        CursorResidentBudgetLimits(
            max_resident_charge_bytes=charge.total_charge_bytes,
            max_resident_chunks=max(charge.chunk_count, 1),
            max_resident_cursors=1,
        )
    )
    evicted: list[str] = []
    first = manager.prepare_admission(
        owner_runtime_session_id="runtime:first",
        anchor_generation=1,
        candidate=cursor,
        replaces=None,
        eviction_callback=lambda entry_id: evicted.append(entry_id) is None,
    )
    assert first is not None
    first_handle = manager.commit_admission(first)
    aborted_evictions: list[str] = []
    aborted = manager.prepare_admission(
        owner_runtime_session_id="runtime:aborted",
        anchor_generation=1,
        candidate=cursor,
        replaces=None,
        eviction_callback=lambda entry_id: aborted_evictions.append(entry_id) is None,
    )
    assert aborted is not None
    manager.abort_admission(aborted)
    assert aborted_evictions == []
    preserved = manager.borrow(first_handle)
    assert preserved is not None
    preserved.release()
    second = manager.prepare_admission(
        owner_runtime_session_id="runtime:second",
        anchor_generation=1,
        candidate=cursor,
        replaces=None,
        eviction_callback=lambda _entry_id: True,
    )
    assert second is not None
    second_handle = manager.commit_admission(second)
    assert evicted == [first_handle.resident_entry_id]
    assert manager.borrow(first_handle) is None
    lease = manager.borrow(second_handle)
    assert lease is not None
    lease.release()
    assert manager.diagnostics().eviction_count == 1
    runtime.close()


def test_resident_replacement_counts_shared_chunks_once(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    context = EventContext(
        run_id="run:evidence-cursor-shared-chunks",
        turn_id="turn:evidence-cursor-shared-chunks",
        reply_id="reply:evidence-cursor-shared-chunks",
    )

    async def prepare_cursor():
        await emit_test_accepted_model_reply(
            runtime,
            event_context=context,
            assistant_text="shared immutable chunks",
        )
        high_water = runtime.event_log.next_sequence() - 1
        await runtime.transcript_projection_checkpoint_service.prepare_projection_evidence(
            requested_through_sequence=high_water
        )
        handle = (
            runtime.transcript_projection_checkpoint_service._verified_evidence_cursor_handle  # noqa: SLF001
        )
        assert handle is not None
        return handle.cursor

    cursor = asyncio.run(prepare_cursor())
    charge = estimate_cursor_resident_charge(cursor)
    manager = CursorResidentBudgetManager(
        CursorResidentBudgetLimits(
            max_resident_charge_bytes=charge.total_charge_bytes,
            max_resident_chunks=max(charge.chunk_count, 1),
            max_resident_cursors=1,
        )
    )
    first = manager.prepare_admission(
        owner_runtime_session_id="runtime:shared",
        anchor_generation=1,
        candidate=cursor,
        replaces=None,
        eviction_callback=lambda _entry_id: True,
    )
    assert first is not None
    first_handle = manager.commit_admission(first)
    replacement = manager.prepare_admission(
        owner_runtime_session_id="runtime:shared",
        anchor_generation=2,
        candidate=cursor,
        replaces=first_handle,
        eviction_callback=lambda _entry_id: True,
    )
    assert replacement is not None
    replacement_handle = manager.commit_admission(replacement)
    diagnostic = manager.diagnostics()
    assert diagnostic.resident_charge_bytes == charge.total_charge_bytes
    assert diagnostic.resident_chunk_count == charge.chunk_count
    assert diagnostic.resident_cursor_count == 1
    assert manager.borrow(first_handle) is None
    lease = manager.borrow(replacement_handle)
    assert lease is not None
    lease.release()
    runtime.close()


def test_resident_budget_does_not_dedupe_distinct_equal_chunk_objects(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    context = EventContext(
        run_id="run:evidence-cursor-distinct-chunks",
        turn_id="turn:evidence-cursor-distinct-chunks",
        reply_id="reply:evidence-cursor-distinct-chunks",
    )

    async def prepare_cursors():
        await emit_test_accepted_model_reply(
            runtime,
            event_context=context,
            assistant_text="physically distinct immutable chunks",
        )
        high_water = runtime.event_log.next_sequence() - 1
        await runtime.transcript_projection_checkpoint_service.prepare_projection_evidence(
            requested_through_sequence=high_water
        )
        handle = (
            runtime.transcript_projection_checkpoint_service._verified_evidence_cursor_handle  # noqa: SLF001
        )
        assert handle is not None
        original = handle.cursor
        rebuilt_vector = PersistentTranscriptSemanticEnvelopeVector.build(
            original.semantic_envelopes.materialize(),
            max_payload_bytes=(
                runtime.authority_materialization_contracts.limits.max_unreclaimable_charged_payload_bytes
            ),
        )
        rebuilt = ValidatedCursorSnapshotFactory.build(
            generation=original.generation,
            base_identity=original.base_identity,
            projection_base=original.projection_base,
            base_prefix=original.delta_before,
            through_prefix=original.delta_after,
            semantic_envelopes=rebuilt_vector,
            semantic_source=original.semantic_source,
            domain_completeness_proof=original.domain_completeness_proof,
            reducer_snapshot=(
                runtime.transcript_projection_state_store.evidence_snapshot()
            ),
            event_domain_binding=(
                runtime.authority_materialization_contracts.event_domain
            ),
        )
        return original, rebuilt

    original, rebuilt = asyncio.run(prepare_cursors())
    assert original.semantic_envelopes.chunks == rebuilt.semantic_envelopes.chunks
    assert all(
        left is not right
        for left, right in zip(
            original.semantic_envelopes.chunks,
            rebuilt.semantic_envelopes.chunks,
            strict=True,
        )
    )
    charge = estimate_cursor_resident_charge(original)
    manager = CursorResidentBudgetManager(
        CursorResidentBudgetLimits(
            max_resident_charge_bytes=(
                charge.total_charge_bytes + charge.cursor_object_reserve_bytes
            ),
            max_resident_chunks=max(charge.chunk_count * 2, 1),
            max_resident_cursors=2,
        )
    )
    first = manager.prepare_admission(
        owner_runtime_session_id="runtime:physical-first",
        anchor_generation=1,
        candidate=original,
        replaces=None,
        eviction_callback=lambda _entry_id: True,
    )
    assert first is not None
    first_handle = manager.commit_admission(first)
    lease = manager.borrow(first_handle)
    assert lease is not None
    second = manager.prepare_admission(
        owner_runtime_session_id="runtime:physical-second",
        anchor_generation=1,
        candidate=rebuilt,
        replaces=None,
        eviction_callback=lambda _entry_id: True,
    )
    assert second is None
    lease.release()
    runtime.close()


def test_resident_eviction_callback_runs_outside_manager_locks(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    context = EventContext(
        run_id="run:evidence-cursor-callback-lock",
        turn_id="turn:evidence-cursor-callback-lock",
        reply_id="reply:evidence-cursor-callback-lock",
    )

    async def prepare_cursor():
        await emit_test_accepted_model_reply(
            runtime,
            event_context=context,
            assistant_text="callback lock order",
        )
        high_water = runtime.event_log.next_sequence() - 1
        await runtime.transcript_projection_checkpoint_service.prepare_projection_evidence(
            requested_through_sequence=high_water
        )
        handle = (
            runtime.transcript_projection_checkpoint_service._verified_evidence_cursor_handle  # noqa: SLF001
        )
        assert handle is not None
        return handle.cursor

    cursor = asyncio.run(prepare_cursor())
    charge = estimate_cursor_resident_charge(cursor)
    manager = CursorResidentBudgetManager(
        CursorResidentBudgetLimits(
            max_resident_charge_bytes=charge.total_charge_bytes,
            max_resident_chunks=max(charge.chunk_count, 1),
            max_resident_cursors=1,
        )
    )
    anchor_lock = RLock()
    callback_entered = Event()
    anchor_acquired = Event()

    def eviction_callback(_entry_id: str) -> bool:
        callback_entered.set()
        with anchor_lock:
            return True

    first = manager.prepare_admission(
        owner_runtime_session_id="runtime:lock-first",
        anchor_generation=1,
        candidate=cursor,
        replaces=None,
        eviction_callback=eviction_callback,
    )
    assert first is not None
    manager.commit_admission(first)
    second = manager.prepare_admission(
        owner_runtime_session_id="runtime:lock-second",
        anchor_generation=1,
        candidate=cursor,
        replaces=None,
        eviction_callback=lambda _entry_id: True,
    )
    assert second is not None
    failures: list[BaseException] = []

    def anchor_owner() -> None:
        try:
            with anchor_lock:
                anchor_acquired.set()
                assert callback_entered.wait(timeout=2)
                manager.abort_admission(second)
        except BaseException as exc:  # pragma: no cover - assertion relay
            failures.append(exc)

    anchor_thread = Thread(target=anchor_owner)
    anchor_thread.start()
    assert anchor_acquired.wait(timeout=2)

    def commit() -> None:
        try:
            manager.commit_admission(second)
        except BaseException as exc:  # pragma: no cover - assertion relay
            failures.append(exc)

    commit_thread = Thread(target=commit)
    commit_thread.start()
    anchor_thread.join(timeout=2)
    commit_thread.join(timeout=2)
    assert not anchor_thread.is_alive()
    assert not commit_thread.is_alive()
    assert failures == []
    runtime.close()


def test_new_run_anchor_cannot_answer_older_high_water(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    first = EventContext(
        run_id="run:evidence-cursor-first",
        turn_id="turn:evidence-cursor-first",
        reply_id="reply:evidence-cursor-first",
    )
    second = EventContext(
        run_id="run:evidence-cursor-second",
        turn_id="turn:evidence-cursor-second",
        reply_id="reply:evidence-cursor-second",
    )

    async def scenario() -> None:
        await emit_test_accepted_model_reply(
            runtime,
            event_context=first,
            assistant_text="first reply",
        )
        old_high_water = runtime.event_log.next_sequence() - 1
        await emit_test_accepted_model_reply(
            runtime,
            event_context=second,
            assistant_text="second reply",
        )
        evidence = await (
            runtime.transcript_projection_checkpoint_service.prepare_projection_evidence(
                requested_through_sequence=old_high_water
            )
        )
        assert evidence.cursor_outcome is (
            ProjectionEvidenceCursorOutcome.EXACT_RESTORE_ANCHOR_CHANGED
        )
        assert evidence.domain_completeness_proof.through_sequence == old_high_water

    asyncio.run(scenario())
    runtime.close()


def test_delta_read_cancellation_preserves_previous_cursor(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    context = EventContext(
        run_id="run:evidence-cursor-cancel",
        turn_id="turn:evidence-cursor-cancel",
        reply_id="reply:evidence-cursor-cancel",
    )

    async def scenario() -> None:
        await emit_test_accepted_model_reply(
            runtime,
            event_context=context,
            assistant_text="cursor survives cancelled waiter",
        )
        service = runtime.transcript_projection_checkpoint_service
        high_water = runtime.event_log.next_sequence() - 1
        await service.prepare_projection_evidence(
            requested_through_sequence=high_water
        )
        original_handle = service._verified_evidence_cursor_handle  # noqa: SLF001
        assert original_handle is not None
        await runtime.emit(
            CustomEvent(
                **context.event_fields(),
                name="cursor-cancelled-suffix",
                value={"step": 1},
            )
        )
        next_high_water = runtime.event_log.next_sequence() - 1
        started = Event()
        release = Event()
        event_log_type = type(runtime.event_log)
        original_read = event_log_type.read_transcript_domain_delta

        def blocking_read(self, *, after_sequence, through_sequence, **kwargs):
            if self is runtime.event_log and after_sequence == high_water:
                started.set()
                release.wait(timeout=5)
            return original_read(
                self,
                after_sequence=after_sequence,
                through_sequence=through_sequence,
                **kwargs,
            )

        monkeypatch.setattr(
            event_log_type,
            "read_transcript_domain_delta",
            blocking_read,
        )
        task = asyncio.create_task(
            service.prepare_projection_evidence(
                requested_through_sequence=next_high_water
            )
        )
        assert await asyncio.to_thread(started.wait, 2)
        assert (
            service._cursor_resident_budget.diagnostics().active_borrow_count  # noqa: SLF001
            == 1
        )
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert service._verified_evidence_cursor_handle == original_handle  # noqa: SLF001
        release.set()
        await runtime.context_input_io_service.drain_pending(
            deadline_monotonic=asyncio.get_running_loop().time() + 5
        )
        assert service._verified_evidence_cursor_handle == original_handle  # noqa: SLF001

    asyncio.run(scenario())
    runtime.close()


def test_frozen_run_seed_restore_hydrates_exact_anchor_carrier(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    context = EventContext(
        run_id="run:evidence-cursor-frozen-carrier",
        turn_id="turn:evidence-cursor-frozen-carrier",
        reply_id="reply:evidence-cursor-frozen-carrier",
    )

    async def prepare():
        await emit_test_accepted_model_reply(
            runtime,
            event_context=context,
            assistant_text="frozen carrier",
        )
        high_water = runtime.event_log.next_sequence() - 1
        await runtime.transcript_projection_checkpoint_service.prepare_projection_evidence(
            requested_through_sequence=high_water
        )
        handle = (
            runtime.transcript_projection_checkpoint_service._verified_evidence_cursor_handle  # noqa: SLF001
        )
        assert handle is not None
        return high_water, handle.cursor

    high_water, cursor = asyncio.run(prepare())
    restored = restore_transcript_projection_from_base(
        event_log=runtime.event_log,
        archive=runtime.archive,
        runtime_session_id=runtime.runtime_session_id,
        requested_through_sequence=high_water,
        projection_base=cursor.projection_base,
        frozen_anchor_identity=cursor.base_identity,
        event_domain_binding=runtime.authority_materialization_contracts.event_domain,
        materialization_contracts=(
            runtime.transcript_projection_materialization_contracts
        ),
        limits=runtime.authority_materialization_contracts.limits,
    )
    assert restored.anchor_carrier_event is not None
    assert (
        restored.anchor_carrier_event.id
        == cursor.base_identity.anchor_carrier.stable_event_identity.event_id
    )
    before_carrier_high_water = (
        cursor.base_identity.anchor_carrier.committed_sequence - 1
    )
    with pytest.raises(ValueError, match="frozen run-seed anchor identity"):
        restore_transcript_projection_from_base(
            event_log=runtime.event_log,
            archive=runtime.archive,
            runtime_session_id=runtime.runtime_session_id,
            requested_through_sequence=before_carrier_high_water,
            projection_base=cursor.projection_base,
            frozen_anchor_identity=cursor.base_identity,
            event_domain_binding=(
                runtime.authority_materialization_contracts.event_domain
            ),
            materialization_contracts=(
                runtime.transcript_projection_materialization_contracts
            ),
            limits=runtime.authority_materialization_contracts.limits,
        )
    runtime.close()
