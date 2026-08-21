"""Round 10 hierarchical worker/task-board product contracts."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from hashlib import sha256
import json
from datetime import datetime, timedelta, timezone
from time import monotonic
from types import SimpleNamespace
from uuid import uuid4

import pytest

from pulsara_agent.capability.builtin_catalog import builtin_tool_catalog
from pulsara_agent.conversation_kernel.contracts import HostWriterGuard, InlineContent
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.cancellation import stable_subagent_turn_id
from pulsara_agent.conversation_kernel.cold_epoch import build_subagent_initial_seed
from pulsara_agent.conversation_kernel.reader import CanonicalProviderInputReader
from pulsara_agent.conversation_kernel.repository import (
    AssistantTextBlock,
    AssistantToolCallBlock,
    ConversationKernelConflict,
    ConversationKernelRepository,
)
from pulsara_agent.conversation_kernel.runner import KernelRunResult
from pulsara_agent.conversation_kernel.subagent import (
    ROOT_ORCHESTRATION_TOOL_NAMES,
    SUBAGENT_TOOL_NAMES,
    KernelSubagentManager,
)
from pulsara_agent.conversation_kernel.tool_policy import (
    DefaultToolDispatchAuthorizationPolicy,
)
from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
from pulsara_agent.conversation_kernel.subagents.contracts import (
    SubagentContextMode,
    SubagentProfileKind,
    SubagentResultSource,
    build_parent_context_call_subject,
    build_parent_context_selection,
    build_root_context_unit,
    build_subagent_result_public_fact,
)
from pulsara_agent.conversation_kernel.todo_runtime import TodoRunStateOwner
from pulsara_agent.primitives.context import freeze_json
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.model_input.contracts import (
    FrozenProviderInputItemKind,
    ModelInputScopeKind,
)
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from tests.support.postgres import verified_postgres_provider
from tests.support.round3 import prepare_test_direct_tool_surface
from tests.support.subagents import accept_active_subagent_fixture


def _id(prefix: str) -> str:
    return f"{prefix}:{uuid4().hex}"


def _round10_id(namespace: str, *parts: str) -> str:
    digest = sha256()
    digest.update(f"pulsara:round10:{namespace}:v1\0".encode())
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    return f"{namespace}:{digest.hexdigest()}"


def test_round10_tool_inventory_and_result_fact_are_closed() -> None:
    assert ROOT_ORCHESTRATION_TOOL_NAMES == {
        "spawn_agent",
        "create_agent_tasks",
        "list_agents",
        "wait_agent",
        "wait_agent_tasks",
        "send_agent_message",
        "stop_agent",
    }
    assert SUBAGENT_TOOL_NAMES == ROOT_ORCHESTRATION_TOOL_NAMES | {
        "report_agent_result"
    }
    catalog = {
        entry.name: entry.tool_family
        for entry in builtin_tool_catalog()
        if entry.tool_family in {"subagent_parent", "subagent_child"}
    }
    assert catalog == {
        **{name: "subagent_parent" for name in ROOT_ORCHESTRATION_TOOL_NAMES},
        "report_agent_result": "subagent_child",
    }
    assert "report_agent_phase" not in {entry.name for entry in builtin_tool_catalog()}

    with pytest.raises(TypeError, match="frozen objects"):
        build_subagent_result_public_fact(
            task_id="task:test",
            result_id="result:test",
            source=SubagentResultSource.EXPLICIT,
            producer_entry_id="entry:test",
            summary="done",
            diagnostics=["not-an-object"],
        )
    with pytest.raises(ValueError, match="item bound"):
        build_subagent_result_public_fact(
            task_id="task:test",
            result_id="result:test",
            source=SubagentResultSource.EXPLICIT,
            producer_entry_id="entry:test",
            summary="done",
            diagnostics=[{"code": str(index)} for index in range(33)],
        )


def test_round10_list_pages_dependency_hydration_without_inventory_cap() -> None:
    class _InlineIO:
        async def run(self, function, *args: object, **kwargs: object):
            return function(*args, **kwargs)

    class _Repository:
        def __init__(self) -> None:
            self.page_widths: list[int] = []
            accepted_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
            self.rows = tuple(
                {
                    "id": f"task:{index:02d}",
                    "task_key": f"task-{index:02d}",
                    "label": None,
                    "profile_kind": "GENERAL_WORKER",
                    "objective": f"objective {index}",
                    "status": "ACTIVE" if index == 54 else "COMPLETED",
                    "pending_reason": None,
                    "terminal_reason": None,
                    "result_id": f"result:{index:02d}",
                    "result_source": "EXPLICIT",
                    "result_summary": "done",
                    "accepted_root_entry_id": None,
                    "accepted_at": accepted_at + timedelta(microseconds=index),
                    "total_count": 55,
                }
                for index in range(55)
            )

        def list_subagent_tasks(
            self,
            *,
            maximum_items: int,
            after_accepted_at: datetime | None,
            after_task_id: str | None,
            **_kwargs: object,
        ):
            rows = self.rows
            if after_accepted_at is not None:
                assert after_task_id is not None
                rows = tuple(
                    row
                    for row in rows
                    if (row["accepted_at"], row["id"])
                    > (after_accepted_at, after_task_id)
                )
            return rows[: maximum_items + 1]

        def read_subagent_dependencies(
            self, *, task_ids: tuple[str, ...], **_kwargs: object
        ):
            self.page_widths.append(len(task_ids))
            assert len(task_ids) <= 32
            return tuple(
                {
                    "task_id": task_id,
                    "dependency_task_id": f"dependency:{task_id}",
                    "status": "COMPLETED",
                }
                for task_id in task_ids
            )

    async def exercise() -> None:
        repository = _Repository()
        manager = object.__new__(KernelSubagentManager)
        manager._io = _InlineIO()  # type: ignore[attr-defined]
        manager._repository = repository  # type: ignore[attr-defined]
        manager._guard = SimpleNamespace(session_id="session:test")  # type: ignore[attr-defined]
        manager._deadlines = SimpleNamespace(  # type: ignore[attr-defined]
            deadline=lambda _owner: monotonic() + 30
        )
        manager._lock = asyncio.Lock()  # type: ignore[attr-defined]
        manager._mailboxes = {}  # type: ignore[attr-defined]
        manager._list_cursor_secret = b"x" * 32  # type: ignore[attr-defined]

        first = await manager._list(  # noqa: SLF001
            {"max_items": 50, "include_dependencies": True}
        )
        first_body = json.loads(first.content)
        assert first.state == "SUCCESS"
        assert len(first_body["tasks"]) == 50
        assert first_body["page_count"] == 50
        assert first_body["total_count"] == 55
        assert first_body["omitted_count"] == 5
        assert first_body["next_cursor"]

        stale = await manager._list(  # noqa: SLF001
            {
                "max_items": 50,
                "include_dependencies": False,
                "cursor": first_body["next_cursor"],
            }
        )
        assert stale.state == "APPLICATION_ERROR"
        assert json.loads(stale.content)["error"] == "STALE_CURSOR"

        invalid = await manager._list(  # noqa: SLF001
            {
                "max_items": 50,
                "include_dependencies": True,
                "cursor": "not-a-signed-cursor",
            }
        )
        assert invalid.state == "APPLICATION_ERROR"
        assert json.loads(invalid.content)["error"] == "INVALID_CURSOR"

        second = await manager._list(  # noqa: SLF001
            {
                "max_items": 50,
                "include_dependencies": True,
                "cursor": first_body["next_cursor"],
            }
        )
        second_body = json.loads(second.content)
        assert second.state == "SUCCESS"
        assert [item["task_id"] for item in second_body["tasks"]] == [
            f"task:{index:02d}" for index in range(50, 55)
        ]
        assert second_body["tasks"][-1]["status"] == "active"
        assert second_body["page_count"] == 5
        assert second_body["total_count"] == 55
        assert second_body["omitted_count"] == 0
        assert second_body["next_cursor"] is None
        assert repository.page_widths == [32, 18, 5]
        assert all(
            len(item["dependencies"]) == 1
            for item in first_body["tasks"] + second_body["tasks"]
        )

    asyncio.run(exercise())


def test_round10_root_and_child_tool_surfaces_have_one_fixed_layer_boundary(
    tmp_path,
) -> None:
    async def exercise() -> None:
        port = DirectKernelToolPort(
            workspace_root=tmp_path,
            host_owner_id="host:test",
            session_id="session:test",
            live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
        )
        port.bind_subagent_port(  # type: ignore[arg-type]
            SimpleNamespace(tool_names=SUBAGENT_TOOL_NAMES)
        )
        root = prepare_test_direct_tool_surface(
            port,
            conversation_scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        )
        child = prepare_test_direct_tool_surface(
            port,
            conversation_scope_kind=ModelInputScopeKind.SUBAGENT_TASK,
            scope_subagent_task_id="task:test",
        )
        root_names = tuple(item.name for item in root.model_surface.tool_specs)
        child_names = tuple(item.name for item in child.model_surface.tool_specs)
        assert set(root_names).intersection(SUBAGENT_TOOL_NAMES) == (
            ROOT_ORCHESTRATION_TOOL_NAMES
        )
        assert set(child_names).intersection(SUBAGENT_TOOL_NAMES) == {
            "report_agent_result"
        }
        await port.aclose(timeout_seconds=2)

    asyncio.run(exercise())


def test_round10_last_n_uses_exact_units_and_none_remains_absent() -> None:
    units = (
        build_root_context_unit(
            ordered_entry_ids=("entry:one",),
            ordered_public_items=("user: one", "assistant: first"),
        ),
        build_root_context_unit(
            ordered_entry_ids=("entry:two-a", "entry:two-b"),
            ordered_public_items=(
                "user: second-a",
                "user: second-b",
                "steer: revise",
                "assistant: second",
            ),
        ),
        build_root_context_unit(
            ordered_entry_ids=("entry:three",),
            ordered_public_items=("user: open unit",),
        ),
    )
    subject = build_parent_context_call_subject(
        session_id="session:test",
        caller_turn_id="turn:test",
        provider_input_cut_fingerprint="sha256:cut",
        continuity_epoch_nonce="epoch:test",
        continuity_epoch_revision=7,
        compiled_semantic_input_fingerprint="sha256:semantic",
        compiled_message_placements_fingerprint="sha256:placements",
        ordered_eligible_units=units,
    )
    selected = build_parent_context_selection(
        subject,
        mode=SubagentContextMode.LAST_N,
        last_n_turns=2,
    )
    assert selected.selected_units == units[-2:]
    assert selected.selected_units[0] is units[1]
    assert selected.rendered_body is not None
    assert "second-a" in selected.rendered_body
    assert "open unit" in selected.rendered_body
    assert "first" not in selected.rendered_body

    absent = build_parent_context_selection(
        subject,
        mode=SubagentContextMode.NONE,
        last_n_turns=None,
    )
    assert absent.selected_units == ()
    assert absent.rendered_body is None
    assert absent.source_fingerprint is None


@pytest.mark.postgres
def test_round10_subagent_initial_seed_exact_joins_child_cut_and_none_sources(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _id("session")
    workspace_id = _id("workspace")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_id("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    parent_turn_id = _id("turn")
    repository.start_root_turn(
        lease.guard,
        command_id=_id("command"),
        turn_id=parent_turn_id,
        entry_id=_id("entry"),
        context_binding_revision_id=_id("revision"),
        permission_snapshot_id=_id("permission"),
        requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
        content=InlineContent.from_bytes(b"delegate exact seed"),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )
    objective = "read only the sealed child objective"
    task_id = accept_active_subagent_fixture(
        repository,
        lease,
        parent_turn_id=parent_turn_id,
        objective=objective,
    )
    child_turn = stable_subagent_turn_id(session_id=session_id, task_id=task_id)
    repository.start_subagent_turn(
        lease.guard,
        task_id=task_id,
        turn_id=child_turn,
        entry_id=_id("entry"),
        context_binding_revision_id=_id("revision"),
        content=InlineContent.from_bytes(objective.encode("utf-8")),
        occurred_at=datetime.now(timezone.utc),
        actor_id=task_id,
        deadline_monotonic=monotonic() + 30,
    )
    cut = repository.prepare_provider_input_cut(
        lease.guard,
        turn_id=child_turn,
        deadline_monotonic=monotonic() + 30,
    )
    dispatch_read = CanonicalProviderInputReader(provider).read_frozen_dispatch(
        cut,
        deadline_monotonic=monotonic() + 30,
    )
    subject = build_parent_context_call_subject(
        session_id=session_id,
        caller_turn_id=parent_turn_id,
        provider_input_cut_fingerprint="sha256:seed-cut",
        continuity_epoch_nonce="epoch:parent",
        continuity_epoch_revision=3,
        compiled_semantic_input_fingerprint="sha256:parent-semantic",
        compiled_message_placements_fingerprint="sha256:parent-placements",
        ordered_eligible_units=(),
    )
    selection = build_parent_context_selection(
        subject,
        mode=SubagentContextMode.NONE,
        last_n_turns=None,
    )
    seed = build_subagent_initial_seed(
        dispatch_read=dispatch_read,
        task_id=task_id,
        parent_turn_id=parent_turn_id,
        profile_kind=SubagentProfileKind.GENERAL_WORKER,
        objective=objective,
        parent_call_subject=subject,
        parent_context_selection=selection,
        dependency_context=None,
    )
    assert seed.task_id == task_id
    assert seed.parent_context_selection.mode is SubagentContextMode.NONE
    assert seed.dependency_context is None
    assert all(
        source.__class__.__name__ == "ContextSourceAbsentFact"
        for source in seed.source_replacements
    )
    with pytest.raises(ValueError, match="subagent initial seed is invalid"):
        replace(seed, task_id="task:foreign")
    with pytest.raises(ValueError, match="subagent initial seed is invalid"):
        replace(seed, objective="different objective")


@pytest.mark.parametrize(
    "permission_mode",
    (
        PermissionMode.READ_ONLY,
        PermissionMode.ASK_PERMISSIONS,
        PermissionMode.ACCEPT_EDITS,
    ),
)
def test_round10_all_root_orchestration_tools_are_bypass_only_before_owner_io(
    permission_mode: PermissionMode,
) -> None:
    async def exercise() -> None:
        manager = KernelSubagentManager(
            repository=SimpleNamespace(),  # no operation may reach it
            guard=HostWriterGuard("session:test", 7, "host:test"),
            host_owner_id="host:test",
            io_owner=KernelSessionIO(),
            live_bus=LiveAgentEventBus(),
            todo_owner=TodoRunStateOwner(
                session_id="session:test",
                owner_epoch="todo:test",
            ),
        )
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
        context = SimpleNamespace(
            session_id="session:test",
            workspace_id="workspace:test",
            turn_id="turn:test",
            assistant_entry_id="entry:test",
            tool_call_id="call:test",
            attempt_id="attempt:test",
            conversation_scope_kind="ROOT",
            scope_subagent_task_id=None,
            host_owner_epoch=7,
            effective_permission_mode=permission_mode,
            permission_snapshot_fingerprint="sha256:permission",
            attempt_permission_snapshot_fingerprint="sha256:permission",
            subagent_parent_context_subject=subject,
        )
        arguments = {
            "spawn_agent": {"task": "bounded objective"},
            "create_agent_tasks": {
                "tasks": [{"task": "bounded objective", "depends_on": []}]
            },
            "list_agents": {},
            "wait_agent": {"task_id": "task:test"},
            "wait_agent_tasks": {"task_ids": ["task:test"]},
            "send_agent_message": {
                "task_id": "task:test",
                "message": "bounded message",
            },
            "stop_agent": {"task_id": "task:test"},
        }
        for tool_name in ROOT_ORCHESTRATION_TOOL_NAMES:
            result = await manager.invoke(
                tool_name=tool_name,
                arguments=arguments[tool_name],
                invocation_context=context,
            )
            assert result.state == "PERMISSION_DENIED"
            assert b"subagent_requires_bypass_mode" in result.content

        context.effective_permission_mode = PermissionMode.BYPASS_PERMISSIONS
        context.host_owner_epoch = 8
        stale = await manager.invoke(
            tool_name="list_agents",
            arguments={},
            invocation_context=context,
        )
        assert stale.state == "PERMISSION_DENIED"
        assert b"subagent_authority_mismatch" in stale.content

    asyncio.run(exercise())


def test_round10_send_and_completion_share_one_exact_linearization() -> None:
    async def exercise() -> None:
        manager = KernelSubagentManager(
            repository=SimpleNamespace(),
            guard=HostWriterGuard("session:test", 1, "host:test"),
            host_owner_id="host:test",
            io_owner=KernelSessionIO(),
            live_bus=LiveAgentEventBus(),
            todo_owner=TodoRunStateOwner(
                session_id="session:test",
                owner_epoch="todo:test",
            ),
        )
        task_id = "task:test"
        physical = asyncio.create_task(asyncio.Event().wait())
        manager._tasks[task_id] = SimpleNamespace(
            task_id=task_id,
            status="ACTIVE",
            task=physical,
            cancellation_intent=SimpleNamespace(turn_id="turn:child"),
        )
        manager._mailboxes[task_id] = []
        context = SimpleNamespace(
            session_id="session:test",
            turn_id="turn:root",
            tool_call_id="call:first",
            attempt_id="attempt:first",
        )

        # Send wins: the mailbox is non-empty, so the final answer cannot
        # install a completion permit and must perform another provider call.
        queued = await manager._send_message(
            {"task_id": task_id, "message": "revise before finishing"},
            context,
        )
        assert queued.state == "SUCCESS"
        assert (
            await manager.prepare_inferred_completion(
                task_id=task_id,
                entry_id="entry:first-final",
                public_text="premature answer",
            )
            is None
        )

        # Completion wins: once its one-shot permit is installed, a later send
        # is rejected rather than being silently stranded behind final output.
        manager._mailboxes[task_id].clear()
        prepared = await manager.prepare_inferred_completion(
            task_id=task_id,
            entry_id="entry:real-final",
            public_text="final answer",
        )
        assert prepared is not None
        context.tool_call_id = "call:late"
        context.attempt_id = "attempt:late"
        late = await manager._send_message(
            {"task_id": task_id, "message": "too late"},
            context,
        )
        assert late.state == "TOOL_UNAVAILABLE"
        await manager.finish_completion(prepared[0], committed=False)
        physical.cancel()
        await asyncio.gather(physical, return_exceptions=True)

    asyncio.run(exercise())


class _BlockingChildRunner:
    async def run_subagent_turn(self, **_kwargs: object):
        await asyncio.Event().wait()


class _CountingBlockingChildRunner:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.changed = asyncio.Event()

    async def run_subagent_turn(self, *, task_id: str, **_kwargs: object):
        self.started.append(task_id)
        self.changed.set()
        await asyncio.Event().wait()


class _CanonicalBlockingChildRunner:
    def __init__(self, repository: ConversationKernelRepository, lease) -> None:
        self._repository = repository
        self._lease = lease
        self.started = asyncio.Event()

    async def run_subagent_turn(
        self,
        *,
        task_id: str,
        objective: str,
        cancellation_intent,
    ) -> KernelRunResult:
        self._repository.start_subagent_turn(
            self._lease.guard,
            task_id=task_id,
            turn_id=cancellation_intent.turn_id,
            entry_id=_id("entry"),
            context_binding_revision_id=_id("revision"),
            content=InlineContent.from_bytes(objective.encode("utf-8")),
            occurred_at=datetime.now(timezone.utc),
            actor_id=task_id,
            deadline_monotonic=monotonic() + 30,
        )
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("canonical blocking child unexpectedly resumed")


class _CompletingChildRunner:
    """Canonical inferred-result worker used by the Round 10 DAG probe."""

    def __init__(
        self,
        *,
        repository: ConversationKernelRepository,
        lease,
        manager: KernelSubagentManager,
        started: list[str],
        source_bodies: dict[str, tuple[str, ...]],
    ) -> None:
        self._repository = repository
        self._lease = lease
        self._manager = manager
        self._started = started
        self._source_bodies = source_bodies

    async def run_subagent_turn(
        self,
        *,
        task_id: str,
        objective: str,
        cancellation_intent,
    ) -> KernelRunResult:
        self._started.append(objective)
        self._source_bodies[objective] = tuple(
            variant.text
            for source in self._manager.initial_context_sources(task_id=task_id)
            if hasattr(source, "variants")
            for variant in source.variants[:1]
        )
        turn_id = stable_subagent_turn_id(
            session_id=self._lease.guard.session_id,
            task_id=task_id,
        )
        self._repository.start_subagent_turn(
            self._lease.guard,
            task_id=task_id,
            turn_id=turn_id,
            entry_id=_id("entry"),
            context_binding_revision_id=_id("revision"),
            content=InlineContent.from_bytes(objective.encode("utf-8")),
            occurred_at=datetime.now(timezone.utc),
            actor_id=task_id,
            deadline_monotonic=monotonic() + 30,
        )
        cut = self._repository.prepare_provider_input_cut(
            self._lease.guard,
            turn_id=turn_id,
            deadline_monotonic=monotonic() + 30,
        )
        final_entry_id = _id("entry")
        summary = f"result for {objective}"
        content = InlineContent.from_bytes(summary.encode("utf-8"))
        result = build_subagent_result_public_fact(
            task_id=task_id,
            result_id=(
                "subagent-result:"
                + sha256(f"{task_id}:{final_entry_id}".encode()).hexdigest()
            ),
            source=SubagentResultSource.INFERRED,
            producer_entry_id=final_entry_id,
            summary=summary,
            source_assistant_content_digest=content.digest,
        )
        self._repository.commit_assistant_message(
            self._lease.guard,
            cut=cut,
            entry_id=final_entry_id,
            parent_content=content,
            blocks=(
                AssistantTextBlock(
                    block_id=_id("block"),
                    text=content,
                ),
            ),
            subagent_result=result,
            complete_turn=True,
            occurred_at=datetime.now(timezone.utc),
            actor_id="model:round10-test",
            deadline_monotonic=monotonic() + 30,
        )
        return KernelRunResult(
            turn_id=turn_id,
            final_entry_id=final_entry_id,
            final_text=summary,
            model_call_count=1,
            tool_call_count=0,
        )


def _permission_fingerprint(
    repository: ConversationKernelRepository,
    *,
    session_id: str,
    turn_id: str,
) -> str:
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        row = connection.execute(
            "SELECT permission_snapshot_fingerprint FROM pulsara_v3.turns "
            "WHERE session_id=%s AND id=%s",
            (session_id, turn_id),
        ).fetchone()
    assert row is not None
    return str(row[0])


def _prepare_root_tool_attempt(
    repository: ConversationKernelRepository,
    *,
    session_id: str,
    workspace_id: str,
    tool_name: str,
    arguments: dict[str, object],
):
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_id("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    turn_id = _id("turn")
    repository.start_root_turn(
        lease.guard,
        command_id=_id("command"),
        turn_id=turn_id,
        entry_id=_id("entry"),
        context_binding_revision_id=_id("revision"),
        permission_snapshot_id=_id("permission"),
        requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
        content=InlineContent.from_bytes(b"delegate bounded work"),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )
    permission = _permission_fingerprint(
        repository, session_id=session_id, turn_id=turn_id
    )
    cut = repository.prepare_provider_input_cut(
        lease.guard,
        turn_id=turn_id,
        deadline_monotonic=monotonic() + 30,
    )
    assistant_entry_id = _id("entry")
    tool_call_id = _id("call")
    repository.commit_assistant_message(
        lease.guard,
        cut=cut,
        entry_id=assistant_entry_id,
        parent_content=InlineContent.from_bytes(b"delegate"),
        blocks=(
            AssistantToolCallBlock(
                _id("block"),
                tool_call_id,
                tool_name,
                freeze_json(arguments),
            ),
        ),
        occurred_at=datetime.now(timezone.utc),
        actor_id="model:test",
        deadline_monotonic=monotonic() + 30,
    )
    attempt_id = _id("attempt")
    repository.accept_tool_attempt(
        lease.guard,
        attempt_id=attempt_id,
        assistant_entry_id=assistant_entry_id,
        tool_call_id=tool_call_id,
        authorization_kind="policy",
        authorization_reference="allow",
        actor_kind="runtime",
        actor_id="executor",
        remote_idempotency_key=None,
        retry_of_attempt_id=None,
        permission_snapshot_fingerprint=permission,
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )
    subject = build_parent_context_call_subject(
        session_id=session_id,
        caller_turn_id=turn_id,
        provider_input_cut_fingerprint="sha256:test-cut",
        continuity_epoch_nonce="epoch:test",
        continuity_epoch_revision=0,
        compiled_semantic_input_fingerprint="sha256:test-semantic",
        compiled_message_placements_fingerprint="sha256:test-placements",
        ordered_eligible_units=(),
    )
    context = SimpleNamespace(
        session_id=session_id,
        workspace_id=workspace_id,
        turn_id=turn_id,
        attempt_id=attempt_id,
        conversation_scope_kind="ROOT",
        scope_subagent_task_id=None,
        host_owner_epoch=lease.guard.writer_generation,
        effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
        permission_snapshot_fingerprint=permission,
        attempt_permission_snapshot_fingerprint=permission,
        subagent_parent_context_subject=subject,
    )
    return lease, context


def _prepare_root_tool_batch(
    repository: ConversationKernelRepository,
    *,
    session_id: str,
    workspace_id: str,
    calls: tuple[tuple[str, str, str, dict[str, object]], ...],
):
    """Freeze one real ROOT assistant batch and its accepted attempts."""

    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_id("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    turn_id = _id("turn")
    repository.start_root_turn(
        lease.guard,
        command_id=_id("command"),
        turn_id=turn_id,
        entry_id=_id("entry"),
        context_binding_revision_id=_id("revision"),
        permission_snapshot_id=_id("permission"),
        requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
        content=InlineContent.from_bytes(b"orchestrate and send exact messages"),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )
    permission = _permission_fingerprint(
        repository,
        session_id=session_id,
        turn_id=turn_id,
    )
    cut = repository.prepare_provider_input_cut(
        lease.guard,
        turn_id=turn_id,
        deadline_monotonic=monotonic() + 30,
    )
    assistant_entry_id = _id("entry")
    repository.commit_assistant_message(
        lease.guard,
        cut=cut,
        entry_id=assistant_entry_id,
        parent_content=InlineContent.from_bytes(b"orchestration batch"),
        blocks=tuple(
            AssistantToolCallBlock(
                _id("block"),
                tool_call_id,
                tool_name,
                freeze_json(arguments),
            )
            for tool_name, tool_call_id, _attempt_id, arguments in calls
        ),
        occurred_at=datetime.now(timezone.utc),
        actor_id="model:test",
        deadline_monotonic=monotonic() + 30,
    )
    subject = build_parent_context_call_subject(
        session_id=session_id,
        caller_turn_id=turn_id,
        provider_input_cut_fingerprint="sha256:test-batch-cut",
        continuity_epoch_nonce="epoch:test-batch",
        continuity_epoch_revision=0,
        compiled_semantic_input_fingerprint="sha256:test-batch-semantic",
        compiled_message_placements_fingerprint="sha256:test-batch-placements",
        ordered_eligible_units=(),
    )
    contexts = []
    for tool_name, tool_call_id, attempt_id, _arguments in calls:
        repository.accept_tool_attempt(
            lease.guard,
            attempt_id=attempt_id,
            assistant_entry_id=assistant_entry_id,
            tool_call_id=tool_call_id,
            authorization_kind="policy",
            authorization_reference="allow",
            actor_kind="runtime",
            actor_id="executor",
            remote_idempotency_key=None,
            retry_of_attempt_id=None,
            permission_snapshot_fingerprint=permission,
            occurred_at=datetime.now(timezone.utc),
            deadline_monotonic=monotonic() + 30,
        )
        contexts.append(
            SimpleNamespace(
                session_id=session_id,
                workspace_id=workspace_id,
                turn_id=turn_id,
                assistant_entry_id=assistant_entry_id,
                tool_call_id=tool_call_id,
                attempt_id=attempt_id,
                conversation_scope_kind="ROOT",
                scope_subagent_task_id=None,
                host_owner_epoch=lease.guard.writer_generation,
                effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
                permission_snapshot_fingerprint=permission,
                attempt_permission_snapshot_fingerprint=permission,
                subagent_parent_context_subject=subject,
            )
        )
    return lease, tuple(contexts)


@pytest.mark.postgres
def test_round10_batch_admission_exact_joins_args_permission_and_ack_unknown(
    stage2_migrated_postgres_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)

    async def exercise() -> None:
        session_id = _id("session")
        workspace_id = _id("workspace")
        arguments = {
            "tasks": [
                {
                    "task_key": "inspect",
                    "task": "Inspect the exact transaction boundary",
                    "context": {"mode": "none"},
                    "depends_on": [],
                },
                {
                    "task_key": "review",
                    "task": "Review only the direct dependency result",
                    "profile": "review_worker",
                    "depends_on": ["inspect"],
                },
            ]
        }
        lease, context = _prepare_root_tool_attempt(
            repository,
            session_id=session_id,
            workspace_id=workspace_id,
            tool_name="create_agent_tasks",
            arguments=arguments,
        )
        manager = KernelSubagentManager(
            repository=repository,
            guard=lease.guard,
            host_owner_id=_id("host"),
            io_owner=KernelSessionIO(),
            live_bus=LiveAgentEventBus(),
            todo_owner=TodoRunStateOwner(
                session_id=session_id, owner_epoch=_id("todo")
            ),
        )
        manager.bind_runner_factory(lambda: _BlockingChildRunner())  # type: ignore[arg-type]

        original = repository.accept_subagent_task_batch
        committed = False

        def commit_then_lose_ack(*args: object, **kwargs: object):
            nonlocal committed
            result = original(*args, **kwargs)
            if not committed:
                committed = True
                raise TimeoutError("simulated commit ACK loss")
            return result

        monkeypatch.setattr(
            repository, "accept_subagent_task_batch", commit_then_lose_ack
        )
        result = await manager.invoke(
            tool_name="create_agent_tasks",
            arguments=arguments,
            invocation_context=context,
        )
        assert result.state == "SUCCESS"
        assert committed
        rows = repository.list_subagent_tasks(
            session_id=session_id,
            maximum_items=50,
            deadline_monotonic=monotonic() + 30,
        )
        assert [row["task_key"] for row in rows] == ["inspect", "review"]
        assert [row["status"] for row in rows] == ["ACTIVE", "WAITING_DEPENDENCY"]
        first_with_lookahead = repository.list_subagent_tasks(
            session_id=session_id,
            maximum_items=1,
            include_lookahead=True,
            deadline_monotonic=monotonic() + 30,
        )
        assert [row["task_key"] for row in first_with_lookahead] == [
            "inspect",
            "review",
        ]
        second_page = repository.list_subagent_tasks(
            session_id=session_id,
            maximum_items=1,
            after_accepted_at=first_with_lookahead[0]["accepted_at"],
            after_task_id=str(first_with_lookahead[0]["id"]),
            include_lookahead=True,
            deadline_monotonic=monotonic() + 30,
        )
        assert [row["task_key"] for row in second_page] == ["review"]
        waiter = asyncio.create_task(
            manager.invoke(
                tool_name="wait_agent",
                arguments={
                    "task_id": str(rows[0]["id"]),
                    "timeout_seconds": 300,
                },
                invocation_context=context,
            )
        )
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        still_active = repository.query_subagent_task(
            session_id=session_id,
            task_id=str(rows[0]["id"]),
            deadline_monotonic=monotonic() + 30,
        )
        assert still_active is not None and still_active["status"] == "ACTIVE"
        await manager.aclose(timeout_seconds=2)

        mismatch_session = _id("session")
        mismatch_workspace = _id("workspace")
        stored = {"task": "canonical objective"}
        mismatch_lease, mismatch_context = _prepare_root_tool_attempt(
            repository,
            session_id=mismatch_session,
            workspace_id=mismatch_workspace,
            tool_name="spawn_agent",
            arguments=stored,
        )
        mismatch_manager = KernelSubagentManager(
            repository=repository,
            guard=mismatch_lease.guard,
            host_owner_id=_id("host"),
            io_owner=KernelSessionIO(),
            live_bus=LiveAgentEventBus(),
            todo_owner=TodoRunStateOwner(
                session_id=mismatch_session, owner_epoch=_id("todo")
            ),
        )
        mismatch_manager.bind_runner_factory(lambda: _BlockingChildRunner())  # type: ignore[arg-type]
        with pytest.raises(ConversationKernelConflict):
            await mismatch_manager.invoke(
                tool_name="spawn_agent",
                arguments={"task": "different objective"},
                invocation_context=mismatch_context,
            )
        assert (
            repository.list_subagent_tasks(
                session_id=mismatch_session,
                maximum_items=50,
                deadline_monotonic=monotonic() + 30,
            )
            == ()
        )

        mismatch_context.permission_snapshot_fingerprint = "sha256:foreign"
        stale = await mismatch_manager.invoke(
            tool_name="spawn_agent",
            arguments=stored,
            invocation_context=mismatch_context,
        )
        assert stale.state == "PERMISSION_DENIED"
        assert b"subagent_authority_mismatch" in stale.content
        await mismatch_manager.aclose(timeout_seconds=2)

        with repository.connection_provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            deadline_monotonic=monotonic() + 30,
        ) as connection:
            index = connection.execute(
                "SELECT indexdef FROM pg_catalog.pg_indexes "
                "WHERE schemaname='pulsara_v3' "
                "AND indexname='uq_pulsara_v3_subagent_task_terminal_result'"
            ).fetchone()
        assert index is not None
        assert "WHERE (child_kind = 'RESULT'::text)" in str(index[0])

    asyncio.run(exercise())


@pytest.mark.postgres
def test_round10_dependency_chain_routes_only_direct_result_and_retires_physical_carriers(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)

    async def exercise() -> None:
        session_id = _id("session")
        workspace_id = _id("workspace")
        arguments = {
            "tasks": [
                {
                    "task_key": "a",
                    "task": "alpha-only",
                    "depends_on": [],
                },
                {
                    "task_key": "b",
                    "task": "bravo-only",
                    "depends_on": ["a"],
                },
                {
                    "task_key": "c",
                    "task": "charlie-only",
                    "depends_on": ["b"],
                },
            ]
        }
        lease, context = _prepare_root_tool_attempt(
            repository,
            session_id=session_id,
            workspace_id=workspace_id,
            tool_name="create_agent_tasks",
            arguments=arguments,
        )
        manager = KernelSubagentManager(
            repository=repository,
            guard=lease.guard,
            host_owner_id=_id("host"),
            io_owner=KernelSessionIO(),
            live_bus=LiveAgentEventBus(),
            todo_owner=TodoRunStateOwner(
                session_id=session_id,
                owner_epoch=_id("todo"),
            ),
        )
        started: list[str] = []
        source_bodies: dict[str, tuple[str, ...]] = {}
        manager.bind_runner_factory(
            lambda: _CompletingChildRunner(
                repository=repository,
                lease=lease,
                manager=manager,
                started=started,
                source_bodies=source_bodies,
            )
        )  # type: ignore[arg-type]

        created = await manager.invoke(
            tool_name="create_agent_tasks",
            arguments=arguments,
            invocation_context=context,
        )
        assert created.state == "SUCCESS"
        task_ids = [item["task_id"] for item in json.loads(created.content)["tasks"]]
        waited = await manager.invoke(
            tool_name="wait_agent_tasks",
            arguments={
                "task_ids": task_ids,
                "settle": "all",
                "timeout_seconds": 10,
            },
            invocation_context=context,
        )
        payload = json.loads(waited.content)
        assert payload["pending_task_ids"] == []
        assert [item["status"] for item in payload["settled"]] == [
            "completed",
            "completed",
            "completed",
        ]
        assert started == ["alpha-only", "bravo-only", "charlie-only"]

        # A has no dependency source. B sees A. C sees only its direct B
        # result, never A's ancestor result or transcript.
        assert source_bodies["alpha-only"] == ()
        assert any(
            "result for alpha-only" in body for body in source_bodies["bravo-only"]
        )
        assert any(
            "result for bravo-only" in body for body in source_bodies["charlie-only"]
        )
        assert all("alpha-only" not in body for body in source_bodies["charlie-only"])

        # Canonical history remains queryable while physical child owners,
        # frozen parent context and mailboxes do not grow with terminal tasks.
        physical = tuple(item.task for item in manager._tasks.values())
        if physical:
            await asyncio.gather(*physical, return_exceptions=True)
        await asyncio.sleep(0)
        assert manager._tasks == {}
        assert manager._start_materials == {}
        assert manager._mailboxes == {}
        rows = repository.list_subagent_tasks(
            session_id=session_id,
            maximum_items=50,
            deadline_monotonic=monotonic() + 30,
        )
        assert len(rows) == 3
        assert all(row["status"] == "COMPLETED" for row in rows)
        await manager.aclose(timeout_seconds=2)

    asyncio.run(exercise())


@pytest.mark.postgres
def test_round10_global_four_worker_capacity_queues_without_limiting_task_horizon(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)

    async def exercise() -> None:
        session_id = _id("session")
        workspace_id = _id("workspace")
        arguments = {
            "tasks": [
                {
                    "task_key": f"worker_{index}",
                    "task": f"worker objective {index}",
                    "depends_on": [],
                }
                for index in range(5)
            ]
        }
        lease, context = _prepare_root_tool_attempt(
            repository,
            session_id=session_id,
            workspace_id=workspace_id,
            tool_name="create_agent_tasks",
            arguments=arguments,
        )
        manager = KernelSubagentManager(
            repository=repository,
            guard=lease.guard,
            host_owner_id=_id("host"),
            io_owner=KernelSessionIO(),
            live_bus=LiveAgentEventBus(),
            todo_owner=TodoRunStateOwner(
                session_id=session_id,
                owner_epoch=_id("todo"),
            ),
        )
        blocker = _CountingBlockingChildRunner()
        manager.bind_runner_factory(lambda: blocker)  # type: ignore[arg-type]
        created = await manager.invoke(
            tool_name="create_agent_tasks",
            arguments=arguments,
            invocation_context=context,
        )
        task_ids = [item["task_id"] for item in json.loads(created.content)["tasks"]]
        for _ in range(20):
            if len(blocker.started) == 4:
                break
            blocker.changed.clear()
            await asyncio.wait_for(blocker.changed.wait(), timeout=1)
        assert blocker.started == task_ids[:4]
        rows = repository.list_subagent_tasks(
            session_id=session_id,
            maximum_items=50,
            deadline_monotonic=monotonic() + 30,
        )
        assert [row["status"] for row in rows] == [
            "ACTIVE",
            "ACTIVE",
            "ACTIVE",
            "ACTIVE",
            "PENDING_START",
        ]

        stopped = await manager.invoke(
            tool_name="stop_agent",
            arguments={"task_id": task_ids[0]},
            invocation_context=context,
        )
        assert json.loads(stopped.content)["status"] == "cancelled"
        for _ in range(20):
            if task_ids[4] in blocker.started:
                break
            blocker.changed.clear()
            await asyncio.wait_for(blocker.changed.wait(), timeout=1)
        assert blocker.started == task_ids
        assert (
            len([item for item in manager._tasks.values() if not item.task.done()]) == 4
        )
        await manager.aclose(timeout_seconds=2)

    asyncio.run(exercise())


@pytest.mark.postgres
def test_round10_wait_agent_waits_for_dormant_dependency_terminalization(
    stage2_migrated_postgres_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)

    async def exercise() -> None:
        session_id = _id("session")
        arguments = {
            "tasks": [
                {"task_key": "upstream", "task": "block until stopped"},
                {
                    "task_key": "downstream",
                    "task": "must not run after upstream cancellation",
                    "depends_on": ["upstream"],
                },
            ]
        }
        lease, context = _prepare_root_tool_attempt(
            repository,
            session_id=session_id,
            workspace_id=_id("workspace"),
            tool_name="create_agent_tasks",
            arguments=arguments,
        )
        manager = KernelSubagentManager(
            repository=repository,
            guard=lease.guard,
            host_owner_id=_id("host"),
            io_owner=KernelSessionIO(),
            live_bus=LiveAgentEventBus(),
            todo_owner=TodoRunStateOwner(
                session_id=session_id,
                owner_epoch=_id("todo"),
            ),
        )
        blocker = _CountingBlockingChildRunner()
        manager.bind_runner_factory(lambda: blocker)  # type: ignore[arg-type]
        created = await manager.invoke(
            tool_name="create_agent_tasks",
            arguments=arguments,
            invocation_context=context,
        )
        task_ids = [item["task_id"] for item in json.loads(created.content)["tasks"]]
        wait = asyncio.create_task(
            manager.invoke(
                tool_name="wait_agent",
                arguments={"task_id": task_ids[1], "timeout_seconds": 10},
                invocation_context=context,
            )
        )
        await asyncio.sleep(0.05)
        assert not wait.done()

        original_frontier = repository.settle_subagent_dependency_frontier
        frontier_ack_lost = False

        def commit_frontier_then_timeout(*args: object, **kwargs: object):
            nonlocal frontier_ack_lost
            changed = original_frontier(*args, **kwargs)
            if not frontier_ack_lost:
                frontier_ack_lost = True
                raise TimeoutError("dependency frontier commit ACK lost")
            return changed

        monkeypatch.setattr(
            repository,
            "settle_subagent_dependency_frontier",
            commit_frontier_then_timeout,
        )

        await manager.invoke(
            tool_name="stop_agent",
            arguments={"task_id": task_ids[0]},
            invocation_context=context,
        )
        result = await asyncio.wait_for(wait, timeout=2)
        payload = json.loads(result.content)
        assert payload["task_id"] == task_ids[1]
        assert payload["status"] == "blocked_dependency_failed"
        assert blocker.started == [task_ids[0]]
        assert frontier_ack_lost
        assert task_ids[1] not in manager._start_materials
        assert task_ids[1] not in manager._mailboxes
        assert task_ids[1] not in manager._mailbox_ordinals
        assert task_ids[1] not in manager._completing
        await manager.aclose(timeout_seconds=2)

    asyncio.run(exercise())


@pytest.mark.postgres
def test_round10_mailbox_exact_fifo_ack_unknown_and_typed_child_projection(
    stage2_migrated_postgres_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)

    async def exercise() -> None:
        session_id = _id("session")
        workspace_id = _id("workspace")
        spawn_call = _id("call")
        spawn_attempt = _id("attempt")
        task_id = _round10_id("subagent-task", spawn_attempt, "0")
        first_call, first_attempt = _id("call"), _id("attempt")
        second_call, second_attempt = _id("call"), _id("attempt")
        calls = (
            (
                "spawn_agent",
                spawn_call,
                spawn_attempt,
                {"task": "wait for exact ROOT messages"},
            ),
            (
                "send_agent_message",
                first_call,
                first_attempt,
                {"task_id": task_id, "message": "first exact message"},
            ),
            (
                "send_agent_message",
                second_call,
                second_attempt,
                {"task_id": task_id, "message": "second exact message"},
            ),
        )
        lease, contexts = _prepare_root_tool_batch(
            repository,
            session_id=session_id,
            workspace_id=workspace_id,
            calls=calls,
        )
        manager = KernelSubagentManager(
            repository=repository,
            guard=lease.guard,
            host_owner_id=_id("host"),
            io_owner=KernelSessionIO(),
            live_bus=LiveAgentEventBus(),
            todo_owner=TodoRunStateOwner(
                session_id=session_id,
                owner_epoch=_id("todo"),
            ),
        )
        child = _CanonicalBlockingChildRunner(repository, lease)
        manager.bind_runner_factory(lambda: child)  # type: ignore[arg-type]
        spawned = await manager.invoke(
            tool_name="spawn_agent",
            arguments=calls[0][3],
            invocation_context=contexts[0],
        )
        assert json.loads(spawned.content)["task_id"] == task_id
        await asyncio.wait_for(child.started.wait(), timeout=5)
        for call, context in zip(calls[1:], contexts[1:], strict=True):
            queued = await manager.invoke(
                tool_name="send_agent_message",
                arguments=call[3],
                invocation_context=context,
            )
            assert json.loads(queued.content)["status"] == "queued"
        duplicate = await manager.invoke(
            tool_name="send_agent_message",
            arguments=calls[1][3],
            invocation_context=contexts[1],
        )
        assert json.loads(duplicate.content)["status"] == "queued"
        assert len(manager._mailboxes[task_id]) == 2

        original = repository.accept_inter_agent_mailbox_batch
        lost_ack = False

        def commit_then_timeout(*args: object, **kwargs: object):
            nonlocal lost_ack
            result = original(*args, **kwargs)
            if not lost_ack:
                lost_ack = True
                raise TimeoutError("mailbox commit ACK lost")
            return result

        monkeypatch.setattr(
            repository,
            "accept_inter_agent_mailbox_batch",
            commit_then_timeout,
        )
        consumed = await asyncio.gather(
            manager.consume_mailbox_safe_point(task_id),
            manager.consume_mailbox_safe_point(task_id),
        )
        assert consumed == [True, True]
        assert lost_ack
        assert manager._mailboxes[task_id] == []
        await asyncio.sleep(0)
        assert manager._mailbox_consumptions == {}

        child_turn = stable_subagent_turn_id(
            session_id=session_id,
            task_id=task_id,
        )
        cut = repository.prepare_provider_input_cut(
            lease.guard,
            turn_id=child_turn,
            deadline_monotonic=monotonic() + 30,
        )
        frozen = CanonicalProviderInputReader(provider).read_frozen_compile_snapshot(
            cut,
            deadline_monotonic=monotonic() + 30,
        )
        messages = [
            item
            for item in frozen.canonical_input.items
            if item.item_kind is FrozenProviderInputItemKind.INTER_AGENT_MESSAGE
        ]
        assert [
            json.loads(item.text)["pulsara_inter_agent_message"]["content"]
            for item in messages
        ] == [
            "first exact message",
            "second exact message",
        ]
        assert all(
            json.loads(item.text)["pulsara_inter_agent_message"]["sender"] == "ROOT"
            for item in messages
        )
        with provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            deadline_monotonic=monotonic() + 30,
        ) as connection:
            assert connection.execute(
                "SELECT array_agg(source_inter_agent_tool_attempt_id "
                "ORDER BY entry_sequence) "
                "FROM pulsara_v3.transcript_entries "
                "WHERE session_id=%s AND entry_kind='INTER_AGENT_MESSAGE'",
                (session_id,),
            ).fetchone() == ([first_attempt, second_attempt],)
            assert connection.execute(
                "SELECT count(*) FROM pulsara_v3.transcript_entries "
                "WHERE session_id=%s AND entry_kind='INTER_AGENT_MESSAGE'",
                (session_id,),
            ).fetchone() == (2,)
            assert connection.execute(
                "SELECT count(*) FROM pulsara_v3.agent_events "
                "WHERE session_id=%s "
                "AND event_type='InterAgentMessageAccepted'",
                (session_id,),
            ).fetchone() == (2,)
        await manager.aclose(timeout_seconds=2)

    asyncio.run(exercise())
