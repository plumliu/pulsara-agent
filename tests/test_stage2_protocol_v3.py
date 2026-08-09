from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from pulsara_agent.conversation_kernel.host import KernelCommandOutcome
from pulsara_agent.conversation_kernel.interaction import KernelInteractionCoordinator
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.terminal_protocol.canonical_v3 import (
    COMMITTED_PROJECTION_BRANCH_BY_TYPE,
)
from pulsara_agent.conversation_kernel.vocabulary import CommittedEventType
from pulsara_agent.conversation_kernel.live_control import (
    CurrentInteractionView,
    LiveControlObservationKind,
    SessionLiveControlOwner,
)
from pulsara_agent.conversation_kernel.contracts import HostWriterGuard
from pulsara_agent.conversation_kernel.repository import (
    AcceptedInteractionDecision,
    ConversationKernelConflict,
)
from pulsara_agent.terminal_protocol.generated_v3 import terminal_kernel_v3_pb2 as wire
from pulsara_agent.terminal_protocol.v3_gateway import (
    MAXIMUM_PROMPT_BYTES,
    TerminalKernelProtocolServer,
    _Connection,
)


class _CommandHost:
    session_id = "session:test"

    def __init__(self) -> None:
        self.submitted: list[tuple[str, str]] = []
        self.steered: list[tuple[str, str, str]] = []
        self.resolved: list[dict[str, object]] = []

    async def submit_prompt(self, *, command_id: str, text: str) -> KernelCommandOutcome:
        self.submitted.append((command_id, text))
        return KernelCommandOutcome(
            command_id, "PENDING", "queue:item", "PROMPT_QUEUED", "Queued."
        )

    async def stop_current_turn(self) -> bool:
        return False

    async def steer_active_turn(
        self, *, command_id: str, text: str, target_turn_id: str
    ) -> KernelCommandOutcome:
        self.steered.append((command_id, text, target_turn_id))
        return KernelCommandOutcome(
            command_id,
            "PENDING",
            target_turn_id,
            "PROMPT_QUEUED",
            "Steer queued.",
        )

    async def query_command(self, command_id: str) -> None:
        del command_id
        return None

    async def resolve_tool_interaction(self, **kwargs) -> KernelCommandOutcome:
        self.resolved.append(dict(kwargs))
        return KernelCommandOutcome(
            str(kwargs["command_id"]),
            "SUCCEEDED",
            "decision:1",
            "INTERACTION_ALLOW",
            "Accepted.",
        )


def _server() -> TerminalKernelProtocolServer:
    return TerminalKernelProtocolServer(
        socket_path=Path("/tmp/pulsara-stage2-protocol-test.sock"),
        session_provider=lambda _: (_ for _ in ()).throw(KeyError()),
    )


def _state(*, role: int) -> _Connection:
    return _Connection(
        attachment_id="attachment:test",
        attachment_generation=1,
        host_session=_CommandHost(),  # type: ignore[arg-type]
        granted_role=role,
        authenticated=True,
    )


def test_stage2_observer_cannot_mutate_but_can_detach() -> None:
    server = _server()
    observer = _state(role=wire.ATTACHMENT_ROLE_OBSERVER)
    rejected = asyncio.run(
        server._command(
            observer,
            wire.CommandRequest(
                request_id="request:submit",
                command_id="command:submit",
                client_submission_id="command:submit",
                command_kind=wire.SUBMIT_PROMPT,
                text="hello",
            ),
        )
    )
    assert rejected.error.stable_code == "CONTROLLER_REQUIRED"
    assert observer.host_session.submitted == []

    detached = asyncio.run(
        server._command(
            observer,
            wire.CommandRequest(
                request_id="request:detach",
                command_id="command:detach",
                client_submission_id="command:detach",
                command_kind=wire.DETACH,
            ),
        )
    )
    assert detached.command_outcome.status == wire.SUCCEEDED


def test_stage2_controller_prompt_bounds_are_authoritative() -> None:
    server = _server()
    controller = _state(role=wire.ATTACHMENT_ROLE_CONTROLLER)
    for text in ("", "bad\x00text", "x" * (MAXIMUM_PROMPT_BYTES + 1)):
        result = asyncio.run(
            server._command(
                controller,
                wire.CommandRequest(
                    request_id="request:submit",
                    command_id="command:submit",
                    client_submission_id="command:submit",
                    command_kind=wire.SUBMIT_PROMPT,
                    text=text,
                ),
            )
        )
        assert result.error.stable_code == "PROMPT_INVALID"
    assert controller.host_session.submitted == []


def test_stage2_controller_can_send_an_exact_active_turn_steer() -> None:
    server = _server()
    controller = _state(role=wire.ATTACHMENT_ROLE_CONTROLLER)
    result = asyncio.run(
        server._command(
            controller,
            wire.CommandRequest(
                request_id="request:steer",
                command_id="command:steer",
                client_submission_id="command:steer",
                command_kind=wire.STEER_ACTIVE_TURN,
                text="new direction",
                target_turn_id="turn:active",
            ),
        )
    )
    assert result.command_outcome.status == wire.PENDING
    assert controller.host_session.steered == [
        ("command:steer", "new direction", "turn:active")
    ]


def test_stage2_interaction_resolution_is_controller_only_and_exact() -> None:
    server = _server()
    request = wire.ResolveInteractionRequest(
        request_id="request:resolve",
        command_id="command:resolve",
        expected_writer_generation=3,
        expected_owner_epoch=3,
        expected_live_revision=4,
        interaction_id="interaction:1",
        decision=wire.INTERACTION_ALLOW,
    )
    observer = _state(role=wire.ATTACHMENT_ROLE_OBSERVER)
    rejected = asyncio.run(server._resolve_interaction(observer, request))
    assert rejected.error.stable_code == "CONTROLLER_REQUIRED"
    assert observer.host_session.resolved == []

    controller = _state(role=wire.ATTACHMENT_ROLE_CONTROLLER)
    accepted = asyncio.run(server._resolve_interaction(controller, request))
    assert accepted.command_outcome.status == wire.SUCCEEDED
    assert controller.host_session.resolved == [
        {
            "expected_writer_generation": 3,
            "expected_owner_epoch": 3,
            "expected_live_revision": 4,
            "interaction_id": "interaction:1",
            "command_id": "command:resolve",
            "decision": "ALLOW",
            "actor_id": "attachment:test",
        }
    ]


class _InteractionRepository:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def accept_tool_interaction_decision(self, guard, **kwargs):
        del guard
        self.calls.append(dict(kwargs))
        return AcceptedInteractionDecision(
            str(kwargs["decision_id"]),
            str(kwargs["command_id"]),
            str(kwargs["decision"]),
            str(kwargs["assistant_entry_id"]),
            str(kwargs["tool_call_id"]),
            kwargs["attempt_id"],
            kwargs["result_entry_id"],
        )


def test_stage2_pending_interaction_is_same_host_ephemeral_and_stale_safe() -> None:
    async def exercise() -> None:
        repository = _InteractionRepository()
        guard = HostWriterGuard(
            session_id="session:test",
            writer_generation=7,
            writer_owner_id="host:test",
        )
        owner = SessionLiveControlOwner(session_id="session:test", owner_epoch=7)
        coordinator = KernelInteractionCoordinator(
            repository=repository,  # type: ignore[arg-type]
            guard=guard,
            live_control=owner,
            live_bus=LiveAgentEventBus(),
            io_owner=KernelSessionIO(),
        )
        assert coordinator.attach_controller("attachment:1")
        waiter = asyncio.create_task(
            coordinator.request_tool_confirmation(
                turn_id="turn:1",
                assistant_entry_id="entry:assistant",
                tool_call_id="call:1",
                tool_name="terminal",
            )
        )
        await asyncio.sleep(0)
        snapshot = owner.current_snapshot()
        assert snapshot.owner_epoch == 7
        assert snapshot.current_interaction is not None
        with pytest.raises(ConversationKernelConflict):
            await coordinator.resolve_tool_interaction(
                expected_writer_generation=7,
                expected_owner_epoch=7,
                expected_live_revision=snapshot.revision + 1,
                interaction_id=snapshot.current_interaction.interaction_id,
                command_id="command:stale",
                decision="ALLOW",
                actor_id="attachment:1",
            )
        accepted = await coordinator.resolve_tool_interaction(
            expected_writer_generation=7,
            expected_owner_epoch=7,
            expected_live_revision=snapshot.revision,
            interaction_id=snapshot.current_interaction.interaction_id,
            command_id="command:allow",
            decision="ALLOW",
            actor_id="attachment:1",
        )
        resolution = await waiter
        assert accepted.attempt_id == resolution.attempt_id
        assert owner.current_snapshot().current_interaction is None
        assert len(repository.calls) == 1
        await coordinator.aclose()

    asyncio.run(exercise())


def test_stage2_live_control_snapshot_and_cursor_are_linearized() -> None:
    owner = SessionLiveControlOwner(
        session_id="session:test", maximum_events=2, maximum_public_bytes=4096
    )
    subscriber, snapshot = owner.snapshot_and_subscribe()
    assert snapshot.revision == 0 and snapshot.current_interaction is None
    first = CurrentInteractionView(
        "interaction:1", "APPROVAL", "Allow the tool?", ("allow", "deny"), ""
    )
    opened = owner.install_interaction(first)
    repeated = owner.observe(
        subscriber, owner_epoch=1, after_revision=0, maximum_events=2
    )
    assert repeated.events == (opened,)
    assert owner.observe(
        subscriber, owner_epoch=1, after_revision=0, maximum_events=2
    ) == repeated
    second = CurrentInteractionView(
        "interaction:2", "PLAN", "Accept the plan?", ("yes", "no"), ""
    )
    owner.install_interaction(second, replace_expected_interaction_id="interaction:1")
    owner.close_interaction(expected_interaction_id="interaction:2")
    gap = owner.observe(
        subscriber, owner_epoch=1, after_revision=0, maximum_events=2
    )
    assert gap.kind is LiveControlObservationKind.GAP


def test_stage2_protocol_v3_closed_vocabularies_are_exact() -> None:
    committed = {
        item.name
        for item in wire.CommittedEventType.DESCRIPTOR.values
        if item.number != 0
    }
    live = {
        item.name for item in wire.LiveEventType.DESCRIPTOR.values if item.number != 0
    }
    assert len(committed) == 26
    assert len(live) == 23
    assert set(COMMITTED_PROJECTION_BRANCH_BY_TYPE) == {
        item.value for item in CommittedEventType
    }
    assert {
        key
        for key, value in COMMITTED_PROJECTION_BRANCH_BY_TYPE.items()
        if value == "IMMUTABLE_ENTRY"
    } == {
        "UserMessageAccepted",
        "AssistantMessageAccepted",
        "AssistantToolRequestAccepted",
        "ToolResultAccepted",
        "UserSteerAccepted",
    }
    assert sum(
        value == "CURRENT_CONTROL"
        for value in COMMITTED_PROJECTION_BRANCH_BY_TYPE.values()
    ) == 17
    assert sum(
        value == "EVENT_ONLY"
        for value in COMMITTED_PROJECTION_BRANCH_BY_TYPE.values()
    ) == 4
    assert {item.name for item in wire.ObservationGapKind.DESCRIPTOR.values} == {
        "OBSERVATION_GAP_KIND_UNSPECIFIED",
        "COMMITTED_GAP",
        "LIVE_GAP",
        "LIVE_CONTROL_GAP",
    }
