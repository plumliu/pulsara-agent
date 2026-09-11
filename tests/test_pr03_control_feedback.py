from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
from time import monotonic
from types import SimpleNamespace
from uuid import uuid4

import pytest

from pulsara_agent.conversation_kernel.contracts import InlineContent
from pulsara_agent.conversation_kernel.host import (
    KernelCommandOutcome,
    KernelHostSession,
    _UserControlAttempt,
)
from pulsara_agent.conversation_kernel.reader import (
    CanonicalProviderInputReader,
    ProviderInputItemKind,
)
from pulsara_agent.conversation_kernel.repository import ConversationKernelRepository
from pulsara_agent.conversation_kernel.safe_point import ProviderSafePointCoordinator
from pulsara_agent.conversation_kernel.user_control import (
    FeedbackCanonicalStatus,
    FeedbackInclusionStatus,
    FeedbackOwnerAvailability,
    UserControlExecution,
    UserControlFeedbackState,
    UserControlOperation,
    UserControlOutcome,
    UserControlRequest,
    UserControlTarget,
    UserControlTargetKind,
)
from pulsara_agent.model_input.contracts import (
    CanonicalInputOriginKind,
    ModelInputScopeKind,
)
from pulsara_agent.ports.user_control_feedback import (
    USER_CONTROL_FEEDBACK_MEDIA_TYPE,
    UserControlFeedbackContentV1,
    UserControlFeedbackInstallationAttempt,
    UserControlProcessFact,
    project_user_control_feedback_for_provider,
)
from pulsara_agent.primitives.permission import DEFAULT_PERMISSION_MODE
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from pulsara_agent.terminal_protocol.canonical_v3 import (
    MAXIMUM_CONTROL_ITEMS,
    CanonicalProtocolReader,
)
from pulsara_agent.terminal_protocol.generated_v3 import terminal_kernel_v3_pb2 as wire
from tests.support.model_config import (
    bind_test_session,
    start_test_root_turn,
)
from tests.support.postgres import verified_postgres_provider


def _id(prefix: str) -> str:
    return f"{prefix}:{uuid4().hex}"


def _content(*, session_id: str = "session:one") -> UserControlFeedbackContentV1:
    return UserControlFeedbackContentV1(
        command_id="command:control:999999:one",
        session_id=session_id,
        host_session_id="host:one",
        process_id="process:one",
        command="python worker.py --literal 你好",
        cwd="/tmp/work tree",
        origin_turn_id="turn:origin",
        origin_subagent_task_id=None,
        target_root_turn_id="turn:target",
        process=UserControlProcessFact(
            "TERMINATION_COMPLETED", "killed", -15, "TERMINAL", False
        ),
        monitor=None,
        public_code="BACKGROUND_CONTROL_COMPLETED",
        public_detail="The exact process control completed.",
    )


def _feedback_host() -> tuple[KernelHostSession, _UserControlAttempt, str]:
    host = object.__new__(KernelHostSession)
    host.session_id = "session:one"
    host.host_session_id = "host:one"
    host._lock = asyncio.Lock()
    host._control_monotonic = lambda: 100.0
    host._active_task = None
    host._active_turn_id = None
    host._control_completion_sealed_turn_id = None
    host._closing = False
    host._closed = False
    provider_text = project_user_control_feedback_for_provider(
        _content().canonical_bytes()
    )
    request = UserControlRequest(
        UserControlOperation.TERMINATE_BACKGROUND_PROCESS,
        "command:control:999999:one",
        host.session_id,
        host.host_session_id,
        UserControlTarget(UserControlTargetKind.BACKGROUND_PROCESS, "process:one"),
    )
    feedback = UserControlFeedbackState(
        FeedbackCanonicalStatus.ACCEPTED,
        FeedbackInclusionStatus.PENDING,
        FeedbackOwnerAvailability.AVAILABLE,
        target_root_turn_id="turn:A",
        entry_id="entry:feedback",
    )
    control = UserControlOutcome(
        request.operation,
        request.session_id,
        request.host_session_id,
        request.target,
        True,
        UserControlExecution.FINISHED,
        feedback=feedback,
    )
    attempt = _UserControlAttempt(
        request,
        KernelCommandOutcome(
            request.command_id,
            "SUCCEEDED",
            request.target.target_id,
            "BACKGROUND_CONTROL_COMPLETED",
            "done",
            user_control=control,
        ),
        feedback_target_turn_id="turn:A",
        feedback_provider_text=provider_text,
    )
    host._user_control_attempts = {request.command_id: attempt}
    return host, attempt, provider_text


def _provider_request(
    *, turn_id: str, entry_id: str, body: str, revision: str = "revision:one"
):
    return SimpleNamespace(
        session_id="session:one",
        turn_id=turn_id,
        model_call_index=2,
        compiled_input=SimpleNamespace(
            canonical_input_identity=SimpleNamespace(
                conversation_scope_kind=ModelInputScopeKind.ROOT,
                provider_input_through_sequence=999,
            ),
            messages=(SimpleNamespace(content=(body,)),),
            message_placements=(SimpleNamespace(origin_entry_id=entry_id),),
        ),
        cut=SimpleNamespace(context_binding_revision_id=revision),
    )


def test_pr03_inclusion_requires_exact_final_placement_body_and_root() -> None:
    async def scenario() -> None:
        host, attempt, provider_text = _feedback_host()
        await host._record_user_control_provider_input_install(
            _provider_request(
                turn_id="turn:B", entry_id="entry:feedback", body=provider_text
            )
        )
        await host._record_user_control_provider_input_install(
            _provider_request(
                turn_id="turn:A", entry_id="entry:other", body=provider_text
            )
        )
        await host._record_user_control_provider_input_install(
            _provider_request(
                turn_id="turn:A", entry_id="entry:feedback", body="similar"
            )
        )
        assert attempt.outcome.user_control is not None
        assert attempt.outcome.user_control.feedback is not None
        assert (
            attempt.outcome.user_control.feedback.inclusion_status
            is FeedbackInclusionStatus.PENDING
        )

        exact = _provider_request(
            turn_id="turn:A", entry_id="entry:feedback", body=provider_text
        )
        await host._record_user_control_provider_input_install(exact)
        feedback = attempt.outcome.user_control.feedback
        assert feedback.inclusion_status is FeedbackInclusionStatus.INCLUDED
        assert feedback.context_binding_revision_id == "revision:one"
        assert feedback.model_call_index == 2

        await host._record_user_control_transport_invocation(
            _provider_request(
                turn_id="turn:A",
                entry_id="entry:feedback",
                body=provider_text,
                revision="revision:other",
            ),
            succeeded=True,
            detail=None,
        )
        assert not feedback.transport.invocation_attempted
        await host._record_user_control_transport_invocation(
            exact, succeeded=False, detail="transport open failed"
        )
        transport = attempt.outcome.user_control.feedback.transport
        assert transport.invocation_attempted
        assert transport.invocation_succeeded is False
        assert transport.detail == "transport open failed"

    asyncio.run(scenario())


def test_pr03_early_inclusion_and_transport_observations_reconcile_after_ack() -> None:
    async def scenario() -> None:
        host, attempt, provider_text = _feedback_host()
        feedback = attempt.outcome.user_control.feedback
        assert feedback is not None
        candidate = UserControlFeedbackInstallationAttempt(
            session_id=host.session_id,
            workspace_id="workspace:one",
            writer_generation=1,
            target_root_turn_id="turn:A",
            entry_id="entry:feedback",
            content=replace(_content(), target_root_turn_id="turn:A"),
            occurred_at=datetime.now(timezone.utc),
            actor_id=host.host_session_id,
        )
        attempt.feedback_candidate = candidate
        provider_text = project_user_control_feedback_for_provider(
            candidate.content.canonical_bytes()
        )
        attempt.feedback_provider_text = provider_text
        host._replace_control_feedback_locked(
            attempt,
            replace(
                feedback,
                canonical_status=FeedbackCanonicalStatus.PENDING,
                entry_id=None,
            ),
        )
        request = _provider_request(
            turn_id="turn:A",
            entry_id=candidate.entry_id,
            body=provider_text,
        )

        await host._record_user_control_transport_invocation(
            request, succeeded=True, detail=None
        )
        await host._record_user_control_provider_input_install(request)
        pending = attempt.outcome.user_control.feedback
        assert pending is not None
        assert pending.canonical_status is FeedbackCanonicalStatus.PENDING
        assert pending.inclusion_status is FeedbackInclusionStatus.PENDING

        async with host._lock:
            host._replace_control_feedback_locked(
                attempt,
                replace(
                    pending,
                    canonical_status=FeedbackCanonicalStatus.ACCEPTED,
                    entry_id=candidate.entry_id,
                ),
            )
        reconciled = attempt.outcome.user_control.feedback
        assert reconciled is not None
        assert reconciled.inclusion_status is FeedbackInclusionStatus.INCLUDED
        assert reconciled.context_binding_revision_id == "revision:one"
        assert reconciled.model_call_index == 2
        assert reconciled.transport.invocation_attempted
        assert reconciled.transport.invocation_succeeded is True

    asyncio.run(scenario())


def test_pr03_normal_root_completion_fences_feedback_into_the_next_request() -> None:
    async def scenario() -> None:
        host, attempt, provider_text = _feedback_host()
        active = asyncio.create_task(asyncio.Event().wait())
        host._active_task = active
        host._active_turn_id = "turn:A"
        host._canonical_deadline = lambda: monotonic() + 1
        feedback = attempt.outcome.user_control.feedback
        assert feedback is not None
        candidate = UserControlFeedbackInstallationAttempt(
            session_id=host.session_id,
            workspace_id="workspace:one",
            writer_generation=1,
            target_root_turn_id="turn:A",
            entry_id="entry:feedback",
            content=replace(_content(), target_root_turn_id="turn:A"),
            occurred_at=datetime.now(timezone.utc),
            actor_id=host.host_session_id,
        )
        attempt.feedback_candidate = candidate
        attempt.feedback_candidate_ready.set()
        attempt.feedback_provider_text = project_user_control_feedback_for_provider(
            candidate.content.canonical_bytes()
        )
        host._replace_control_feedback_locked(
            attempt,
            replace(
                feedback,
                canonical_status=FeedbackCanonicalStatus.PENDING,
                entry_id=None,
            ),
        )

        assert await host._coordinate_user_control_feedback_at_root_completion("turn:A")
        barrier = asyncio.create_task(
            host._await_user_control_feedback_before_root_provider("turn:A")
        )
        await asyncio.sleep(0)
        assert not barrier.done()
        async with host._lock:
            pending = attempt.outcome.user_control.feedback
            assert pending is not None
            host._replace_control_feedback_locked(
                attempt,
                replace(
                    pending,
                    canonical_status=FeedbackCanonicalStatus.ACCEPTED,
                    entry_id=candidate.entry_id,
                ),
            )
        await barrier
        await host._record_user_control_provider_input_install(
            _provider_request(
                turn_id="turn:A",
                entry_id=candidate.entry_id,
                body=attempt.feedback_provider_text,
            )
        )
        assert not await host._coordinate_user_control_feedback_at_root_completion(
            "turn:A"
        )
        active.cancel()
        await asyncio.gather(active, return_exceptions=True)

    asyncio.run(scenario())


def test_pr03_accepted_feedback_fences_without_waiting_for_a_future_request() -> None:
    async def scenario() -> None:
        host, attempt, _provider_text = _feedback_host()
        host._canonical_deadline = lambda: monotonic() + 1
        feedback = attempt.outcome.user_control.feedback
        assert feedback is not None
        assert feedback.canonical_status is FeedbackCanonicalStatus.ACCEPTED
        assert feedback.inclusion_status is FeedbackInclusionStatus.PENDING

        assert await asyncio.wait_for(
            host._coordinate_user_control_feedback_at_root_completion("turn:A"),
            timeout=0.1,
        )
        assert attempt.normal_end_coordination_fenced

    asyncio.run(scenario())


def test_pr03_accepted_feedback_stops_fencing_at_its_existing_watchdog() -> None:
    async def scenario() -> None:
        host, attempt, _provider_text = _feedback_host()
        host._canonical_deadline = lambda: monotonic() + 1
        attempt.normal_end_coordination_deadline = monotonic() - 1

        results = [
            await host._coordinate_user_control_feedback_at_root_completion("turn:A")
            for _ in range(3)
        ]

        assert results == [False, False, False]
        assert attempt.normal_end_coordination_exhausted

    asyncio.run(scenario())


def test_pr03_normal_root_completion_does_not_wait_past_existing_watchdog() -> None:
    async def scenario() -> None:
        host, attempt, _provider_text = _feedback_host()
        feedback = attempt.outcome.user_control.feedback
        assert feedback is not None
        host._replace_control_feedback_locked(
            attempt,
            replace(
                feedback,
                canonical_status=FeedbackCanonicalStatus.PENDING,
                entry_id=None,
            ),
        )
        attempt.feedback_candidate = None
        attempt.feedback_candidate_ready.clear()
        host._canonical_deadline = lambda: monotonic() + 0.01
        assert not await host._coordinate_user_control_feedback_at_root_completion(
            "turn:A"
        )
        assert attempt.normal_end_coordination_exhausted

    asyncio.run(scenario())


def test_pr03_target_end_exact_confirms_an_ambiguous_feedback_commit() -> None:
    async def scenario() -> None:
        host, attempt, _provider_text = _feedback_host()
        active = asyncio.create_task(asyncio.Event().wait())
        host._active_task = active
        host._active_turn_id = "turn:A"
        host.workspace = SimpleNamespace(workspace_key="workspace:one")
        host._lease = SimpleNamespace(guard=SimpleNamespace(writer_generation=1))
        host._canonical_deadline = lambda: monotonic() + 1
        attempt.process_at_admission = SimpleNamespace(
            process_id="process:one",
            command="python worker.py",
            cwd="/tmp",
            origin=SimpleNamespace(turn_id="turn:origin", scope_subagent_task_id=None),
        )
        feedback = attempt.outcome.user_control.feedback
        assert feedback is not None
        host._replace_control_feedback_locked(
            attempt,
            replace(
                feedback,
                canonical_status=FeedbackCanonicalStatus.PENDING,
                inclusion_status=FeedbackInclusionStatus.PENDING,
                entry_id=None,
            ),
        )
        install_observed = asyncio.Event()

        class _AmbiguousRunner:
            async def install_user_control_feedback(
                self, *, attempt, deadline_monotonic
            ):
                del attempt, deadline_monotonic
                install_observed.set()
                raise ConnectionError("commit response lost")

            async def confirm_user_control_feedback(
                self, *, attempt, deadline_monotonic
            ):
                del deadline_monotonic
                return SimpleNamespace(entry_id=attempt.entry_id)

        host._runner = _AmbiguousRunner()
        installation = asyncio.create_task(
            host._install_user_control_feedback_for_attempt(
                attempt,
                process=SimpleNamespace(
                    disposition="TERMINATION_COMPLETED",
                    status="killed",
                    exit_code=-15,
                    physical_state="PHYSICALLY_JOINED",
                    group_alive=False,
                ),
                monitor=None,
                public_code="BACKGROUND_CONTROL_COMPLETED",
                public_detail="done",
            )
        )
        await install_observed.wait()
        async with host._lock:
            host._active_task = None
            host._active_turn_id = None
            host._mark_feedback_target_ended_locked("turn:A")
            still_pending = attempt.outcome.user_control.feedback
            assert still_pending is not None
            assert still_pending.canonical_status is FeedbackCanonicalStatus.PENDING
        await installation
        final = attempt.outcome.user_control.feedback
        assert final is not None
        assert final.canonical_status is FeedbackCanonicalStatus.ACCEPTED
        assert (
            final.inclusion_status
            is FeedbackInclusionStatus.TARGET_ENDED_BEFORE_INCLUSION
        )
        assert final.reason == "TARGET_CLOSED"
        assert final.entry_id is not None
        active.cancel()
        await asyncio.gather(active, return_exceptions=True)

    asyncio.run(scenario())


def test_pr03_host_close_exact_confirms_an_ambiguous_feedback_commit() -> None:
    async def scenario() -> None:
        host, attempt, _provider_text = _feedback_host()
        active = asyncio.create_task(asyncio.Event().wait())
        host._active_task = active
        host._active_turn_id = "turn:A"
        host.workspace = SimpleNamespace(workspace_key="workspace:one")
        host._lease = SimpleNamespace(guard=SimpleNamespace(writer_generation=1))
        host._canonical_deadline = lambda: monotonic() + 1
        attempt.process_at_admission = SimpleNamespace(
            process_id="process:one",
            command="python worker.py",
            cwd="/tmp",
            origin=SimpleNamespace(turn_id="turn:origin", scope_subagent_task_id=None),
        )
        feedback = attempt.outcome.user_control.feedback
        assert feedback is not None
        host._replace_control_feedback_locked(
            attempt,
            replace(
                feedback,
                canonical_status=FeedbackCanonicalStatus.PENDING,
                inclusion_status=FeedbackInclusionStatus.PENDING,
                entry_id=None,
            ),
        )
        install_observed = asyncio.Event()
        release_install = asyncio.Event()
        confirmations: list[str] = []

        class _AmbiguousRunner:
            async def install_user_control_feedback(
                self, *, attempt, deadline_monotonic
            ):
                del attempt, deadline_monotonic
                install_observed.set()
                await release_install.wait()
                raise ConnectionError("commit response lost")

            async def confirm_user_control_feedback(
                self, *, attempt, deadline_monotonic
            ):
                del deadline_monotonic
                confirmations.append(attempt.entry_id)
                return SimpleNamespace(entry_id=attempt.entry_id)

        host._runner = _AmbiguousRunner()
        installation = asyncio.create_task(
            host._install_user_control_feedback_for_attempt(
                attempt,
                process=SimpleNamespace(
                    disposition="TERMINATION_COMPLETED",
                    status="killed",
                    exit_code=-15,
                    physical_state="PHYSICALLY_JOINED",
                    group_alive=False,
                ),
                monitor=None,
                public_code="BACKGROUND_CONTROL_COMPLETED",
                public_detail="done",
            )
        )
        await install_observed.wait()
        assert attempt.feedback_candidate is not None
        assert attempt.feedback_provider_text is not None
        request = _provider_request(
            turn_id="turn:A",
            entry_id=attempt.feedback_candidate.entry_id,
            body=attempt.feedback_provider_text,
        )
        await host._record_user_control_provider_input_install(request)
        await host._record_user_control_transport_invocation(
            request, succeeded=True, detail=None
        )
        async with host._lock:
            host._closing = True
        release_install.set()
        await installation

        final = attempt.outcome.user_control.feedback
        assert final is not None
        assert attempt.feedback_candidate is not None
        assert confirmations == [attempt.feedback_candidate.entry_id]
        assert final.canonical_status is FeedbackCanonicalStatus.ACCEPTED
        assert final.inclusion_status is FeedbackInclusionStatus.INCLUDED
        assert final.owner_availability is FeedbackOwnerAvailability.UNAVAILABLE
        assert final.entry_id == attempt.feedback_candidate.entry_id
        assert final.context_binding_revision_id == "revision:one"
        assert final.model_call_index == 2
        assert final.transport.invocation_attempted
        assert final.transport.invocation_succeeded is True
        active.cancel()
        await asyncio.gather(active, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("reason", ("INPUT_COMPILE_FAILED", "INPUT_ADMISSION_FAILED"))
def test_pr03_terminal_input_selection_failure_closes_only_pending_inclusion(
    reason: str,
) -> None:
    async def scenario() -> None:
        host, attempt, provider_text = _feedback_host()
        await host._record_user_control_provider_input_failure("turn:A", reason)
        feedback = attempt.outcome.user_control.feedback
        assert feedback is not None
        assert feedback.canonical_status is FeedbackCanonicalStatus.ACCEPTED
        assert feedback.inclusion_status is FeedbackInclusionStatus.FAILED
        assert feedback.reason == reason
        host._mark_feedback_target_ended_locked("turn:A")
        assert attempt.outcome.user_control.feedback == feedback

        included_host, included_attempt, _ = _feedback_host()
        await included_host._record_user_control_provider_input_install(
            _provider_request(
                turn_id="turn:A",
                entry_id="entry:feedback",
                body=provider_text,
            )
        )
        included = included_attempt.outcome.user_control.feedback
        assert included is not None
        assert included.inclusion_status is FeedbackInclusionStatus.INCLUDED
        await included_host._record_user_control_provider_input_failure(
            "turn:A", reason
        )
        assert included_attempt.outcome.user_control.feedback == included

    asyncio.run(scenario())


def test_pr03_feedback_projection_is_typed_and_preserves_literal_facts() -> None:
    content = _content()
    projection = project_user_control_feedback_for_provider(content.canonical_bytes())
    projected = json.loads(projection)["pulsara_user_control_feedback"]
    assert "python worker.py --literal 你好" in projection
    assert '"source":"USER_CONTROL"' in projection
    assert '"other_work_stopped":false' in projection
    assert "schema_version" not in projected
    assert "user_control_feedback.v1" not in projection
    assert USER_CONTROL_FEEDBACK_MEDIA_TYPE.endswith("+json")
    with pytest.raises(ValueError, match="identity"):
        project_user_control_feedback_for_provider(
            content.canonical_bytes().replace(
                b'"source":"USER_CONTROL"', b'"source":"HUMAN"'
            )
        )
    parsed = content.canonical_mapping()
    parsed["unexpected"] = "not part of the closed contract"
    with pytest.raises(ValueError, match="identity"):
        project_user_control_feedback_for_provider(json.dumps(parsed).encode("utf-8"))
    malformed_process = content.canonical_mapping()
    malformed_process["process"] = {"status": "killed"}
    with pytest.raises(ValueError, match="process"):
        project_user_control_feedback_for_provider(
            json.dumps(malformed_process).encode("utf-8")
        )


@pytest.mark.postgres
def test_pr03_feedback_entry_and_unique_event_commit_together_and_lower_exactly(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    deadline = monotonic() + 30
    workspace_id = _id("workspace")
    lease = repository.acquire_host_writer(
        session_id=_id("session"),
        workspace_id=workspace_id,
        writer_owner_id=_id("host"),
        lease_seconds=30,
        deadline_monotonic=deadline,
    )
    turn_id = _id("turn")
    binding = bind_test_session(repository, lease)
    start_test_root_turn(
        repository,
        lease.guard,
        command_id=_id("command"),
        turn_id=turn_id,
        entry_id=_id("entry"),
        context_binding_revision_id=_id("revision"),
        permission_snapshot_id=_id("permission"),
        requested_permission_mode=DEFAULT_PERMISSION_MODE,
        model_call_binding=binding,
        content=InlineContent.from_bytes(b"keep working"),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    base = _content(session_id=lease.guard.session_id)
    content = replace(base, target_root_turn_id=turn_id)
    candidate = UserControlFeedbackInstallationAttempt(
        session_id=lease.guard.session_id,
        workspace_id=workspace_id,
        writer_generation=lease.guard.writer_generation,
        target_root_turn_id=turn_id,
        entry_id=_id("entry:user-control"),
        content=content,
        occurred_at=datetime.now(timezone.utc),
        actor_id="host:one",
    )
    safe_point = ProviderSafePointCoordinator(repository=repository, guard=lease.guard)
    accepted = safe_point.install_user_control_feedback(
        attempt=candidate, deadline_monotonic=deadline
    )
    confirmed = safe_point.install_user_control_feedback(
        attempt=candidate, deadline_monotonic=deadline
    )
    assert confirmed == accepted
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=deadline,
    ) as connection:
        entry_count = connection.execute(
            "SELECT count(*) FROM pulsara_v3.transcript_entries WHERE session_id=%s AND id=%s",
            (lease.guard.session_id, candidate.entry_id),
        ).fetchone()[0]
        event_count = connection.execute(
            "SELECT count(*) FROM pulsara_v3.agent_events WHERE session_id=%s "
            "AND event_type='UserControlFeedbackAccepted' AND subject_entry_id=%s",
            (lease.guard.session_id, candidate.entry_id),
        ).fetchone()[0]
    assert (entry_count, event_count) == (1, 1)

    cut = repository.prepare_provider_input_cut(
        lease.guard, turn_id=turn_id, deadline_monotonic=deadline
    )
    snapshot = CanonicalProviderInputReader(provider).read_frozen_snapshot(
        cut, deadline_monotonic=deadline
    )
    item = next(
        value for value in snapshot.items if value.source_entry_id == candidate.entry_id
    )
    assert item.item_kind is ProviderInputItemKind.USER
    assert item.input_origin is CanonicalInputOriginKind.USER_CONTROL_FEEDBACK
    assert item.text == project_user_control_feedback_for_provider(
        candidate.content.canonical_bytes()
    )

    protocol_snapshot = CanonicalProtocolReader(provider).snapshot(
        session_id=lease.guard.session_id,
        maximum_entries=256,
        maximum_control_items=MAXIMUM_CONTROL_ITEMS,
        deadline_monotonic=deadline,
    )
    projected = next(
        value
        for value in protocol_snapshot.entries
        if value.entry_id == candidate.entry_id
    )
    assert projected.entry_kind == wire.USER_CONTROL_FEEDBACK
