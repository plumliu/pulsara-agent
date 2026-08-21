from __future__ import annotations

import asyncio
from types import SimpleNamespace

from pulsara_agent.conversation_kernel.contracts import HostWriterGuard
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.conversation_kernel.repository import (
    TurnAdmissionConfirmation,
    TurnAdmissionConfirmationKind,
)
from pulsara_agent.conversation_kernel.subagent import KernelSubagentManager
from pulsara_agent.conversation_kernel.subagents.contracts import (
    build_parent_context_call_subject,
)
from pulsara_agent.conversation_kernel.todo_runtime import TodoRunStateOwner


class _Repository:
    def __init__(self) -> None:
        self.statuses: list[tuple[str, str | None]] = []
        self.row: dict[str, object] | None = None

    def accept_subagent_task_batch(self, *_args, **kwargs):
        draft = kwargs["candidate"].ordered_tasks[0]
        self.row = {
            "id": draft.task_id,
            "workspace_id": "workspace:test",
            "status": draft.initial_status.value,
            "terminal_reason": draft.terminal_reason,
            "result_id": None,
            "result_entry_id": None,
        }
        self.statuses.append((draft.initial_status.value, draft.terminal_reason))

    def query_subagent_task(self, **_kwargs):
        return None if self.row is None else dict(self.row)

    def list_runnable_subagent_tasks(self, *_args, **_kwargs):
        if self.row is None or self.row["status"] != "PENDING_START":
            return ()
        return (dict(self.row),)

    def accept_subagent_task_start(self, *_args, **_kwargs):
        assert self.row is not None
        self.row["status"] = "ACTIVE"
        self.statuses.append(("ACTIVE", None))
        return True

    def settle_subagent_dependency_frontier(self, *_args, **_kwargs):
        return ()

    def confirm_cancelled_subagent_turn_and_task(self, **_kwargs):
        return TurnAdmissionConfirmation(TurnAdmissionConfirmationKind.NONE)

    def settle_cancelled_subagent_turn_and_task(self, *_args, **kwargs):
        assert self.row is not None
        self.row["status"] = str(kwargs["task_status"])
        self.row["terminal_reason"] = kwargs["task_reason"]
        self.statuses.append((str(kwargs["task_status"]), kwargs["task_reason"]))
        return True


class _BlockingRunner:
    async def run_subagent_turn(self, **_kwargs):
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def test_host_close_interrupts_subagent_instead_of_user_cancelling_it() -> None:
    async def exercise() -> None:
        repository = _Repository()
        manager = KernelSubagentManager(
            repository=repository,  # type: ignore[arg-type]
            guard=HostWriterGuard("session:test", 1, "host:test"),
            host_owner_id="host:test",
            io_owner=KernelSessionIO(),
            live_bus=LiveAgentEventBus(),
            todo_owner=TodoRunStateOwner(
                session_id="session:test", owner_epoch="host:test"
            ),
        )
        manager.bind_runner_factory(lambda: _BlockingRunner())  # type: ignore[arg-type]
        subject = build_parent_context_call_subject(
            session_id="session:test",
            caller_turn_id="turn:test",
            provider_input_cut_fingerprint="sha256:cut",
            continuity_epoch_nonce="epoch:test",
            continuity_epoch_revision=0,
            compiled_semantic_input_fingerprint="sha256:semantic",
            compiled_message_placements_fingerprint="sha256:placements",
            ordered_eligible_units=(),
        )
        result = await manager.invoke(
            tool_name="spawn_agent",
            arguments={"task": "wait forever"},
            invocation_context=SimpleNamespace(
                session_id="session:test",
                workspace_id="workspace:test",
                turn_id="turn:test",
                attempt_id="attempt:test",
                conversation_scope_kind="ROOT",
                scope_subagent_task_id=None,
                host_owner_epoch=1,
                effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
                permission_snapshot_fingerprint="sha256:permission",
                attempt_permission_snapshot_fingerprint="sha256:permission",
                subagent_parent_context_subject=subject,
            ),
        )
        assert result.state == "SUCCESS"
        await manager.aclose(timeout_seconds=1)
        assert repository.statuses == [
            ("PENDING_START", None),
            ("ACTIVE", None),
            ("INTERRUPTED", "HOST_CLOSING"),
        ]

    asyncio.run(exercise())
