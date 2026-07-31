from __future__ import annotations

import asyncio
from dataclasses import dataclass
from threading import Event
from time import monotonic
from types import SimpleNamespace

import pytest

from pulsara_agent.event import EventContext, RunEndEvent, RunStartEvent
from pulsara_agent.event_log import InMemoryEventLog
from pulsara_agent.memory.artifacts.archive import InMemoryArchiveStore
from pulsara_agent.runtime.projection_jobs.compaction_memory_driver_registry import (
    ProcessCompactionMemoryExtractionDriverRegistry,
)
from pulsara_agent.runtime.projection_jobs.compaction_memory_driver import (
    CompactionMemoryExtractionSessionDriver,
)
from pulsara_agent.projection_jobs.compaction_memory_policy import (
    default_compaction_memory_delivery_policy,
)
from pulsara_agent.projection_jobs.contracts import (
    DurableProjectionCommitConfirmation,
)
from pulsara_agent.memory.compaction.extension import (
    MemoryCompactionPostCompletionExtension,
)
from pulsara_agent.blocking_executor import auxiliary_io_executor
from pulsara_agent.memory.compaction.contracts import (
    CompactionHumanEvidenceManifestConsumedAbandoned,
)
from pulsara_agent.memory.scope import MemoryDomainContext
from pulsara_agent.runtime.authority_materialization.transcript_reducer import (
    TranscriptProjectionDocumentRegistry,
    TranscriptProjectionStateStore,
)
from tests.conftest import run_end_contract_fields, run_start_permission_fields
from tests.support.model_call import test_resolved_target_fact


class _BlockingArchive(InMemoryArchiveStore):
    def __init__(self) -> None:
        super().__init__()
        self.started = Event()
        self.release = Event()

    def put_text_if_absent_or_confirm_identical(self, *args, **kwargs):
        self.started.set()
        if not self.release.wait(timeout=5.0):
            raise TimeoutError("test artifact writer remained blocked")
        return super().put_text_if_absent_or_confirm_identical(*args, **kwargs)


class _DeadlineArchive:
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()
        self.deadline_monotonic: float | None = None

    def put_text_if_absent_or_confirm_identical(self, *args, **kwargs) -> None:
        del args
        self.deadline_monotonic = kwargs["deadline_monotonic"]
        self.started.set()
        if not self.release.wait(timeout=5.0):
            raise TimeoutError("test input artifact writer remained blocked")


@dataclass(frozen=True, slots=True)
class _InputDocument:
    document_fingerprint: str = "sha256:test-input-document"

    def model_dump(self, *, mode: str):
        assert mode == "json"
        return {"schema_version": "test_input_document.v1"}


@dataclass(frozen=True, slots=True)
class _SelectedInput:
    document: _InputDocument = _InputDocument()


@dataclass(frozen=True, slots=True)
class _RuntimeSessionStub:
    runtime_session_id: str


class _DeferralRepository:
    def __init__(self, confirmation: DurableProjectionCommitConfirmation) -> None:
        self.confirmation = confirmation
        self.delay_seconds: float | None = None

    def defer_session_model_job_after_attempt(
        self,
        lease,
        *,
        failure,
        delay_seconds: float,
        deadline_monotonic: float,
    ) -> DurableProjectionCommitConfirmation:
        del lease, failure, deadline_monotonic
        self.delay_seconds = delay_seconds
        return self.confirmation


def _authority(text: str):
    runtime_session_id = "runtime:manifest-owner"
    context = EventContext(
        run_id="run:manifest-owner",
        turn_id="turn:manifest-owner",
        reply_id="reply:manifest-owner",
    )
    log = InMemoryEventLog(runtime_session_id=runtime_session_id)
    committed = log.extend(
        (
            RunStartEvent(
                **context.event_fields(),
                **run_start_permission_fields(context.run_id, user_input=text),
                user_input_chars=len(text),
                metadata={"user_input": text},
            ),
            RunEndEvent(
                **context.event_fields(),
                **run_end_contract_fields(context.run_id, status="finished"),
                status="finished",
                stop_reason="final",
            ),
        )
    )
    reducer = TranscriptProjectionStateStore(
        runtime_session_id=runtime_session_id,
        documents=TranscriptProjectionDocumentRegistry(),
    )
    reducer.apply_committed(tuple(committed))
    return runtime_session_id, context, log, reducer.capture_governance_authority_snapshot()


def test_manifest_abandon_retires_only_after_physical_exit() -> None:
    async def run() -> None:
        runtime_session_id, context, log, authority = _authority(
            "I prefer compact progress reports."
        )
        archive = _BlockingArchive()
        extension = MemoryCompactionPostCompletionExtension(
            archive=archive,
            runtime_session_id=runtime_session_id,
            memory_domain=MemoryDomainContext(
                memory_domain_id="u_test",
                workspace_kind="transient",
            ),
            resolved_model_target_factory=test_resolved_target_fact,
            physical_executor=auxiliary_io_executor(),
        )
        intent = extension.prepare_intent(
            runtime_session_id=runtime_session_id,
            event_context=context,
            compaction_id="compaction:manifest-owner",
            completed_event_id="compaction-completed:manifest-owner",
            trigger="manual",
            phase="manual",
            previous_keep_after_sequence=0,
            current_keep_after_sequence=authority.ledger_through_sequence,
            current_through_sequence=authority.ledger_through_sequence,
            predecessor_completed_event_id=None,
            transcript_authority_snapshot=authority,
            event_lookup=log.get_by_id,
        )
        assert intent is not None
        assert await asyncio.to_thread(archive.started.wait, 2.0)

        handle = intent.private_handle
        consumed = handle.manifest_operation.consume_full_or_abandon()
        assert isinstance(
            consumed, CompactionHumanEvidenceManifestConsumedAbandoned
        )
        handle.abandon_before_write(reason="call_a_completed")
        assert extension.pending_physical_operation_count == 1
        assert handle.identity.handle_id in extension._handles

        archive.release.set()
        assert await handle.manifest_operation.wait_physical_exit(
            deadline_monotonic=monotonic() + 2.0
        )
        await asyncio.sleep(0)
        assert extension.pending_physical_operation_count == 0
        assert handle.identity.handle_id not in extension._handles

    asyncio.run(run())


@dataclass(slots=True)
class _FakeDriver:
    runtime_session_id: str
    driver_generation: int
    binding_fingerprint: str

    async def acquire_model_safe_point(self, **_kwargs):
        return None

    async def execute_leased_job(self, _job, **_kwargs) -> None:
        return None

    async def settle_result_candidate(self, _result_candidate, **_kwargs) -> None:
        return None

    async def close(self, **_kwargs) -> None:
        return None


def test_driver_registry_is_generation_aware_and_borrow_fails_closed() -> None:
    registry = ProcessCompactionMemoryExtractionDriverRegistry()
    session_id = "runtime:driver-registry"
    driver = _FakeDriver(session_id, 1, "sha256:binding:1")
    registration = registry.register(driver)

    assert registry.available_runtime_session_ids(
        now_monotonic=monotonic()
    ) == (session_id,)
    borrow = registry.borrow(session_id)
    assert borrow is not None and borrow.driver is driver
    assert registry.active_borrow_count(session_id) == 1
    borrow.release()
    assert registry.active_borrow_count(session_id) == 0
    with pytest.raises(RuntimeError, match="released"):
        _ = borrow.driver

    registration.revoke()
    assert registry.borrow(session_id) is None
    next_driver = _FakeDriver(session_id, 2, "sha256:binding:2")
    next_registration = registry.register(next_driver)
    assert next_registration.identity.driver_generation == 2


def test_driver_registry_rejects_stale_generation() -> None:
    registry = ProcessCompactionMemoryExtractionDriverRegistry()
    with pytest.raises(ValueError, match="generation"):
        registry.register(
            _FakeDriver(
                "runtime:stale-driver",
                2,
                "sha256:stale-binding",
            )
        )


def test_input_artifact_uses_attempt_deadline_and_retains_physical_owner() -> None:
    async def run() -> None:
        archive = _DeadlineArchive()
        driver = CompactionMemoryExtractionSessionDriver(
            runtime_session=_RuntimeSessionStub("runtime:input-artifact-deadline"),
            llm_runtime=None,
            event_log=None,
            archive=archive,
            repository=None,
            settlement_port=None,
            model_lifecycle_companion_factory=None,
            driver_registry=None,
            safe_point_acquirer=None,
            driver_generation=1,
            binding_fingerprint="sha256:test-driver-binding",
        )
        deadline = monotonic() + 5.0
        task = asyncio.create_task(
            driver._persist_input(
                _SelectedInput(),
                deadline_monotonic=deadline,
            )
        )
        assert await asyncio.to_thread(archive.started.wait, 2.0)
        task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done()
        assert archive.deadline_monotonic == deadline

        archive.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())


def test_provider_failure_rejects_conflicting_retry_transition() -> None:
    async def run() -> None:
        repository = _DeferralRepository(
            DurableProjectionCommitConfirmation.CONFLICT
        )
        driver = CompactionMemoryExtractionSessionDriver(
            runtime_session=_RuntimeSessionStub("runtime:retry-conflict"),
            llm_runtime=None,
            event_log=None,
            archive=None,
            repository=repository,
            settlement_port=None,
            model_lifecycle_companion_factory=None,
            driver_registry=None,
            safe_point_acquirer=None,
            driver_generation=1,
            binding_fingerprint="sha256:test-driver-binding",
        )
        lease = SimpleNamespace(
            delivery_policy=default_compaction_memory_delivery_policy()
        )

        with pytest.raises(RuntimeError, match="provider retry transition was conflict"):
            await driver._defer_failed_model_attempt(
                lease,
                dispatch_attempt_ordinal=1,
                failure_message="provider failed",
                transition_name="provider",
                deadline_monotonic=monotonic() + 5.0,
            )
        assert repository.delay_seconds == 1.0

    asyncio.run(run())
