"""Host-scoped, same-session Stage 2 subagent execution.

The manager owns only live asyncio tasks.  Accepted task coordination and the
task-scoped conversation are canonical PostgreSQL facts; no execution attempt,
lease, checkpoint, or resume carrier is durable.
"""

from __future__ import annotations

import asyncio
import base64
from collections import deque
from dataclasses import dataclass, field as dataclass_field, replace
from datetime import datetime, timezone
import hmac
import json
from hashlib import sha256
import math
from pathlib import Path
import re
import secrets
from time import monotonic
from typing import Callable, Mapping

from psycopg import InterfaceError, OperationalError

from pulsara_agent.conversation_kernel.contracts import HostWriterGuard
from pulsara_agent.conversation_kernel.cancellation import (
    ActiveTurnCancellationIntent,
    ForegroundCancellationCause,
    stable_subagent_turn_id,
)
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.execution_watchdogs import (
    KernelExecutionDeadlineFactory,
    KernelWatchdogOwner,
)
from pulsara_agent.conversation_kernel.live import (
    LiveAgentEventBus,
    LiveBlockKind,
    LiveChannelKind,
)
from pulsara_agent.ports.live_agent_event import (
    SubagentProgressPayload,
    live_digest,
)
from pulsara_agent.conversation_kernel.vocabulary import LiveEventType
from pulsara_agent.conversation_kernel.todo_runtime import (
    FrozenTodoCloseProjection,
    TodoRunStateOwner,
)
from pulsara_agent.conversation_kernel.repository import (
    AcceptedEntry,
    ConversationKernelConflict,
    ConversationKernelRepository,
    StaleHostWriter,
    TurnAdmissionConfirmationKind,
)
from pulsara_agent.model_input.contracts import (
    ContextSourceAbsentFact,
    ContextSourceCandidate,
    ContextSourceKind,
    ModelInputScopeKind,
)
from pulsara_agent.conversation_kernel.runner import (
    ChildCompactionContinuationBlocked,
    ConversationKernelRunner,
    KernelRunResult,
)
from pulsara_agent.conversation_kernel.turn_admission import (
    SubagentTurnAdmissionPostCommitError,
)
from pulsara_agent.conversation_kernel.tool_contracts import (
    KernelToolInvocationContext,
    KernelToolResult,
)
from pulsara_agent.conversation_kernel.cold_epoch import (
    SubagentInitialSeed,
    build_subagent_initial_seed,
)
from pulsara_agent.conversation_kernel.context_sources import (
    build_subagent_context_source,
)
from pulsara_agent.conversation_kernel.compaction.runtime_handoff import (
    FrozenRootSubagentTaskBoardHandoffFact,
    bounded_handoff_preview,
    freeze_subagent_task_board_fact,
)
from pulsara_agent.model_input.provider_replay import (
    FrozenCanonicalProviderDispatchRead,
)
from pulsara_agent.primitives.context import canonical_json_bytes
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.run_permission import FrozenRunPermissionSnapshot
from pulsara_agent.conversation_kernel.subagents.contracts import (
    FrozenDependencyResultContext,
    FrozenSubagentParentContextCallSubject,
    FrozenSubagentParentContextSelection,
    MAXIMUM_INTER_AGENT_MESSAGE_UTF8_BYTES,
    MAXIMUM_RESULT_DIAGNOSTICS_ITEMS,
    MAXIMUM_RESULT_DIAGNOSTICS_UTF8_BYTES,
    MAXIMUM_RESULT_OUTPUT_PREVIEW_UTF8_BYTES,
    MAXIMUM_RESULT_SUMMARY_UTF8_BYTES,
    MAXIMUM_TASK_OBJECTIVE_UTF8_BYTES,
    PreparedInterAgentMailboxBatch,
    PreparedInterAgentMailboxItem,
    PreparedSubagentTaskBatchAdmission,
    PreparedSubagentTaskDraft,
    PreparedSubagentLaunch,
    SubagentBatchConfirmationKind,
    SubagentContextMode,
    SubagentProfileKind,
    SubagentResultSource,
    SubagentTaskTerminalConfirmationKind,
    SubagentTaskStatus,
    bounded_terminal_public_detail,
    build_default_terminal_public_detail,
    build_dependency_result_context,
    derive_subagent_batch_initial_dispositions,
    build_inter_agent_mailbox_batch,
    build_parent_context_selection,
    build_subagent_task_terminal_settlement,
    build_subagent_task_start,
    build_subagent_result_public_fact,
    dependency_result_context_identity_digest,
    parent_context_call_subject_identity_digest,
    parent_context_selection_identity_digest,
    parent_context_source_identity_digest,
)
from pulsara_agent.conversation_kernel.subagents.launch import (
    SubagentLaunchPreparationPort,
)
from pulsara_agent.conversation_kernel.subagents.runtime_port import (
    PreparedExplicitSubagentCompletion,
    PreparedInferredSubagentCompletion,
)
from pulsara_agent.hooks.context import (
    HookContextOwner,
    PendingHookContextReservation,
)
from pulsara_agent.hooks.contracts import (
    ContinuationDecision,
    ContinuationOutcome,
    ContextOutcome,
    HookDispatchEnvelope,
    HookDispatchScopeRef,
    HookContextEntry,
    HookScopeKind,
    SubagentStartInput,
    SubagentStartRef,
    SubagentStopInput,
    SubagentStopRef,
    external_permission_mode,
)
from pulsara_agent.hooks.dispatcher import KernelHookDispatcher
from pulsara_agent.hooks.matcher import event_matcher_subject


SUBAGENT_TOOL_NAMES = frozenset(
    {
        "spawn_agent",
        "create_agent_tasks",
        "list_agents",
        "wait_agent",
        "send_agent_message",
        "stop_agent",
        "report_agent_result",
    }
)
ROOT_ORCHESTRATION_TOOL_NAMES = SUBAGENT_TOOL_NAMES - {"report_agent_result"}
MAXIMUM_LIVE_SUBAGENTS = 4
MAXIMUM_MAILBOX_ITEMS = 16
MAXIMUM_MAILBOX_UTF8_BYTES = 65_536
MAXIMUM_PARENT_CONTEXT_SOURCE_UTF8_BYTES = 1 << 20
# Repository dependency hydration is deliberately paged.  This is a local
# query/RSS bound, not a cap on the session task inventory returned by list.
_DEPENDENCY_READ_PAGE_ITEMS = 32
_MAXIMUM_LIST_CURSOR_BYTES = 512
_RETRYABLE_CANONICAL_ERRORS = (
    TimeoutError,
    ConnectionError,
    InterfaceError,
    OperationalError,
)


@dataclass(slots=True)
class _LiveTask:
    task_id: str
    parent_turn_id: str
    task: asyncio.Task[KernelRunResult]
    cancellation_intent: ActiveTurnCancellationIntent
    launch: PreparedSubagentLaunch
    hook_scope: HookDispatchScopeRef | None
    status: str = "ACTIVE"
    cancellation_reason: str | None = None
    completion_continuation_used: bool = False


@dataclass(frozen=True, slots=True)
class _TaskStartMaterial:
    task_id: str
    parent_turn_id: str
    objective: str
    profile: SubagentProfileKind
    parent_call_subject: FrozenSubagentParentContextCallSubject
    context: FrozenSubagentParentContextSelection
    dependency_task_ids: tuple[str, ...]
    dependency_context: FrozenDependencyResultContext | None = None


@dataclass(slots=True)
class _SubagentLaunchPermit:
    material: _TaskStartMaterial
    cancellation_signal: asyncio.Event = dataclass_field(default_factory=asyncio.Event)
    settled: asyncio.Event = dataclass_field(default_factory=asyncio.Event)
    stop_claimed: bool = False
    launching: bool = False
    start_committed: bool = False
    cancellation_reason: str | None = None


@dataclass(frozen=True, slots=True)
class _CompletionPermit:
    task_id: str
    continuation_requested: bool
    causal_ref: SubagentStopRef
    continuation_source: HookContextEntry | None = dataclass_field(
        default=None, repr=False
    )
    continuation_reason: str | None = None


@dataclass(frozen=True, slots=True)
class _SubagentListCursor:
    after_accepted_at: datetime
    after_task_id: str
    seen_count: int


@dataclass(slots=True)
class _BatchAdmissionAttempt:
    candidate: PreparedSubagentTaskBatchAdmission
    task: asyncio.Task[KernelToolResult]


class KernelSubagentManager:
    def __init__(
        self,
        *,
        repository: ConversationKernelRepository,
        guard: HostWriterGuard,
        host_owner_id: str,
        io_owner: KernelSessionIO,
        live_bus: LiveAgentEventBus,
        todo_owner: TodoRunStateOwner,
        launch_preparation: SubagentLaunchPreparationPort,
        terminal_cwd: Callable[[], Path],
        todo_close_projector: Callable[[FrozenTodoCloseProjection | None], None]
        | None = None,
        deadline_factory: KernelExecutionDeadlineFactory | None = None,
        hook_dispatcher: KernelHookDispatcher | None = None,
        hook_context_owner: HookContextOwner | None = None,
        hook_root_scope: HookDispatchScopeRef | None = None,
    ) -> None:
        self._repository = repository
        self._guard = guard
        self._host_owner_id = host_owner_id
        self._io = io_owner
        self._live_bus = live_bus
        self._todo_owner = todo_owner
        self._launch_preparation = launch_preparation
        self._terminal_cwd = terminal_cwd
        self._todo_close_projector = todo_close_projector or (lambda _value: None)
        self._deadlines = deadline_factory or KernelExecutionDeadlineFactory()
        self._runner_factory: (
            Callable[[HookDispatchScopeRef | None], ConversationKernelRunner] | None
        ) = None
        self._tasks: dict[str, _LiveTask] = {}
        self._launch_permits: dict[str, _SubagentLaunchPermit] = {}
        self._start_materials: dict[str, _TaskStartMaterial] = {}
        self._mailboxes: dict[str, list[PreparedInterAgentMailboxItem]] = {}
        self._mailbox_ordinals: dict[str, int] = {}
        self._mailbox_consumptions: dict[str, asyncio.Task[bool]] = {}
        self._completing: set[str] = set()
        self._batch_admissions: dict[str, _BatchAdmissionAttempt] = {}
        self._scheduler_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._state_changed = asyncio.Condition(self._lock)
        self._state_revision = 0
        self._root_completion_queue: deque[str] = deque()
        self._root_completion_set: set[str] = set()
        self._root_completion_turn_id: str | None = None
        self._root_completion_delivery_open = False
        self._closed = False
        self._list_cursor_secret = secrets.token_bytes(32)
        self._hook_dispatcher = hook_dispatcher
        self._hook_context_owner = hook_context_owner
        self._hook_root_scope = hook_root_scope
        if (hook_dispatcher is None) != (hook_context_owner is None) or (
            hook_dispatcher is None
        ) != (hook_root_scope is None):
            raise ValueError("subagent Hook composition must be complete or absent")

    def _canonical_deadline(self) -> float:
        return self._deadlines.deadline(KernelWatchdogOwner.FOREGROUND_CANONICAL)

    def _notify_state_changed_locked(self) -> None:
        """Advance the exact process-local wait frontier while holding `_lock`."""

        self._state_revision += 1
        self._state_changed.notify_all()

    async def offer_subagent_completion(self, task_id: str) -> bool:
        """Offer one durable terminal task to this HostSession's ROOT inbox."""

        if not task_id:
            raise ValueError("completion task identity is empty")
        async with self._state_changed:
            if self._closed or task_id in self._root_completion_set:
                return False
            self._root_completion_set.add(task_id)
            self._root_completion_queue.append(task_id)
            self._notify_state_changed_locked()
            return True

    async def open_root_completion_delivery(self, turn_id: str) -> None:
        """Open the exact ROOT turn's ordinary pending-input phase."""

        if not turn_id:
            raise ValueError("ROOT completion turn identity is empty")
        async with self._state_changed:
            if self._closed:
                return
            if (
                self._root_completion_turn_id is not None
                and self._root_completion_turn_id != turn_id
                and self._root_completion_delivery_open
            ):
                raise RuntimeError("another ROOT completion phase is still open")
            self._root_completion_turn_id = turn_id
            self._root_completion_delivery_open = True
            self._notify_state_changed_locked()

    async def seal_root_completion_delivery(self, turn_id: str) -> bool:
        """Linearize a no-tool answer fence against completion offers."""

        async with self._state_changed:
            if (
                self._root_completion_turn_id != turn_id
                or not self._root_completion_delivery_open
            ):
                raise RuntimeError("ROOT completion phase is not open")
            pending = bool(self._root_completion_queue)
            self._root_completion_delivery_open = False
            self._notify_state_changed_locked()
            return pending

    async def settle_root_completion_delivery(
        self, turn_id: str, *, turn_completed: bool
    ) -> None:
        """Resolve the answer fence from the canonical assistant settlement."""

        async with self._state_changed:
            if self._root_completion_turn_id != turn_id:
                return
            if turn_completed:
                self._root_completion_turn_id = None
                self._root_completion_delivery_open = False
            else:
                self._root_completion_delivery_open = True
            self._notify_state_changed_locked()

    async def close_root_completion_delivery(self, turn_id: str) -> None:
        """Retire an exact ROOT phase without discarding queued completions."""

        async with self._state_changed:
            if self._root_completion_turn_id != turn_id:
                return
            self._root_completion_turn_id = None
            self._root_completion_delivery_open = False
            self._notify_state_changed_locked()

    async def snapshot_pending_root_completions(self, turn_id: str) -> tuple[str, ...]:
        async with self._lock:
            if (
                self._root_completion_turn_id != turn_id
                or not self._root_completion_delivery_open
            ):
                return ()
            return tuple(self._root_completion_queue)

    async def retire_root_completion(self, task_id: str) -> bool:
        async with self._state_changed:
            if task_id not in self._root_completion_set:
                return False
            self._root_completion_set.remove(task_id)
            self._root_completion_queue = deque(
                value for value in self._root_completion_queue if value != task_id
            )
            self._notify_state_changed_locked()
            return True

    async def notify_root_input_activity(self) -> None:
        async with self._state_changed:
            if not self._closed:
                self._notify_state_changed_locked()

    @property
    def tool_names(self) -> frozenset[str]:
        return SUBAGENT_TOOL_NAMES

    def validate_arguments(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, object],
    ) -> str | None:
        """Validate closed Round 10 argument bounds before attempt admission."""

        try:
            _validate_subagent_tool_arguments(tool_name, arguments)
        except (TypeError, ValueError) as exc:
            return f"invalid tool arguments: {exc}"
        return None

    def owns_active_compaction_target(
        self,
        *,
        task_id: str,
        turn_id: str,
        owner_task: asyncio.Task[object],
    ) -> bool:
        """Exact same-loop ownership check used under the Host safe-point lock.

        The map is mutated only by this event loop.  This deliberately performs
        no await and no canonical read while the Host lock is held; the later
        repository compaction preconditions still revalidate the durable turn.
        """

        live = self._tasks.get(task_id)
        return bool(
            live is not None
            and live.status == "ACTIVE"
            and live.task is owner_task
            and stable_subagent_turn_id(
                session_id=self._guard.session_id, task_id=task_id
            )
            == turn_id
        )

    def bind_runner_factory(
        self,
        factory: Callable[[HookDispatchScopeRef | None], ConversationKernelRunner],
    ) -> None:
        if self._runner_factory is not None:
            raise RuntimeError("subagent runner factory is already bound")
        self._runner_factory = factory

    def build_initial_seed(
        self,
        *,
        task_id: str,
        dispatch_read: FrozenCanonicalProviderDispatchRead,
    ) -> SubagentInitialSeed:
        """Seal one child first-open seed from installed Host start material."""

        material = self._start_materials.get(task_id)
        if material is None:
            raise RuntimeError("subagent start material is not installed")
        return build_subagent_initial_seed(
            dispatch_read=dispatch_read,
            task_id=task_id,
            parent_turn_id=material.parent_turn_id,
            profile_kind=material.profile,
            objective=material.objective,
            parent_call_subject=material.parent_call_subject,
            parent_context_selection=material.context,
            dependency_context=material.dependency_context,
        )

    def profile_kind(self, *, task_id: str) -> SubagentProfileKind:
        material = self._start_materials.get(task_id)
        if material is None:
            raise RuntimeError("subagent start material is not installed")
        return material.profile

    def initial_context_sources(
        self, *, task_id: str
    ) -> tuple[ContextSourceCandidate | ContextSourceAbsentFact, ...]:
        """Return the exact immutable source leaves installed for one child."""

        material = self._start_materials.get(task_id)
        if material is None:
            raise RuntimeError("subagent start material is not installed")
        return (
            build_subagent_context_source(
                kind=ContextSourceKind.PARENT_CONTEXT,
                text=material.context.rendered_body,
                domain_identity={
                    "subject": parent_context_call_subject_identity_digest(
                        material.parent_call_subject
                    ),
                    "selection": parent_context_selection_identity_digest(
                        material.parent_call_subject, material.context
                    ),
                    "source": parent_context_source_identity_digest(
                        material.parent_call_subject, material.context
                    ),
                },
            ),
            build_subagent_context_source(
                kind=ContextSourceKind.DEPENDENCY_RESULTS,
                text=(
                    None
                    if material.dependency_context is None
                    else material.dependency_context.rendered_body
                ),
                domain_identity=(
                    None
                    if material.dependency_context is None
                    else dependency_result_context_identity_digest(
                        material.dependency_context
                    )
                ),
            ),
        )

    async def invoke(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, object],
        invocation_context: KernelToolInvocationContext,
    ) -> KernelToolResult:
        argument_error = self.validate_arguments(
            tool_name=tool_name,
            arguments=arguments,
        )
        if argument_error is not None:
            return _result("INVALID_ARGUMENTS", {"error": argument_error})
        if (
            invocation_context.session_id != self._guard.session_id
            or invocation_context.host_owner_epoch != self._guard.writer_generation
            or invocation_context.attempt_permission_snapshot_fingerprint
            != invocation_context.permission_snapshot_fingerprint
        ):
            return _result(
                "PERMISSION_DENIED",
                {"error": "subagent_authority_mismatch"},
            )
        root = invocation_context.conversation_scope_kind == "ROOT"
        if tool_name in ROOT_ORCHESTRATION_TOOL_NAMES:
            if (
                not root
                or invocation_context.effective_permission_mode
                is not PermissionMode.BYPASS_PERMISSIONS
                or invocation_context.subagent_parent_context_subject is None
            ):
                return _result(
                    "PERMISSION_DENIED",
                    {"error": "subagent_requires_bypass_mode"},
                )
        elif tool_name == "report_agent_result":
            if root or invocation_context.scope_subagent_task_id is None:
                return _result(
                    "PERMISSION_DENIED", {"error": "subagent_report_scope_invalid"}
                )
        else:
            raise KeyError(tool_name)
        if tool_name == "spawn_agent":
            return await self._spawn(arguments, invocation_context=invocation_context)
        if tool_name == "create_agent_tasks":
            return await self._create_agent_tasks(
                arguments, invocation_context=invocation_context
            )
        if tool_name == "list_agents":
            return await self._list(arguments)
        if tool_name == "wait_agent":
            return await self._wait(arguments, invocation_context)
        if tool_name == "stop_agent":
            return await self._stop(arguments)
        if tool_name == "send_agent_message":
            return await self._send_message(arguments, invocation_context)
        raise RuntimeError(
            "report_agent_result escaped its dedicated runner settlement seam"
        )

    async def freeze_compaction_handoff(
        self,
    ) -> tuple[
        tuple[FrozenRootSubagentTaskBoardHandoffFact, ...],
        tuple[tuple[str, int], ...],
    ]:
        """Return the bounded hierarchical task-board view owned by this Host."""

        rows, totals = await self._io.run(
            self._repository.read_subagent_task_board,
            session_id=self._guard.session_id,
            deadline_monotonic=self._canonical_deadline(),
        )
        async with self._lock:
            pending_counts = {
                str(row["id"]): len(self._mailboxes.get(str(row["id"]), ()))
                for row in rows
            }
        return (
            tuple(
                freeze_subagent_task_board_fact(
                    task_id=str(row["id"]),
                    task_key=row["task_key"],
                    label=row["label"],
                    objective_preview=bounded_handoff_preview(str(row["objective"])),
                    status=str(row["status"]),
                    dependency_total=int(row["dependency_total"]),
                    dependency_remaining=int(row["dependency_remaining"]),
                    pending_message_count=pending_counts[str(row["id"])],
                    accepted_at=row["accepted_at"],
                )
                for row in rows
            ),
            totals,
        )

    async def _spawn(
        self,
        arguments: Mapping[str, object],
        *,
        invocation_context: KernelToolInvocationContext,
    ) -> KernelToolResult:
        task = arguments.get("task")
        single: dict[str, object] = {
            "task": task,
            "profile": arguments.get("profile", "general_worker"),
            "context": arguments.get("context", {"mode": "none"}),
            "depends_on": [],
        }
        if arguments.get("task_name") is not None:
            single["task_key"] = arguments["task_name"]
            single["label"] = arguments["task_name"]
        result = await self._create_agent_tasks(
            {"tasks": [single]}, invocation_context=invocation_context
        )
        if result.state != "SUCCESS":
            return result
        payload = json.loads(result.content.decode("utf-8"))
        first = payload["tasks"][0]
        return _result(
            "SUCCESS",
            {"task_id": first["task_id"], "status": first["status"]},
            remote_identity=first["task_id"],
        )

    async def _create_agent_tasks(
        self,
        arguments: Mapping[str, object],
        *,
        invocation_context: KernelToolInvocationContext,
    ) -> KernelToolResult:
        raw_tasks = arguments.get("tasks")
        subject = invocation_context.subagent_parent_context_subject
        if (
            not isinstance(raw_tasks, list)
            or not 1 <= len(raw_tasks) <= 16
            or subject is None
            or subject.session_id != invocation_context.session_id
            or subject.caller_turn_id != invocation_context.turn_id
        ):
            return _result("INVALID_ARGUMENTS", {"error": "invalid task batch"})
        try:
            batch_id = _stable_id(
                "subagent-batch", invocation_context.attempt_id, "batch"
            )
            normalized: list[dict[str, object]] = []
            key_to_id: dict[str, str] = {}
            for ordinal, raw in enumerate(raw_tasks):
                if not isinstance(raw, Mapping):
                    raise ValueError("task item must be an object")
                allowed = {
                    "task",
                    "task_key",
                    "label",
                    "profile",
                    "display_role",
                    "context",
                    "depends_on",
                }
                if set(raw) - allowed:
                    raise ValueError("task item has unknown fields")
                objective = raw.get("task")
                if (
                    not isinstance(objective, str)
                    or not objective
                    or len(objective.encode("utf-8")) > 65_536
                ):
                    raise ValueError("task is required and bounded")
                task_key = raw.get("task_key")
                if task_key is not None and (
                    not isinstance(task_key, str)
                    or re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", task_key) is None
                ):
                    raise ValueError("task_key is invalid")
                if task_key is not None and task_key in key_to_id:
                    raise ValueError("task_key is duplicated")
                task_id = _stable_id(
                    "subagent-task", invocation_context.attempt_id, str(ordinal)
                )
                if task_key is not None:
                    key_to_id[task_key] = task_id
                profile = SubagentProfileKind(str(raw.get("profile", "general_worker")))
                context = raw.get("context", {"mode": "none"})
                mode, turns = _parse_context(context)
                selection = build_parent_context_selection(
                    subject, mode=mode, last_n_turns=turns
                )
                # Admission-time source quote: no silent shrink to NONE.
                if (
                    selection.rendered_body is not None
                    and len(selection.rendered_body.encode("utf-8"))
                    > MAXIMUM_PARENT_CONTEXT_SOURCE_UTF8_BYTES
                ):
                    raise ValueError("parent context exceeds source bound")
                depends = raw.get("depends_on", [])
                if not isinstance(depends, list) or len(depends) > 16:
                    raise ValueError("depends_on is invalid")
                normalized.append(
                    {
                        "task_id": task_id,
                        "task_key": task_key,
                        "label": raw.get("label"),
                        "profile": profile,
                        "display_role": raw.get("display_role"),
                        "objective": objective,
                        "selection": selection,
                        "raw_dependencies": tuple(depends),
                    }
                )
            batch_task_ids = {str(item["task_id"]) for item in normalized}
            edge_count = 0
            for item in normalized:
                dependency_ids: list[str] = []
                for token in item["raw_dependencies"]:
                    if not isinstance(token, str) or not token:
                        raise ValueError("dependency reference is invalid")
                    if token in key_to_id:
                        resolved = key_to_id[token]
                    elif token.startswith("task:") and len(token) > 5:
                        resolved = token[5:]
                    else:
                        raise ValueError("dependency reference is not exact")
                    if resolved in dependency_ids:
                        raise ValueError("dependency reference is duplicated")
                    dependency_ids.append(resolved)
                edge_count += len(dependency_ids)
                item["dependency_task_ids"] = tuple(dependency_ids)
            if edge_count > 64:
                raise ValueError("task batch exceeds edge bound")
            external_ids = tuple(
                dict.fromkeys(
                    dependency_id
                    for item in normalized
                    for dependency_id in item["dependency_task_ids"]
                    if dependency_id not in batch_task_ids
                )
            )
            external_rows: dict[str, Mapping[str, object]] = {}
            for dependency_id in external_ids:
                row = await self._io.run(
                    self._repository.query_subagent_task,
                    session_id=self._guard.session_id,
                    task_id=dependency_id,
                    deadline_monotonic=self._canonical_deadline(),
                )
                if row is None:
                    raise ValueError("dependency reference is unknown")
                external_rows[dependency_id] = row
            dispositions = dict(
                derive_subagent_batch_initial_dispositions(
                    ordered_tasks=tuple(
                        (
                            str(item["task_id"]),
                            tuple(item["dependency_task_ids"]),
                        )
                        for item in normalized
                    ),
                    external_states={
                        task_id: (
                            SubagentTaskStatus(str(row["status"])),
                            row.get("result_id") is not None,
                        )
                        for task_id, row in external_rows.items()
                    },
                )
            )
            drafts: list[PreparedSubagentTaskDraft] = []
            for item in normalized:
                dependency_ids = item["dependency_task_ids"]
                disposition = dispositions[str(item["task_id"])]
                status = disposition.status
                pending_reason = disposition.pending_reason
                terminal_reason = disposition.terminal_reason
                drafts.append(
                    PreparedSubagentTaskDraft(
                        task_id=str(item["task_id"]),
                        task_key=item["task_key"],
                        label=item["label"],
                        profile=item["profile"],
                        display_role=item["display_role"],
                        objective=str(item["objective"]),
                        context=item["selection"],
                        dependency_task_ids=tuple(dependency_ids),
                        initial_status=status,
                        pending_reason=pending_reason,
                        terminal_reason=terminal_reason,
                    )
                )
            _require_acyclic(drafts)
            occurred_at = datetime.now(timezone.utc)
            candidate = PreparedSubagentTaskBatchAdmission(
                session_id=invocation_context.session_id,
                workspace_id=invocation_context.workspace_id,
                writer_generation=self._guard.writer_generation,
                parent_turn_id=invocation_context.turn_id,
                source_tool_attempt_id=invocation_context.attempt_id,
                permission_snapshot_fingerprint=(
                    invocation_context.permission_snapshot_fingerprint
                ),
                parent_call_subject=subject,
                batch_id=batch_id,
                ordered_tasks=tuple(drafts),
                occurred_at=occurred_at,
                actor_id=self._host_owner_id,
            )
        except (TypeError, ValueError) as exc:
            return _result("INVALID_ARGUMENTS", {"error": str(exc)})

        materials = tuple(
            _TaskStartMaterial(
                task_id=draft.task_id,
                parent_turn_id=invocation_context.turn_id,
                objective=draft.objective,
                profile=draft.profile,
                parent_call_subject=subject,
                context=draft.context,
                dependency_task_ids=draft.dependency_task_ids,
            )
            for draft in drafts
            if not draft.initial_status.terminal
        )
        return await self._settle_batch_admission(
            candidate=candidate,
            drafts=tuple(drafts),
            materials=materials,
        )

    async def _settle_batch_admission(
        self,
        *,
        candidate: PreparedSubagentTaskBatchAdmission,
        drafts: tuple[PreparedSubagentTaskDraft, ...],
        materials: tuple[_TaskStartMaterial, ...],
    ) -> KernelToolResult:
        """Own one exact admission through canonical FULL and material install."""

        async with self._lock:
            if self._closed or self._runner_factory is None:
                return _result(
                    "TOOL_UNAVAILABLE", {"error": "subagent owner is closed"}
                )
            current = self._batch_admissions.get(candidate.batch_id)
            if current is not None:
                if current.candidate != candidate:
                    raise ConversationKernelConflict(
                        "subagent batch admission identity conflicts"
                    )
                task = current.task
            else:
                task = asyncio.create_task(
                    self._settle_batch_admission_worker(
                        candidate=candidate,
                        drafts=drafts,
                        materials=materials,
                    ),
                    name=f"kernel-subagent-batch-admission:{candidate.batch_id}",
                )
                self._batch_admissions[candidate.batch_id] = _BatchAdmissionAttempt(
                    candidate,
                    task,
                )
                task.add_done_callback(
                    lambda completed, batch_id=candidate.batch_id: (
                        self._retire_batch_admission(batch_id, completed)
                    )
                )
        cancellation: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
                continue
            except BaseException:
                break
        result = task.result()
        if cancellation is not None:
            raise cancellation
        return result

    def _retire_batch_admission(
        self,
        batch_id: str,
        task: asyncio.Task[KernelToolResult],
    ) -> None:
        current = self._batch_admissions.get(batch_id)
        if current is not None and current.task is task:
            self._batch_admissions.pop(batch_id, None)

    async def _settle_batch_admission_worker(
        self,
        *,
        candidate: PreparedSubagentTaskBatchAdmission,
        drafts: tuple[PreparedSubagentTaskDraft, ...],
        materials: tuple[_TaskStartMaterial, ...],
    ) -> KernelToolResult:
        async with self._lock:
            if self._closed:
                return _result(
                    "TOOL_UNAVAILABLE", {"error": "subagent owner is closed"}
                )
        while True:
            try:
                await self._io.run(
                    self._repository.accept_subagent_task_batch,
                    self._guard,
                    candidate=candidate,
                    deadline_monotonic=self._canonical_deadline(),
                )
                break
            except (ConversationKernelConflict, StaleHostWriter, ValueError):
                raise
            except _RETRYABLE_CANONICAL_ERRORS:
                try:
                    confirmation = await self._io.run(
                        self._repository.confirm_subagent_task_batch,
                        candidate=candidate,
                        deadline_monotonic=self._canonical_deadline(),
                    )
                except (ConversationKernelConflict, StaleHostWriter, ValueError):
                    raise
                except _RETRYABLE_CANONICAL_ERRORS:
                    await asyncio.sleep(0.05)
                    continue
                if confirmation.kind is SubagentBatchConfirmationKind.FULL:
                    break
                if confirmation.kind is SubagentBatchConfirmationKind.CONFLICT:
                    raise ConversationKernelConflict(
                        "subagent batch has a conflicting canonical winner"
                    )
                continue
        async with self._lock:
            if self._closed:
                closing = True
            else:
                closing = False
                for material in materials:
                    self._start_materials[material.task_id] = material
                    self._mailboxes.setdefault(material.task_id, [])
        if closing:
            for material in materials:
                await self._settle_task_terminal_exact(
                    material.task_id,
                    SubagentTaskStatus.INTERRUPTED,
                    "HOST_CLOSING",
                    require_absent_turn=True,
                )
            return _result("TOOL_UNAVAILABLE", {"error": "subagent owner is closed"})
        await self._start_available_tasks()
        result_tasks = []
        for draft in drafts:
            durable = await self._io.run(
                self._repository.query_subagent_task,
                session_id=self._guard.session_id,
                task_id=draft.task_id,
                deadline_monotonic=self._canonical_deadline(),
            )
            assert durable is not None
            if SubagentTaskStatus(str(durable["status"])).terminal:
                await self.offer_subagent_completion(draft.task_id)
            result_tasks.append(
                {
                    "task_key": draft.task_key,
                    "task_id": draft.task_id,
                    "status": str(durable["status"]).lower(),
                }
            )
        return _result(
            "SUCCESS",
            {"batch_id": candidate.batch_id, "tasks": result_tasks},
            remote_identity=candidate.batch_id,
        )

    async def _start_available_tasks(self) -> None:
        async with self._lock:
            task = self._scheduler_task
            if task is None or task.done():
                task = asyncio.create_task(
                    self._start_available_tasks_worker(),
                    name=f"kernel-subagent-scheduler:{self._guard.session_id}",
                )
                self._scheduler_task = task
        cancellation: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
            except BaseException:
                break
        try:
            task.result()
        finally:
            async with self._lock:
                if self._scheduler_task is task:
                    self._scheduler_task = None
        if cancellation is not None:
            raise cancellation

    async def _start_available_tasks_worker(self) -> None:
        while True:
            async with self._lock:
                if self._closed:
                    return
                # Capacity belongs to physical child execution, not the small
                # post-terminal bookkeeping tail.  Every terminal path changes
                # ``status`` and retires its TODO owner before it asks the
                # scheduler to fill the slot, so a canonical terminal child no
                # longer blocks the next accepted task merely because its
                # coroutine has not returned from final settlement yet.
                active = sum(item.status == "ACTIVE" for item in self._tasks.values())
                reserved = len(self._launch_permits)
                available = MAXIMUM_LIVE_SUBAGENTS - active - reserved
                if available <= 0:
                    return
            rows = await self._io.run(
                self._repository.list_runnable_subagent_tasks,
                self._guard,
                maximum_items=available,
                deadline_monotonic=self._canonical_deadline(),
            )
            if not rows:
                return
            started_any = False
            for row in rows:
                task_id = str(row["id"])
                async with self._lock:
                    material = self._start_materials.get(task_id)
                    if material is None or task_id in self._launch_permits:
                        continue
                    permit = _SubagentLaunchPermit(material)
                    self._launch_permits[task_id] = permit
                admission_full = False
                live_installed = False
                hook_scope: HookDispatchScopeRef | None = None
                hook_reservation: PendingHookContextReservation | None = None
                try:
                    dependencies = ()
                    if material.dependency_task_ids:
                        dependencies = await self._io.run(
                            self._repository.read_subagent_dependencies,
                            session_id=self._guard.session_id,
                            task_ids=(task_id,),
                            deadline_monotonic=self._canonical_deadline(),
                        )
                        if (
                            tuple(
                                str(item["dependency_task_id"]) for item in dependencies
                            )
                            != material.dependency_task_ids
                        ):
                            raise ConversationKernelConflict(
                                "subagent start dependency set drifted"
                            )
                    dependency_context = build_dependency_result_context(
                        target_task_id=task_id,
                        rows=dependencies,
                    )
                    material = replace(material, dependency_context=dependency_context)
                    async with self._lock:
                        if self._launch_permits.get(task_id) is not permit:
                            continue
                        permit.material = material
                        self._start_materials[task_id] = material
                        if self._closed or permit.stop_claimed:
                            continue
                    candidate = build_subagent_task_start(
                        session_id=self._guard.session_id,
                        workspace_id=str(row["workspace_id"]),
                        writer_generation=self._guard.writer_generation,
                        task_id=task_id,
                        parent_turn_id=material.parent_turn_id,
                        objective=material.objective,
                        profile=material.profile,
                        parent_context=material.context,
                        dependency_context=dependency_context,
                        occurred_at=datetime.now(timezone.utc),
                        actor_id=self._host_owner_id,
                    )
                    while True:
                        try:
                            changed = await self._io.run(
                                self._repository.accept_subagent_task_start,
                                self._guard,
                                candidate=candidate,
                                deadline_monotonic=self._canonical_deadline(),
                            )
                        except StaleHostWriter:
                            return
                        except _RETRYABLE_CANONICAL_ERRORS:
                            changed = False
                        if changed:
                            confirmation_kind = SubagentBatchConfirmationKind.FULL
                        else:
                            try:
                                confirmation = await self._io.run(
                                    self._repository.confirm_subagent_task_start,
                                    candidate=candidate,
                                    deadline_monotonic=self._canonical_deadline(),
                                )
                            except StaleHostWriter:
                                return
                            except _RETRYABLE_CANONICAL_ERRORS:
                                await asyncio.sleep(0.05)
                                continue
                            confirmation_kind = confirmation.kind
                        if confirmation_kind is SubagentBatchConfirmationKind.FULL:
                            permit.start_committed = True
                            break
                        if confirmation_kind is SubagentBatchConfirmationKind.CONFLICT:
                            raise ConversationKernelConflict(
                                "subagent start has a conflicting canonical winner"
                            )
                        async with self._lock:
                            if self._closed or permit.stop_claimed:
                                return
                        await asyncio.sleep(0)
                    async with self._lock:
                        if self._closed or permit.stop_claimed:
                            continue
                    launch = await self._launch_preparation.prepare_launch(candidate)
                    if launch.task_start != candidate:
                        raise ConversationKernelConflict(
                            "subagent launch port returned another task start"
                        )
                    hook_scope = self._register_child_hook_scope(task_id)
                    if (
                        self._hook_dispatcher is not None
                        and self._hook_context_owner is not None
                        and hook_scope is not None
                    ):
                        public_input = SubagentStartInput(
                            session_id=self._guard.session_id,
                            cwd=str(self._terminal_cwd()),
                            model=launch.configured_model_identity,
                            turn_id=launch.child_turn_id,
                            agent_id=task_id,
                            agent_type=material.profile.value,
                            permission_mode=external_permission_mode(
                                launch.parent_permission_snapshot.effective_mode.value,
                                active_plan_workflow=(
                                    launch.parent_permission_snapshot.plan_workflow_id
                                    is not None
                                ),
                            ),
                        )
                        causal_ref = SubagentStartRef(
                            candidate.event_id,
                            task_id,
                            launch.child_turn_id,
                        )
                        outcome = await self._hook_dispatcher.dispatch(
                            HookDispatchEnvelope(
                                self._hook_dispatcher.capture_view(),
                                hook_scope,
                                public_input,
                                causal_ref,
                                self._canonical_deadline(),
                                permit.cancellation_signal,
                            ),
                            matcher_subject=event_matcher_subject(
                                public_input.event_type,
                                agent_type=material.profile.value,
                            ),
                        )
                        if not isinstance(outcome, ContextOutcome):
                            raise RuntimeError(
                                "SubagentStart returned another outcome family"
                            )
                        hook_reservation = self._hook_context_owner.prepare_sync(
                            scope=hook_scope,
                            causal_ref=causal_ref,
                            entries=outcome.context_entries,
                        )
                    async with self._lock:
                        if (
                            self._closed
                            or permit.stop_claimed
                            or self._launch_permits.get(task_id) is not permit
                            or permit.material != material
                        ):
                            continue
                        permit.launching = True
                    runner_factory = self._runner_factory
                    if runner_factory is None:
                        raise RuntimeError("subagent runner factory is not bound")
                    runner = runner_factory(hook_scope)
                    intent = ActiveTurnCancellationIntent(
                        turn_id=launch.child_turn_id,
                        scope_kind=ModelInputScopeKind.SUBAGENT_TASK,
                        scope_subagent_task_id=task_id,
                    )
                    await runner.admit_subagent_turn(
                        launch=launch,
                        cancellation_intent=intent,
                    )
                    admission_full = True
                    if hook_reservation is not None:
                        hook_reservation.commit()
                    live_installed = await self._install_live_task(
                        material=material,
                        launch=launch,
                        runner=runner,
                        intent=intent,
                        hook_scope=hook_scope,
                        permit=permit,
                    )
                    if not live_installed:
                        raise ConversationKernelConflict(
                            "admitted child lost its exact launch permit"
                        )
                    started_any = True
                except StaleHostWriter:
                    return
                except asyncio.CancelledError:
                    if admission_full and not live_installed:
                        await asyncio.shield(
                            self._settle_cancelled_child(
                                task_id=task_id,
                                turn_id=stable_subagent_turn_id(
                                    session_id=self._guard.session_id,
                                    task_id=task_id,
                                ),
                                task_status="INTERRUPTED",
                                task_reason="HOST_CLOSING",
                                turn_reason="SESSION_CLOSED",
                            )
                        )
                    raise
                except BaseException as exc:
                    admission_error = exc
                    if isinstance(exc, SubagentTurnAdmissionPostCommitError):
                        admission_full = True
                        admission_error = exc.activation_error
                    if permit.cancellation_reason is not None:
                        if admission_full and not live_installed:
                            explicit = permit.cancellation_reason == "USER_CANCELLED"
                            await self._settle_cancelled_child(
                                task_id=task_id,
                                turn_id=stable_subagent_turn_id(
                                    session_id=self._guard.session_id,
                                    task_id=task_id,
                                ),
                                task_status=(
                                    "CANCELLED" if explicit else "INTERRUPTED"
                                ),
                                task_reason=permit.cancellation_reason,
                                turn_reason=(
                                    "USER_STOPPED" if explicit else "SESSION_CLOSED"
                                ),
                            )
                        continue
                    dependency_failure = (
                        not admission_full
                        and isinstance(admission_error, (TypeError, ValueError))
                        and "dependency" in str(admission_error).lower()
                    )
                    code = (
                        "DEPENDENCY_RESULT_INVARIANT"
                        if dependency_failure
                        else "CHILD_START_FAILED"
                    )
                    public_detail = bounded_terminal_public_detail(
                        f"{type(admission_error).__name__}: {admission_error}",
                    )
                    try:
                        if admission_full:
                            await self._settle_cancelled_child(
                                task_id=task_id,
                                turn_id=stable_subagent_turn_id(
                                    session_id=self._guard.session_id,
                                    task_id=task_id,
                                ),
                                task_status="FAILED",
                                task_reason=code,
                                turn_reason=code,
                                terminal_public_detail=public_detail,
                            )
                        else:
                            await self._settle_task_terminal_exact(
                                task_id,
                                SubagentTaskStatus.FAILED,
                                code,
                                require_absent_turn=True,
                                public_detail=public_detail,
                            )
                        await self._settle_dependency_frontier(
                            task_id, schedule_after=False
                        )
                        started_any = True
                    except StaleHostWriter:
                        return
                    self._offer_progress(
                        task_id, material.parent_turn_id, "FAILED", code
                    )
                finally:
                    if hook_reservation is not None and not live_installed:
                        hook_reservation.retire()
                    if hook_scope is not None and not live_installed:
                        assert self._hook_context_owner is not None
                        self._hook_context_owner.retire_scope(hook_scope)
                    async with self._lock:
                        if self._launch_permits.get(task_id) is permit:
                            self._launch_permits.pop(task_id, None)
                        permit.settled.set()
                        self._notify_state_changed_locked()
            if not started_any:
                return

    def _register_child_hook_scope(self, task_id: str) -> HookDispatchScopeRef | None:
        root = self._hook_root_scope
        owner = self._hook_context_owner
        if root is None or owner is None:
            return None
        scope = HookDispatchScopeRef(
            root.host_session_owner,
            root.workspace_owner,
            HookScopeKind.CHILD,
            task_id,
        )
        owner.register_scope(scope)
        return scope

    async def _install_live_task(
        self,
        *,
        material: _TaskStartMaterial,
        launch: PreparedSubagentLaunch,
        runner: ConversationKernelRunner,
        intent: ActiveTurnCancellationIntent,
        hook_scope: HookDispatchScopeRef | None,
        permit: _SubagentLaunchPermit,
    ) -> bool:
        async with self._state_changed:
            if (
                self._launch_permits.get(material.task_id) is not permit
                or not permit.launching
                or permit.material != material
                or material.task_id in self._tasks
            ):
                return False
            task = asyncio.create_task(
                self._run_child(
                    launch=launch,
                    runner=runner,
                    cancellation_intent=intent,
                ),
                name=f"kernel-subagent:{material.task_id}",
            )
            task.add_done_callback(
                lambda completed, task_id=material.task_id: self._retire_live_task(
                    task_id, completed
                )
            )
            live = _LiveTask(
                material.task_id,
                material.parent_turn_id,
                task,
                intent,
                launch,
                hook_scope,
            )
            if permit.cancellation_reason is not None:
                cause = intent.install_cause(
                    ForegroundCancellationCause.USER_REQUEST
                    if permit.cancellation_reason == "USER_CANCELLED"
                    else ForegroundCancellationCause.HOST_SESSION_CLOSE
                )
                live.cancellation_reason = (
                    "USER_CANCELLED"
                    if cause is ForegroundCancellationCause.USER_REQUEST
                    else "HOST_CLOSING"
                )
            self._tasks[material.task_id] = live
            self._notify_state_changed_locked()
        if live.cancellation_reason is not None:
            task.cancel()
        else:
            self._offer_progress(
                material.task_id,
                material.parent_turn_id,
                "ACTIVE",
                "Subagent started",
            )
        return True

    def _retire_live_task(
        self,
        task_id: str,
        completed: asyncio.Task[KernelRunResult],
    ) -> None:
        """Retire terminal process-local carriers without capping task history.

        Canonical task/result rows remain the query authority.  Keeping the
        physical Task, frozen parent context and mailbox after the exact child
        owner has finished would make a long-lived Host grow with historical
        task count.  This callback runs on the manager event loop after
        ``_run_child`` has settled the dependency frontier and closed its TODO
        run, so no separate cleanup task or close-drain owner is required.
        """

        current = self._tasks.get(task_id)
        if current is None or current.task is not completed:
            return
        # A task cancelled before its coroutine body starts has not executed
        # the canonical cancellation settlement.  Leave that ACTIVE carrier
        # for ``aclose``'s exact fallback instead of retiring its only owner.
        if current.status == "ACTIVE":
            return
        if not completed.cancelled():
            # Observe failures even when ROOT never called wait_agent.  The
            # canonical FAILED/result settlement remains the public outcome.
            completed.exception()
        self._tasks.pop(task_id, None)
        self._retire_dormant_carriers_locked(task_id)

    def _retire_dormant_carriers_locked(self, task_id: str) -> bool:
        """Drop carriers for a terminal task that has no physical child owner."""

        if task_id in self._tasks:
            return False
        changed = (
            any(
                task_id in collection
                for collection in (
                    self._start_materials,
                    self._mailboxes,
                    self._mailbox_ordinals,
                )
            )
            or task_id in self._completing
        )
        self._start_materials.pop(task_id, None)
        self._mailboxes.pop(task_id, None)
        self._mailbox_ordinals.pop(task_id, None)
        self._completing.discard(task_id)
        return changed

    async def _retire_dormant_terminal_task(self, task_id: str) -> None:
        """Confirm canonical terminality before retiring a never-started task."""

        async with self._lock:
            if task_id in self._tasks or task_id not in self._start_materials:
                return
        while True:
            try:
                durable = await self._io.run(
                    self._repository.query_subagent_task,
                    session_id=self._guard.session_id,
                    task_id=task_id,
                    deadline_monotonic=self._canonical_deadline(),
                )
                break
            except _RETRYABLE_CANONICAL_ERRORS:
                await asyncio.sleep(0.05)
        if durable is None or not SubagentTaskStatus(str(durable["status"])).terminal:
            return
        await self.offer_subagent_completion(task_id)
        async with self._state_changed:
            if self._retire_dormant_carriers_locked(task_id):
                self._notify_state_changed_locked()

    async def _dependency_frontier_candidate_ids(
        self, terminal_task_id: str
    ) -> tuple[str, ...]:
        """Freeze the exact process-local descendant closure for one frontier."""

        async with self._lock:
            materials = tuple(self._start_materials.values())
        reached = {terminal_task_id}
        candidates: list[str] = []
        changed = True
        while changed:
            changed = False
            for material in materials:
                if material.task_id in reached:
                    continue
                if any(
                    dependency_task_id in reached
                    for dependency_task_id in material.dependency_task_ids
                ):
                    reached.add(material.task_id)
                    candidates.append(material.task_id)
                    changed = True
        return tuple(candidates)

    async def _retire_canonical_terminal_dormant_tasks(
        self, candidates: tuple[str, ...]
    ) -> None:
        """ACK-unknown fallback over one causal dependency frontier only."""

        for task_id in candidates:
            await self._retire_dormant_terminal_task(task_id)

    async def _run_child(
        self,
        *,
        launch: PreparedSubagentLaunch,
        runner: ConversationKernelRunner,
        cancellation_intent: ActiveTurnCancellationIntent,
    ) -> KernelRunResult:
        task_id = launch.task_start.task_id
        try:
            result = await runner.run_admitted_subagent_turn(
                launch=launch,
                cancellation_intent=cancellation_intent,
            )
            while True:
                try:
                    durable_result = await self._io.run(
                        self._repository.query_subagent_task,
                        session_id=self._guard.session_id,
                        task_id=task_id,
                        deadline_monotonic=self._canonical_deadline(),
                    )
                    break
                except _RETRYABLE_CANONICAL_ERRORS:
                    # The assistant/result/task winner is already one atomic
                    # canonical transaction.  A transient read cannot turn
                    # that known completion into a synthetic FAILED outcome.
                    await asyncio.sleep(0.05)
            if (
                durable_result is None
                or str(durable_result["status"]) != "COMPLETED"
                or durable_result.get("result_id") is None
                or durable_result.get("result_entry_id") != result.final_entry_id
            ):
                raise ConversationKernelConflict(
                    "completed child lacks its atomic canonical result winner"
                )
            async with self._state_changed:
                live = self._tasks.get(task_id)
                if live is not None:
                    live.status = "COMPLETED"
                    parent_turn_id = live.parent_turn_id
                else:
                    parent_turn_id = task_id
                self._notify_state_changed_locked()
            self._offer_progress(
                task_id,
                parent_turn_id,
                "COMPLETED",
                result.final_text,
            )
            await self.offer_subagent_completion(task_id)
            await self._settle_dependency_frontier(task_id)
            return result
        except asyncio.CancelledError:
            async with self._lock:
                live = self._tasks.get(task_id)
                reason = (
                    live.cancellation_reason if live is not None else None
                ) or "HOST_CLOSING"
            explicit_stop = reason == "USER_CANCELLED"
            status = "CANCELLED" if explicit_stop else "INTERRUPTED"
            turn_reason = "USER_STOPPED" if explicit_stop else "SESSION_CLOSED"
            cancellation_settlement = asyncio.create_task(
                self._settle_cancelled_child(
                    task_id=task_id,
                    turn_id=cancellation_intent.turn_id,
                    task_status=status,
                    task_reason=reason,
                    turn_reason=turn_reason,
                ),
                name=f"kernel-subagent-cancellation:{task_id}",
            )
            historical = await _join_child_settlement(cancellation_settlement)
            async with self._state_changed:
                live = self._tasks.get(task_id)
                if live is not None:
                    if historical is not None:
                        live.status = "COMPLETED"
                    else:
                        live.status = status
                    parent_turn_id = live.parent_turn_id
                else:
                    parent_turn_id = task_id
                self._notify_state_changed_locked()
            self._offer_progress(
                task_id,
                parent_turn_id,
                "COMPLETED" if historical is not None else status,
                (
                    "Subagent completed before cancellation"
                    if historical is not None
                    else reason
                ),
            )
            await self._settle_dependency_frontier(task_id)
            raise
        except ChildCompactionContinuationBlocked:
            await self._settle_cancelled_child(
                task_id=task_id,
                turn_id=cancellation_intent.turn_id,
                task_status="INTERRUPTED",
                task_reason="HOOK_COMPACTION_BLOCKED",
                turn_reason="HOOK_COMPACTION_BLOCKED",
                terminal_public_detail=build_default_terminal_public_detail(
                    "HOOK_COMPACTION_BLOCKED"
                ),
            )
            async with self._state_changed:
                live = self._tasks.get(task_id)
                if live is not None:
                    live.status = "INTERRUPTED"
                    parent_turn_id = live.parent_turn_id
                else:
                    parent_turn_id = task_id
                self._notify_state_changed_locked()
            self._offer_progress(
                task_id,
                parent_turn_id,
                "INTERRUPTED",
                "HOOK_COMPACTION_BLOCKED",
            )
            await self._settle_dependency_frontier(task_id)
            raise
        except BaseException as exc:
            code = "CHILD_EXECUTION_FAILED"
            public_detail = bounded_terminal_public_detail(
                f"{type(exc).__name__}: {exc}",
            )
            await self._settle_task_terminal_exact(
                task_id,
                SubagentTaskStatus.FAILED,
                code,
                require_absent_turn=False,
                public_detail=public_detail,
            )
            async with self._state_changed:
                live = self._tasks.get(task_id)
                if live is not None:
                    live.status = "FAILED"
                    parent_turn_id = live.parent_turn_id
                else:
                    parent_turn_id = task_id
                self._notify_state_changed_locked()
            self._offer_progress(task_id, parent_turn_id, "FAILED", code)
            await self._settle_dependency_frontier(task_id)
            raise
        finally:
            await self._close_todo_child_run(task_id)
            async with self._lock:
                live = self._tasks.get(task_id)
                hook_scope = None if live is None else live.hook_scope
            if hook_scope is not None and self._hook_context_owner is not None:
                self._hook_context_owner.retire_scope(hook_scope)

    async def _close_todo_child_run(self, task_id: str) -> None:
        if not self._todo_owner.mark_closing(
            scope_kind=ModelInputScopeKind.SUBAGENT_TASK,
            scope_subagent_task_id=task_id,
        ):
            return
        # The child runner and its shielded result settlement have joined.
        # Any remaining token is an invariant failure, not independently
        # runnable work that a condition wait could advance.
        projection = self._todo_owner.close_run(
            scope_kind=ModelInputScopeKind.SUBAGENT_TASK,
            scope_subagent_task_id=task_id,
        )
        self._todo_close_projector(projection)

    async def _settle_task_terminal_exact(
        self,
        task_id: str,
        status: SubagentTaskStatus,
        reason: str,
        *,
        require_absent_turn: bool,
        public_detail: str | None = None,
    ) -> None:
        exact_public_detail = public_detail or build_default_terminal_public_detail(
            reason
        )
        confirmed_terminal = False

        async def worker() -> None:
            nonlocal confirmed_terminal
            while True:
                try:
                    durable = await self._io.run(
                        self._repository.query_subagent_task,
                        session_id=self._guard.session_id,
                        task_id=task_id,
                        deadline_monotonic=self._canonical_deadline(),
                    )
                    break
                except StaleHostWriter:
                    return
                except _RETRYABLE_CANONICAL_ERRORS:
                    await asyncio.sleep(0.05)
            if durable is None:
                raise ConversationKernelConflict(
                    "subagent terminal settlement target is absent"
                )
            observed = SubagentTaskStatus(str(durable["status"]))
            if observed.terminal:
                if (
                    observed is status
                    and durable.get("terminal_reason") == reason
                    and durable.get("terminal_public_detail") == exact_public_detail
                ):
                    confirmed_terminal = True
                    return
                raise ConversationKernelConflict(
                    "subagent task already has another terminal winner"
                )
            candidate = build_subagent_task_terminal_settlement(
                session_id=self._guard.session_id,
                workspace_id=str(durable["workspace_id"]),
                writer_generation=self._guard.writer_generation,
                task_id=task_id,
                expected_turn_id=stable_subagent_turn_id(
                    session_id=self._guard.session_id, task_id=task_id
                ),
                status=status,
                reason=reason,
                public_detail=exact_public_detail,
                require_absent_turn=require_absent_turn,
                occurred_at=datetime.now(timezone.utc),
                actor_id=self._host_owner_id,
            )
            while True:
                try:
                    changed = await self._io.run(
                        self._repository.accept_subagent_task_terminal_settlement,
                        self._guard,
                        candidate=candidate,
                        deadline_monotonic=self._canonical_deadline(),
                    )
                except StaleHostWriter:
                    return
                except ConversationKernelConflict:
                    raise
                except _RETRYABLE_CANONICAL_ERRORS:
                    changed = False
                if changed:
                    confirmed_terminal = True
                    return
                try:
                    confirmation = await self._io.run(
                        self._repository.confirm_subagent_task_terminal_settlement,
                        candidate=candidate,
                        deadline_monotonic=self._canonical_deadline(),
                    )
                except StaleHostWriter:
                    return
                except ConversationKernelConflict:
                    raise
                except _RETRYABLE_CANONICAL_ERRORS:
                    await asyncio.sleep(0.05)
                    continue
                if confirmation is SubagentTaskTerminalConfirmationKind.FULL:
                    confirmed_terminal = True
                    return
                if confirmation is SubagentTaskTerminalConfirmationKind.CONFLICT:
                    raise ConversationKernelConflict(
                        "subagent terminal settlement has a conflicting winner"
                    )
                await asyncio.sleep(0)

        settlement = asyncio.create_task(
            worker(), name=f"kernel-subagent-task-terminal:{task_id}"
        )
        await _join_child_settlement(settlement)
        if not confirmed_terminal:
            return
        await self.offer_subagent_completion(task_id)
        await self._retire_dormant_terminal_task(task_id)

    async def _settle_cancelled_child(
        self,
        *,
        task_id: str,
        turn_id: str,
        task_status: str,
        task_reason: str,
        turn_reason: str,
        terminal_public_detail: str | None = None,
    ) -> AcceptedEntry | None:
        exact_public_detail = (
            terminal_public_detail or build_default_terminal_public_detail(task_reason)
        )
        occurred_at = datetime.now(timezone.utc)
        while True:
            try:
                confirmation = await self._io.run(
                    self._repository.confirm_cancelled_subagent_turn_and_task,
                    session_id=self._guard.session_id,
                    task_id=task_id,
                    turn_id=turn_id,
                    task_status=task_status,
                    task_reason=task_reason,
                    terminal_public_detail=exact_public_detail,
                    turn_reason=turn_reason,
                    occurred_at=occurred_at,
                    actor_id=self._host_owner_id,
                    deadline_monotonic=self._canonical_deadline(),
                )
                if confirmation.kind is TurnAdmissionConfirmationKind.FULL:
                    await self.offer_subagent_completion(task_id)
                    return None
                if (
                    confirmation.kind
                    is TurnAdmissionConfirmationKind.HISTORICAL_TERMINAL
                ):
                    accepted = confirmation.accepted
                    if accepted is None:
                        raise RuntimeError(
                            "historical child winner lacks its final entry"
                        )
                    durable = await self._io.run(
                        self._repository.query_subagent_task,
                        session_id=self._guard.session_id,
                        task_id=task_id,
                        deadline_monotonic=self._canonical_deadline(),
                    )
                    if (
                        durable is None
                        or durable.get("status") != "COMPLETED"
                        or durable.get("result_id") is None
                        or durable.get("result_entry_id") != accepted.entry_id
                    ):
                        raise ConversationKernelConflict(
                            "completed child winner lacks atomic task result lineage"
                        )
                    await self.offer_subagent_completion(task_id)
                    return accepted
                if confirmation.kind is TurnAdmissionConfirmationKind.CONFLICT:
                    raise ConversationKernelConflict(
                        "subagent cancellation winner conflicts"
                    )
            except StaleHostWriter:
                return None
            except ConversationKernelConflict:
                raise
            except _RETRYABLE_CANONICAL_ERRORS:
                # NONE and transient confirmation failure both proceed to the
                # same immutable candidate write.  Exact conflicts are never
                # retried or overwritten.
                pass
            try:
                changed = await self._io.run(
                    self._repository.settle_cancelled_subagent_turn_and_task,
                    self._guard,
                    task_id=task_id,
                    turn_id=turn_id,
                    task_status=task_status,
                    task_reason=task_reason,
                    terminal_public_detail=exact_public_detail,
                    turn_reason=turn_reason,
                    occurred_at=occurred_at,
                    actor_id=self._host_owner_id,
                    deadline_monotonic=self._canonical_deadline(),
                )
                if changed:
                    await self.offer_subagent_completion(task_id)
                    return None
                # A cancellation can win before the task-scoped turn admission.
                # The guarded task-only CAS is legal only while that exact turn
                # is still absent; if a terminal turn raced us, loop back to the
                # closed confirmation instead of overwriting its lineage.
                try:
                    await self._settle_task_terminal_exact(
                        task_id,
                        SubagentTaskStatus(task_status),
                        task_reason,
                        require_absent_turn=True,
                        public_detail=exact_public_detail,
                    )
                except ConversationKernelConflict:
                    # The exact child turn may have appeared after the joint
                    # cancellation CAS observed it absent.  Re-enter the closed
                    # joint confirmation instead of overwriting that lineage.
                    await asyncio.sleep(0)
                    continue
                return None
            except StaleHostWriter:
                return None
            except ConversationKernelConflict:
                raise
            except _RETRYABLE_CANONICAL_ERRORS:
                await asyncio.sleep(0.05)

    def _encode_list_cursor(
        self,
        *,
        after_accepted_at: datetime,
        after_task_id: str,
        seen_count: int,
        maximum_items: int,
        include_dependencies: bool,
    ) -> str:
        body = canonical_json_bytes(
            {
                "after_accepted_at": after_accepted_at.isoformat(),
                "after_task_id": after_task_id,
                "include_dependencies": include_dependencies,
                "maximum_items": maximum_items,
                "order": "accepted_at,id",
                "seen_count": seen_count,
                "session_fingerprint": "sha256:"
                + sha256(self._guard.session_id.encode("utf-8")).hexdigest(),
                "version": 1,
            }
        )
        signature = hmac.new(self._list_cursor_secret, body, sha256).digest()
        token = base64.urlsafe_b64encode(signature + body).decode("ascii").rstrip("=")
        if len(token.encode("ascii")) > _MAXIMUM_LIST_CURSOR_BYTES:
            raise RuntimeError("subagent list cursor exceeded its physical bound")
        return token

    def _decode_list_cursor(
        self,
        token: str,
        *,
        maximum_items: int,
        include_dependencies: bool,
    ) -> _SubagentListCursor:
        if not token or len(token.encode("utf-8")) > _MAXIMUM_LIST_CURSOR_BYTES:
            raise ValueError("INVALID_CURSOR")
        try:
            raw = base64.b64decode(
                token + ("=" * (-len(token) % 4)),
                altchars=b"-_",
                validate=True,
            )
        except (ValueError, UnicodeEncodeError) as exc:
            raise ValueError("INVALID_CURSOR") from exc
        if len(raw) <= 32:
            raise ValueError("INVALID_CURSOR")
        signature, body = raw[:32], raw[32:]
        if not hmac.compare_digest(
            signature, hmac.new(self._list_cursor_secret, body, sha256).digest()
        ):
            raise ValueError("STALE_CURSOR")
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("INVALID_CURSOR") from exc
        if not isinstance(payload, dict) or canonical_json_bytes(payload) != body:
            raise ValueError("INVALID_CURSOR")
        expected = {
            "include_dependencies": include_dependencies,
            "maximum_items": maximum_items,
            "order": "accepted_at,id",
            "session_fingerprint": "sha256:"
            + sha256(self._guard.session_id.encode("utf-8")).hexdigest(),
            "version": 1,
        }
        if any(payload.get(key) != value for key, value in expected.items()):
            raise ValueError("STALE_CURSOR")
        accepted_at_raw = payload.get("after_accepted_at")
        task_id = payload.get("after_task_id")
        seen_count = payload.get("seen_count")
        if (
            not isinstance(accepted_at_raw, str)
            or not isinstance(task_id, str)
            or not task_id
            or isinstance(seen_count, bool)
            or not isinstance(seen_count, int)
            or seen_count < 1
        ):
            raise ValueError("INVALID_CURSOR")
        try:
            accepted_at = datetime.fromisoformat(accepted_at_raw)
        except ValueError as exc:
            raise ValueError("INVALID_CURSOR") from exc
        if accepted_at.tzinfo is None:
            raise ValueError("INVALID_CURSOR")
        return _SubagentListCursor(accepted_at, task_id, seen_count)

    async def _list(self, arguments: Mapping[str, object]) -> KernelToolResult:
        maximum = int(arguments.get("max_items", 50))
        maximum = max(1, min(maximum, 50))
        include_dependencies = bool(arguments.get("include_dependencies", True))
        cursor: _SubagentListCursor | None = None
        raw_cursor = arguments.get("cursor")
        if raw_cursor is not None:
            assert isinstance(raw_cursor, str)
            try:
                cursor = self._decode_list_cursor(
                    raw_cursor,
                    maximum_items=maximum,
                    include_dependencies=include_dependencies,
                )
            except ValueError as exc:
                code = str(exc)
                return _result(
                    # Schema/byte validation already succeeded before the
                    # canonical tool attempt was accepted.  Signature and
                    # lineage rejection is therefore an attempted, typed
                    # application result rather than a no-attempt argument
                    # result; the public error code keeps the distinction.
                    "APPLICATION_ERROR",
                    {"error": code},
                )
        durable = await self._io.run(
            self._repository.list_subagent_tasks,
            session_id=self._guard.session_id,
            maximum_items=maximum,
            after_accepted_at=(None if cursor is None else cursor.after_accepted_at),
            after_task_id=None if cursor is None else cursor.after_task_id,
            include_lookahead=True,
            deadline_monotonic=self._canonical_deadline(),
        )
        has_more = len(durable) > maximum
        page = durable[:maximum]
        task_ids = tuple(str(item["id"]) for item in page)
        dependencies: dict[str, list[dict[str, object]]] = {
            task_id: [] for task_id in task_ids
        }
        if include_dependencies and task_ids:
            for start in range(0, len(task_ids), _DEPENDENCY_READ_PAGE_ITEMS):
                dependency_page = task_ids[start : start + _DEPENDENCY_READ_PAGE_ITEMS]
                for edge in await self._io.run(
                    self._repository.read_subagent_dependencies,
                    session_id=self._guard.session_id,
                    task_ids=dependency_page,
                    deadline_monotonic=self._canonical_deadline(),
                ):
                    dependencies[str(edge["task_id"])].append(
                        {
                            "task_id": str(edge["dependency_task_id"]),
                            "status": str(edge["status"]).lower(),
                        }
                    )
        async with self._lock:
            mailbox_counts = {
                task_id: len(self._mailboxes.get(task_id, ())) for task_id in task_ids
            }
        rows = []
        for item in page:
            task_id = str(item["id"])
            objective = str(item["objective"])
            rows.append(
                {
                    "task_id": task_id,
                    "task_key": item.get("task_key"),
                    "label": item.get("label"),
                    "profile": item.get("profile_kind"),
                    "objective_preview": _bounded_text(objective, 4096),
                    "status": str(item["status"]).lower(),
                    "pending_reason": item.get("pending_reason"),
                    "terminal_reason": item.get("terminal_reason"),
                    "terminal_public_detail": item.get("terminal_public_detail"),
                    "dependencies": dependencies[task_id],
                    "result_id": item.get("result_id"),
                    "result_source": item.get("result_source"),
                    "result_summary": item.get("result_summary"),
                    "completion_delivered": (
                        item.get("accepted_root_entry_id") is not None
                    ),
                    "pending_message_count": mailbox_counts[task_id],
                }
            )
        seen_before = 0 if cursor is None else cursor.seen_count
        total_count = seen_before if not page else int(page[0]["total_count"])
        seen_count = seen_before + len(page)
        next_cursor = None
        if has_more:
            last = page[-1]
            accepted_at = last["accepted_at"]
            if not isinstance(accepted_at, datetime):
                raise ConversationKernelConflict(
                    "subagent list row lacks its canonical ordering timestamp"
                )
            next_cursor = self._encode_list_cursor(
                after_accepted_at=accepted_at,
                after_task_id=str(last["id"]),
                seen_count=seen_count,
                maximum_items=maximum,
                include_dependencies=include_dependencies,
            )
        return _result(
            "SUCCESS",
            {
                "tasks": rows,
                "total_count": total_count,
                "page_count": len(rows),
                "omitted_count": max(0, total_count - seen_count),
                "next_cursor": next_cursor,
            },
        )

    async def _wait(
        self,
        arguments: Mapping[str, object],
        invocation_context: KernelToolInvocationContext,
    ) -> KernelToolResult:
        raw_task_ids = arguments.get("task_ids")
        task_ids = () if raw_task_ids is None else tuple(raw_task_ids)
        settle = str(arguments.get("settle", "all"))
        timeout = float(arguments.get("timeout_seconds", 30.0))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            async with self._lock:
                observed_revision = self._state_revision
                completion_pending = bool(self._root_completion_queue)
            pending_steer = bool(
                await self._io.run(
                    self._repository.read_pending_prompt_steer_facts,
                    session_id=self._guard.session_id,
                    target_turn_id=invocation_context.turn_id,
                    maximum_items=1,
                    deadline_monotonic=self._canonical_deadline(),
                )
            )
            rows = tuple(
                [
                    await self._io.run(
                        self._repository.query_subagent_task,
                        session_id=self._guard.session_id,
                        task_id=task_id,
                        deadline_monotonic=self._canonical_deadline(),
                    )
                    for task_id in task_ids
                ]
            )
            if any(row is None for row in rows):
                unknown = task_ids[rows.index(None)]
                return _result(
                    "APPLICATION_ERROR", {"error": f"unknown task: {unknown}"}
                )
            satisfied = tuple(
                str(row["id"])
                for row in rows
                if SubagentTaskStatus(str(row["status"])).terminal
            )
            pending = tuple(
                str(row["id"])
                for row in rows
                if not SubagentTaskStatus(str(row["status"])).terminal
            )
            predicate_satisfied = bool(task_ids) and (
                (settle == "first" and bool(satisfied)) or not pending
            )
            if predicate_satisfied:
                return _result(
                    "SUCCESS",
                    {
                        "outcome": "predicate_satisfied",
                        "satisfied_task_ids": list(satisfied),
                        "pending_task_ids": list(pending),
                    },
                )
            if completion_pending or pending_steer:
                return _result(
                    "SUCCESS",
                    {
                        "outcome": "input_available",
                        "satisfied_task_ids": list(satisfied),
                        "pending_task_ids": list(pending),
                    },
                )
            if not task_ids:
                _board, totals = await self._io.run(
                    self._repository.read_subagent_task_board,
                    session_id=self._guard.session_id,
                    deadline_monotonic=self._canonical_deadline(),
                )
                if (
                    sum(
                        total
                        for status, total in totals
                        if not SubagentTaskStatus(str(status)).terminal
                    )
                    == 0
                ):
                    return _result(
                        "SUCCESS",
                        {
                            "outcome": "nothing_pending",
                            "satisfied_task_ids": [],
                            "pending_task_ids": [],
                        },
                    )
            remaining = deadline - loop.time()
            if remaining <= 0:
                return _result(
                    "SUCCESS",
                    {
                        "outcome": "timeout",
                        "satisfied_task_ids": list(satisfied),
                        "pending_task_ids": list(pending),
                    },
                )
            async with self._state_changed:
                if self._state_revision != observed_revision:
                    continue
                try:
                    await asyncio.wait_for(self._state_changed.wait(), remaining)
                except TimeoutError:
                    return _result(
                        "SUCCESS",
                        {
                            "outcome": "timeout",
                            "satisfied_task_ids": list(satisfied),
                            "pending_task_ids": list(pending),
                        },
                    )

    async def _stop(self, arguments: Mapping[str, object]) -> KernelToolResult:
        task_id = str(arguments.get("task_id") or "")
        if not task_id:
            return _result("INVALID_ARGUMENTS", {"error": "task_id is required"})
        async with self._lock:
            live = self._tasks.get(task_id)
            permit = self._launch_permits.get(task_id)
            if live is None and permit is not None:
                if permit.cancellation_reason is None:
                    permit.cancellation_reason = "USER_CANCELLED"
                if not permit.launching:
                    permit.stop_claimed = True
                permit.cancellation_signal.set()
        if live is None and permit is not None:
            await asyncio.shield(permit.settled.wait())
            async with self._lock:
                live = self._tasks.get(task_id)
        if live is None:
            durable = await self._io.run(
                self._repository.query_subagent_task,
                session_id=self._guard.session_id,
                task_id=task_id,
                deadline_monotonic=self._canonical_deadline(),
            )
            if durable is None:
                return _result("APPLICATION_ERROR", {"error": "subagent is unknown"})
            status = str(durable["status"])
            if SubagentTaskStatus(status).terminal:
                return _task_status_acknowledgement(durable)
            await self._settle_task_terminal_exact(
                task_id,
                SubagentTaskStatus.CANCELLED,
                "USER_CANCELLED",
                require_absent_turn=True,
            )
            await self._settle_dependency_frontier(task_id)
            durable = await self._io.run(
                self._repository.query_subagent_task,
                session_id=self._guard.session_id,
                task_id=task_id,
                deadline_monotonic=self._canonical_deadline(),
            )
            assert durable is not None
            return _task_status_acknowledgement(durable)
        if not live.task.done():
            async with self._lock:
                current = self._tasks.get(task_id)
                if current is not None:
                    cause = current.cancellation_intent.install_cause(
                        ForegroundCancellationCause.USER_REQUEST
                    )
                    current.cancellation_reason = (
                        "USER_CANCELLED"
                        if cause is ForegroundCancellationCause.USER_REQUEST
                        else "HOST_CLOSING"
                    )
            live.task.cancel()
            await asyncio.gather(live.task, return_exceptions=True)
        if live.status == "ACTIVE":
            await self._settle_never_started_live_cancellation(
                live,
                task_status="CANCELLED",
                task_reason="USER_CANCELLED",
                turn_reason="USER_STOPPED",
                schedule_after=True,
            )
        return _result("SUCCESS", {"status": live.status.lower(), "task_id": task_id})

    async def _send_message(
        self,
        arguments: Mapping[str, object],
        invocation_context: KernelToolInvocationContext,
    ) -> KernelToolResult:
        task_id = arguments.get("task_id")
        message = arguments.get("message")
        if (
            not isinstance(task_id, str)
            or not task_id
            or not isinstance(message, str)
            or not message
            or len(message.encode("utf-8")) > 16_384
        ):
            return _result("INVALID_ARGUMENTS", {"error": "invalid message"})
        async with self._state_changed:
            live = self._tasks.get(task_id)
            mailbox = self._mailboxes.get(task_id)
            if (
                self._closed
                or live is None
                or live.status != "ACTIVE"
                or task_id in self._completing
                or mailbox is None
            ):
                return _result(
                    "TOOL_UNAVAILABLE", {"error": "target task is not active"}
                )
            duplicate = next(
                (
                    item
                    for item in mailbox
                    if item.sender_tool_attempt_id == invocation_context.attempt_id
                ),
                None,
            )
            if duplicate is not None:
                if not (
                    duplicate.sender_turn_id == invocation_context.turn_id
                    and duplicate.sender_tool_call_id == invocation_context.tool_call_id
                    and duplicate.recipient_task_id == task_id
                    and duplicate.message == message
                ):
                    raise ConversationKernelConflict(
                        "inter-agent send attempt identity conflicts"
                    )
                return _result("SUCCESS", {"status": "queued", "task_id": task_id})
            if len(mailbox) >= MAXIMUM_MAILBOX_ITEMS or (
                sum(len(item.message.encode("utf-8")) for item in mailbox)
                + len(message.encode("utf-8"))
                > MAXIMUM_MAILBOX_UTF8_BYTES
            ):
                return _result("RESOURCE_EXHAUSTED", {"error": "mailbox is full"})
            ordinal = self._mailbox_ordinals.get(task_id, 0)
            self._mailbox_ordinals[task_id] = ordinal + 1
            digest = "sha256:" + sha256(message.encode("utf-8")).hexdigest()
            entry_id = _stable_id(
                "inter-agent-entry", invocation_context.attempt_id, task_id
            )
            event_id = _stable_id(
                "inter-agent-event", invocation_context.attempt_id, task_id
            )
            mailbox.append(
                PreparedInterAgentMailboxItem(
                    session_id=invocation_context.session_id,
                    sender_turn_id=invocation_context.turn_id,
                    sender_tool_attempt_id=invocation_context.attempt_id,
                    sender_tool_call_id=invocation_context.tool_call_id,
                    recipient_task_id=task_id,
                    recipient_turn_id=live.cancellation_intent.turn_id,
                    ordinal=ordinal,
                    message=message,
                    message_digest=digest,
                    entry_id=entry_id,
                    event_id=event_id,
                )
            )
            self._notify_state_changed_locked()
        return _result("SUCCESS", {"status": "queued", "task_id": task_id})

    async def prepare_explicit_completion(
        self,
        *,
        task_id: str,
        result_entry_id: str,
        arguments: Mapping[str, object],
        last_assistant_message: str | None,
        model_id: str,
        permission_snapshot: FrozenRunPermissionSnapshot,
    ) -> PreparedExplicitSubagentCompletion | None:
        """Freeze a sole-call explicit result before canonical settlement.

        The runner has already accepted the exact assistant request and tool
        attempt.  This lock is the linearization point shared with ROOT mailbox
        admission and inferred completion.
        """

        summary = arguments.get("summary")
        preview = arguments.get("output_preview")
        diagnostics = arguments.get("diagnostics", [])
        try:
            if (
                not isinstance(summary, str)
                or not summary
                or len(summary.encode("utf-8")) > 16_384
                or (preview is not None and not isinstance(preview, str))
                or (isinstance(preview, str) and len(preview.encode("utf-8")) > 32_768)
                or not isinstance(diagnostics, list)
                or len(diagnostics) > 32
                or any(len(canonical_json_bytes(item)) > 8_192 for item in diagnostics)
                or len(canonical_json_bytes(diagnostics)) > 65_536
            ):
                return None
        except (TypeError, ValueError):
            return None
        async with self._lock:
            live = self._tasks.get(task_id)
            if (
                live is None
                or live.status != "ACTIVE"
                or self._mailboxes.get(task_id)
                or task_id in self._completing
            ):
                return None
            self._completing.add(task_id)
        try:
            continuation = await self._dispatch_subagent_stop(
                live=live,
                completion_kind="EXPLICIT",
                completion_entry_id=result_entry_id,
                last_assistant_message=last_assistant_message,
                model_id=model_id,
                permission_snapshot=permission_snapshot,
            )
        except BaseException:
            async with self._state_changed:
                self._completing.discard(task_id)
                self._notify_state_changed_locked()
            raise
        causal_ref = SubagentStopRef(task_id, "EXPLICIT", result_entry_id)
        continuation_requested = (
            continuation.decision is ContinuationDecision.CONTINUE_ONCE
        )
        permit = _CompletionPermit(
            task_id,
            continuation_requested,
            causal_ref,
            continuation.continuation_source,
            continuation.reason,
        )
        if continuation_requested:
            return PreparedExplicitSubagentCompletion(
                permit,
                None,
                _result(
                    "SUCCESS",
                    {
                        "status": "not_accepted",
                        "task_id": task_id,
                        "reason": (
                            continuation.reason
                            or "Lifecycle Hook requested one additional pass."
                        ),
                    },
                ),
            )
        result_id = _stable_child_id(task_id, result_entry_id)
        fact = build_subagent_result_public_fact(
            task_id=task_id,
            result_id=result_id,
            source=SubagentResultSource.EXPLICIT,
            producer_entry_id=result_entry_id,
            summary=summary,
            output_preview=preview,
            diagnostics=diagnostics,
        )
        acknowledgement = _result(
            "SUCCESS",
            {"status": "accepted", "task_id": task_id},
        )
        return PreparedExplicitSubagentCompletion(permit, fact, acknowledgement)

    async def consume_mailbox_safe_point(self, task_id: str) -> bool:
        """Commit the exact queued prefix before the child's next model call."""

        async with self._lock:
            items = tuple(self._mailboxes.get(task_id, ()))
            task = self._mailbox_consumptions.get(task_id)
            if task is None:
                if not items:
                    return False
                candidate = build_inter_agent_mailbox_batch(
                    items=items,
                    actor_id=self._host_owner_id,
                    occurred_at=datetime.now(timezone.utc),
                )
                task = asyncio.create_task(
                    self._consume_mailbox_safe_point_worker(candidate),
                    name=f"kernel-subagent-mailbox:{task_id}:{items[0].ordinal}",
                )
                self._mailbox_consumptions[task_id] = task
                task.add_done_callback(
                    lambda completed, exact_task_id=task_id: (
                        self._retire_mailbox_consumption(exact_task_id, completed)
                    )
                )
        cancellation: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                cancellation = exc
            except BaseException:
                break
        result = task.result()
        if cancellation is not None:
            raise cancellation
        return result

    def _retire_mailbox_consumption(
        self, task_id: str, task: asyncio.Task[bool]
    ) -> None:
        current = self._mailbox_consumptions.get(task_id)
        if current is task:
            self._mailbox_consumptions.pop(task_id, None)

    async def _consume_mailbox_safe_point_worker(
        self, candidate: PreparedInterAgentMailboxBatch
    ) -> bool:
        items = candidate.items
        while True:
            try:
                await self._io.run(
                    self._repository.accept_inter_agent_mailbox_batch,
                    self._guard,
                    candidate=candidate,
                    deadline_monotonic=self._canonical_deadline(),
                )
                break
            except (ConversationKernelConflict, StaleHostWriter, ValueError):
                raise
            except _RETRYABLE_CANONICAL_ERRORS:
                try:
                    confirmation = await self._io.run(
                        self._repository.confirm_inter_agent_mailbox_batch,
                        self._guard,
                        candidate=candidate,
                        deadline_monotonic=self._canonical_deadline(),
                    )
                except (ConversationKernelConflict, StaleHostWriter, ValueError):
                    raise
                except _RETRYABLE_CANONICAL_ERRORS:
                    await asyncio.sleep(0.05)
                    continue
                if confirmation == "FULL":
                    break
                if confirmation == "CONFLICT":
                    raise ConversationKernelConflict(
                        "inter-agent mailbox has a conflicting winner"
                    )
                await asyncio.sleep(0)
        async with self._state_changed:
            current = self._mailboxes.get(items[0].recipient_task_id)
            if current is None or tuple(current[: len(items)]) != items:
                raise ConversationKernelConflict(
                    "mailbox prefix changed before retirement"
                )
            del current[: len(items)]
            self._notify_state_changed_locked()
        return True

    async def prepare_inferred_completion(
        self,
        *,
        task_id: str,
        entry_id: str,
        public_text: str,
        model_id: str,
        permission_snapshot: FrozenRunPermissionSnapshot,
    ) -> PreparedInferredSubagentCompletion | None:
        from pulsara_agent.conversation_kernel.subagents.contracts import (
            SubagentResultSource,
            build_subagent_result_public_fact,
        )

        async with self._lock:
            live = self._tasks.get(task_id)
            if (
                live is None
                or live.status != "ACTIVE"
                or self._mailboxes.get(task_id)
                or task_id in self._completing
            ):
                return None
            self._completing.add(task_id)
        try:
            continuation = await self._dispatch_subagent_stop(
                live=live,
                completion_kind="INFERRED",
                completion_entry_id=entry_id,
                last_assistant_message=public_text,
                model_id=model_id,
                permission_snapshot=permission_snapshot,
            )
        except BaseException:
            async with self._state_changed:
                self._completing.discard(task_id)
                self._notify_state_changed_locked()
            raise
        causal_ref = SubagentStopRef(task_id, "INFERRED", entry_id)
        continuation_requested = (
            continuation.decision is ContinuationDecision.CONTINUE_ONCE
        )
        permit = _CompletionPermit(
            task_id,
            continuation_requested,
            causal_ref,
            continuation.continuation_source,
            continuation.reason,
        )
        if continuation_requested:
            return PreparedInferredSubagentCompletion(permit, None)
        result_id = _stable_child_id(task_id, entry_id)
        summary = _bounded_text(
            public_text or "Task completed without public text.", 16_384
        )
        fact = build_subagent_result_public_fact(
            task_id=task_id,
            result_id=result_id,
            source=SubagentResultSource.INFERRED,
            producer_entry_id=entry_id,
            summary=summary,
            source_assistant_content_digest=(
                "sha256:" + sha256(public_text.encode("utf-8")).hexdigest()
            ),
        )
        return PreparedInferredSubagentCompletion(permit, fact)

    async def _dispatch_subagent_stop(
        self,
        *,
        live: _LiveTask,
        completion_kind: str,
        completion_entry_id: str,
        last_assistant_message: str | None,
        model_id: str,
        permission_snapshot: FrozenRunPermissionSnapshot,
    ) -> ContinuationOutcome:
        launch = live.launch
        if (
            not model_id
            or permission_snapshot.inherited_from_turn_id
            != launch.task_start.parent_turn_id
            or permission_snapshot.effective_mode
            is not launch.parent_permission_snapshot.effective_mode
        ):
            raise ConversationKernelConflict(
                "SubagentStop carrier drifted from child launch truth"
            )
        dispatcher = self._hook_dispatcher
        scope = live.hook_scope
        if dispatcher is None or scope is None:
            return ContinuationOutcome()
        public_input = SubagentStopInput(
            session_id=self._guard.session_id,
            cwd=str(self._terminal_cwd()),
            model=model_id,
            turn_id=launch.child_turn_id,
            agent_id=launch.task_start.task_id,
            agent_type=launch.task_start.profile.value,
            stop_hook_active=live.completion_continuation_used,
            last_assistant_message=last_assistant_message,
            permission_mode=external_permission_mode(
                permission_snapshot.effective_mode.value,
                active_plan_workflow=(permission_snapshot.plan_workflow_id is not None),
            ),
        )
        causal_ref = SubagentStopRef(
            live.task_id,
            completion_kind,
            completion_entry_id,
        )
        return await dispatcher.dispatch(
            HookDispatchEnvelope(
                dispatcher.capture_view(),
                scope,
                public_input,
                causal_ref,
                self._canonical_deadline(),
            ),
            matcher_subject=event_matcher_subject(
                public_input.event_type,
                agent_type=launch.task_start.profile.value,
            ),
            continuation_already_used=live.completion_continuation_used,
        )

    async def finish_completion(
        self, permit: _CompletionPermit, *, committed: bool
    ) -> None:
        continuation: (
            tuple[HookDispatchScopeRef, SubagentStopRef, HookContextEntry, str | None]
            | None
        ) = None
        terminal_scope: HookDispatchScopeRef | None = None
        async with self._state_changed:
            if permit.task_id not in self._completing:
                raise ConversationKernelConflict("subagent completion permit is stale")
            self._completing.remove(permit.task_id)
            live = self._tasks.get(permit.task_id)
            if committed and live is not None and live.status == "ACTIVE":
                if permit.continuation_requested:
                    if (
                        live.completion_continuation_used
                        or live.hook_scope is None
                        or permit.continuation_source is None
                    ):
                        raise ConversationKernelConflict(
                            "SubagentStop continuation permit is stale"
                        )
                    live.completion_continuation_used = True
                    continuation = (
                        live.hook_scope,
                        permit.causal_ref,
                        permit.continuation_source,
                        permit.continuation_reason,
                    )
                else:
                    live.status = "COMPLETED"
                    terminal_scope = live.hook_scope
            self._notify_state_changed_locked()
        if terminal_scope is not None and self._hook_context_owner is not None:
            self._hook_context_owner.retire_scope(terminal_scope)
        if continuation is not None:
            scope, causal_ref, source, reason = continuation
            assert self._hook_context_owner is not None
            self._hook_context_owner.accept_continuation(
                scope=scope,
                causal_ref=causal_ref,
                source_entry=source,
                reason=reason,
            )

    async def _settle_dependency_frontier(
        self, task_id: str, *, schedule_after: bool = True
    ) -> None:
        occurred_at = datetime.now(timezone.utc)
        causal_candidates = await self._dependency_frontier_candidate_ids(task_id)

        async def worker() -> None:
            ack_unknown = False
            changed: tuple[tuple[str, str], ...] = ()
            while True:
                try:
                    changed = await self._io.run(
                        self._repository.settle_subagent_dependency_frontier,
                        self._guard,
                        terminal_task_id=task_id,
                        occurred_at=occurred_at,
                        actor_id=self._host_owner_id,
                        deadline_monotonic=self._canonical_deadline(),
                    )
                    break
                except StaleHostWriter:
                    return
                except ConversationKernelConflict:
                    raise
                except _RETRYABLE_CANONICAL_ERRORS:
                    # The whole frontier update is one transaction.  Reissuing
                    # is safe after ACK-unknown: an already-committed frontier
                    # has no remaining WAITING rows, while an absent winner is
                    # recomputed from the same terminal dependency facts.
                    ack_unknown = True
                    await asyncio.sleep(0.05)
            terminal_ids = tuple(
                changed_task_id
                for changed_task_id, status in changed
                if SubagentTaskStatus(status).terminal
            )
            for changed_task_id in terminal_ids:
                await self.offer_subagent_completion(changed_task_id)
            async with self._state_changed:
                retired = False
                for changed_task_id in terminal_ids:
                    retired = (
                        self._retire_dormant_carriers_locked(changed_task_id) or retired
                    )
                if retired:
                    self._notify_state_changed_locked()
            if ack_unknown:
                # A committed first attempt may lose its return rows.  The
                # retry then observes an empty frontier, so confirm only the
                # descendant closure causally reachable from this settlement.
                await self._retire_canonical_terminal_dormant_tasks(causal_candidates)
            async with self._state_changed:
                self._notify_state_changed_locked()
            # TODO child ownership is the same four-slot physical resource as
            # live execution.  Release it before recursively admitting work
            # from the newly opened dependency frontier.
            await self._close_todo_child_run(task_id)
            if schedule_after:
                await self._start_available_tasks()

        settlement = asyncio.create_task(
            worker(), name=f"kernel-subagent-dependency-frontier:{task_id}"
        )
        await _join_child_settlement(settlement)

    async def aclose(self, *, deadline_monotonic: float) -> None:
        close_error: BaseException | None = None
        close_deadline_expired = monotonic() >= deadline_monotonic
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            for permit in self._launch_permits.values():
                if permit.cancellation_reason is None:
                    permit.cancellation_reason = "HOST_CLOSING"
                if not permit.launching:
                    permit.stop_claimed = True
                permit.cancellation_signal.set()
            admissions = tuple(
                attempt.task for attempt in self._batch_admissions.values()
            )
            mailbox_consumptions = tuple(self._mailbox_consumptions.values())
            scheduler = self._scheduler_task
        producer_tasks = tuple(
            dict.fromkeys(
                (
                    *admissions,
                    *mailbox_consumptions,
                    *((scheduler,) if scheduler is not None else ()),
                )
            )
        )
        if producer_tasks:
            expired, error = await _cancel_and_join_at_absolute_deadline(
                producer_tasks,
                deadline_monotonic=deadline_monotonic,
            )
            close_deadline_expired = close_deadline_expired or expired
            close_error = error
        async with self._lock:
            tasks = tuple(
                item.task for item in self._tasks.values() if not item.task.done()
            )
            for item in self._tasks.values():
                if not item.task.done():
                    self._todo_owner.mark_closing(
                        scope_kind=ModelInputScopeKind.SUBAGENT_TASK,
                        scope_subagent_task_id=item.task_id,
                    )
                    cause = item.cancellation_intent.install_cause(
                        ForegroundCancellationCause.HOST_SESSION_CLOSE
                    )
                    item.cancellation_reason = (
                        "USER_CANCELLED"
                        if cause is ForegroundCancellationCause.USER_REQUEST
                        else "HOST_CLOSING"
                    )
        for task in tasks:
            task.cancel()
        if tasks:
            expired, error = await _cancel_and_join_at_absolute_deadline(
                tasks,
                deadline_monotonic=deadline_monotonic,
            )
            close_deadline_expired = close_deadline_expired or expired
            close_error = close_error or error
        # asyncio can cancel a newly-created Task before its coroutine body
        # executes, in which case _run_child() never observes CancelledError.
        # The Host owner still has to install the frozen close disposition for
        # every accepted ACTIVE task.
        async with self._lock:
            unterminalized = tuple(
                item for item in self._tasks.values() if item.status == "ACTIVE"
            )
        for item in unterminalized:
            await self._settle_never_started_live_cancellation(
                item,
                task_status="INTERRUPTED",
                task_reason="HOST_CLOSING",
                turn_reason="SESSION_CLOSED",
                schedule_after=False,
            )
        async with self._lock:
            dormant_ids = tuple(
                task_id
                for task_id in self._start_materials
                if task_id not in self._tasks
            )
        for task_id in dormant_ids:
            while True:
                try:
                    dormant = await self._io.run(
                        self._repository.query_subagent_task,
                        session_id=self._guard.session_id,
                        task_id=task_id,
                        deadline_monotonic=self._canonical_deadline(),
                    )
                    break
                except StaleHostWriter:
                    dormant = None
                    break
                except _RETRYABLE_CANONICAL_ERRORS:
                    await asyncio.sleep(0.05)
            if dormant is None or SubagentTaskStatus(str(dormant["status"])).terminal:
                continue
            await self._settle_cancelled_child(
                task_id=task_id,
                turn_id=stable_subagent_turn_id(
                    session_id=self._guard.session_id,
                    task_id=task_id,
                ),
                task_status="INTERRUPTED",
                task_reason="HOST_CLOSING",
                turn_reason="SESSION_CLOSED",
            )
        async with self._state_changed:
            self._tasks.clear()
            self._mailboxes.clear()
            self._mailbox_ordinals.clear()
            self._mailbox_consumptions.clear()
            self._start_materials.clear()
            self._launch_permits.clear()
            self._completing.clear()
            self._root_completion_queue.clear()
            self._root_completion_set.clear()
            self._root_completion_turn_id = None
            self._root_completion_delivery_open = False
            self._notify_state_changed_locked()
        if close_deadline_expired:
            raise TimeoutError("subagent owner exited after close deadline")
        if close_error is not None:
            raise close_error

    async def _settle_never_started_live_cancellation(
        self,
        live: _LiveTask,
        *,
        task_status: str,
        task_reason: str,
        turn_reason: str,
        schedule_after: bool,
    ) -> None:
        """Settle a child cancelled before ``_run_child`` entered its body."""

        if not live.task.done() or live.status != "ACTIVE":
            raise RuntimeError("pre-start child cancellation carrier is not terminal")
        historical = await self._settle_cancelled_child(
            task_id=live.task_id,
            turn_id=live.cancellation_intent.turn_id,
            task_status=task_status,
            task_reason=task_reason,
            turn_reason=turn_reason,
        )
        final_status = "COMPLETED" if historical is not None else task_status
        summary = (
            "Subagent completed before cancellation"
            if historical is not None
            else task_reason
        )
        async with self._state_changed:
            current = self._tasks.get(live.task_id)
            if current is not live or current.status != "ACTIVE":
                raise ConversationKernelConflict(
                    "pre-start child cancellation carrier changed"
                )
            live.status = final_status
            self._tasks.pop(live.task_id)
            self._retire_dormant_carriers_locked(live.task_id)
            self._notify_state_changed_locked()
        self._offer_progress(
            live.task_id,
            live.parent_turn_id,
            final_status,
            summary,
        )
        await self._settle_dependency_frontier(
            live.task_id, schedule_after=schedule_after
        )
        await self._close_todo_child_run(live.task_id)
        if live.hook_scope is not None and self._hook_context_owner is not None:
            self._hook_context_owner.retire_scope(live.hook_scope)

    def _offer_progress(
        self, task_id: str, parent_turn_id: str, status: str, summary: str
    ) -> None:
        public = _bounded_text(summary, 4096)
        self._live_bus.offer_nowait(
            event_type=LiveEventType.SUBAGENT_PROGRESS,
            session_id=self._guard.session_id,
            turn_id=parent_turn_id,
            draft_identity=task_id,
            payload=SubagentProgressPayload(
                task_id,
                status,
                public,
                len(public.encode("utf-8")),
                live_digest(public),
            ),
            scope_kind="SUBAGENT_TASK",
            scope_subagent_task_id=task_id,
            channel_kind=LiveChannelKind.SUBAGENT_EXTENSION,
            generation_id=f"subagent:{task_id}",
            block_id=task_id,
            block_ordinal=0,
            block_kind=LiveBlockKind.OPERATIONAL,
        )


def _result(
    state: str,
    value: Mapping[str, object],
    *,
    remote_identity: str | None = None,
) -> KernelToolResult:
    return KernelToolResult(
        state=state,
        content=json.dumps(dict(value), ensure_ascii=False, sort_keys=True).encode(),
        remote_identity=remote_identity,
    )


def _stable_child_id(task_id: str, entry_id: str) -> str:
    return "subagent-result:" + sha256(f"{task_id}:{entry_id}".encode()).hexdigest()


def _stable_id(namespace: str, *parts: str) -> str:
    digest = sha256()
    digest.update(f"pulsara:round10:{namespace}:v1\0".encode())
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    return f"{namespace}:" + digest.hexdigest()


def _parse_context(value: object) -> tuple[SubagentContextMode, int | None]:
    if not isinstance(value, Mapping) or set(value) - {"mode", "turns"}:
        raise ValueError("context must be a closed object")
    raw_mode = value.get("mode", "none")
    if raw_mode == "none":
        if value.get("turns") is not None:
            raise ValueError("NONE context cannot specify turns")
        return SubagentContextMode.NONE, None
    if raw_mode != "last_n":
        raise ValueError("context mode is invalid")
    turns = value.get("turns")
    if not isinstance(turns, int) or isinstance(turns, bool) or not 1 <= turns <= 3:
        raise ValueError("LAST_N context requires 1..3 turns")
    return SubagentContextMode.LAST_N, turns


def _bounded_nonempty_text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be non-empty text")
    if len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{field} exceeds its UTF-8 bound")
    return value


def _bounded_timeout(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("timeout_seconds must be a number")
    timeout = float(value)
    if not math.isfinite(timeout) or not 0 <= timeout <= 300:
        raise ValueError("timeout_seconds is out of bounds")
    return timeout


def _validate_task_definition(value: object) -> None:
    if not isinstance(value, Mapping):
        raise ValueError("task item must be an object")
    allowed = {
        "task",
        "task_key",
        "label",
        "profile",
        "display_role",
        "context",
        "depends_on",
    }
    if set(value) - allowed:
        raise ValueError("task item has unknown fields")
    _bounded_nonempty_text(value.get("task"), "task", MAXIMUM_TASK_OBJECTIVE_UTF8_BYTES)
    task_key = value.get("task_key")
    if task_key is not None and (
        not isinstance(task_key, str)
        or re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", task_key) is None
    ):
        raise ValueError("task_key is invalid")
    for field in ("label", "display_role"):
        item = value.get(field)
        if item is not None:
            _bounded_nonempty_text(item, field, 256)
    try:
        SubagentProfileKind(str(value.get("profile", "general_worker")))
    except ValueError as exc:
        raise ValueError("profile is invalid") from exc
    _parse_context(value.get("context", {"mode": "none"}))
    dependencies = value.get("depends_on", [])
    if (
        not isinstance(dependencies, list)
        or not len(dependencies) <= 16
        or any(
            not isinstance(item, str) or not item or len(item.encode("utf-8")) > 1024
            for item in dependencies
        )
        or len(set(dependencies)) != len(dependencies)
    ):
        raise ValueError("depends_on is invalid")


def _validate_subagent_tool_arguments(
    tool_name: str,
    arguments: Mapping[str, object],
) -> None:
    """Enforce byte and cross-field contracts the public schema cannot express."""

    if tool_name not in SUBAGENT_TOOL_NAMES:
        raise ValueError("unknown subagent tool")
    if tool_name == "spawn_agent":
        if set(arguments) - {"task", "task_name", "profile", "context"}:
            raise ValueError("spawn request has unknown fields")
        task_name = arguments.get("task_name")
        normalized = {
            "task": arguments.get("task"),
            "task_key": task_name,
            "label": task_name,
            "profile": arguments.get("profile", "general_worker"),
            "context": arguments.get("context", {"mode": "none"}),
            "depends_on": [],
        }
        _validate_task_definition(normalized)
        return
    if tool_name == "create_agent_tasks":
        if set(arguments) != {"tasks"}:
            raise ValueError("task batch must contain only tasks")
        tasks = arguments.get("tasks")
        if not isinstance(tasks, list) or not 1 <= len(tasks) <= 16:
            raise ValueError("task batch must contain 1..16 tasks")
        for item in tasks:
            _validate_task_definition(item)
        keys = [
            item.get("task_key")
            for item in tasks
            if isinstance(item, Mapping) and item.get("task_key") is not None
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("task_key is duplicated")
        if (
            sum(
                len(item.get("depends_on", []))
                for item in tasks
                if isinstance(item, Mapping)
            )
            > 64
        ):
            raise ValueError("task batch exceeds edge bound")
        return
    if tool_name == "list_agents":
        if set(arguments) - {"max_items", "include_dependencies", "cursor"}:
            raise ValueError("list request has unknown fields")
        maximum = arguments.get("max_items", 50)
        if (
            isinstance(maximum, bool)
            or not isinstance(maximum, int)
            or not 1 <= maximum <= 50
        ):
            raise ValueError("max_items must be an integer from 1 to 50")
        include = arguments.get("include_dependencies", True)
        if not isinstance(include, bool):
            raise ValueError("include_dependencies must be boolean")
        cursor = arguments.get("cursor")
        if cursor is not None:
            _bounded_nonempty_text(cursor, "cursor", _MAXIMUM_LIST_CURSOR_BYTES)
        return
    if tool_name == "wait_agent":
        if set(arguments) - {"task_ids", "settle", "timeout_seconds"}:
            raise ValueError("wait request has unknown fields")
        task_ids = arguments.get("task_ids")
        if task_ids is not None:
            if (
                not isinstance(task_ids, list)
                or not 1 <= len(task_ids) <= 16
                or any(
                    not isinstance(item, str)
                    or not item
                    or len(item.encode("utf-8")) > 512
                    for item in task_ids
                )
                or len(set(task_ids)) != len(task_ids)
            ):
                raise ValueError("task_ids must contain 1..16 unique identities")
        elif "settle" in arguments:
            raise ValueError("settle requires task_ids")
        if arguments.get("settle", "all") not in {"first", "all"}:
            raise ValueError("settle must be first or all")
        _bounded_timeout(arguments.get("timeout_seconds", 30.0))
        return
    if tool_name == "stop_agent":
        if set(arguments) - {"task_id", "reason"}:
            raise ValueError("stop request has unknown fields")
        _bounded_nonempty_text(arguments.get("task_id"), "task_id", 512)
        reason = arguments.get("reason")
        if reason is not None:
            _bounded_nonempty_text(reason, "reason", 4096)
        return
    if tool_name == "send_agent_message":
        if set(arguments) != {"task_id", "message"}:
            raise ValueError("message request must contain task_id and message")
        _bounded_nonempty_text(arguments.get("task_id"), "task_id", 512)
        _bounded_nonempty_text(
            arguments.get("message"),
            "message",
            MAXIMUM_INTER_AGENT_MESSAGE_UTF8_BYTES,
        )
        return
    if set(arguments) - {"summary", "output_preview", "diagnostics"}:
        raise ValueError("result request has unknown fields")
    _bounded_nonempty_text(
        arguments.get("summary"), "summary", MAXIMUM_RESULT_SUMMARY_UTF8_BYTES
    )
    preview = arguments.get("output_preview")
    if preview is not None:
        if not isinstance(preview, str):
            raise ValueError("output_preview must be text")
        if len(preview.encode("utf-8")) > MAXIMUM_RESULT_OUTPUT_PREVIEW_UTF8_BYTES:
            raise ValueError("output_preview exceeds its UTF-8 bound")
    diagnostics = arguments.get("diagnostics", [])
    if (
        not isinstance(diagnostics, list)
        or len(diagnostics) > MAXIMUM_RESULT_DIAGNOSTICS_ITEMS
    ):
        raise ValueError("diagnostics exceeds its item bound")
    try:
        if any(not isinstance(item, Mapping) for item in diagnostics):
            raise ValueError("diagnostics items must be objects")
        if any(len(canonical_json_bytes(item)) > 8192 for item in diagnostics):
            raise ValueError("diagnostic item exceeds its UTF-8 bound")
        if (
            len(canonical_json_bytes(diagnostics))
            > MAXIMUM_RESULT_DIAGNOSTICS_UTF8_BYTES
        ):
            raise ValueError("diagnostics exceeds its aggregate UTF-8 bound")
    except TypeError as exc:
        raise ValueError("diagnostics must contain JSON values") from exc


def _require_acyclic(drafts: list[PreparedSubagentTaskDraft]) -> None:
    graph = {
        item.task_id: tuple(
            dependency
            for dependency in item.dependency_task_ids
            if dependency in {draft.task_id for draft in drafts}
        )
        for item in drafts
    }
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visiting:
            raise ValueError("task batch contains a dependency cycle")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in graph[task_id]:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in graph:
        visit(task_id)


def _bounded_text(value: str, maximum_bytes: int) -> str:
    raw = value.encode("utf-8")
    if len(raw) <= maximum_bytes:
        return value
    marker = "\n… omitted …\n".encode("utf-8")
    budget = maximum_bytes - len(marker)
    head = raw[: budget // 2]
    tail = raw[-(budget - len(head)) :]
    while head:
        try:
            left = head.decode("utf-8")
            break
        except UnicodeDecodeError:
            head = head[:-1]
    else:
        left = ""
    while tail:
        try:
            right = tail.decode("utf-8")
            break
        except UnicodeDecodeError:
            tail = tail[1:]
    else:
        right = ""
    return left + marker.decode("utf-8") + right


def _task_status_acknowledgement(
    row: Mapping[str, object],
) -> KernelToolResult:
    return _result(
        "SUCCESS",
        {
            "task_id": str(row["id"]),
            "status": str(row["status"]).lower(),
        },
    )


async def _join_child_settlement(
    task: asyncio.Task[AcceptedEntry | None] | asyncio.Task[None],
) -> AcceptedEntry | None:
    """Join one physical settlement despite repeated waiter cancellation."""

    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()
            # The child task already records the caller's cancellation cause.
            # Additional stop/close calls only detach their waiter and cannot
            # cancel or replace the exact settlement owner.
            continue


async def _cancel_and_join_at_absolute_deadline(
    tasks: tuple[asyncio.Task[object], ...],
    *,
    deadline_monotonic: float,
) -> tuple[bool, BaseException | None]:
    """Select the logical close outcome once, then retain physical ownership."""

    pending = {task for task in tasks if not task.done()}
    waiter_cancellation: asyncio.CancelledError | None = None
    while pending:
        remaining = deadline_monotonic - monotonic()
        if remaining <= 0:
            break
        try:
            done, pending = await asyncio.wait(pending, timeout=remaining)
        except asyncio.CancelledError as exc:
            waiter_cancellation = waiter_cancellation or exc
            continue
        if not done:
            break
    deadline_expired = bool(pending)
    for task in pending:
        task.cancel()
    for task in tasks:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                if task.done():
                    break
                waiter_cancellation = waiter_cancellation or exc
            except BaseException:
                break
    task_error: BaseException | None = None
    for task in tasks:
        if task.cancelled():
            continue
        try:
            task.result()
        except BaseException as exc:
            task_error = task_error or exc
    return deadline_expired, waiter_cancellation or task_error


__all__ = ["KernelSubagentManager", "MAXIMUM_LIVE_SUBAGENTS", "SUBAGENT_TOOL_NAMES"]
