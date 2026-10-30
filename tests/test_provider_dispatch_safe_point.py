"""Continuation resource ownership at the shared provider-input safe point."""

from __future__ import annotations

import asyncio
from threading import Event, Lock
from time import monotonic
from types import SimpleNamespace

import pytest

from pulsara_agent.conversation_kernel.cold_epoch import (
    CanonicalColdContinuationSeed,
    _issue_adopted_compaction_continuation_seed,
)
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.compaction.contracts import (
    CompactionAdoptionConfirmation,
    CompactionConfirmationKind,
)
from pulsara_agent.conversation_kernel.input_continuity import (
    NoContinuationAdmissionFence,
    _NO_CONTINUATION_FENCE_SEAL,
)
from pulsara_agent.conversation_kernel.provider_dispatch import (
    PreparedProviderDispatch,
    PreparedProspectiveActiveRootInput,
    ProviderDispatchCoordinator,
    ProviderDispatchExecutionAuthority,
    _compaction_seed_with_dispatch_read,
)
from pulsara_agent.conversation_kernel.repository import StaleHostWriter
from pulsara_agent.conversation_kernel.steer_consumption import SteerConsumptionCoordinator
from pulsara_agent.model_input.continuity import ProviderInputContinuityScope
from pulsara_agent.model_input.contracts import ModelInputScopeKind, PreparedProviderInputCut


class _Resource:
    def __init__(self, name, events):
        self.name, self.events = name, events

    def close(self):
        self.events.append(self.name)

    def retire(self):
        self.close()


def test_suffix_authority_transfers_the_exact_borrow_without_closing_it():
    events = []
    handle, borrow = _Resource("handle", events), _Resource("borrow", events)
    basis = object()
    safe = SimpleNamespace(
        seal_provider_input_preparation_basis=lambda **_: basis,
        revoke_provider_input_preparation_basis=lambda value: events.append(
            "revoke" if value is basis else "wrong basis"
        ),
    )
    authority = ProviderDispatchExecutionAuthority(
        handle, borrow, safe_point=safe, canonical_observation=object(),
        capability_observation=object(), call_target=object(),
    )
    transferred = authority.take_for_suffix_preparation()
    assert transferred == (handle, borrow)
    assert events == ["revoke"]
    with pytest.raises(RuntimeError, match="already consumed"):
        authority.take_for_suffix_preparation()


@pytest.mark.parametrize("failure_at", [None, "physical", "planning", "hook"])
def test_abandonment_runs_after_every_physical_and_hook_resource_is_released(failure_at):
    events = []
    cleanup_error = OSError("exact physical release failed")

    def release(name):
        events.append(name)
        if failure_at == name:
            raise cleanup_error

    dispatch = object.__new__(PreparedProviderDispatch)
    dispatch._authority_lock = Lock()
    dispatch._execution_authority = SimpleNamespace(close=lambda: release("physical"))
    dispatch._continuity_owner = SimpleNamespace(
        abort_planning=lambda _: release("planning")
    )
    dispatch._hook_context_reservation = SimpleNamespace(retire=lambda: release("hook"))
    dispatch._continuation_abandonment = None
    dispatch._installed_sealed = False
    dispatch.planning = object()
    error = asyncio.CancelledError()

    def abandon(actual):
        assert actual is error
        assert events == ["physical", "planning", "hook"]
        events.append("abandon")

    dispatch.install_continuation_abandonment(abandon)
    if failure_at is None:
        dispatch.close(error=error)
    else:
        with pytest.raises(OSError) as raised:
            dispatch.close(error=error)
        assert raised.value is cleanup_error
    assert events == ["physical", "planning", "hook", "abandon"]
    with pytest.raises(RuntimeError, match="consumed"):
        dispatch.close()
    assert events.count("abandon") == 1


@pytest.mark.parametrize("route", ["freeze", "rotate"])
def test_repeated_cancel_drains_preparation_and_closes_its_returned_handle(route):
    async def exercise():
        io = KernelSessionIO(maximum_concurrency=1)
        started, release = Event(), Event()
        events = []
        previous = _Resource("previous", events)
        successor = _Resource("successor", events)

        def rotate(handle, *, turn_id, deadline_monotonic):
            assert handle is previous
            assert turn_id == "turn"
            assert monotonic() < deadline_monotonic
            started.set()
            assert release.wait(5)
            return successor

        coordinator = object.__new__(ProviderDispatchCoordinator)
        coordinator._io = io
        coordinator._safe_point = SimpleNamespace(rotate_provider_input=rotate)
        if route == "rotate":
            operation = coordinator._rotate_suffix_handle(
                previous, turn_id="turn", deadline=monotonic() + 5
            )
        else:
            operation = coordinator._run_preparation_handle(
                lambda **kwargs: rotate(previous, **kwargs),
                turn_id="turn", deadline=monotonic() + 5,
            )
        task = asyncio.create_task(operation)
        try:
            assert await asyncio.to_thread(started.wait, 2)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            assert events == []
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert events == ["successor"]
            assert io._active == set()
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await io.aclose(deadline_monotonic=monotonic() + 2)

    asyncio.run(exercise())


def test_late_physical_preparation_retires_returned_handle_before_timeout():
    async def exercise():
        io = KernelSessionIO(maximum_concurrency=1)
        started, release = Event(), Event()
        events = []
        handle = _Resource("late handle", events)

        def freeze(*, deadline_monotonic):
            started.set()
            assert release.wait(5)
            return handle

        coordinator = object.__new__(ProviderDispatchCoordinator)
        coordinator._io = io
        task = asyncio.create_task(coordinator._run_preparation_handle(
            freeze, deadline=monotonic() + 0.1
        ))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            await asyncio.sleep(0.15)
            assert not task.done()
            release.set()
            with pytest.raises(TimeoutError, match="physical I/O"):
                await task
            assert events == ["late handle"]
            assert io._active == set()
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await io.aclose(deadline_monotonic=monotonic() + 2)

    asyncio.run(exercise())


def test_adopted_suffix_variants_share_one_authority_while_empty_stays_cold():
    def read(sequence):
        return SimpleNamespace(compile_snapshot=SimpleNamespace(
            context_binding_fact=SimpleNamespace(
                context_snapshot_id="snapshot", source_through_sequence=3,
                binding_revision_id="revision",
            ),
            canonical_input=SimpleNamespace(identity=SimpleNamespace(
                session_id="session", turn_id="turn",
                context_binding_revision_id="revision",
                provider_input_through_sequence=sequence,
            )),
        ))

    original, appended = read(4), read(5)
    predecessor, destination = object(), SimpleNamespace(turn_id="turn")
    seed = _issue_adopted_compaction_continuation_seed(
        dispatch_read=original, binding_rewrite_identity="rewrite",
        protected_tail_selection_fingerprint="sha256:tail",
        predecessor=predecessor, destination=destination,
        confirmation=SimpleNamespace(kind=SimpleNamespace(value="FULL")),
        attempt_token=object(), candidate=object(),
    )
    suffix = _compaction_seed_with_dispatch_read(seed, appended)
    assert suffix._authority is seed._authority
    suffix._consume_for(
        predecessor=predecessor, destination=destination,
        preparation_basis=SimpleNamespace(canonical_read=appended),
    )
    with pytest.raises(RuntimeError, match="already consumed"):
        seed._consume_for(
            predecessor=predecessor, destination=destination,
            preparation_basis=SimpleNamespace(canonical_read=original),
        )
    empty = _compaction_seed_with_dispatch_read(
        CanonicalColdContinuationSeed(original), appended
    )
    assert type(empty) is CanonicalColdContinuationSeed
    assert empty.dispatch_read is appended


def test_steer_consumption_does_not_reissue_after_definite_writer_loss():
    async def exercise():
        events = []

        async def run(operation, *args, **kwargs):
            events.append(operation)
            raise StaleHostWriter("replaced")

        coordinator = object.__new__(SteerConsumptionCoordinator)
        write, confirm = object(), object()
        coordinator._repository = SimpleNamespace(
            consume_prepared_prompt_steer=write,
            confirm_prepared_prompt_steer=confirm,
        )
        coordinator._writer_lease = SimpleNamespace(guard=object())
        coordinator._io = SimpleNamespace(run=run)
        coordinator._canonical_deadline = lambda: monotonic() + 1
        with pytest.raises(StaleHostWriter):
            await coordinator._consume_plan_worker(SimpleNamespace(
                selected_consumption_candidates=(object(),)
            ))
        assert events == [write]

    asyncio.run(exercise())


def test_adoption_abandonment_keeps_full_head_distinct_from_summary_source_cut():
    scope = ProviderInputContinuityScope(
        session_id="session", scope_kind=ModelInputScopeKind.ROOT
    )
    candidate = SimpleNamespace(
        scope=SimpleNamespace(session_id="session", turn_id="turn",
                              scope_kind=ModelInputScopeKind.ROOT),
        predecessor=SimpleNamespace(binding_revision_id="revision:before"),
        snapshot=SimpleNamespace(snapshot_id="snapshot", source_through_sequence=3),
        binding=SimpleNamespace(binding_revision_id="revision:after"),
    )
    confirmation = object.__new__(CompactionAdoptionConfirmation)
    object.__setattr__(confirmation, "kind", CompactionConfirmationKind.FULL)
    object.__setattr__(confirmation, "candidate", candidate)
    object.__setattr__(confirmation, "revision_ordinal", 1)
    arguments = dict(
        scope=scope, predecessor=object(), turn_id="turn",
        attempt_token=SimpleNamespace(session_id="session", turn_id="turn",
                                      scope_kind=ModelInputScopeKind.ROOT),
        confirmation=confirmation, candidate=candidate,
        cut=PreparedProviderInputCut("session", "turn", "revision:before", 5),
        destination=SimpleNamespace(session_id="session", turn_id="turn"),
        adopted_snapshot_id="snapshot", adopted_binding_revision_id="revision:after",
        fence_nonce="fence", _seal=_NO_CONTINUATION_FENCE_SEAL,
    )
    fence = NoContinuationAdmissionFence(**arguments)
    assert fence.cut.provider_input_through_sequence == 5
    assert fence.candidate.snapshot.source_through_sequence == 3
    arguments["cut"] = PreparedProviderInputCut(
        "session", "turn", "revision:before", 2
    )
    with pytest.raises(TypeError, match="compaction-owner issued"):
        NoContinuationAdmissionFence(**arguments)


@pytest.mark.parametrize("failure_at", [None, "physical", "planning", "hook"])
def test_active_publication_retires_its_continuation_after_handle_release(failure_at):
    events = []
    cleanup_error = OSError("publication cleanup failed")
    cancellation = asyncio.CancelledError()

    def release(name):
        events.append(name)
        if failure_at == name:
            raise cleanup_error

    dispatch = object.__new__(PreparedProviderDispatch)
    dispatch._authority_lock = Lock()
    dispatch._execution_authority = None
    dispatch._continuity_owner = SimpleNamespace(abort_planning=lambda _: release("planning"))
    dispatch._hook_context_reservation = SimpleNamespace(retire=lambda: release("hook"))
    dispatch.planning = object()
    dispatch._continuation_abandonment = lambda error: (
        events.append("abandon") if error is cancellation else pytest.fail("lost stop cause")
    )
    prepared = object.__new__(PreparedProspectiveActiveRootInput)
    prepared._dispatch = dispatch
    prepared._publication_handle = SimpleNamespace(close=lambda: release("physical"))
    if failure_at is None:
        prepared.close(error=cancellation)
    else:
        with pytest.raises(OSError) as raised:
            prepared.close(error=cancellation)
        assert raised.value is cleanup_error
    assert events == ["physical", "planning", "hook", "abandon"]
    prepared.close(error=cancellation)
    assert events.count("abandon") == 1


@pytest.mark.parametrize("failure_at", ["revoke", "borrow"])
def test_rotation_transfer_failure_closes_the_unreturned_handle(failure_at):
    events = []
    failure = OSError("transfer failed")

    def release(name):
        events.append(name)
        if name == failure_at:
            raise failure

    handle = SimpleNamespace(close=lambda: release("physical"))
    authority = ProviderDispatchExecutionAuthority(
        handle,
        SimpleNamespace(close=lambda: release("borrow")),
        safe_point=SimpleNamespace(
            seal_provider_input_preparation_basis=lambda **_: object(),
            revoke_provider_input_preparation_basis=lambda _: release("revoke"),
        ),
        canonical_observation=object(), capability_observation=object(), call_target=object(),
    )
    with pytest.raises(OSError) as raised:
        authority.take_handle_for_rotation()
    assert raised.value is failure
    assert events == ["revoke", "borrow", "physical"]
