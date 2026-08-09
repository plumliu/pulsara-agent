"""Production Host composition for the Stage 2 conversation kernel.

This composition deliberately has no EventLog, RuntimeSession, presentation
foundation, Oxigraph, durable terminal monitor, or execution-replay owner.
Resume acquires a new writer generation and rehydrates canonical rows only.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from time import monotonic
from uuid import uuid4

from psycopg import IsolationLevel
from psycopg.rows import dict_row

from pulsara_agent.conversation_kernel.direct_model import DirectKernelModelPort
from pulsara_agent.conversation_kernel.activation import (
    require_stage2_runtime_privilege_boundary,
)
from pulsara_agent.conversation_kernel.blob import (
    CanonicalContentPublisher,
    PostgresCanonicalBlobStore,
)
from pulsara_agent.conversation_kernel.capability import KernelCapabilityComposer
from pulsara_agent.conversation_kernel.contracts import (
    PromptDeliveryMode,
    StoredCommittedEvent,
    WriterLease,
)
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.extensions import (
    ExtensionPlane,
    ExtensionPrincipal,
    ExtensionRegistrationLease,
    ExtensionRegistrationRequest,
    KernelExtensionHost,
    PostCommitHookOffer,
)
from pulsara_agent.conversation_kernel.jobs import KernelDurableJobExecutor
from pulsara_agent.conversation_kernel.interaction import KernelInteractionCoordinator
from pulsara_agent.conversation_kernel.limits import STAGE2_LIMITS
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.live_control import SessionLiveControlOwner
from pulsara_agent.conversation_kernel.memory_tools import KernelMemoryToolPort
from pulsara_agent.conversation_kernel.query import CanonicalConversationQuery
from pulsara_agent.conversation_kernel.repository import (
    ConversationKernelConflict,
    ConversationKernelRepository,
)
from pulsara_agent.conversation_kernel.runner import (
    ConversationKernelRunner,
    KernelRunResult,
)
from pulsara_agent.conversation_kernel.subagent import KernelSubagentManager
from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
from pulsara_agent.conversation_kernel.tool_policy import (
    DefaultToolDispatchAuthorizationPolicy,
)
from pulsara_agent.workspace_identity import (
    HostWorkspaceInput,
    ResolvedWorkspace,
    resolve_workspace,
)
from pulsara_agent.llm.models import ModelRole
from pulsara_agent.runtime.permission import (
    EffectivePermissionPolicy,
    default_permission_policy,
)
from pulsara_agent.ports.system_prompt import DEFAULT_SYSTEM_PROMPT
from pulsara_agent.runtime.mcp.store import load_mcp_server_configs
from pulsara_agent.settings import PulsaraSettings
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from pulsara_agent.storage.schema_verification_service import (
    VerifiedPostgresAccessLease,
    process_postgres_schema_verification_service,
)


WRITER_LEASE_SECONDS = 30.0
WRITER_RENEW_INTERVAL_SECONDS = 10.0
HOST_CLOSE_SECONDS = STAGE2_LIMITS.host_close_hard_ms / 1000
MAXIMUM_PROMPT_BYTES = STAGE2_LIMITS.prompt_hard_bytes


@dataclass(frozen=True, slots=True)
class KernelSessionSummary:
    session_id: str
    workspace_id: str
    lifecycle: str
    writer_generation: int
    latest_entry_sequence: int
    updated_at: datetime

    @property
    def runtime_session_id(self) -> str:
        return self.session_id

    def to_dict(self) -> dict[str, object]:
        return {
            "runtime_session_id": self.session_id,
            "workspace_id": self.workspace_id,
            "lifecycle": self.lifecycle,
            "writer_generation": self.writer_generation,
            "latest_entry_sequence": self.latest_entry_sequence,
            "updated_at": self.updated_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class KernelCommandOutcome:
    command_id: str
    status: str
    target_id: str
    public_code: str
    public_message: str


class KernelCompositionUnavailable(ValueError):
    pass


class KernelHostSession:
    def __init__(
        self,
        *,
        settings: PulsaraSettings,
        workspace: ResolvedWorkspace,
        repository: ConversationKernelRepository,
        writer_lease: WriterLease,
        io_owner: KernelSessionIO,
        session_id: str,
        host_session_id: str,
        permission_policy: EffectivePermissionPolicy,
        model_role: ModelRole,
        system_prompt: str | None,
        active_skill_names: frozenset[str],
        authenticated_first_party_extension_ids: frozenset[str],
    ) -> None:
        self.settings = settings
        self.workspace = workspace
        self.repository = repository
        self.runtime_session_id = session_id
        self.session_id = session_id
        self.host_session_id = host_session_id
        self.live_bus = LiveAgentEventBus()
        self.extensions = KernelExtensionHost(
            session_id=session_id,
            authenticated_first_party_principal_ids=(
                authenticated_first_party_extension_ids
            ),
        )
        self.live_bus.bind_extension_tap(self.extensions.offer_live_nowait)
        self.query = CanonicalConversationQuery(repository.connection_provider)
        self._content_publisher = CanonicalContentPublisher(
            repository.connection_provider
        )
        self._io = io_owner
        self._lease = writer_lease
        self.live_control = SessionLiveControlOwner(
            session_id=session_id,
            owner_epoch=self._lease.guard.writer_generation,
        )
        self._interactions = KernelInteractionCoordinator(
            repository=repository,
            guard=self._lease.guard,
            live_control=self.live_control,
            live_bus=self.live_bus,
            io_owner=self._io,
        )
        self._tools = DirectKernelToolPort(
            workspace_root=workspace.workspace_root,
            host_owner_id=host_session_id,
            session_id=session_id,
            live_bus=self.live_bus,
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(
                permission_policy
            ),
        )
        self._tools.bind_interaction_port(self._interactions)
        self._subagents = KernelSubagentManager(
            repository=repository,
            guard=self._lease.guard,
            host_owner_id=host_session_id,
            io_owner=self._io,
            live_bus=self.live_bus,
        )
        self._tools.bind_subagent_port(self._subagents)
        self._memory_tools = KernelMemoryToolPort(
            repository=repository,
            workspace_id=workspace.workspace_key,
            embedding_config=settings.retrieval.embedding,
            io_owner=self._io,
        )
        self._tools.bind_memory_port(self._memory_tools)
        self._capabilities = KernelCapabilityComposer(
            workspace_root=workspace.workspace_root,
            workspace_kind=workspace.workspace_kind,
            memory_domain=workspace.memory_domain,
            available_tool_names=frozenset(spec.name for spec in self._tools.tool_specs),
            configured_active_skill_names=active_skill_names,
            base_system_prompt=system_prompt or DEFAULT_SYSTEM_PROMPT,
        )
        self._model = DirectKernelModelPort(
            config=settings.llm,
            tools=self._tools.tool_specs,
            system_prompt=system_prompt or DEFAULT_SYSTEM_PROMPT,
            role=model_role,
        )
        self._runner = ConversationKernelRunner(
            repository=repository,
            writer_lease=self._lease,
            model=self._model,
            tools=self._tools,
            live_bus=self.live_bus,
            io_owner=self._io,
            capability_composer=self._capabilities,
            extensions=self.extensions,
            steer_consumer=self._consume_pending_steers,
        )
        self._subagents.bind_runner_factory(self._new_child_runner)
        self._active_task: asyncio.Task[KernelRunResult] | None = None
        self._active_turn_id: str | None = None
        self._active_command_id: str | None = None
        self._command_failures: dict[str, KernelCommandOutcome] = {}
        self._lock = asyncio.Lock()
        self._closing = False
        self._queue_wake = asyncio.Event()
        self._renewal_task = asyncio.create_task(
            self._renew_writer(), name=f"kernel-writer-renew:{session_id}"
        )
        self._delivery_task = asyncio.create_task(
            self._prompt_delivery_loop(),
            name=f"kernel-prompt-delivery:{session_id}",
        )
        self._queue_wake.set()

    @property
    def writer_generation(self) -> int:
        return self._lease.guard.writer_generation

    @property
    def active_skill_names(self) -> frozenset[str]:
        return self._capabilities.configured_active_skill_names

    async def register_extension(
        self, request: ExtensionRegistrationRequest
    ) -> ExtensionRegistrationLease:
        self._require_open()
        if request.plane is ExtensionPlane.LIVE:
            generation, revision = self.live_bus.current_cut()
        else:
            generation, revision = self.extensions.current_cut(request.plane)
        return self.extensions.register(
            request,
            registration_cut_generation=generation,
            registration_cut_revision=revision,
        )

    def authenticate_extension_principal(
        self, *, extension_principal_id: str
    ) -> ExtensionPrincipal:
        self._require_open()
        return self.extensions.authenticate_principal(
            extension_principal_id=extension_principal_id,
        )

    async def run_turn(
        self,
        text: str,
        *,
        command_id: str | None = None,
    ) -> KernelRunResult:
        _validate_prompt(text)
        command = command_id or f"command:{uuid4().hex}"
        turn_id = _stable_id("turn", self.session_id, command)
        existing = await self._query_command_row(command)
        if existing is not None:
            raise RuntimeError("command was already accepted; query its outcome")
        async with self._lock:
            self._require_open()
            if self._active_task is not None and not self._active_task.done():
                raise RuntimeError("a canonical ROOT turn is already running")
            task = asyncio.create_task(
                self._runner.run_turn(text, command_id=command),
                name=f"kernel-turn:{command}",
            )
            self._active_task = task
            self._active_turn_id = turn_id
            self._active_command_id = command
        try:
            return await task
        finally:
            async with self._lock:
                if self._active_task is task:
                    self._active_task = None
                    self._active_turn_id = None
                    self._active_command_id = None

    async def submit_prompt(
        self,
        *,
        command_id: str,
        text: str,
        delivery_mode: PromptDeliveryMode = PromptDeliveryMode.NEW_TURN,
        target_turn_id: str | None = None,
    ) -> KernelCommandOutcome:
        if not command_id:
            return KernelCommandOutcome(
                command_id, "REJECTED", "", "INVALID_REQUEST", "Command is invalid."
            )
        try:
            _validate_prompt(text)
        except ValueError:
            return KernelCommandOutcome(
                command_id, "REJECTED", "", "INVALID_PROMPT", "Prompt is invalid."
            )
        existing = await self.query_command(command_id)
        if existing is not None:
            return existing
        self._require_open()
        if (delivery_mode is PromptDeliveryMode.NEW_TURN) != (
            target_turn_id is None
        ):
            return KernelCommandOutcome(
                command_id,
                "REJECTED",
                "",
                "INVALID_STEER_TARGET",
                "Prompt delivery target is invalid.",
            )
        queue_item_id = _stable_id("queue-item", self.session_id, command_id)
        deadline = monotonic() + 10.0
        content = await self._io.run(
            self._content_publisher.materialize,
            session_id=self.session_id,
            content=text.encode("utf-8"),
            media_type="text/plain",
            codec="utf-8",
            deadline_monotonic=deadline,
        )
        try:
            queue_sequence = await self._io.run(
                self.repository.enqueue_prompt,
                self._lease.guard,
                command_id=command_id,
                queue_item_id=queue_item_id,
                client_submission_id=command_id,
                delivery_mode=delivery_mode,
                target_turn_id=target_turn_id,
                content=content,
                occurred_at=datetime.now().astimezone(),
                actor_id=self.host_session_id,
                deadline_monotonic=deadline,
            )
        except ConversationKernelConflict:
            if delivery_mode is PromptDeliveryMode.STEER_ACTIVE_TURN:
                return KernelCommandOutcome(
                    command_id,
                    "REJECTED",
                    target_turn_id or "",
                    "STEER_TARGET_STALE",
                    "The target turn no longer accepts steering.",
                )
            raise
        self._queue_wake.set()
        return KernelCommandOutcome(
            command_id,
            "PENDING",
            queue_item_id,
            "PROMPT_QUEUED",
            f"Prompt accepted at queue sequence {queue_sequence}.",
        )

    async def steer_active_turn(
        self, *, command_id: str, text: str, target_turn_id: str
    ) -> KernelCommandOutcome:
        async with self._lock:
            task = self._active_task
            active_turn_id = self._active_turn_id
        if (
            task is None
            or task.done()
            or active_turn_id is None
            or target_turn_id != active_turn_id
        ):
            return KernelCommandOutcome(
                command_id,
                "REJECTED",
                target_turn_id,
                "NO_ACTIVE_TURN",
                "No active turn can accept a steer.",
            )
        return await self.submit_prompt(
            command_id=command_id,
            text=text,
            delivery_mode=PromptDeliveryMode.STEER_ACTIVE_TURN,
            target_turn_id=target_turn_id,
        )

    async def _consume_pending_steers(
        self, turn_id: str, deadline_monotonic: float
    ) -> int:
        consumed = 0
        while consumed < STAGE2_LIMITS.pending_prompt_hard_items:
            accepted = await self._io.run(
                self.repository.consume_prompt_steer_for_turn,
                self._lease.guard,
                target_turn_id=turn_id,
                new_entry_id=f"entry:{uuid4().hex}",
                occurred_at=datetime.now().astimezone(),
                actor_id=self.host_session_id,
                deadline_monotonic=deadline_monotonic,
            )
            if accepted is None:
                return consumed
            consumed += 1
        raise RuntimeError("steer safe-point batch exceeded its hard bound")

    async def _prompt_delivery_loop(self) -> None:
        while True:
            await self._queue_wake.wait()
            self._queue_wake.clear()
            if self._closing:
                return
            while not self._closing:
                async with self._lock:
                    active = self._active_task
                if active is not None and not active.done():
                    try:
                        await asyncio.shield(active)
                    except BaseException:
                        pass
                    continue
                try:
                    accepted = await self._io.run(
                        self.repository.consume_prompt_head,
                        self._lease.guard,
                        new_turn_id=f"turn:{uuid4().hex}",
                        new_entry_id=f"entry:{uuid4().hex}",
                        new_context_binding_revision_id=(
                            f"context-revision:{uuid4().hex}"
                        ),
                        occurred_at=datetime.now().astimezone(),
                        actor_id=self.host_session_id,
                        deadline_monotonic=monotonic() + 10.0,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # The queue row is the level truth.  A transient checkout
                    # or writer failure must not terminate the only process-
                    # local delivery loop or create a durable retry owner.
                    await asyncio.sleep(0.1)
                    self._queue_wake.set()
                    break
                if accepted is None:
                    try:
                        pending = await self._io.run(
                            self.repository.has_pending_prompt,
                            session_id=self.session_id,
                            delivery_mode=PromptDeliveryMode.NEW_TURN,
                            deadline_monotonic=monotonic() + 5.0,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        await asyncio.sleep(0.1)
                        self._queue_wake.set()
                        break
                    if pending:
                        continue
                    break
                task = asyncio.create_task(
                    self._runner.run_accepted_turn(accepted.turn_id),
                    name=f"kernel-queued-turn:{accepted.turn_id}",
                )
                async with self._lock:
                    if self._closing:
                        task.cancel()
                    self._active_task = task
                    self._active_turn_id = accepted.turn_id
                    self._active_command_id = None
                try:
                    await task
                except BaseException:
                    pass
                finally:
                    async with self._lock:
                        if self._active_task is task:
                            self._active_task = None
                            self._active_turn_id = None
                            self._active_command_id = None

    async def _query_command_row(self, command_id: str):
        return await self._io.run(
            self.repository.query_command,
            session_id=self.session_id,
            command_id=command_id,
            deadline_monotonic=monotonic() + 10.0,
        )

    async def query_command(self, command_id: str) -> KernelCommandOutcome | None:
        failure = self._command_failures.get(command_id)
        row = await self._query_command_row(command_id)
        if row is None:
            return failure
        if row.get("interaction_decision") is not None:
            decision = str(row["interaction_decision"])
            return KernelCommandOutcome(
                command_id,
                "SUCCEEDED",
                str(row.get("target_interaction_decision_id") or ""),
                f"INTERACTION_{decision}",
                "Interaction decision accepted.",
            )
        status = str(row.get("turn_status") or "")
        target = str(row.get("target_turn_id") or "")
        if row.get("target_queue_item_id") is not None:
            queue_status = str(row.get("queue_status") or "")
            target = str(row.get("consumed_turn_id") or row["target_queue_item_id"])
            status = str(row.get("consumed_turn_status") or "")
            if queue_status == "PENDING":
                return KernelCommandOutcome(
                    command_id, "PENDING", target, "PROMPT_QUEUED", "Prompt is queued."
                )
            if queue_status in {"CANCELLED", "REJECTED"}:
                return KernelCommandOutcome(
                    command_id,
                    "REJECTED",
                    target,
                    str(row.get("queue_terminal_reason") or queue_status),
                    "The queued prompt was not delivered.",
                )
            if queue_status == "CONSUMED" and not status:
                return KernelCommandOutcome(
                    command_id,
                    "PENDING",
                    target,
                    "PROMPT_CONSUMED",
                    "Prompt was accepted into a canonical turn.",
                )
        if status == "COMPLETED":
            return KernelCommandOutcome(
                command_id, "SUCCEEDED", target, "TURN_COMPLETED", "Reply accepted."
            )
        if status == "INTERRUPTED":
            return KernelCommandOutcome(
                command_id,
                "REJECTED",
                target,
                str(row.get("terminal_reason") or "TURN_INTERRUPTED"),
                "The turn was interrupted and will not be replayed.",
            )
        return KernelCommandOutcome(
            command_id, "PENDING", target, "TURN_RUNNING", "The turn is running."
        )

    def attach_controller(self, attachment_id: str) -> bool:
        self._require_open()
        return self._interactions.attach_controller(attachment_id)

    async def controller_detached(self, attachment_id: str) -> None:
        await self._interactions.controller_detached(attachment_id)

    async def resolve_tool_interaction(
        self,
        *,
        expected_writer_generation: int,
        expected_owner_epoch: int,
        expected_live_revision: int,
        interaction_id: str,
        command_id: str,
        decision: str,
        actor_id: str,
    ) -> KernelCommandOutcome:
        self._require_open()
        accepted = await self._interactions.resolve_tool_interaction(
            expected_writer_generation=expected_writer_generation,
            expected_owner_epoch=expected_owner_epoch,
            expected_live_revision=expected_live_revision,
            interaction_id=interaction_id,
            command_id=command_id,
            decision=decision,
            actor_id=actor_id,
        )
        return KernelCommandOutcome(
            command_id,
            "SUCCEEDED",
            accepted.decision_id,
            f"INTERACTION_{accepted.decision}",
            "Interaction decision accepted.",
        )

    async def stop_current_turn(self) -> bool:
        async with self._lock:
            task = self._active_task
        if task is None or task.done():
            return False
        task.cancel()
        try:
            await task
        except BaseException:
            pass
        return True

    async def request_job_cancel(
        self,
        *,
        job_id: str,
        reason: str = "USER_CANCELLED",
    ) -> str:
        self._require_open()
        return await self._io.run(
            self.repository.request_job_cancel,
            self._lease.guard,
            job_id=job_id,
            actor_id=self.host_session_id,
            reason=reason,
            deadline_monotonic=monotonic() + 10.0,
        )

    async def aclose(self, *, close_conversation: bool) -> None:
        async with self._lock:
            if self._closing:
                return
            self._closing = True
            task = self._active_task
            self._queue_wake.set()
        self.extensions.stop_admission()
        deadline = monotonic() + HOST_CLOSE_SECONDS
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.shield(task), timeout=max(0.01, deadline - monotonic())
                )
            except BaseException:
                if not task.done():
                    raise TimeoutError(
                        "canonical foreground physical task did not exit"
                    )
        self._renewal_task.cancel()
        try:
            await self._renewal_task
        except BaseException:
            pass
        self._delivery_task.cancel()
        try:
            await self._delivery_task
        except BaseException:
            pass
        await self._interactions.aclose()
        await self._subagents.aclose(timeout_seconds=max(0.01, deadline - monotonic()))
        await self._memory_tools.aclose()
        await self._tools.aclose(timeout_seconds=max(0.01, deadline - monotonic()))
        await self.extensions.aclose(deadline_monotonic=deadline)
        if close_conversation:
            await self._io.run(
                self.repository.close_session,
                self._lease.guard,
                deadline_monotonic=deadline,
            )
        self.live_bus.close()
        self.live_control.close()
        await self._io.aclose(deadline_monotonic=deadline)

    async def _renew_writer(self) -> None:
        while True:
            await asyncio.sleep(WRITER_RENEW_INTERVAL_SECONDS)
            self._lease = await self._io.run(
                self.repository.renew_host_writer,
                self._lease.guard,
                lease_seconds=WRITER_LEASE_SECONDS,
                deadline_monotonic=monotonic() + WRITER_RENEW_INTERVAL_SECONDS,
            )

    def _new_child_runner(self) -> ConversationKernelRunner:
        return ConversationKernelRunner(
            repository=self.repository,
            writer_lease=self._lease,
            model=self._model,
            tools=self._tools,
            live_bus=self.live_bus,
            io_owner=self._io,
            capability_composer=self._capabilities,
            extensions=self.extensions,
            steer_consumer=self._consume_pending_steers,
        )

    def _require_open(self) -> None:
        if self._closing:
            raise RuntimeError("kernel Host session is closing")


def _stable_id(namespace: str, *parts: str) -> str:
    digest = sha256("\0".join(parts).encode("utf-8")).hexdigest()
    return f"{namespace}:{digest}"


def _validate_prompt(value: str) -> None:
    encoded = value.encode("utf-8")
    if not encoded or len(encoded) > MAXIMUM_PROMPT_BYTES:
        raise ValueError("prompt is outside its finite byte bound")
    if any(character not in "\n\t" and ord(character) < 0x20 for character in value):
        raise ValueError("prompt contains a forbidden control character")


def _list_resumable_session_rows(
    repository: ConversationKernelRepository,
    workspace_id: str,
    include_closed: bool,
    limit: int,
    deadline_monotonic: float,
):
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=deadline_monotonic,
        isolation_level=IsolationLevel.REPEATABLE_READ,
    ) as connection:
        return connection.execute(
            """
            SELECT id, workspace_id, lifecycle, writer_generation,
                   latest_entry_sequence, updated_at
            FROM pulsara_v3.sessions
            WHERE workspace_id = %s AND (%s OR lifecycle = 'OPEN')
            ORDER BY updated_at DESC, id LIMIT %s
            """,
            (workspace_id, include_closed, limit),
        ).fetchall()


class KernelHostCore:
    """Process owner for verified PostgreSQL and live Host sessions."""

    def __init__(
        self,
        *,
        settings: PulsaraSettings,
        authenticated_first_party_extension_ids: frozenset[str] = frozenset(),
    ) -> None:
        self.settings = settings
        self._access: VerifiedPostgresAccessLease | None = None
        self._repository: ConversationKernelRepository | None = None
        self._jobs: KernelDurableJobExecutor | None = None
        self._blob_store: PostgresCanonicalBlobStore | None = None
        self._blob_gc_io: KernelSessionIO | None = None
        self._blob_gc_task: asyncio.Task[None] | None = None
        self._sessions: dict[str, KernelHostSession] = {}
        self._extension_routes: dict[
            str, tuple[str, KernelExtensionHost]
        ] = {}
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._lock = asyncio.Lock()
        self._authenticated_first_party_extension_ids = (
            authenticated_first_party_extension_ids
        )

    @classmethod
    def production(
        cls,
        *,
        settings: PulsaraSettings,
        authenticated_first_party_extension_ids: frozenset[str] = frozenset(),
    ) -> "KernelHostCore":
        return cls(
            settings=settings,
            authenticated_first_party_extension_ids=(
                authenticated_first_party_extension_ids
            ),
        )

    async def _ensure_resources(self) -> ConversationKernelRepository:
        if self._repository is None:
            self._event_loop = asyncio.get_running_loop()
            self._access = await process_postgres_schema_verification_service().acquire(
                self.settings.storage.postgres_dsn,
                deadline_monotonic=monotonic() + 30.0,
            )
            try:
                await asyncio.to_thread(
                    require_stage2_runtime_privilege_boundary,
                    self._access.connection_provider,
                    deadline_monotonic=monotonic() + 30.0,
                )
            except BaseException:
                self._access.release()
                self._access = None
                raise
            self._repository = ConversationKernelRepository(
                self._access.connection_provider,
                post_commit_tap=self._route_committed_events_from_thread,
            )
            self._jobs = KernelDurableJobExecutor(
                repository=self._repository,
                llm_config=self.settings.llm,
                embedding_config=self.settings.retrieval.embedding,
            )
            self._jobs.start()
            self._blob_store = PostgresCanonicalBlobStore(
                self._access.connection_provider
            )
            self._blob_gc_io = KernelSessionIO(maximum_concurrency=1)
            self._blob_gc_task = asyncio.create_task(
                self._blob_gc_loop(), name="kernel-blob-orphan-gc"
            )
        return self._repository

    async def open_session(
        self,
        workspace_input: HostWorkspaceInput,
        *,
        model_role: ModelRole = ModelRole.PRO,
        permission_policy: EffectivePermissionPolicy | None = None,
        system_prompt: str | None = None,
        active_skill_names: frozenset[str] = frozenset(),
    ) -> KernelHostSession:
        return await self._open(
            workspace_input,
            session_id=f"session:{uuid4().hex}",
            model_role=model_role,
            permission_policy=permission_policy,
            system_prompt=system_prompt,
            active_skill_names=active_skill_names,
        )

    async def resume_session(
        self,
        session_id: str,
        *,
        workspace_input: HostWorkspaceInput,
        model_role: ModelRole = ModelRole.PRO,
        permission_policy: EffectivePermissionPolicy | None = None,
        system_prompt: str | None = None,
        active_skill_names: frozenset[str] = frozenset(),
    ) -> KernelHostSession:
        return await self._open(
            workspace_input,
            session_id=session_id,
            model_role=model_role,
            permission_policy=permission_policy,
            system_prompt=system_prompt,
            active_skill_names=active_skill_names,
        )

    async def resume_most_recent_session(
        self,
        workspace_input: HostWorkspaceInput,
        **kwargs: object,
    ) -> KernelHostSession:
        sessions = await self.list_resumable_sessions(
            workspace_input=workspace_input, limit=1
        )
        if not sessions:
            raise KeyError("no resumable runtime session found")
        return await self.resume_session(
            sessions[0].session_id, workspace_input=workspace_input, **kwargs
        )

    async def _open(
        self,
        workspace_input: HostWorkspaceInput,
        *,
        session_id: str,
        model_role: ModelRole,
        permission_policy: EffectivePermissionPolicy | None,
        system_prompt: str | None,
        active_skill_names: frozenset[str],
    ) -> KernelHostSession:
        workspace = resolve_workspace(workspace_input)
        mcp_configs = await asyncio.to_thread(
            load_mcp_server_configs,
            workspace_root=workspace.workspace_root,
        )
        enabled_mcp = tuple(item.server_id for item in mcp_configs if item.enabled)
        if enabled_mcp:
            raise KernelCompositionUnavailable(
                "Stage 2 MCP composition is not installed; disable configured MCP "
                "servers before opening the canonical kernel: " + ", ".join(enabled_mcp)
            )
        repository = await self._ensure_resources()
        host_id = f"host:{uuid4().hex}"
        io_owner = KernelSessionIO()
        deadline = monotonic() + STAGE2_LIMITS.foreground_io_timeout_ms / 1000
        try:
            writer_lease = await io_owner.run(
                repository.acquire_host_writer,
                session_id=session_id,
                workspace_id=workspace.workspace_key,
                writer_owner_id=host_id,
                lease_seconds=WRITER_LEASE_SECONDS,
                deadline_monotonic=deadline,
            )
            session = KernelHostSession(
                settings=self.settings,
                workspace=workspace,
                repository=repository,
                writer_lease=writer_lease,
                io_owner=io_owner,
                session_id=session_id,
                host_session_id=host_id,
                permission_policy=permission_policy or default_permission_policy(),
                model_role=model_role,
                system_prompt=system_prompt,
                active_skill_names=active_skill_names,
                authenticated_first_party_extension_ids=(
                    self._authenticated_first_party_extension_ids
                ),
            )
        except BaseException:
            await io_owner.aclose(deadline_monotonic=deadline)
            raise
        async with self._lock:
            self._sessions[host_id] = session
            self._extension_routes[session_id] = (host_id, session.extensions)
        return session

    async def list_resumable_sessions(
        self,
        *,
        workspace_input: HostWorkspaceInput,
        include_closed: bool = False,
        limit: int = 20,
    ) -> list[KernelSessionSummary]:
        if not 1 <= limit <= 100:
            raise ValueError("session list limit is out of bounds")
        repository = await self._ensure_resources()
        workspace = resolve_workspace(workspace_input)
        rows = await asyncio.to_thread(
            _list_resumable_session_rows,
            repository,
            workspace.workspace_key,
            include_closed,
            limit,
            monotonic() + 10.0,
        )
        return [
            KernelSessionSummary(
                session_id=str(row["id"]),
                workspace_id=str(row["workspace_id"]),
                lifecycle=str(row["lifecycle"]),
                writer_generation=int(row["writer_generation"]),
                latest_entry_sequence=int(row["latest_entry_sequence"]),
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    async def close_session(
        self, host_session_id: str, *, close_conversation: bool
    ) -> None:
        async with self._lock:
            session = self._sessions.pop(host_session_id, None)
        if session is None:
            return
        await session.aclose(close_conversation=close_conversation)
        async with self._lock:
            route = self._extension_routes.get(session.session_id)
            if route is not None and route[0] == host_session_id:
                self._extension_routes.pop(session.session_id, None)

    async def shutdown(self) -> None:
        async with self._lock:
            sessions = tuple(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            await session.aclose(close_conversation=False)
        self._extension_routes.clear()
        if self._jobs is not None:
            await self._jobs.aclose(timeout_seconds=HOST_CLOSE_SECONDS)
            self._jobs = None
        if self._blob_gc_task is not None:
            self._blob_gc_task.cancel()
            try:
                await self._blob_gc_task
            except asyncio.CancelledError:
                pass
            self._blob_gc_task = None
        if self._blob_gc_io is not None:
            await self._blob_gc_io.aclose(
                deadline_monotonic=monotonic() + HOST_CLOSE_SECONDS
            )
            self._blob_gc_io = None
        self._blob_store = None
        if self._access is not None:
            self._access.release()
            self._access = None
            self._repository = None
            self._event_loop = None

    def _route_committed_events_from_thread(
        self, events: tuple[StoredCommittedEvent, ...]
    ) -> None:
        """Cross the physical transaction boundary without waiting on a hook."""

        loop = self._event_loop
        if loop is None or loop.is_closed() or not events:
            return
        try:
            loop.call_soon_threadsafe(self._deliver_committed_events, events)
        except RuntimeError:
            # A process-local observer cannot keep a closing event loop alive.
            return

    def _deliver_committed_events(
        self, events: tuple[StoredCommittedEvent, ...]
    ) -> None:
        for event in events:
            route = self._extension_routes.get(event.session_id)
            if route is None:
                continue
            route[1].offer_post_commit_nowait(
                PostCommitHookOffer(
                    event_type=event.event_type,
                    session_id=event.session_id,
                    turn_id=event.turn_id,
                    subject_id=event.subject.subject_id,
                    event_sequence=event.event_sequence,
                    public_payload={
                        "subject_slot": event.subject.slot.value,
                        "accepted_at": event.accepted_at.isoformat(),
                        "occurred_at": event.occurred_at.isoformat(),
                        "actor_kind": event.actor_kind,
                        "sensitivity_class": event.sensitivity_class,
                        "projection_profile": event.projection_profile,
                        "payload": dict(event.payload),
                    },
                )
            )

    async def _blob_gc_loop(self) -> None:
        store = self._blob_store
        io_owner = self._blob_gc_io
        assert store is not None and io_owner is not None
        while True:
            try:
                await io_owner.run(
                    store.delete_orphans,
                    grace_seconds=STAGE2_LIMITS.blob_orphan_grace_seconds,
                    maximum_items=STAGE2_LIMITS.blob_gc_batch_hard_items,
                    deadline_monotonic=(
                        monotonic()
                        + STAGE2_LIMITS.foreground_io_timeout_ms / 1_000
                    ),
                )
            except asyncio.CancelledError:
                raise
            except BaseException:
                # Orphan collection is disposable maintenance.  The next
                # bounded interval retries; product writes never wait for it.
                pass
            await asyncio.sleep(STAGE2_LIMITS.blob_gc_interval_ms / 1_000)


__all__ = [
    "KernelCommandOutcome",
    "KernelCompositionUnavailable",
    "KernelHostCore",
    "KernelHostSession",
    "KernelSessionSummary",
]
