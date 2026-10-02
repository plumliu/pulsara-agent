"""Round 10 hierarchical worker/task-board product contracts."""

from __future__ import annotations

from pulsara_agent.llm.input import FrozenPromptContent
from pulsara_agent.llm.runtime import ModelRuntimeUnavailable

import asyncio
from dataclasses import replace
from hashlib import sha256
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from uuid import uuid4

import pytest

from pulsara_agent.capability.builtin_catalog import builtin_tool_catalog
from pulsara_agent.conversation_kernel.contracts import HostWriterGuard, InlineContent
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.execution_watchdogs import (
    KernelExecutionDeadlineFactory,
)
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.cancellation import stable_subagent_turn_id
from pulsara_agent.conversation_kernel.cold_epoch import build_subagent_initial_seed
from pulsara_agent.conversation_kernel.context_sources import (
    KernelContextSourceCollector,
)
from pulsara_agent.conversation_kernel.reader import CanonicalProviderInputReader
from pulsara_agent.conversation_kernel.repository import (
    AssistantTextBlock,
    AssistantToolCallBlock,
    ConversationKernelConflict,
    ConversationKernelRepository,
    StaleHostWriter,
)
from pulsara_agent.conversation_kernel.runner import KernelRunResult
from pulsara_agent.conversation_kernel.turn_admission import (
    SubagentTurnAdmissionPostCommitError,
)
from pulsara_agent.conversation_kernel.subagent import (
    ROOT_ORCHESTRATION_TOOL_NAMES,
    SUBAGENT_TOOL_NAMES,
    KernelSubagentManager,
    RootCompletionReadiness,
)
from pulsara_agent.conversation_kernel.subagents.launch import (
    CanonicalSubagentLaunchPreparationPort,
)
from pulsara_agent.conversation_kernel.tool_policy import (
    DefaultToolDispatchAuthorizationPolicy,
)
from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
from pulsara_agent.conversation_kernel.subagents.contracts import (
    SubagentContextMode,
    SubagentProfileKind,
    SubagentResultSource,
    build_dependency_result_context,
    build_parent_context_call_subject,
    build_parent_context_selection,
    build_root_context_unit,
    build_subagent_result_public_fact,
)
from pulsara_agent.conversation_kernel.todo_runtime import TodoRunStateOwner
from pulsara_agent.conversation_kernel.todo_runtime import build_child_activation
from pulsara_agent.primitives.context import canonical_json_bytes, freeze_json
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.run_permission import (
    RunPermissionAdmissionSource,
    build_run_permission_snapshot,
)
from pulsara_agent.model_input.contracts import (
    FrozenProviderInputItemKind,
    CanonicalInputOriginKind,
    ModelInputScopeKind,
    provider_input_item_text,
)
from pulsara_agent.ports.system_prompt import DEFAULT_SYSTEM_PROMPT
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from pulsara_agent.terminal_protocol.canonical_v3 import CanonicalProtocolReader
from tests.support.postgres import verified_postgres_provider
from tests.support.model_config import (
    acquire_bound_test_writer,
    start_test_root_turn,
    test_model_binding,
    test_model_runtime,
)
from tests.support.round3 import prepare_test_direct_tool_surface
from tests.support.subagents import (
    fixture_parent_target,
    StaticSubagentLaunchPreparationPort,
    accept_active_subagent_fixture,
)


async def _set_capacity(manager, target):
    if await manager.set_capacity_target(target):
        await manager.fill_available_capacity()
    return await manager.capacity_state()


def _id(prefix: str) -> str:
    if prefix == "workspace":
        return f"ctx:workspace/{uuid4().hex}"
    return f"{prefix}:{uuid4().hex}"


def _round10_id(namespace: str, *parts: str) -> str:
    digest = sha256()
    digest.update(f"pulsara:round10:{namespace}:v1\0".encode())
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    return f"{namespace}:{digest.hexdigest()}"


def _manager_launch_kwargs(
    repository: ConversationKernelRepository | None = None,
    guard: HostWriterGuard | None = None,
) -> dict[str, object]:
    launch_preparation = StaticSubagentLaunchPreparationPort()
    if repository is not None and guard is not None:
        launch_preparation = CanonicalSubagentLaunchPreparationPort(  # type: ignore[assignment]
            inherit_parent_target=fixture_parent_target,
            repository=repository,
            guard=guard,
            io_owner=KernelSessionIO(),
            model_runtime=test_model_runtime(),
            deadline_factory=KernelExecutionDeadlineFactory(),
        )
    return {
        "launch_preparation": launch_preparation,
        "hook_workspace_root": Path.cwd,
    }


def test_round10_tool_inventory_and_result_fact_are_closed() -> None:
    assert ROOT_ORCHESTRATION_TOOL_NAMES == {
        "spawn_agent",
        "create_agent_tasks",
        "list_agents",
        "list_agent_models",
        "wait_agent",
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
    absent_data = build_subagent_result_public_fact(
        task_id="task:test", result_id="result:absent", source=SubagentResultSource.EXPLICIT,
        producer_entry_id="entry:test", summary="done",
    )
    assert absent_data.data is None
    structured = build_subagent_result_public_fact(
        task_id="task:test", result_id="result:structured", source=SubagentResultSource.EXPLICIT,
        producer_entry_id="entry:test", summary="needs changes",
        data={"decision": "needs_changes", "issue_count": 2}, data_present=True,
    )
    assert json.loads(canonical_json_bytes(structured.data)) == {"decision": "needs_changes", "issue_count": 2}
    with pytest.raises(ValueError, match="delivery bound"):
        build_subagent_result_public_fact(
            task_id="task:test", result_id="result:large", source=SubagentResultSource.EXPLICIT,
            producer_entry_id="entry:test", summary="x" * 16_380,
            data={"decision": "needs_changes"}, data_present=True,
        )


def test_round10_subagent_guidance_is_complete_and_product_facing() -> None:
    descriptions = {
        entry.name: entry.descriptor.description
        for entry in builtin_tool_catalog()
        if entry.name in SUBAGENT_TOOL_NAMES
    }
    parent_guidance = "\n".join(
        descriptions[name] for name in sorted(ROOT_ORCHESTRATION_TOOL_NAMES)
    )
    delegated_prompt = DEFAULT_SYSTEM_PROMPT.split("Delegated work:\n", 1)[1].split(
        "\n\nCommunication:", 1
    )[0]

    skill = Path(
        "src/pulsara_agent/bundled_skills/pulsara-subagent/SKILL.md"
    ).read_text()
    assert "pulsara-subagent Skill" in descriptions["spawn_agent"]
    assert "separate conversation messages" in descriptions["wait_agent"]
    assert "predicate_satisfied does not mean success" in descriptions["wait_agent"]
    assert "not repeated list polling" in descriptions["list_agents"]
    assert "does not undo side effects" in descriptions["stop_agent"]
    assert "not read or acted on" in descriptions["send_agent_message"]
    assert "does not start another model reply" in delegated_prompt
    assert "wait after independent work is complete" in delegated_prompt
    assert "do not promise an automatic" in delegated_prompt
    assert "Finished tasks stay finished" in skill
    assert "worker_history" in skill and "material_task_ids" in skill

    for internal_term in (
        "ROOT",
        "canonical",
        "inbox",
        "safe point",
        "legal input boundary",
        "exact-turn",
    ):
        assert internal_term not in parent_guidance
        assert internal_term not in delegated_prompt


def test_round10_wait_is_level_triggered_input_barrier_without_result_transport() -> (
    None
):
    class _InlineIO:
        async def run(self, function, *args: object, **kwargs: object):
            return function(*args, **kwargs)

    class _Repository:
        @staticmethod
        def read_pending_prompt_steer_facts(**_kwargs: object):
            return ()

        @staticmethod
        def read_subagent_task_board(**_kwargs: object):
            # Historical terminal tasks do not make an untargeted wait block.
            return (), (("COMPLETED", 7), ("FAILED", 3))

    async def exercise() -> None:
        manager = KernelSubagentManager(
            **_manager_launch_kwargs(),
            repository=_Repository(),  # type: ignore[arg-type]
            guard=HostWriterGuard("session:test", 1, "host:test"),
            host_owner_id="host:test",
            io_owner=_InlineIO(),  # type: ignore[arg-type]
            live_bus=LiveAgentEventBus(),
            todo_owner=TodoRunStateOwner(
                session_id="session:test",
                owner_epoch="todo:test",
            ),
        )
        context = SimpleNamespace(turn_id="turn:root")
        idle = await manager._wait(  # noqa: SLF001
            {"timeout_seconds": 0}, context
        )
        assert json.loads(idle.content) == {
            "outcome": "nothing_pending",
            "satisfied_task_ids": [],
            "pending_task_ids": [],
        }

        assert await manager.offer_subagent_completion("task:done")
        immediate = await manager._wait(  # noqa: SLF001
            {"timeout_seconds": 30}, context
        )
        assert json.loads(immediate.content) == {
            "outcome": "completion_available",
            "satisfied_task_ids": [],
            "pending_task_ids": [],
        }
        assert b"result" not in immediate.content.lower()

    asyncio.run(exercise())


def test_round10_targeted_all_ignores_partial_and_unrelated_completions() -> None:
    class _InlineIO:
        async def run(self, function, *args: object, **kwargs: object):
            return function(*args, **kwargs)

    class _Repository:
        def __init__(self) -> None:
            self.statuses = {
                "task:a": "COMPLETED",
                "task:b": "ACTIVE",
            }

        @staticmethod
        def validate_host_writer(*_args: object, **_kwargs: object) -> None:
            return None

        @staticmethod
        def read_pending_prompt_steer_facts(**_kwargs: object):
            return ()

        def query_subagent_task(self, *, task_id: str, **_kwargs: object):
            return {
                "id": task_id,
                "status": self.statuses[task_id],
                "accepted_root_entry_id": None,
            }

    async def exercise() -> None:
        repository = _Repository()
        manager = KernelSubagentManager(
            **_manager_launch_kwargs(),
            repository=repository,  # type: ignore[arg-type]
            guard=HostWriterGuard("session:test", 1, "host:test"),
            host_owner_id="host:test",
            io_owner=_InlineIO(),  # type: ignore[arg-type]
            live_bus=LiveAgentEventBus(),
            todo_owner=TodoRunStateOwner(
                session_id="session:test",
                owner_epoch="todo:test",
            ),
        )
        await manager.open_root_completion_delivery("turn:root")
        assert await manager.offer_subagent_completion("task:unrelated")

        waiter = asyncio.create_task(
            manager._wait(  # noqa: SLF001
                {
                    "task_ids": ["task:a", "task:b"],
                    "settle": "all",
                    "timeout_seconds": 1,
                },
                SimpleNamespace(turn_id="turn:root", session_id="session:test"),
            )
        )
        await asyncio.sleep(0)
        assert not waiter.done()

        repository.statuses["task:b"] = "COMPLETED"
        await manager.notify_root_input_activity()
        result = await asyncio.wait_for(waiter, timeout=1)
        assert json.loads(result.content) == {
            "outcome": "predicate_satisfied",
            "satisfied_task_ids": ["task:a", "task:b"],
            "pending_task_ids": [],
        }
        assert await manager.snapshot_pending_root_completions("turn:root") == (
            "task:unrelated",
            "task:a",
            "task:b",
        )

    asyncio.run(exercise())


def test_round10_targeted_wait_claims_terminal_source_before_success() -> None:
    class _InlineIO:
        async def run(self, function, *args: object, **kwargs: object):
            return function(*args, **kwargs)

    class _Repository:
        @staticmethod
        def validate_host_writer(*_args: object, **_kwargs: object) -> None:
            return None

        @staticmethod
        def read_pending_prompt_steer_facts(**_kwargs: object):
            return ()

        @staticmethod
        def query_subagent_task(*, task_id: str, **_kwargs: object):
            return {
                "id": task_id,
                "status": "COMPLETED",
                "accepted_root_entry_id": None,
            }

    async def exercise() -> None:
        manager = KernelSubagentManager(
            **_manager_launch_kwargs(),
            repository=_Repository(),  # type: ignore[arg-type]
            guard=HostWriterGuard("session:test", 1, "host:test"),
            host_owner_id="host:test",
            io_owner=_InlineIO(),  # type: ignore[arg-type]
            live_bus=LiveAgentEventBus(),
            todo_owner=TodoRunStateOwner(
                session_id="session:test",
                owner_epoch="todo:test",
            ),
        )
        await manager.open_root_completion_delivery("turn:root")
        result = await manager._wait(  # noqa: SLF001
            {"task_ids": ["task:done"], "timeout_seconds": 0.001},
            SimpleNamespace(turn_id="turn:root", session_id="session:test"),
        )
        assert json.loads(result.content)["outcome"] == "predicate_satisfied"
        assert await manager.snapshot_pending_root_completions("turn:root") == (
            "task:done",
        )

    asyncio.run(exercise())


def test_round10_wait_exact_steer_precedes_join_and_untargeted_completion() -> None:
    class _InlineIO:
        async def run(self, function, *args: object, **kwargs: object):
            return function(*args, **kwargs)

    class _Repository:
        @staticmethod
        def validate_host_writer(*_args: object, **_kwargs: object) -> None:
            return None

        @staticmethod
        def read_pending_prompt_steer_facts(**kwargs: object):
            assert kwargs["target_turn_id"] == "turn:root"
            return ({"queue_item_id": "steer:exact"},)

        @staticmethod
        def query_subagent_task(*, task_id: str, **_kwargs: object):
            return {
                "id": task_id,
                "status": "COMPLETED",
                "accepted_root_entry_id": None,
            }

    async def exercise() -> None:
        manager = KernelSubagentManager(
            **_manager_launch_kwargs(),
            repository=_Repository(),  # type: ignore[arg-type]
            guard=HostWriterGuard("session:test", 1, "host:test"),
            host_owner_id="host:test",
            io_owner=_InlineIO(),  # type: ignore[arg-type]
            live_bus=LiveAgentEventBus(),
            todo_owner=TodoRunStateOwner(
                session_id="session:test", owner_epoch="todo:test"
            ),
        )
        await manager.open_root_completion_delivery("turn:root")
        assert await manager.offer_subagent_completion("task:unrelated")
        context = SimpleNamespace(turn_id="turn:root", session_id="session:test")

        targeted = await manager._wait(  # noqa: SLF001
            {"task_ids": ["task:done"], "settle": "all", "timeout_seconds": 0},
            context,
        )
        assert json.loads(targeted.content) == {
            "outcome": "steer_available",
            "satisfied_task_ids": ["task:done"],
            "pending_task_ids": [],
        }
        untargeted = await manager._wait(  # noqa: SLF001
            {"timeout_seconds": 0}, context
        )
        assert json.loads(untargeted.content)["outcome"] == "steer_available"

    asyncio.run(exercise())


def test_round10_wait_deadline_performs_final_exact_predicate_check() -> None:
    class _InlineIO:
        async def run(self, function, *args: object, **kwargs: object):
            return function(*args, **kwargs)

    class _Repository:
        query_count = 0

        @staticmethod
        def validate_host_writer(*_args: object, **_kwargs: object) -> None:
            return None

        @staticmethod
        def read_pending_prompt_steer_facts(**_kwargs: object):
            return ()

        def query_subagent_task(self, *, task_id: str, **_kwargs: object):
            self.query_count += 1
            return {
                "id": task_id,
                "status": "ACTIVE" if self.query_count == 1 else "COMPLETED",
                "accepted_root_entry_id": None,
            }

    async def exercise() -> None:
        repository = _Repository()
        manager = KernelSubagentManager(
            **_manager_launch_kwargs(),
            repository=repository,  # type: ignore[arg-type]
            guard=HostWriterGuard("session:test", 1, "host:test"),
            host_owner_id="host:test",
            io_owner=_InlineIO(),  # type: ignore[arg-type]
            live_bus=LiveAgentEventBus(),
            todo_owner=TodoRunStateOwner(
                session_id="session:test", owner_epoch="todo:test"
            ),
        )
        await manager.open_root_completion_delivery("turn:root")
        result = await manager._wait(  # noqa: SLF001
            {"task_ids": ["task:done"], "timeout_seconds": 0.001},
            SimpleNamespace(turn_id="turn:root", session_id="session:test"),
        )
        assert json.loads(result.content)["outcome"] == "predicate_satisfied"
        assert repository.query_count >= 2

    asyncio.run(exercise())


def test_round10_join_readiness_rejects_seal_race_without_enqueuing() -> None:
    class _InlineIO:
        async def run(self, function, *args: object, **kwargs: object):
            if asyncio.iscoroutinefunction(function):
                return await function(*args, **kwargs)
            return function(*args, **kwargs)

    class _Repository:
        entered = asyncio.Event()
        release = asyncio.Event()

        @staticmethod
        def validate_host_writer(*_args: object, **_kwargs: object) -> None:
            return None

        @staticmethod
        def read_pending_prompt_steer_facts(**_kwargs: object):
            return ()

        async def query_subagent_task(self, *, task_id: str, **_kwargs: object):
            self.entered.set()
            await self.release.wait()
            return {
                "id": task_id,
                "status": "COMPLETED",
                "accepted_root_entry_id": None,
            }

    async def exercise() -> None:
        repository = _Repository()
        manager = KernelSubagentManager(
            **_manager_launch_kwargs(),
            repository=repository,  # type: ignore[arg-type]
            guard=HostWriterGuard("session:test", 1, "host:test"),
            host_owner_id="host:test",
            io_owner=_InlineIO(),  # type: ignore[arg-type]
            live_bus=LiveAgentEventBus(),
            todo_owner=TodoRunStateOwner(
                session_id="session:test", owner_epoch="todo:test"
            ),
        )
        await manager.open_root_completion_delivery("turn:root")
        wait = asyncio.create_task(
            manager._wait(  # noqa: SLF001
                {"task_ids": ["task:done"], "timeout_seconds": 30},
                SimpleNamespace(turn_id="turn:root", session_id="session:test"),
            )
        )
        await repository.entered.wait()
        await manager.seal_root_completion_delivery("turn:root")
        repository.release.set()
        result = await wait
        assert result.state == "APPLICATION_ERROR"
        assert json.loads(result.content) == {"error": "wait_target_not_open"}
        assert tuple(manager._root_completion_queue) == ()  # noqa: SLF001

    asyncio.run(exercise())


def test_round10_join_readiness_rechecks_owner_after_canonical_reads() -> None:
    class _InlineIO:
        async def run(self, function, *args: object, **kwargs: object):
            return function(*args, **kwargs)

    class _Repository:
        validations = 0

        def validate_host_writer(self, *_args: object, **_kwargs: object) -> None:
            self.validations += 1
            if self.validations == 2:
                raise StaleHostWriter("dogfood owner takeover")

        @staticmethod
        def query_subagent_task(*, task_id: str, **_kwargs: object):
            return {
                "id": task_id,
                "status": "COMPLETED",
                "accepted_root_entry_id": None,
            }

    async def exercise() -> None:
        repository = _Repository()
        manager = KernelSubagentManager(
            **_manager_launch_kwargs(),
            repository=repository,  # type: ignore[arg-type]
            guard=HostWriterGuard("session:test", 1, "host:test"),
            host_owner_id="host:test",
            io_owner=_InlineIO(),  # type: ignore[arg-type]
            live_bus=LiveAgentEventBus(),
            todo_owner=TodoRunStateOwner(
                session_id="session:test", owner_epoch="todo:test"
            ),
        )
        await manager.open_root_completion_delivery("turn:root")
        readiness = await manager.ensure_root_completion_ready(
            session_id="session:test",
            root_turn_id="turn:root",
            task_ids=("task:done",),
        )
        assert readiness is RootCompletionReadiness.OWNER_UNAVAILABLE
        assert repository.validations == 2
        assert tuple(manager._root_completion_queue) == ()  # noqa: SLF001

    asyncio.run(exercise())


def test_round10_join_readiness_preserves_fifo_and_skips_accepted_sources() -> None:
    class _InlineIO:
        async def run(self, function, *args: object, **kwargs: object):
            return function(*args, **kwargs)

    class _Repository:
        @staticmethod
        def validate_host_writer(*_args: object, **_kwargs: object) -> None:
            return None

        @staticmethod
        def query_subagent_task(*, task_id: str, **_kwargs: object):
            return {
                "id": task_id,
                "status": "FAILED" if task_id == "task:new" else "COMPLETED",
                "accepted_root_entry_id": (
                    "entry:accepted" if task_id == "task:accepted" else None
                ),
            }

    async def exercise() -> None:
        manager = KernelSubagentManager(
            **_manager_launch_kwargs(),
            repository=_Repository(),  # type: ignore[arg-type]
            guard=HostWriterGuard("session:test", 1, "host:test"),
            host_owner_id="host:test",
            io_owner=_InlineIO(),  # type: ignore[arg-type]
            live_bus=LiveAgentEventBus(),
            todo_owner=TodoRunStateOwner(
                session_id="session:test", owner_epoch="todo:test"
            ),
        )
        await manager.open_root_completion_delivery("turn:root")
        assert await manager.offer_subagent_completion("task:existing")
        readiness = await manager.ensure_root_completion_ready(
            session_id="session:test",
            root_turn_id="turn:root",
            task_ids=("task:accepted", "task:existing", "task:new"),
        )
        assert readiness is RootCompletionReadiness.READY
        assert await manager.snapshot_pending_root_completions("turn:root") == (
            "task:existing",
            "task:new",
        )

    asyncio.run(exercise())


def test_round10_root_completion_answer_fence_keeps_late_delivery_for_next_turn() -> (
    None
):
    async def exercise() -> None:
        manager = KernelSubagentManager(
            **_manager_launch_kwargs(),
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
        await manager.open_root_completion_delivery("turn:first")
        assert await manager.offer_subagent_completion("task:before-fence")
        assert not await manager.offer_subagent_completion("task:before-fence")
        assert await manager.seal_root_completion_delivery("turn:first")
        assert await manager.snapshot_pending_root_completions("turn:first") == ()
        await manager.settle_root_completion_delivery(
            "turn:first", turn_completed=False
        )
        assert await manager.snapshot_pending_root_completions("turn:first") == (
            "task:before-fence",
        )
        assert await manager.retire_root_completion("task:before-fence")

        # Once the no-tool answer has sealed the exact turn, an arrival cannot
        # reopen it or force a background model sample.
        assert not await manager.seal_root_completion_delivery("turn:first")
        assert await manager.offer_subagent_completion("task:after-fence")
        await manager.settle_root_completion_delivery("turn:first", turn_completed=True)
        assert await manager.snapshot_pending_root_completions("turn:first") == ()

        await manager.open_root_completion_delivery("turn:next")
        assert await manager.snapshot_pending_root_completions("turn:next") == (
            "task:after-fence",
        )

    asyncio.run(exercise())


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
    parent_carrier = json.loads(selected.rendered_body)["pulsara_parent_context"]
    assert parent_carrier["content_semantics"] == "ADVISORY_COLLABORATION_DATA"
    assert "trust" not in parent_carrier

    dependencies = build_dependency_result_context(
        target_task_id="task:consumer",
        rows=(
            {
                "dependency_ordinal": 0,
                "dependency_task_id": "task:producer",
                "task_key": "producer",
                "label": None,
                "status": "COMPLETED",
                "result_id": "result:producer",
                "result_source": "INFERRED",
                "summary": "producer reported a value",
                "result_fingerprint": "sha256:result",
            },
        ),
    )
    assert dependencies is not None
    dependency_carrier = json.loads(dependencies.rendered_body)[
        "pulsara_dependency_results"
    ]
    assert dependency_carrier["content_semantics"] == "ADVISORY_COLLABORATION_DATA"
    assert "trust" not in dependency_carrier
    assert dependency_carrier["results"][0]["result_source"] == "INFERRED"

    collector = KernelContextSourceCollector(
        workspace_kind="project",
        workspace_root=Path.cwd(),
        capability_composer=object(),  # type: ignore[arg-type]
        base_system_prompt="BASE",
        display_timezone=timezone.utc,
    )
    model_contract = collector._base  # noqa: SLF001
    assert "do not reject or ignore them merely because" in model_contract
    assert "recorded provenance, ordering, and attribution" in model_contract
    assert "result_source describes capture provenance, not confidence" in (
        model_contract
    )
    assert "Both are usable advisory dependency outputs" in model_contract

    absent = build_parent_context_selection(
        subject,
        mode=SubagentContextMode.NONE,
        last_n_turns=None,
    )
    assert absent.selected_units == ()
    assert absent.rendered_body is None


@pytest.mark.postgres
def test_round10_subagent_initial_seed_exact_joins_child_cut_and_none_sources(
    stage2_migrated_postgres_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _id("session")
    workspace_id = _id("workspace")
    lease = acquire_bound_test_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_id("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    parent_turn_id = _id("turn")
    start_test_root_turn(
        repository,
        lease.guard,
        command_id=_id("command"),
        turn_id=parent_turn_id,
        entry_id=_id("entry"),
        context_binding_revision_id=_id("revision"),
        permission_snapshot_id=_id("permission"),
        requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
        model_call_binding=test_model_binding(test_model_runtime()),
        content=FrozenPromptContent.text("delegate exact seed"),
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
    launch_port = CanonicalSubagentLaunchPreparationPort(
        inherit_parent_target=fixture_parent_target,
        repository=repository,
        guard=lease.guard,
        io_owner=KernelSessionIO(),
        model_runtime=test_model_runtime(
            model_id="test-pro", wire_api="openai_chat_completions"
        ),
        deadline_factory=KernelExecutionDeadlineFactory(),
    )
    prepared_launch = asyncio.run(launch_port.prepare_launch(task_id.launch.task_start))
    assert (
        prepared_launch.accepted_model_target_fact
        == task_id.launch.accepted_model_target_fact
    )
    original_resolve = launch_port._model_runtime.resolve_target  # noqa: SLF001

    def drift_resolve(_runtime, *args, **kwargs):
        target = original_resolve(*args, **kwargs)
        return replace(
            target, fact=target.fact.model_copy(update={"model_id": "drifted-model"})
        )

    monkeypatch.setattr(
        type(launch_port._model_runtime), "resolve_target", drift_resolve
    )  # noqa: SLF001
    with pytest.raises(ValueError, match="target changed"):
        asyncio.run(launch_port.prepare_launch(task_id.launch.task_start))
    child_turn = stable_subagent_turn_id(session_id=session_id, task_id=task_id)
    repository.start_subagent_turn(
        lease.guard,
        task_id=task_id,
        turn_id=child_turn,
        entry_id=_id("entry"),
        context_binding_revision_id=_id("revision"),
        task_start_event_id=task_id.launch.task_start.event_id,
        expected_parent_permission_snapshot=(task_id.launch.parent_permission_snapshot),
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
    seed = build_subagent_initial_seed(
        dispatch_read=dispatch_read,
        task_id=task_id,
        parent_turn_id=parent_turn_id,
        profile_kind=SubagentProfileKind.GENERAL_WORKER,
        objective=objective,
    )
    assert seed.task_id == task_id
    assert (
        seed.objective_item.source_entry_id
        == dispatch_read.compile_snapshot.canonical_input.identity.initial_entry_id
    )
    assert all(
        source.__class__.__name__ == "ContextSourceAbsentFact"
        for source in seed.source_replacements
    )
    with pytest.raises(ValueError, match="subagent canonical initial seed is invalid"):
        replace(seed, task_id="task:foreign")
    with pytest.raises(ValueError, match="subagent canonical initial seed is invalid"):
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
            **_manager_launch_kwargs(),
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
            "list_agent_models": {},
            "wait_agent": {"task_ids": ["task:test"]},
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


@pytest.mark.parametrize(
    "tool_name", ("list_agent_models", "spawn_agent", "create_agent_tasks")
)
def test_model_configuration_failure_is_returned_before_creating_workers(
    tool_name: str,
) -> None:
    class UnavailableModels(StaticSubagentLaunchPreparationPort):
        def list_models(self):
            raise ModelRuntimeUnavailable("local model settings are unavailable")

        def freeze_target(self, *_args, **_kwargs):
            raise ModelRuntimeUnavailable("local model settings are unavailable")

    async def exercise():
        binding = test_model_binding(test_model_runtime())
        # Only the read before launch preparation is allowed. No admission or
        # task execution method exists on this repository double.
        repository = SimpleNamespace(
            read_turn_model_call_binding=lambda *_args, **_kwargs: binding
        )
        manager = KernelSubagentManager(
            launch_preparation=UnavailableModels(),
            hook_workspace_root=Path.cwd,
            repository=repository,
            guard=HostWriterGuard("session:test", 1, "host:test"),
            host_owner_id="host:test",
            io_owner=KernelSessionIO(),
            live_bus=LiveAgentEventBus(),
            todo_owner=TodoRunStateOwner(
                session_id="session:test", owner_epoch="todo:test"
            ),
        )
        subject = build_parent_context_call_subject(
            session_id="session:test",
            caller_turn_id="turn:test",
            provider_input_cut_fingerprint="cut:test",
            continuity_epoch_nonce="epoch:test",
            continuity_epoch_revision=0,
            compiled_semantic_input_fingerprint="semantic:test",
            compiled_message_placements_fingerprint="placements:test",
            ordered_eligible_units=(),
        )
        context = SimpleNamespace(
            session_id="session:test",
            workspace_id="workspace:test",
            turn_id="turn:test",
            attempt_id="attempt:test",
            conversation_scope_kind="ROOT",
            scope_subagent_task_id=None,
            host_owner_epoch=1,
            effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
            permission_snapshot_fingerprint="permission:test",
            attempt_permission_snapshot_fingerprint="permission:test",
            subagent_parent_context_subject=subject,
        )
        task = {
            "task": "inspect source",
            "model": {"connection_id": binding.connection_id.value},
        }
        arguments = (
            {}
            if tool_name == "list_agent_models"
            else task
            if tool_name == "spawn_agent"
            else {"tasks": [task]}
        )
        result = await manager.invoke(
            tool_name=tool_name, arguments=arguments, invocation_context=context
        )
        assert result.state == "SYSTEM_ERROR"
        assert (
            json.loads(result.content)["error"]
            == "local model settings are unavailable"
        )
        assert not manager.has_active_work()

    asyncio.run(exercise())


def test_round10_send_and_completion_share_one_exact_linearization() -> None:
    async def exercise() -> None:
        manager = KernelSubagentManager(
            **_manager_launch_kwargs(),
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
        parent_permission = build_run_permission_snapshot(
            snapshot_id="permission:parent",
            requested_mode=PermissionMode.BYPASS_PERMISSIONS,
            effective_mode=PermissionMode.BYPASS_PERMISSIONS,
            admission_source=RunPermissionAdmissionSource.USER_SUBMISSION,
        )
        child_permission = build_run_permission_snapshot(
            snapshot_id="permission:child",
            requested_mode=PermissionMode.BYPASS_PERMISSIONS,
            effective_mode=PermissionMode.BYPASS_PERMISSIONS,
            admission_source=RunPermissionAdmissionSource.SUBAGENT_INHERITANCE,
            inherited_from_turn_id="turn:parent",
        )
        launch = SimpleNamespace(
            configured_model_identity="test-pro",
            child_turn_id="turn:child",
            parent_permission_snapshot=parent_permission,
            task_start=SimpleNamespace(
                task_id=task_id,
                parent_turn_id="turn:parent",
                profile=SubagentProfileKind.GENERAL_WORKER,
            ),
        )
        physical = asyncio.create_task(asyncio.Event().wait())
        manager._tasks[task_id] = SimpleNamespace(
            task_id=task_id,
            status="ACTIVE",
            task=physical,
            cancellation_intent=SimpleNamespace(turn_id="turn:child"),
            launch=launch,
            hook_scope=None,
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
                model_id="test-pro",
                permission_snapshot=child_permission,
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
            model_id="test-pro",
            permission_snapshot=child_permission,
        )
        assert prepared is not None
        context.tool_call_id = "call:late"
        context.attempt_id = "attempt:late"
        late = await manager._send_message(
            {"task_id": task_id, "message": "too late"},
            context,
        )
        assert late.state == "TOOL_UNAVAILABLE"
        await manager.finish_completion(prepared.permit, committed=False)
        physical.cancel()
        await asyncio.gather(physical, return_exceptions=True)

    asyncio.run(exercise())


class _BlockingChildRunner:
    async def admit_subagent_turn(self, **kwargs: object):
        return kwargs["cancellation_intent"]

    async def run_admitted_subagent_turn(self, **_kwargs: object):
        await asyncio.Event().wait()


class _BlockingLaunchPreparation:
    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    def freeze_target(self, binding, *, use_default: bool):
        return self._inner.freeze_target(binding, use_default=use_default)

    def inherit_target(self, subject, binding):
        return self._inner.inherit_target(subject, binding)

    def list_models(self):
        return self._inner.list_models()

    async def prepare_launch(self, candidate):
        self.entered.set()
        await self.release.wait()
        return await self._inner.prepare_launch(candidate)  # type: ignore[attr-defined]


class _BlockingCanonicalAdmissionRunner:
    def __init__(self, repository: ConversationKernelRepository, lease) -> None:
        self._repository = repository
        self._lease = lease
        self.admission_entered = asyncio.Event()
        self.admission_release = asyncio.Event()
        self.run_started = 0

    async def admit_subagent_turn(self, *, launch, cancellation_intent):
        self.admission_entered.set()
        await self.admission_release.wait()
        task_id = launch.task_start.task_id
        self._repository.start_subagent_turn(
            self._lease.guard,
            task_id=task_id,
            turn_id=cancellation_intent.turn_id,
            entry_id=_id("entry"),
            context_binding_revision_id=_id("revision"),
            task_start_event_id=launch.task_start.event_id,
            expected_parent_permission_snapshot=(launch.parent_permission_snapshot),
            content=InlineContent.from_bytes(
                launch.task_start.objective.encode("utf-8")
            ),
            occurred_at=datetime.now(timezone.utc),
            actor_id=task_id,
            deadline_monotonic=monotonic() + 30,
        )
        return cancellation_intent

    async def run_admitted_subagent_turn(self, **_kwargs: object):
        self.run_started += 1
        await asyncio.Event().wait()


class _CountingBlockingChildRunner:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.changed = asyncio.Event()

    async def admit_subagent_turn(self, **kwargs: object):
        return kwargs["cancellation_intent"]

    async def run_admitted_subagent_turn(self, *, launch, **_kwargs: object):
        task_id = launch.task_start.task_id
        self.started.append(task_id)
        self.changed.set()
        await asyncio.Event().wait()


class _TodoAwareCountingBlockingChildRunner:
    """Exercise the real post-admission TODO ownership boundary."""

    def __init__(self, repository, lease, todo_owner: TodoRunStateOwner) -> None:
        self._repository = repository
        self._lease = lease
        self._todo_owner = todo_owner
        self.started: list[str] = []
        self.changed = asyncio.Event()

    async def admit_subagent_turn(self, *, launch, cancellation_intent):
        task_id = launch.task_start.task_id
        context_binding_revision_id = _id("revision")
        accepted = self._repository.start_subagent_turn(
            self._lease.guard,
            task_id=task_id,
            turn_id=cancellation_intent.turn_id,
            entry_id=_id("entry"),
            context_binding_revision_id=context_binding_revision_id,
            task_start_event_id=launch.task_start.event_id,
            expected_parent_permission_snapshot=launch.parent_permission_snapshot,
            content=InlineContent.from_bytes(
                launch.task_start.objective.encode("utf-8")
            ),
            occurred_at=datetime.now(timezone.utc),
            actor_id=task_id,
            deadline_monotonic=monotonic() + 30,
        )
        try:
            self._todo_owner.activate_child_run(
                build_child_activation(
                    session_id=launch.task_start.session_id,
                    subagent_task_id=task_id,
                    exact_turn_id=accepted.turn_id,
                    exact_initial_entry_id=accepted.entry_id,
                    exact_context_binding_revision_id=context_binding_revision_id,
                )
            )
        except BaseException as exc:
            raise SubagentTurnAdmissionPostCommitError(accepted, exc) from exc
        return cancellation_intent

    async def run_admitted_subagent_turn(self, *, launch, **_kwargs: object):
        task_id = launch.task_start.task_id
        self.started.append(task_id)
        self.changed.set()
        await asyncio.Event().wait()


class _CanonicalBlockingChildRunner:
    def __init__(self, repository: ConversationKernelRepository, lease) -> None:
        self._repository = repository
        self._lease = lease
        self.started = asyncio.Event()

    async def admit_subagent_turn(
        self,
        *,
        launch,
        cancellation_intent,
    ):
        task_id = launch.task_start.task_id
        self._repository.start_subagent_turn(
            self._lease.guard,
            task_id=task_id,
            turn_id=cancellation_intent.turn_id,
            entry_id=_id("entry"),
            context_binding_revision_id=_id("revision"),
            task_start_event_id=launch.task_start.event_id,
            expected_parent_permission_snapshot=(launch.parent_permission_snapshot),
            content=InlineContent.from_bytes(
                launch.task_start.objective.encode("utf-8")
            ),
            occurred_at=datetime.now(timezone.utc),
            actor_id=task_id,
            deadline_monotonic=monotonic() + 30,
        )
        return cancellation_intent

    async def run_admitted_subagent_turn(self, **_kwargs: object) -> KernelRunResult:
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

    async def admit_subagent_turn(
        self,
        *,
        launch,
        cancellation_intent,
    ):
        task_id = launch.task_start.task_id
        objective = launch.task_start.objective
        turn_id = launch.child_turn_id
        self._repository.start_subagent_turn(
            self._lease.guard,
            task_id=task_id,
            turn_id=turn_id,
            entry_id=_id("entry"),
            context_binding_revision_id=_id("revision"),
            task_start_event_id=launch.task_start.event_id,
            expected_parent_permission_snapshot=(launch.parent_permission_snapshot),
            content=InlineContent.from_bytes(objective.encode("utf-8")),
            occurred_at=datetime.now(timezone.utc),
            actor_id=task_id,
            deadline_monotonic=monotonic() + 30,
        )
        return cancellation_intent

    async def run_admitted_subagent_turn(
        self,
        *,
        launch,
        cancellation_intent,
    ) -> KernelRunResult:
        del cancellation_intent
        task_id = launch.task_start.task_id
        objective = launch.task_start.objective
        self._started.append(objective)
        turn_id = launch.child_turn_id
        cut = self._repository.prepare_provider_input_cut(
            self._lease.guard,
            turn_id=turn_id,
            deadline_monotonic=monotonic() + 30,
        )
        from pulsara_agent.model_input.lowering import (
            compaction_snapshot_provider_content,
        )

        frozen = CanonicalProviderInputReader(
            self._repository._provider
        ).read_frozen_compile_snapshot(cut, deadline_monotonic=monotonic() + 30)
        self._source_bodies[objective] = tuple(
            part.text
            for item in frozen.canonical_input.items
            if item.source_entry_id != frozen.canonical_input.identity.initial_entry_id
            or item.item_kind is FrozenProviderInputItemKind.INITIAL_CONTEXT_MATERIAL
            for part in (
                compaction_snapshot_provider_content(item.content)
                if item.item_kind is FrozenProviderInputItemKind.CONTEXT_SNAPSHOT
                else item.content
            )
            if hasattr(part, "text")
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
    lease = acquire_bound_test_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_id("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    turn_id = _id("turn")
    start_test_root_turn(
        repository,
        lease.guard,
        command_id=_id("command"),
        turn_id=turn_id,
        entry_id=_id("entry"),
        context_binding_revision_id=_id("revision"),
        permission_snapshot_id=_id("permission"),
        requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
        model_call_binding=test_model_binding(test_model_runtime()),
        content=FrozenPromptContent.text('delegate bounded work'),
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

    lease = acquire_bound_test_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_id("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    turn_id = _id("turn")
    start_test_root_turn(
        repository,
        lease.guard,
        command_id=_id("command"),
        turn_id=turn_id,
        entry_id=_id("entry"),
        context_binding_revision_id=_id("revision"),
        permission_snapshot_id=_id("permission"),
        requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
        model_call_binding=test_model_binding(test_model_runtime()),
        content=FrozenPromptContent.text('orchestrate and send exact messages'),
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
def test_round9_2_subagent_launch_permit_closes_both_stop_race_sides(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)

    async def stop_wins_before_launch_claim() -> None:
        session_id = _id("session")
        lease, context = _prepare_root_tool_attempt(
            repository,
            session_id=session_id,
            workspace_id=_id("workspace"),
            tool_name="spawn_agent",
            arguments={"task": "stop before child launch claim"},
        )
        launch_kwargs = _manager_launch_kwargs(repository, lease.guard)
        launch = _BlockingLaunchPreparation(launch_kwargs["launch_preparation"])
        launch_kwargs["launch_preparation"] = launch
        manager = KernelSubagentManager(
            **launch_kwargs,
            repository=repository,
            guard=lease.guard,
            host_owner_id=_id("host"),
            io_owner=KernelSessionIO(),
            live_bus=LiveAgentEventBus(),
            todo_owner=TodoRunStateOwner(
                session_id=session_id, owner_epoch=_id("todo")
            ),
        )
        runner_factory_calls = 0
        progress: list[tuple[str, str]] = []

        def runner_factory(_scope):
            nonlocal runner_factory_calls
            runner_factory_calls += 1
            return _BlockingChildRunner()

        manager.bind_runner_factory(runner_factory)  # type: ignore[arg-type]
        manager._offer_progress = (  # type: ignore[method-assign]
            lambda _task_id, _parent_turn_id, status, summary: progress.append(
                (status, summary)
            )
        )
        spawning = asyncio.create_task(
            manager.invoke(
                tool_name="spawn_agent",
                arguments={"task": "stop before child launch claim"},
                invocation_context=context,
            )
        )
        await asyncio.wait_for(launch.entered.wait(), timeout=5)
        task_id = _round10_id("subagent-task", context.attempt_id, "0")
        stopping = asyncio.create_task(
            manager.invoke(
                tool_name="stop_agent",
                arguments={"task_id": task_id},
                invocation_context=context,
            )
        )
        deadline = monotonic() + 5
        while not manager._launch_permits[task_id].stop_claimed:
            assert monotonic() < deadline
            await asyncio.sleep(0)
        launch.release.set()
        stopped, _spawned = await asyncio.gather(stopping, spawning)
        assert json.loads(stopped.content)["status"] == "cancelled"
        assert runner_factory_calls == 0
        assert task_id not in manager._tasks
        assert all(status != "ACTIVE" for status, _summary in progress)
        durable = repository.query_subagent_task(
            session_id=session_id,
            task_id=task_id,
            deadline_monotonic=monotonic() + 30,
        )
        assert durable is not None
        assert durable["status"] == "CANCELLED"
        with provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            deadline_monotonic=monotonic() + 30,
        ) as connection:
            assert connection.execute(
                "SELECT count(*) FROM pulsara_v3.turns "
                "WHERE session_id=%s AND scope_subagent_task_id=%s",
                (session_id, task_id),
            ).fetchone() == (0,)
        await manager.aclose(deadline_monotonic=monotonic() + 2)

    async def launch_wins_before_stop_claim() -> None:
        session_id = _id("session")
        todo_owner = TodoRunStateOwner(session_id=session_id, owner_epoch=_id("todo"))
        lease, context = _prepare_root_tool_attempt(
            repository,
            session_id=session_id,
            workspace_id=_id("workspace"),
            tool_name="spawn_agent",
            arguments={"task": "stop after child launch claim"},
        )
        manager = KernelSubagentManager(
            **_manager_launch_kwargs(repository, lease.guard),
            repository=repository,
            guard=lease.guard,
            host_owner_id=_id("host"),
            io_owner=KernelSessionIO(),
            live_bus=LiveAgentEventBus(),
            todo_owner=todo_owner,
        )
        child = _BlockingCanonicalAdmissionRunner(repository, lease)
        progress: list[tuple[str, str]] = []
        manager.bind_runner_factory(lambda _scope: child)  # type: ignore[arg-type]
        manager._offer_progress = (  # type: ignore[method-assign]
            lambda _task_id, _parent_turn_id, status, summary: progress.append(
                (status, summary)
            )
        )
        spawning = asyncio.create_task(
            manager.invoke(
                tool_name="spawn_agent",
                arguments={"task": "stop after child launch claim"},
                invocation_context=context,
            )
        )
        await asyncio.wait_for(child.admission_entered.wait(), timeout=5)
        task_id = _round10_id("subagent-task", context.attempt_id, "0")
        assert manager._launch_permits[task_id].launching
        stopping = asyncio.create_task(
            manager.invoke(
                tool_name="stop_agent",
                arguments={"task_id": task_id},
                invocation_context=context,
            )
        )
        deadline = monotonic() + 5
        while manager._launch_permits[task_id].cancellation_reason is None:
            assert monotonic() < deadline
            await asyncio.sleep(0)
        assert not manager._launch_permits[task_id].stop_claimed
        child.admission_release.set()
        stopped, _spawned = await asyncio.gather(stopping, spawning)
        assert json.loads(stopped.content)["status"] == "cancelled"
        assert child.run_started == 0
        assert task_id not in manager._tasks
        assert all(status != "ACTIVE" for status, _summary in progress)
        assert (
            todo_owner.snapshot(
                scope_kind=ModelInputScopeKind.SUBAGENT_TASK,
                scope_subagent_task_id=task_id,
            )
            is None
        )
        durable = repository.query_subagent_task(
            session_id=session_id,
            task_id=task_id,
            deadline_monotonic=monotonic() + 30,
        )
        assert durable is not None
        assert durable["status"] == "CANCELLED"
        with provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            deadline_monotonic=monotonic() + 30,
        ) as connection:
            child_turn = connection.execute(
                "SELECT status, terminal_reason FROM pulsara_v3.turns "
                "WHERE session_id=%s AND scope_subagent_task_id=%s",
                (session_id, task_id),
            ).fetchall()
        assert child_turn == [("INTERRUPTED", "USER_STOPPED")]
        await manager.aclose(deadline_monotonic=monotonic() + 2)

    asyncio.run(stop_wins_before_launch_claim())
    asyncio.run(launch_wins_before_stop_claim())


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
            **_manager_launch_kwargs(repository, lease.guard),
            repository=repository,
            guard=lease.guard,
            host_owner_id=_id("host"),
            io_owner=KernelSessionIO(),
            live_bus=LiveAgentEventBus(),
            todo_owner=TodoRunStateOwner(
                session_id=session_id, owner_epoch=_id("todo")
            ),
        )
        manager.bind_runner_factory(
            lambda _scope: _BlockingChildRunner()  # type: ignore[arg-type]
        )
        projection_reader = CanonicalProtocolReader(provider)
        before = projection_reader.snapshot(
            session_id=session_id, maximum_entries=64, maximum_control_items=128,
            deadline_monotonic=monotonic() + 30,
        )

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
        observation = projection_reader.observe_committed(
            session_id=session_id, after_event_sequence=before.event_sequence_cut,
            maximum_events=128, maximum_bytes=2 << 20,
            deadline_monotonic=monotonic() + 30,
        )
        assert {item.affected_subagent_task_id for item in observation.projections if item.affected_subagent_task_id} == {
            str(row["id"]) for row in rows
        }
        assert observation.through_event_sequence == max(item.event_sequence for item in observation.projections)
        after = projection_reader.snapshot(
            session_id=session_id, maximum_entries=64, maximum_control_items=128,
            deadline_monotonic=monotonic() + 30,
        )
        assert after.control.task_total_count == 2
        assert after.control.task_unreceived_count == 2
        parent_binding = repository.read_turn_model_call_binding(
            lease.guard, turn_id=context.turn_id, deadline_monotonic=monotonic() + 30,
        )
        assert all(row["model_call_binding"]["connection_id"] == parent_binding.connection_id.value for row in rows)
        assert all(row["model_target_fact"]["model_id"] for row in rows)
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
                    "task_ids": [str(rows[0]["id"])],
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
        await manager.aclose(deadline_monotonic=monotonic() + 2)

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
            **_manager_launch_kwargs(repository, mismatch_lease.guard),
            repository=repository,
            guard=mismatch_lease.guard,
            host_owner_id=_id("host"),
            io_owner=KernelSessionIO(),
            live_bus=LiveAgentEventBus(),
            todo_owner=TodoRunStateOwner(
                session_id=mismatch_session, owner_epoch=_id("todo")
            ),
        )
        mismatch_manager.bind_runner_factory(
            lambda _scope: _BlockingChildRunner()  # type: ignore[arg-type]
        )
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
        await mismatch_manager.aclose(deadline_monotonic=monotonic() + 2)

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

        invalid_session = _id("session")
        invalid_args = {"tasks": [
            {"task_key": "valid", "task": "would be valid", "depends_on": []},
            {"task_key": "invalid", "task": "must abort the batch", "depends_on": [],
             "model": {"connection_id": "model-connection:unknown"}},
        ]}
        invalid_lease, invalid_context = _prepare_root_tool_attempt(
            repository, session_id=invalid_session, workspace_id=_id("workspace"),
            tool_name="create_agent_tasks", arguments=invalid_args,
        )
        invalid_manager = KernelSubagentManager(
            **_manager_launch_kwargs(repository, invalid_lease.guard),
            repository=repository, guard=invalid_lease.guard,
            host_owner_id=_id("host"), io_owner=KernelSessionIO(),
            live_bus=LiveAgentEventBus(),
            todo_owner=TodoRunStateOwner(session_id=invalid_session, owner_epoch=_id("todo")),
        )
        rejected = await invalid_manager.invoke(
            tool_name="create_agent_tasks", arguments=invalid_args,
            invocation_context=invalid_context,
        )
        assert rejected.state == "INVALID_ARGUMENTS"
        assert repository.list_subagent_tasks(
            session_id=invalid_session, maximum_items=50,
            deadline_monotonic=monotonic() + 30,
        ) == ()
        await invalid_manager.aclose(deadline_monotonic=monotonic() + 2)

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
                    "task_key": "A",
                    "task": "alpha-only",
                    "depends_on": [],
                },
                {
                    "task_key": "a",
                    "task": "bravo-only",
                    "depends_on": ["A"],
                },
                {
                    "task_key": "C",
                    "task": "charlie-only",
                    "depends_on": ["a"],
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
            **_manager_launch_kwargs(repository, lease.guard),
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
            lambda _scope: _CompletingChildRunner(
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
        await manager.open_root_completion_delivery(context.turn_id)
        waited = await manager.invoke(
            tool_name="wait_agent",
            arguments={
                "task_ids": task_ids,
                "settle": "all",
                "timeout_seconds": 10,
            },
            invocation_context=context,
        )
        payload = json.loads(waited.content)
        assert payload["pending_task_ids"] == []
        assert payload["outcome"] == "predicate_satisfied"
        assert payload["satisfied_task_ids"] == task_ids
        assert "settled" not in payload
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
        assert {row["objective"]: row["task_key"] for row in rows} == {
            "alpha-only": "A", "bravo-only": "a", "charlie-only": "C",
        }
        assert all(row["status"] == "COMPLETED" for row in rows)
        await manager.aclose(deadline_monotonic=monotonic() + 2)
        from tests.test_conversation_fork import assert_session_aggregate_deleted
        assert_session_aggregate_deleted(repository, lease.guard)

    asyncio.run(exercise())


@pytest.mark.postgres
def test_round10_host_capacity_grandfathers_and_refills_one_ordered_queue(
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
                for index in range(8)
            ]
        }
        lease, context = _prepare_root_tool_attempt(
            repository,
            session_id=session_id,
            workspace_id=workspace_id,
            tool_name="create_agent_tasks",
            arguments=arguments,
        )
        todo_owner = TodoRunStateOwner(
            session_id=session_id,
            owner_epoch=_id("todo"),
        )
        manager = KernelSubagentManager(
            **_manager_launch_kwargs(repository, lease.guard),
            repository=repository,
            guard=lease.guard,
            host_owner_id=_id("host"),
            io_owner=KernelSessionIO(),
            live_bus=LiveAgentEventBus(),
            todo_owner=todo_owner,
        )
        blocker = _TodoAwareCountingBlockingChildRunner(
            repository,
            lease,
            todo_owner,
        )
        manager.bind_runner_factory(lambda _scope: blocker)  # type: ignore[arg-type]
        await manager.open_root_completion_delivery(context.turn_id)
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
            "ACTIVE", "ACTIVE", "ACTIVE", "ACTIVE",
            "PENDING_START", "PENDING_START", "PENDING_START", "PENDING_START",
        ]

        assert await _set_capacity(manager, 2) == (2, 4)

        stopped = await manager.invoke(
            tool_name="stop_agent",
            arguments={"task_id": task_ids[0]},
            invocation_context=context,
        )
        assert json.loads(stopped.content)["status"] == "cancelled"
        assert await manager.capacity_state() == (2, 3)
        assert blocker.started == task_ids[:4]
        stopped = await manager.invoke(
            tool_name="stop_agent",
            arguments={"task_id": task_ids[1]},
            invocation_context=context,
        )
        assert json.loads(stopped.content)["status"] == "cancelled"
        assert await manager.capacity_state() == (2, 2)
        assert blocker.started == task_ids[:4]

        assert await _set_capacity(manager, 6) == (6, 6)
        for _ in range(20):
            if len(blocker.started) == 8:
                break
            blocker.changed.clear()
            await asyncio.wait_for(blocker.changed.wait(), timeout=1)
        assert blocker.started == task_ids
        assert await manager.capacity_state() == (6, 6)
        assert await _set_capacity(manager, 6) == (6, 6)
        assert blocker.started == task_ids
        await manager.aclose(deadline_monotonic=monotonic() + 2)

    asyncio.run(exercise())


@pytest.mark.postgres
def test_round10_postcommit_child_activation_failure_settles_task_and_turn(
    stage2_migrated_postgres_database,
    monkeypatch,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)

    async def exercise() -> None:
        session_id = _id("session")
        arguments = {"task": "fail after the child turn commits"}
        lease, context = _prepare_root_tool_attempt(
            repository,
            session_id=session_id,
            workspace_id=_id("workspace"),
            tool_name="spawn_agent",
            arguments=arguments,
        )
        todo_owner = TodoRunStateOwner(
            session_id=session_id,
            owner_epoch=_id("todo"),
        )
        def reject_postcommit_activation(_prepared):
            raise RuntimeError("simulated TODO activation failure after child commit")
        monkeypatch.setattr(todo_owner, "activate_child_run", reject_postcommit_activation)
        manager = KernelSubagentManager(
            **_manager_launch_kwargs(repository, lease.guard),
            repository=repository,
            guard=lease.guard,
            host_owner_id=_id("host"),
            io_owner=KernelSessionIO(),
            live_bus=LiveAgentEventBus(),
            todo_owner=todo_owner,
        )
        blocker = _TodoAwareCountingBlockingChildRunner(
            repository,
            lease,
            todo_owner,
        )
        manager.bind_runner_factory(lambda _scope: blocker)  # type: ignore[arg-type]

        spawned = await manager.invoke(
            tool_name="spawn_agent",
            arguments=arguments,
            invocation_context=context,
        )
        payload = json.loads(spawned.content)
        task_id = payload["task_id"]
        assert payload["status"] == "failed"
        assert blocker.started == []

        durable = repository.query_subagent_task(
            session_id=session_id,
            task_id=task_id,
            deadline_monotonic=monotonic() + 30,
        )
        assert durable is not None
        assert durable["status"] == "FAILED"
        assert durable["terminal_reason"] == "CHILD_START_FAILED"
        assert durable["terminal_public_detail"]
        child_turn = repository.read_turn_terminal_outcome(
            session_id=session_id,
            turn_id=stable_subagent_turn_id(
                session_id=session_id,
                task_id=task_id,
            ),
            deadline_monotonic=monotonic() + 30,
        )
        assert child_turn is not None
        assert child_turn["status"] == "INTERRUPTED"
        assert child_turn["terminal_reason"] == "CHILD_START_FAILED"

        stopped = await manager.invoke(
            tool_name="stop_agent",
            arguments={"task_id": task_id},
            invocation_context=context,
        )
        assert json.loads(stopped.content)["status"] == "failed"
        await manager.aclose(deadline_monotonic=monotonic() + 2)

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
            **_manager_launch_kwargs(repository, lease.guard),
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
        manager.bind_runner_factory(lambda _scope: blocker)  # type: ignore[arg-type]
        await manager.open_root_completion_delivery(context.turn_id)
        created = await manager.invoke(
            tool_name="create_agent_tasks",
            arguments=arguments,
            invocation_context=context,
        )
        task_ids = [item["task_id"] for item in json.loads(created.content)["tasks"]]
        wait = asyncio.create_task(
            manager.invoke(
                tool_name="wait_agent",
                arguments={"task_ids": [task_ids[1]], "timeout_seconds": 10},
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
        assert payload == {
            "outcome": "predicate_satisfied",
            "satisfied_task_ids": [task_ids[1]],
            "pending_task_ids": [],
        }
        assert blocker.started == [task_ids[0]]
        assert frontier_ack_lost
        assert task_ids[1] not in manager._start_materials
        assert task_ids[1] not in manager._mailboxes
        assert task_ids[1] not in manager._mailbox_ordinals
        assert task_ids[1] not in manager._completing
        await manager.aclose(deadline_monotonic=monotonic() + 2)

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
            **_manager_launch_kwargs(repository, lease.guard),
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
        manager.bind_runner_factory(lambda _scope: child)  # type: ignore[arg-type]
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

        observation = CanonicalProtocolReader(provider).observe_committed(
            session_id=session_id,
            after_event_sequence=0,
            maximum_events=128,
            maximum_bytes=2 << 20,
            deadline_monotonic=monotonic() + 30,
        )
        assert observation.gap_reason is None
        mailbox_events = [
            item for item in observation.projections
            if item.HasField("entry") and item.entry.entry_id in {
                _round10_id("inter-agent-entry", first_attempt, task_id),
                _round10_id("inter-agent-entry", second_attempt, task_id),
            }
        ]
        assert len(mailbox_events) == 2
        assert all(item.affected_subagent_task_id == task_id for item in mailbox_events)

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
            json.loads(provider_input_item_text(item))["pulsara_inter_agent_message"][
                "content"
            ]
            for item in messages
        ] == [
            "first exact message",
            "second exact message",
        ]
        assert all(
            json.loads(provider_input_item_text(item))["pulsara_inter_agent_message"][
                "sender"
            ]
            == {"kind": "ROOT"}
            for item in messages
        )
        assert all(
            json.loads(provider_input_item_text(item))["pulsara_inter_agent_message"][
                "content_semantics"
            ]
            == "ADVISORY_COLLABORATION_DATA"
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
        await manager.aclose(deadline_monotonic=monotonic() + 2)

    asyncio.run(exercise())


def test_scheduler_waiter_cancellation_joins_shared_launch_before_propagating() -> None:
    async def exercise() -> None:
        manager = object.__new__(KernelSubagentManager)
        manager._lock = asyncio.Lock()
        manager._closed = False
        manager._scheduler_task = None
        manager._scheduler_reschedule = False
        manager._guard = SimpleNamespace(session_id="scheduler-cancel")
        entered, finish = asyncio.Event(), asyncio.Event()
        settled: list[bool] = []

        async def scheduler() -> None:
            entered.set()
            await finish.wait()
            settled.append(True)

        manager._start_available_tasks_worker = scheduler
        waiter = asyncio.create_task(manager._start_available_tasks())
        await entered.wait()
        worker = manager._scheduler_task
        waiter.cancel()
        await asyncio.sleep(0)
        waiter.cancel()
        await asyncio.sleep(0)
        assert not waiter.done()
        assert worker is not None and not worker.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert settled == [True]
        assert manager._scheduler_task is None

    asyncio.run(exercise())


@pytest.mark.postgres
@pytest.mark.parametrize("compact_middle", [False, True, "image"])
def test_worker_history_three_generations_survive_reload_and_gc(
    stage2_migrated_postgres_database,
    compact_middle,
) -> None:
    from pulsara_agent.conversation_kernel.blob import PostgresCanonicalBlobStore
    from tests.test_conversation_fork import compact, rows as read_rows

    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)

    async def exercise():
        session_id, workspace_id = _id("session"), _id("workspace")
        attempts = [_id("attempt") for _ in range(3)]
        task_ids = [_round10_id("subagent-task", attempt, "0") for attempt in attempts]
        arguments = [
            {"task": "ancestor-alpha"},
            {
                "task": "middle-bravo",
                "context": {"mode": "worker_history", "task_id": task_ids[0]},
            },
            {
                "task": "latest-charlie",
                "context": {"mode": "worker_history", "task_id": task_ids[1]},
            },
        ]
        lease, contexts = _prepare_root_tool_batch(
            repository,
            session_id=session_id,
            workspace_id=workspace_id,
            calls=tuple(
                ("spawn_agent", _id("call"), attempt, args)
                for attempt, args in zip(attempts, arguments, strict=True)
            ),
        )
        manager = KernelSubagentManager(
            **_manager_launch_kwargs(repository, lease.guard),
            repository=repository,
            guard=lease.guard,
            host_owner_id=_id("host"),
            io_owner=KernelSessionIO(),
            live_bus=LiveAgentEventBus(),
            todo_owner=TodoRunStateOwner(
                session_id=session_id, owner_epoch=_id("todo")
            ),
        )
        sources, started = {}, []
        manager.bind_runner_factory(
            lambda _scope: _CompletingChildRunner(
                repository=repository,
                lease=lease,
                manager=manager,
                started=started,
                source_bodies=sources,
            )
        )
        await manager.open_root_completion_delivery(contexts[0].turn_id)
        for index, (args, ctx) in enumerate(zip(arguments, contexts, strict=True)):
            created = await manager.invoke(
                tool_name="spawn_agent", arguments=args, invocation_context=ctx
            )
            assert created.state == "SUCCESS", created.content
            assert json.loads(created.content)["task_id"] == task_ids[index]
            waited = await manager.invoke(
                tool_name="wait_agent",
                arguments={
                    "task_ids": [task_ids[index]],
                    "settle": "all",
                    "timeout_seconds": 10,
                },
                invocation_context=ctx,
            )
            assert json.loads(waited.content)["pending_task_ids"] == []
            if index == 1 and compact_middle:
                turn = read_rows(
                    repository,
                    "SELECT * FROM pulsara_v3.turns WHERE session_id=%s AND scope_subagent_task_id=%s",
                    (session_id, task_ids[index]),
                )[0]
                boundary = read_rows(
                    repository,
                    "SELECT max(entry_sequence) AS n FROM pulsara_v3.transcript_entries WHERE session_id=%s AND scope_subagent_task_id=%s",
                    (session_id, task_ids[index]),
                )[0]["n"]
                retained = ()
                if compact_middle == "image":
                    from io import BytesIO
                    from PIL import Image
                    from pulsara_agent.llm.input import LLMImagePart, LLMTextPart
                    from pulsara_agent.model_input.contracts import (
                        FrozenRetainedHistoricalRequest,
                    )

                    image_bytes = BytesIO()
                    Image.new("RGB", (4, 4), (12, 34, 56)).save(image_bytes, "PNG")
                    retained = (
                        FrozenRetainedHistoricalRequest(
                            FrozenProviderInputItemKind.USER,
                            CanonicalInputOriginKind.HUMAN_MESSAGE,
                            FrozenPromptContent(
                                (
                                    LLMTextPart("retained illustration"),
                                    LLMImagePart(
                                        "image/png", image_bytes.getvalue(), 4, 4
                                    ),
                                )
                            ),
                        ),
                    )
                compact(
                    repository,
                    lease.guard,
                    turn["id"],
                    boundary,
                    summary="ancestor-alpha and middle-bravo public findings",
                    idle=True,
                    retained_historical_requests=retained,
                )
        assert any("ancestor-alpha" in text for text in sources["latest-charlie"])
        assert any("middle-bravo" in text for text in sources["latest-charlie"])
        await manager.aclose(deadline_monotonic=monotonic() + 5)
        # Reload only canonical owners, then run the real orphan collector.
        reloaded = ConversationKernelRepository(provider)
        PostgresCanonicalBlobStore(provider).delete_orphans(
            grace_seconds=1, deadline_monotonic=monotonic() + 30
        )
        turn = read_rows(
            reloaded,
            "SELECT * FROM pulsara_v3.turns WHERE session_id=%s AND scope_subagent_task_id=%s",
            (session_id, task_ids[2]),
        )[0]
        boundary = read_rows(
            reloaded,
            "SELECT max(entry_sequence) AS n FROM pulsara_v3.transcript_entries WHERE session_id=%s AND scope_subagent_task_id=%s",
            (session_id, task_ids[2]),
        )[0]["n"]
        cut = reloaded.prepare_compaction_input_cut(
            lease.guard,
            turn_id=turn["id"],
            allow_terminal=True,
            deadline_monotonic=monotonic() + 30,
        )
        frozen = CanonicalProviderInputReader(provider).read_frozen_compaction_cut(
            cut, deadline_monotonic=monotonic() + 30
        )
        canonical = frozen.dispatch_read.compile_snapshot.canonical_input
        objectives = [
            provider_input_item_text(i)
            for i in canonical.items
            if i.item_kind is FrozenProviderInputItemKind.USER
            and i.input_origin is CanonicalInputOriginKind.SUBAGENT_OBJECTIVE
        ]
        assert objectives == (
            ["latest-charlie"]
            if compact_middle
            else ["ancestor-alpha", "middle-bravo", "latest-charlie"]
        )
        assert sum(
            i.item_kind is FrozenProviderInputItemKind.CONTEXT_SNAPSHOT
            for i in canonical.items
        ) == bool(compact_middle)
        if compact_middle:
            assert (
                frozen.snapshot_carrier.earlier_context_summary
                == "ancestor-alpha and middle-bravo public findings"
            )
        if compact_middle == "image":
            from pulsara_agent.conversation_kernel.compaction.prompt import (
                compaction_snapshot_image_parts,
            )

            assert len(compaction_snapshot_image_parts(frozen.snapshot_carrier)) == 1

    asyncio.run(exercise())


@pytest.mark.postgres
@pytest.mark.parametrize('hold_after_reservation', [False, True])
def test_capacity_lowering_during_query_or_reserved_launch(
    stage2_migrated_postgres_database, monkeypatch, hold_after_reservation,
):
    from threading import Event
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    async def exercise():
        args = {'tasks':[{'task':f'capacity race {index}'} for index in range(4)]}
        session_id = _id('session')
        lease,ctx = _prepare_root_tool_attempt(repository,session_id=session_id,workspace_id=_id('workspace'),tool_name='create_agent_tasks',arguments=args)
        kwargs = _manager_launch_kwargs(repository, lease.guard)
        launch = _BlockingLaunchPreparation(kwargs['launch_preparation'])
        if hold_after_reservation:
            kwargs['launch_preparation'] = launch
        manager=KernelSubagentManager(**kwargs,repository=repository,guard=lease.guard,host_owner_id=_id('host'),io_owner=KernelSessionIO(),live_bus=LiveAgentEventBus(),todo_owner=TodoRunStateOwner(session_id=session_id,owner_epoch=_id('todo')))
        child = _CanonicalBlockingChildRunner(repository, lease)
        manager.bind_runner_factory(lambda scope:child)
        queried, release = Event(), Event()
        original = repository.list_runnable_subagent_tasks
        def query(*args,**kwargs):
            rows = original(*args,**kwargs)
            if not queried.is_set():
                queried.set()
                assert release.wait(10)
            return rows
        if not hold_after_reservation:
            monkeypatch.setattr(repository, 'list_runnable_subagent_tasks', query)
        creating=asyncio.create_task(manager.invoke(tool_name='create_agent_tasks', arguments=args, invocation_context=ctx))
        if hold_after_reservation:
            await asyncio.wait_for(launch.entered.wait(),5)
            assert await manager.capacity_state() == (4,1)
        else:
            assert await asyncio.to_thread(queried.wait,5)
            assert await manager.capacity_state() == (4,0)
        await _set_capacity(manager, 1)
        release.set()
        launch.release.set()
        result = await asyncio.wait_for(creating,5)
        assert result.state == 'SUCCESS'
        assert await manager.capacity_state() == (1,1)
        rows=repository.list_subagent_tasks(session_id=session_id,maximum_items=50,deadline_monotonic=monotonic()+30)
        assert [row['status'] for row in rows] == ['ACTIVE','PENDING_START','PENDING_START','PENDING_START']
        await manager.aclose(deadline_monotonic=monotonic()+5)
    asyncio.run(exercise())


def test_capacity_raise_while_scheduler_exits_is_not_lost():
    async def exercise():
        manager=object.__new__(KernelSubagentManager)
        manager._lock=asyncio.Lock()
        manager._state_changed=asyncio.Condition(manager._lock)
        manager._state_revision=0
        manager._capacity_target=1
        manager._tasks={}
        manager._launch_permits={}
        manager._closed=False
        manager._scheduler_task=None
        manager._scheduler_reschedule=False
        manager._guard=SimpleNamespace(session_id='exit-race')
        exiting, release=asyncio.Event(),asyncio.Event()
        checks=[]
        async def worker():
            checks.append(manager._capacity_target)
            if len(checks)==1:
                exiting.set()
                await release.wait()
        manager._start_available_tasks_worker=worker
        first=asyncio.create_task(manager.fill_available_capacity())
        await exiting.wait()
        raising=asyncio.create_task(_set_capacity(manager, 6))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first,raising)
        assert checks[0] == 1
        assert checks[1:] and all(target==6 for target in checks[1:])
        assert manager._scheduler_task is None
    asyncio.run(exercise())


@pytest.mark.postgres
def test_worker_controls_beyond_128_remain_queryable_and_cancellable(stage2_migrated_postgres_database):
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)

    async def exercise():
        session_id, workspace_id = _id('session'), _id('workspace')
        calls = tuple(('create_agent_tasks', _id('call'), _id('attempt'), {
            'tasks': [{'task': f'worker {batch * 16 + i}'} for i in range(16)]
        }) for batch in range(9))
        lease, contexts = _prepare_root_tool_batch(repository, session_id=session_id, workspace_id=workspace_id, calls=calls)
        io = KernelSessionIO()
        manager = KernelSubagentManager(**_manager_launch_kwargs(repository, lease.guard),
            repository=repository, guard=lease.guard, host_owner_id=_id('host'),
            io_owner=io, live_bus=LiveAgentEventBus(),
            todo_owner=TodoRunStateOwner(session_id=session_id, owner_epoch=_id('todo')))
        attempts = {}

        class Worker(_CanonicalBlockingChildRunner):
            async def admit_subagent_turn(self, *, launch, cancellation_intent):
                result = await super().admit_subagent_turn(launch=launch, cancellation_intent=cancellation_intent)
                task_id, turn_id = launch.task_start.task_id, cancellation_intent.turn_id
                entry, call_id, attempt = _id('entry'), _id('call'), _id('attempt')
                cut = repository.prepare_provider_input_cut(lease.guard, turn_id=turn_id, deadline_monotonic=monotonic()+30)
                repository.commit_assistant_message(lease.guard, cut=cut, entry_id=entry,
                    parent_content=InlineContent.from_bytes(b'working'),
                    blocks=(AssistantToolCallBlock(_id('block'), call_id, 'terminal', freeze_json({'command':'work'})),),
                    occurred_at=datetime.now(timezone.utc), actor_id=task_id, deadline_monotonic=monotonic()+30)
                repository.accept_tool_attempt(lease.guard, attempt_id=attempt, assistant_entry_id=entry,
                    tool_call_id=call_id, authorization_kind='policy', authorization_reference='allow',
                    actor_kind='runtime', actor_id='executor', remote_idempotency_key=None, retry_of_attempt_id=None,
                    permission_snapshot_fingerprint=_permission_fingerprint(repository, session_id=session_id, turn_id=turn_id),
                    occurred_at=datetime.now(timezone.utc), deadline_monotonic=monotonic()+30)
                attempts[task_id] = (turn_id, attempt)
                return result

        manager.bind_runner_factory(lambda _scope: Worker(repository, lease))
        await _set_capacity(manager, 144)
        try:
            for call, context in zip(calls, contexts, strict=True):
                # This direct-manager fixture has no Host renewal loop. Keep
                # its existing physical lease alive across the 144 workers,
                # just as the production Host does, without a lifetime cap.
                await io.run(repository.renew_host_writer, lease.guard,
                    lease_seconds=30, deadline_monotonic=monotonic()+30)
                response = await manager.invoke(tool_name=call[0], arguments=call[3], invocation_context=context)
                assert response.state == 'SUCCESS'
            assert len(attempts) == 144
            snapshot = CanonicalProtocolReader(provider).snapshot(session_id=session_id,
                maximum_entries=64, maximum_control_items=128, deadline_monotonic=monotonic()+30)
            assert snapshot.control.active_turn_total_count == 145
            assert len(snapshot.control.active_turns) == 128
            assert snapshot.control.tool_attempt_total_count == 153
            assert len(snapshot.control.tool_attempts) == 128
            visible = {item.scope_subagent_task_id for item in snapshot.control.active_turns}
            task_id = next(task for task in attempts if task not in visible)
            entries, blocks, results, has_more, inherited_context = repository.list_subagent_task_activities(
                session_id=session_id, task_id=task_id, maximum_items=50, after_entry_sequence=0, deadline_monotonic=monotonic()+30)
            assert not has_more and not results
            assert {row['turn_id'] for row in entries} == {attempts[task_id][0]}
            assert all(row['turn_status'] == 'RUNNING' for row in entries)
            assert [row['attempt_id'] for row in blocks] == [attempts[task_id][1]]
            stopped = await manager._stop({'task_id': task_id})
            assert stopped.state == 'SUCCESS'
            row = repository.query_subagent_task(session_id=session_id, task_id=task_id, deadline_monotonic=monotonic()+30)
            assert row['status'] == 'CANCELLED'
        finally:
            await manager.aclose(deadline_monotonic=monotonic()+30)
            await io.aclose(deadline_monotonic=monotonic()+5)
    asyncio.run(exercise())


@pytest.mark.postgres
def test_host_takeover_interrupts_active_queued_and_dependency_waiting_workers(
    stage2_migrated_postgres_database,
):
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)

    async def exercise():
        session_id, workspace_id = _id('session'), _id('workspace')
        arguments = {'tasks': [
            {'task_key': 'first', 'task': 'active review'},
            {'task_key': 'second', 'task': 'queued review'},
            {'task_key': 'summary', 'task': 'synthesis', 'depends_on': ['first', 'second']},
        ]}
        lease, context = _prepare_root_tool_attempt(
            repository, session_id=session_id, workspace_id=workspace_id,
            tool_name='create_agent_tasks', arguments=arguments,
        )
        manager = KernelSubagentManager(
            **_manager_launch_kwargs(repository, lease.guard), repository=repository,
            guard=lease.guard, host_owner_id=_id('host'), io_owner=KernelSessionIO(),
            live_bus=LiveAgentEventBus(),
            todo_owner=TodoRunStateOwner(session_id=session_id, owner_epoch=_id('todo')),
        )
        child = _CanonicalBlockingChildRunner(repository, lease)
        manager.bind_runner_factory(lambda scope: child)
        await _set_capacity(manager, 1)
        await manager.invoke(tool_name='create_agent_tasks', arguments=arguments, invocation_context=context)
        def rows():
            return repository.list_subagent_tasks(session_id=session_id, maximum_items=50, deadline_monotonic=monotonic()+10)
        assert [row['status'] for row in rows()] == ['ACTIVE', 'PENDING_START', 'WAITING_DEPENDENCY']
        await asyncio.wait_for(child.started.wait(), 1)
        repository.acquire_host_writer(
            intent='EXISTING', session_id=session_id, workspace_id=workspace_id,
            writer_owner_id=_id('host'), lease_seconds=30, deadline_monotonic=monotonic()+10,
        )
        after = rows()
        assert [row['status'] for row in after] == ['INTERRUPTED'] * 3
        assert all(row['pending_reason'] is None and row['terminal_reason'] == 'HOST_TAKEOVER' for row in after)
        await manager.aclose(deadline_monotonic=monotonic()+5)

    asyncio.run(exercise())
