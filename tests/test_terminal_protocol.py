from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.support.terminal_protocol import HeadlessTerminalConformanceClient
from tests.test_host_core import ScriptedTransport, _core, _open_project_session

from pulsara_agent.terminal_protocol.gateway import TerminalProtocolServer
from pulsara_agent.terminal_protocol.codec import HEARTBEAT_INTERVAL_MS
from pulsara_agent.terminal_protocol.generated import terminal_client_pb2 as wire
from pulsara_agent.event_log.in_memory import InMemoryEventLog
from pulsara_agent.ports.terminal_application import (
    SubmitPromptRequest,
    TerminalCommandBinding,
    TerminalCommandOutcome,
    terminal_command_outcome_fingerprint,
)
from pulsara_agent.runtime.terminal_application.command_receipt import (
    build_terminal_command_receipt_storage,
)
from pulsara_agent.runtime.terminal_application.services import (
    TerminalCommandOwner,
    terminal_request_semantic_fingerprint,
)
from pulsara_agent.runtime.terminal_application.secret import TerminalMcpSecretService


def _submit_request(*, command_id: str, text: str) -> SubmitPromptRequest:
    binding = TerminalCommandBinding(
        client_instance_id="headless:receipt",
        attachment_id="attachment:receipt",
        attachment_generation=1,
        command_id=command_id,
        runtime_session_id="runtime:receipt",
        expected_target_id="host:receipt",
        expected_target_generation=1,
        expected_controller_generation=1,
        request_semantic_fingerprint="placeholder",
    )
    request = SubmitPromptRequest(
        command_kind="submit_prompt",
        binding=binding,
        client_submission_id="submission:receipt",
        text=text,
        requested_delivery_mode="auto",
        request_fingerprint="placeholder",
    )
    semantic = terminal_request_semantic_fingerprint(request)
    return replace(
        request,
        binding=replace(binding, request_semantic_fingerprint=semantic),
        request_fingerprint=semantic,
    )


def _successful_outcome(request: SubmitPromptRequest) -> TerminalCommandOutcome:
    public_text = "The command completed."
    references = ("event:receipt",)
    return TerminalCommandOutcome(
        status="succeeded",
        command_id=request.binding.command_id,
        target_id=request.binding.expected_target_id,
        target_generation=request.binding.expected_target_generation,
        public_result_code="TEST_SUCCEEDED",
        public_result_text=public_text,
        durable_reference_ids=references,
        query_token="query:receipt",
        outcome_fingerprint=terminal_command_outcome_fingerprint(
            status="succeeded",
            command_id=request.binding.command_id,
            target_id=request.binding.expected_target_id,
            target_generation=request.binding.expected_target_generation,
            public_result_code="TEST_SUCCEEDED",
            public_result_text=public_text,
            durable_reference_ids=references,
            query_token="query:receipt",
        ),
    )


def _pending_outcome(request: SubmitPromptRequest) -> TerminalCommandOutcome:
    public_text = "The command is durably admitted."
    return TerminalCommandOutcome(
        status="pending_confirmation",
        command_id=request.binding.command_id,
        target_id=request.binding.expected_target_id,
        target_generation=request.binding.expected_target_generation,
        public_result_code="COMMAND_OWNER_RUNNING",
        public_result_text=public_text,
        durable_reference_ids=(),
        query_token="query:pending-recovery",
        outcome_fingerprint=terminal_command_outcome_fingerprint(
            status="pending_confirmation",
            command_id=request.binding.command_id,
            target_id=request.binding.expected_target_id,
            target_generation=request.binding.expected_target_generation,
            public_result_code="COMMAND_OWNER_RUNNING",
            public_result_text=public_text,
            durable_reference_ids=(),
            query_token="query:pending-recovery",
        ),
    )


def test_mcp_form_secret_handle_binds_exact_request_owner_and_expires() -> None:
    request = SimpleNamespace(
        key="form:profile",
        mode="form",
        request_fingerprint="sha256:" + "1" * 64,
    )
    batch_identity = SimpleNamespace(
        owner_id="mcp-batch:one",
        owner_generation=1,
        round_ordinal=1,
        request_set_fingerprint="sha256:" + "2" * 64,
        ordered_request_keys=(request.key,),
    )
    batch_owner = SimpleNamespace(
        identity=batch_identity,
        item_slots=(SimpleNamespace(request=request),),
    )
    handle = SimpleNamespace(elicitation_batch_owner=batch_owner)
    execution_port = Mock()
    execution_port.handle_for_interaction.return_value = handle
    runtime = SimpleNamespace(mcp_tool_execution_port=execution_port)
    host = Mock()
    host.get_pending_interaction.return_value = SimpleNamespace(
        interaction_id="interaction:form", kind="mcp_input_required"
    )
    host.wiring.runtime_wiring.runtime_session = runtime
    attachments = Mock()
    service = TerminalMcpSecretService(
        host_session=host,
        attachments=attachments,
        lease_ttl_seconds=0.03,
    )
    binding = TerminalCommandBinding(
        client_instance_id="headless:secret",
        attachment_id="attachment:secret",
        attachment_generation=4,
        command_id="command:secret",
        runtime_session_id="runtime:secret",
        expected_target_id="host:secret",
        expected_target_generation=2,
        expected_controller_generation=7,
        request_semantic_fingerprint="sha256:" + "3" * 64,
    )
    payload = b'{"form:profile":{"action":"accept","content":{}}}'

    with pytest.raises(ValueError, match="request identity"):
        service.seal_form_response(
            binding=binding,
            interaction_id="interaction:form",
            request_key="form:other",
            plaintext_json=payload,
        )

    stale_handle = service.seal_form_response(
        binding=binding,
        interaction_id="interaction:form",
        request_key=request.key,
        plaintext_json=payload,
    )
    batch_owner.identity = SimpleNamespace(
        **{**batch_identity.__dict__, "owner_id": "mcp-batch:successor"}
    )
    with pytest.raises(RuntimeError, match="exact request owner changed"):
        service.consume_response(
            handle_id=stale_handle,
            binding=binding,
            interaction_id="interaction:form",
        )

    batch_owner.identity = batch_identity
    expiring_handle = service.seal_form_response(
        binding=binding,
        interaction_id="interaction:form",
        request_key=request.key,
        plaintext_json=payload,
    )
    assert Event().wait(0.06) is False
    with pytest.raises(RuntimeError, match="unavailable"):
        service.consume_response(
            handle_id=expiring_handle,
            binding=binding,
            interaction_id="interaction:form",
        )
    service.close()


def test_headless_client_consumes_snapshot_delta_page_command_and_gap(
    tmp_path: Path, monkeypatch
) -> None:
    core = _core(monkeypatch, ScriptedTransport([{"text": "wire-visible reply"}]))

    async def scenario() -> None:
        session = await _open_project_session(core, tmp_path)
        socket_root = TemporaryDirectory(prefix="pulsara-terminal-", dir="/tmp")
        socket_path = Path(socket_root.name) / "terminal-v1.sock"
        server = TerminalProtocolServer(
            socket_path=socket_path,
            session_provider=lambda host_id: (
                session
                if host_id == session.host_session_id
                else (_ for _ in ()).throw(KeyError(host_id))
            ),
        )
        await server.start()
        client = HeadlessTerminalConformanceClient(
            socket_path=socket_path,
            client_instance_id="headless:test",
            launch_capability=server.launch_capability,
        )
        await client.connect(controller=True)
        await client.attach(session.host_session_id, controller=True)
        initial = await client.snapshot()
        assert initial.host_session_id == session.host_session_id
        assert initial.runtime_session_id == session.runtime_session_id
        operational_initial = await client.operational_snapshot()
        assert operational_initial.operational_generation == 1
        assert operational_initial.operational_cursor == 0
        assert not operational_initial.ordered_activity_cells

        operational_store = (
            session.wiring.runtime_wiring.runtime_session.ui_operational_activity_store
        )
        assert operational_store.offer_nowait(
            activity_kind="model_activity",
            owner_kind="model_call",
            owner_id="model-call:headless-test",
            owner_generation=1,
            coalesce_key="model:model-call:headless-test",
            replacement_semantics="expire_at_terminal",
            public_text="Generating a response…",
        )
        operational_delta = await client.observe_next(
            authority_high_water=initial.authority_high_water,
            projection_revision=initial.projection_revision,
            operational_generation=operational_initial.operational_generation,
            operational_cursor=operational_initial.operational_cursor,
        )
        assert operational_delta.WhichOneof("outcome") == "operational_delta"
        assert len(operational_delta.operational_delta.ordered_changes) == 1
        assert (
            operational_delta.operational_delta.ordered_changes[0].WhichOneof("change")
            == "upsert"
        )
        operational_cursor = operational_delta.operational_delta.operational_cursor
        assert operational_store.retire_nowait(
            coalesce_key="model:model-call:headless-test",
            owner_kind="model_call",
            owner_id="model-call:headless-test",
            owner_generation=1,
            reason="durable_terminal",
        )
        operational_remove = await client.observe_next(
            authority_high_water=initial.authority_high_water,
            projection_revision=initial.projection_revision,
            operational_generation=operational_initial.operational_generation,
            operational_cursor=operational_cursor,
        )
        assert operational_remove.WhichOneof("outcome") == "operational_delta"
        assert (
            operational_remove.operational_delta.ordered_changes[0].WhichOneof("change")
            == "remove"
        )

        command = await client.submit_prompt(
            target_id=session.host_session_id,
            text="hello over the formal terminal protocol",
        )
        assert command.status == "pending_confirmation"
        winner = None
        for _ in range(200):
            query = await client.query_command(command.command_id)
            if query.found and query.outcome.status != "pending_confirmation":
                winner = query.outcome
                break
            await asyncio.sleep(0.01)
        assert winner is not None
        assert winner.status == "succeeded"

        final = None
        for _ in range(200):
            candidate = await client.snapshot()
            contains_target_assistant = any(
                "wire-visible reply" in block.text.text
                for ranked in candidate.ordered_resident_entries
                for block in ranked.entry.durable_history_cell.assistant_message.common.content_blocks
                if block.WhichOneof("block") == "text"
            )
            if contains_target_assistant:
                final = candidate
                break
            await asyncio.sleep(0.01)
        assert final is not None
        assert final.authority_high_water > initial.authority_high_water
        assert any(
            "wire-visible reply" in block.text.text
            for ranked in final.ordered_resident_entries
            for block in ranked.entry.durable_history_cell.assistant_message.common.content_blocks
            if block.WhichOneof("block") == "text"
        )

        delta = await client.observe_next(
            authority_high_water=max(0, final.authority_high_water - 1),
            projection_revision=max(0, final.projection_revision - 1),
            operational_cursor=0,
        )
        assert delta.WhichOneof("outcome") == "root_advanced"

        gap = await client.observe_next(
            authority_high_water=final.authority_high_water + 1,
            projection_revision=final.projection_revision + 1,
            operational_cursor=0,
        )
        assert gap.WhichOneof("outcome") == "gap"

        rebuilt_projection, rebuilt_operational = await client.rebuild_after_gap()
        assert rebuilt_projection.authority_high_water >= final.authority_high_water
        assert rebuilt_projection.projection_revision >= final.projection_revision
        assert rebuilt_operational.operational_generation == (
            operational_initial.operational_generation
        )
        for _ in range(200):
            rebuilt_no_change = await client.observe_next(
                authority_high_water=rebuilt_projection.authority_high_water,
                projection_revision=rebuilt_projection.projection_revision,
                operational_generation=rebuilt_operational.operational_generation,
                operational_cursor=rebuilt_operational.operational_cursor,
                maximum_wait_ms=1,
            )
            if rebuilt_no_change.WhichOneof("outcome") == "no_change":
                break
            assert rebuilt_no_change.WhichOneof("outcome") in {
                "root_advanced",
                "operational_delta",
            }
            rebuilt_projection, rebuilt_operational = await client.rebuild_after_gap()
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("formal terminal projection did not quiesce")

        cursors = rebuilt_projection.latest_root_cursor_pair
        cursor = cursors.after_cursor if cursors.HasField("after_cursor") else None
        assert cursor is not None
        page = await client.page(
            cursor,
            direction=wire.HistoryPageRequest.BEFORE,
        )
        assert page.WhichOneof("outcome") == "page"

        detach = await client.detach()
        assert detach.status == "succeeded"
        await client.close()
        # Reconnect with the same stable client identity.  The durable command
        # receipt must remain queryable without touching HostSession internals.
        await client.connect(controller=False)
        await client.attach(session.host_session_id, controller=False)
        reconnected_query = await client.query_command(command.command_id)
        assert reconnected_query.found
        assert reconnected_query.outcome.command_id == winner.command_id
        assert reconnected_query.outcome.status == winner.status
        assert (
            reconnected_query.outcome.outcome_fingerprint == winner.outcome_fingerprint
        )
        await client.close()
        await server.close()
        socket_root.cleanup()
        await core.shutdown()

    asyncio.run(scenario())


def test_terminal_protocol_schema_has_no_raw_event_or_renderer_vocabulary() -> None:
    schema = Path(
        "src/pulsara_agent/terminal_protocol/schema/terminal_client.proto"
    ).read_text(encoding="utf-8")
    assert "AgentEvent" not in schema
    assert "RawStoredEventEnvelope" not in schema
    assert "layout" not in schema.lower()
    assert "color" not in schema.lower()
    assert not tuple(Path(".").rglob("*.go"))
    assert not tuple(Path(".").rglob("go.mod"))


def test_maximum_observation_wait_leaves_time_for_attachment_heartbeat(
    tmp_path: Path, monkeypatch
) -> None:
    core = _core(monkeypatch, ScriptedTransport([]))

    async def scenario() -> None:
        session = await _open_project_session(core, tmp_path)
        socket_root = TemporaryDirectory(prefix="pulsara-terminal-", dir="/tmp")
        socket_path = Path(socket_root.name) / "terminal-v1.sock"
        server = TerminalProtocolServer(
            socket_path=socket_path,
            session_provider=lambda _host_id: session,
        )
        await server.start()
        client = HeadlessTerminalConformanceClient(
            socket_path=socket_path,
            client_instance_id="headless:long-poll",
            launch_capability=server.launch_capability,
        )
        await client.connect(controller=False)
        await client.attach(session.host_session_id, controller=False)
        projection = await client.snapshot()
        operational = await client.operational_snapshot()
        assert client.hello is not None
        maximum_wait_ms = client.hello.negotiated_limits.maximum_observation_wait_ms
        assert 0 < maximum_wait_ms < HEARTBEAT_INTERVAL_MS

        no_change = await client.observe_next(
            authority_high_water=projection.authority_high_water,
            projection_revision=projection.projection_revision,
            operational_generation=operational.operational_generation,
            operational_cursor=operational.operational_cursor,
            maximum_wait_ms=maximum_wait_ms,
        )
        assert no_change.WhichOneof("outcome") == "no_change"
        heartbeat = await client.heartbeat()
        assert heartbeat.attachment_id

        await client.close()
        await server.close()
        socket_root.cleanup()
        await core.shutdown()

    asyncio.run(scenario())


def test_terminal_command_outcome_is_queryable_after_process_owner_replacement() -> (
    None
):
    async def scenario() -> None:
        event_log = InMemoryEventLog(runtime_session_id="runtime:receipt")
        storage = build_terminal_command_receipt_storage(event_log)
        request = _submit_request(command_id="command:durable", text="hello")
        expected = _successful_outcome(request)
        calls = 0

        async def operation() -> TerminalCommandOutcome:
            nonlocal calls
            calls += 1
            return expected

        first = TerminalCommandOwner(
            runtime_session_id="runtime:receipt", receipt_storage=storage
        )
        assert await first.execute(request, operation) == expected
        assert calls == 1
        first.close()

        replacement = TerminalCommandOwner(
            runtime_session_id="runtime:receipt", receipt_storage=storage
        )
        assert (
            await replacement.query(
                client_instance_id=request.binding.client_instance_id,
                command_id=request.binding.command_id,
            )
            == expected
        )
        assert await replacement.execute(request, operation) == expected
        assert calls == 1
        replacement.close()

    asyncio.run(scenario())


def test_terminal_command_same_id_with_different_semantics_conflicts() -> None:
    async def scenario() -> None:
        event_log = InMemoryEventLog(runtime_session_id="runtime:receipt")
        storage = build_terminal_command_receipt_storage(event_log)
        original = _submit_request(command_id="command:conflict", text="first")
        conflicting = _submit_request(command_id="command:conflict", text="second")
        owner = TerminalCommandOwner(
            runtime_session_id="runtime:receipt", receipt_storage=storage
        )

        async def operation() -> TerminalCommandOutcome:
            return _successful_outcome(original)

        await owner.execute(original, operation)
        try:
            await owner.execute(conflicting, operation)
        except ValueError as exc:
            assert "identity conflicts" in str(exc)
        else:
            raise AssertionError("same command ID accepted different semantics")
        owner.close()

    asyncio.run(scenario())


def test_terminal_command_receipt_transient_failure_keeps_single_live_owner() -> None:
    async def scenario() -> None:
        event_log = InMemoryEventLog(runtime_session_id="runtime:receipt")
        delegate = build_terminal_command_receipt_storage(event_log)

        class FlakyStorage:
            complete_attempts = 0

            def admit_pending(self, **values):
                return delegate.admit_pending(**values)

            def query(self, **values):
                return delegate.query(**values)

            def list_pending(self, **values):
                return delegate.list_pending(**values)

            def complete(self, **values):
                self.complete_attempts += 1
                if self.complete_attempts < 3:
                    raise TimeoutError("injected receipt storage timeout")
                return delegate.complete(**values)

        storage = FlakyStorage()
        request = _submit_request(command_id="command:retry", text="retry")
        expected = _successful_outcome(request)
        operation_calls = 0

        async def operation() -> TerminalCommandOutcome:
            nonlocal operation_calls
            operation_calls += 1
            return expected

        owner = TerminalCommandOwner(
            runtime_session_id="runtime:receipt",
            receipt_storage=storage,
            operation_timeout_seconds=1.0,
        )
        assert await owner.execute(request, operation) == expected
        assert operation_calls == 1
        assert storage.complete_attempts == 3
        await owner.drain(deadline_monotonic=asyncio.get_running_loop().time() + 1)
        owner.close()

    asyncio.run(scenario())


def test_terminal_command_caller_cancel_during_admission_keeps_service_owner() -> None:
    async def scenario() -> None:
        event_log = InMemoryEventLog(runtime_session_id="runtime:receipt")
        delegate = build_terminal_command_receipt_storage(event_log)
        admission_started = Event()
        release_admission = Event()

        class BlockingAdmissionStorage:
            def admit_pending(self, **values):
                admission_started.set()
                if not release_admission.wait(timeout=5.0):
                    raise TimeoutError("test did not release command admission")
                return delegate.admit_pending(**values)

            def query(self, **values):
                return delegate.query(**values)

            def list_pending(self, **values):
                return delegate.list_pending(**values)

            def complete(self, **values):
                return delegate.complete(**values)

        request = _submit_request(command_id="command:detached", text="detach")
        expected = _successful_outcome(request)
        operation_calls = 0

        async def operation() -> TerminalCommandOutcome:
            nonlocal operation_calls
            operation_calls += 1
            return expected

        owner = TerminalCommandOwner(
            runtime_session_id="runtime:receipt",
            receipt_storage=BlockingAdmissionStorage(),
            operation_timeout_seconds=5.0,
        )
        caller = asyncio.create_task(owner.execute(request, operation))
        assert await asyncio.to_thread(admission_started.wait, 1.0)
        caller.cancel()
        try:
            await caller
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("cancelled command waiter did not detach")

        try:
            owner.close()
        except RuntimeError as exc:
            assert "active operations" in str(exc)
        else:
            raise AssertionError("command owner closed across physical admission")

        release_admission.set()
        await owner.drain(deadline_monotonic=asyncio.get_running_loop().time() + 2.0)
        assert operation_calls == 1
        assert (
            await owner.query(
                client_instance_id=request.binding.client_instance_id,
                command_id=request.binding.command_id,
            )
            == expected
        )
        owner.close()

    asyncio.run(scenario())


def test_terminal_command_restart_terminalizes_pending_without_replaying_operation() -> (
    None
):
    async def scenario() -> None:
        event_log = InMemoryEventLog(runtime_session_id="runtime:receipt")
        storage = build_terminal_command_receipt_storage(event_log)
        request = _submit_request(command_id="command:orphan", text="do not replay")
        admission = storage.admit_pending(
            runtime_session_id="runtime:receipt",
            client_instance_id=request.binding.client_instance_id,
            command_id=request.binding.command_id,
            command_kind=request.command_kind,
            request_semantic_fingerprint=request.request_fingerprint,
            target_id=request.binding.expected_target_id,
            target_generation=request.binding.expected_target_generation,
            pending_outcome=_pending_outcome(request),
            deadline_monotonic=asyncio.get_running_loop().time() + 1.0,
        )
        assert admission.execution_owner_won

        operation_calls = 0

        async def operation() -> TerminalCommandOutcome:
            nonlocal operation_calls
            operation_calls += 1
            return _successful_outcome(request)

        replacement = TerminalCommandOwner(
            runtime_session_id="runtime:receipt", receipt_storage=storage
        )
        await replacement.recover_pending(
            deadline_monotonic=asyncio.get_running_loop().time() + 1.0
        )
        recovered = await replacement.query(
            client_instance_id=request.binding.client_instance_id,
            command_id=request.binding.command_id,
        )
        assert recovered is not None
        assert recovered.status == "reconciliation_required"
        assert recovered.public_result_code == "COMMAND_OWNER_LOST_AFTER_RESTART"
        assert operation_calls == 0
        assert await replacement.execute(request, operation) == recovered
        assert operation_calls == 0
        replacement.close()

    asyncio.run(scenario())


def test_headless_successor_command_uses_hostcore_lifecycle_authority(
    tmp_path: Path, monkeypatch
) -> None:
    core = _core(monkeypatch, ScriptedTransport([]))

    async def scenario() -> None:
        session = await _open_project_session(core, tmp_path)
        socket_root = TemporaryDirectory(prefix="pulsara-terminal-", dir="/tmp")
        socket_path = Path(socket_root.name) / "terminal-v1.sock"
        server = TerminalProtocolServer(
            socket_path=socket_path,
            session_provider=lambda host_id: (
                session
                if host_id == session.host_session_id
                else (_ for _ in ()).throw(KeyError(host_id))
            ),
        )
        await server.start()
        client = HeadlessTerminalConformanceClient(
            socket_path=socket_path,
            client_instance_id="headless:successor",
            launch_capability=server.launch_capability,
        )
        await client.connect(controller=True)
        await client.attach(session.host_session_id, controller=True)
        snapshot = await client.snapshot()
        capacity_fingerprint = (
            snapshot.active_head.capacity_state.available.capacity_state_fingerprint
        )
        pending = await client.start_successor_session(
            target_id=session.runtime_session_id,
            source_capacity_state_fingerprint=capacity_fingerprint,
        )
        assert pending.status == "pending_confirmation"
        winner = None
        for _ in range(300):
            result = await client.query_command(pending.command_id)
            if result.found and result.outcome.status != "pending_confirmation":
                winner = result.outcome
                break
            await asyncio.sleep(0.01)
        assert winner is not None
        assert winner.status == "succeeded"
        assert winner.public_result_code == "SUCCESSOR_SESSION_CREATED"
        assert len(winner.durable_reference_ids) == 2
        successor = await core.registry.get(winner.durable_reference_ids[0])
        assert successor.runtime_session_id == winner.durable_reference_ids[1]
        assert successor.runtime_session_id != session.runtime_session_id
        assert not successor.terminal_application_services.query.snapshot().queue_items

        await client.detach()
        await client.close()
        await server.close()
        socket_root.cleanup()
        await core.shutdown()

    asyncio.run(scenario())
