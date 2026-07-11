"""Reusable host facade for web, desktop, and thin CLI drivers."""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from pulsara_agent.host.identity import HostWorkspaceInput, ResolvedWorkspace, resolve_workspace
from pulsara_agent.host.registry import HostSessionRegistry, HostSessionSummary
from pulsara_agent.host.resume import DanglingRunRepairResult, repair_dangling_runs_for_resume
from pulsara_agent.host.session import HostSession
from pulsara_agent.host.session_manifest import (
    ResumableSessionSummary,
    SessionManifestStore,
    permission_policy_from_manifest,
)
from pulsara_agent.host.supervisor import (
    WorkspaceClosingError,
    WorkspaceTerminalSnapshot,
    WorkspaceTerminalSupervisor,
)
from pulsara_agent.llm import ModelRole
from pulsara_agent.llm.request import LLMOptions
from pulsara_agent.runtime.approval import ApprovalResolution, PendingApproval
from pulsara_agent.runtime.agent import AgentRunResult
from pulsara_agent.runtime.permission import EffectivePermissionPolicy
from pulsara_agent.runtime.plan import (
    McpInputRequiredInteractionResolution,
    PendingInteraction,
    PlanInteractionResolution,
)
from pulsara_agent.runtime.recovery import AbortKind
from pulsara_agent.runtime.terminal import (
    BorrowedWorkspaceTerminalRuntime,
    PendingTerminalCompletionError,
    TerminalOwnerContext,
    WorkspaceTerminalLease,
)
from pulsara_agent.runtime.mcp.store import load_mcp_server_configs
from pulsara_agent.runtime.mcp.supervisor import McpServerSupervisor
from pulsara_agent.runtime.mcp.types import McpReconcileTicket
from pulsara_agent.runtime.wiring import build_agent_runtime_wiring
from pulsara_agent.retrieval.runtime import RetrievalRuntimeResources, build_retrieval_runtime_resources
from pulsara_agent.memory.canonical.vector_index_sync import MemoryVectorIndexSync
from pulsara_agent.memory.canonical.vector_worker import MemoryVectorIndexWorker
from pulsara_agent.memory.governance.coordinator import MemoryGovernanceCoordinator
from pulsara_agent.settings import PulsaraSettings


class HostCoreLifecycle(StrEnum):
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class _HostShutdownAttempt:
    attempt_id: str
    completion: asyncio.Future[None]


@dataclass(slots=True)
class HostCore:
    settings: PulsaraSettings
    durable: bool = True
    scratch_root: Path | None = None
    registry: HostSessionRegistry = field(default_factory=HostSessionRegistry)
    _supervisors: dict[str, WorkspaceTerminalSupervisor] = field(default_factory=dict, init=False, repr=False)
    _session_leases: dict[str, WorkspaceTerminalLease] = field(default_factory=dict, init=False, repr=False)
    _supervisor_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _retrieval_resources: RetrievalRuntimeResources | None = field(default=None, init=False, repr=False)
    _retrieval_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _governance_coordinator: MemoryGovernanceCoordinator | None = field(default=None, init=False, repr=False)
    _lifecycle: HostCoreLifecycle = field(default=HostCoreLifecycle.OPEN, init=False, repr=False)
    _lifecycle_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _shutdown_attempt: _HostShutdownAttempt | None = field(default=None, init=False, repr=False)
    _failed_open_mcp_supervisors: dict[int, McpServerSupervisor] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _failed_open_mcp_cleanup_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock,
        init=False,
        repr=False,
    )

    # -- Lifecycle gate -------------------------------------------------------

    @property
    def lifecycle(self) -> HostCoreLifecycle:
        return self._lifecycle

    def _raise_if_not_accepting(self, action: str) -> None:
        if self._lifecycle is not HostCoreLifecycle.OPEN:
            raise RuntimeError(f"HostCore is {self._lifecycle.value}; cannot {action}")

    # -- Open (a single, rollback-safe transaction) ---------------------------

    async def open_session(
        self,
        workspace_input: HostWorkspaceInput,
        *,
        conversation_id: str | None = None,
        host_session_id: str | None = None,
        model_role: ModelRole = ModelRole.PRO,
        options: LLMOptions | None = None,
        system_prompt: str | None = None,
        memory_reflection: bool = True,
        permission_policy: EffectivePermissionPolicy | None = None,
    ) -> HostSession:
        return await self._open_session_with_runtime_id(
            workspace_input,
            runtime_session_id=None,
            conversation_id=conversation_id,
            host_session_id=host_session_id,
            model_role=model_role,
            options=options,
            system_prompt=system_prompt,
            memory_reflection=memory_reflection,
            permission_policy=permission_policy,
            created_by="host_open",
            repair_dangling_on_resume=False,
        )

    async def resume_session(
        self,
        runtime_session_id: str,
        *,
        workspace_input: HostWorkspaceInput | None = None,
        conversation_id: str | None = None,
        host_session_id: str | None = None,
        model_role: ModelRole | None = None,
        options: LLMOptions | None = None,
        system_prompt: str | None = None,
        memory_reflection: bool = True,
        permission_policy: EffectivePermissionPolicy | None = None,
        repair_dangling: bool = True,
    ) -> HostSession:
        """Reopen an existing durable runtime session in this HostCore process."""
        if not self.durable:
            raise RuntimeError("resume_session requires durable HostCore wiring")
        self._raise_if_not_accepting("resume a session")
        manifest = self._manifest_store().get(runtime_session_id)
        if manifest is None:
            raise KeyError(f"runtime session not found: {runtime_session_id}")
        if not manifest.resumable:
            raise RuntimeError(f"runtime session is closed or archived: {runtime_session_id}")
        resolved_workspace_input = workspace_input or manifest.to_workspace_input()
        resolved_model_role = model_role or ModelRole(manifest.model_role)
        return await self._open_session_with_runtime_id(
            resolved_workspace_input,
            runtime_session_id=runtime_session_id,
            conversation_id=conversation_id or manifest.conversation_id,
            host_session_id=host_session_id,
            model_role=resolved_model_role,
            options=options,
            system_prompt=system_prompt,
            memory_reflection=memory_reflection,
            permission_policy=permission_policy or permission_policy_from_manifest(manifest),
            created_by="host_resume",
            repair_dangling_on_resume=repair_dangling,
        )

    async def resume_most_recent_session(
        self,
        workspace_input: HostWorkspaceInput | None = None,
        *,
        host_session_id: str | None = None,
        model_role: ModelRole | None = None,
        options: LLMOptions | None = None,
        system_prompt: str | None = None,
        memory_reflection: bool = True,
        permission_policy: EffectivePermissionPolicy | None = None,
    ) -> HostSession:
        self._raise_if_not_accepting("resume the most recent session")
        sessions = await self.list_resumable_sessions(
            workspace_input=workspace_input,
            limit=1,
        )
        if not sessions:
            raise KeyError("no resumable runtime session found")
        return await self.resume_session(
            sessions[0].runtime_session_id,
            workspace_input=workspace_input,
            host_session_id=host_session_id,
            model_role=model_role,
            options=options,
            system_prompt=system_prompt,
            memory_reflection=memory_reflection,
            permission_policy=permission_policy,
        )

    async def list_resumable_sessions(
        self,
        *,
        workspace_input: HostWorkspaceInput | None = None,
        include_closed: bool = False,
        limit: int = 20,
    ) -> list[ResumableSessionSummary]:
        if not self.durable:
            return []
        workspace = resolve_workspace(workspace_input, scratch_root=self.scratch_root) if workspace_input is not None else None
        pending_runtime_ids = {
            runtime_session_id
            for _host_session_id, runtime_session_id in (
                await self.registry.list_manifest_close_tombstones()
            )
        }
        summaries = self._manifest_store().list_resumable(
            workspace_root=workspace.workspace_root if workspace is not None else None,
            memory_domain_id=workspace.memory_domain.memory_domain_id if workspace is not None else None,
            include_closed=include_closed,
            limit=limit + len(pending_runtime_ids),
        )
        return [
            summary
            for summary in summaries
            if summary.runtime_session_id not in pending_runtime_ids
        ][:limit]

    async def repair_session_for_resume(self, runtime_session_id: str) -> DanglingRunRepairResult:
        if not self.durable:
            raise RuntimeError("repair_session_for_resume requires durable HostCore wiring")
        manifest = self._manifest_store().get(runtime_session_id)
        if manifest is None:
            raise KeyError(f"runtime session not found: {runtime_session_id}")
        return repair_dangling_runs_for_resume(
            dsn=self.settings.storage.postgres_dsn,
            runtime_session_id=runtime_session_id,
            workspace_root=manifest.workspace_root,
        )

    async def _open_session_with_runtime_id(
        self,
        workspace_input: HostWorkspaceInput,
        *,
        runtime_session_id: str | None,
        conversation_id: str | None,
        host_session_id: str | None,
        model_role: ModelRole,
        options: LLMOptions | None,
        system_prompt: str | None,
        memory_reflection: bool,
        permission_policy: EffectivePermissionPolicy | None,
        created_by: str,
        repair_dangling_on_resume: bool,
    ) -> HostSession:
        self._raise_if_not_accepting("open a session")
        await self._drain_failed_open_mcp_supervisors()
        workspace = resolve_workspace(workspace_input, scratch_root=self.scratch_root)
        host_session_id = host_session_id or f"host:{uuid4().hex}"
        conversation_id = conversation_id or f"conversation:{uuid4().hex}"
        # Reserve identity first: fail-closed on duplicate id before any resource
        # is built, so two borrowers can never share a terminal owner principal.
        reservation = await self.registry.reserve(
            host_session_id,
            conversation_id,
            runtime_session_id=runtime_session_id,
        )
        lease: WorkspaceTerminalLease | None = None
        wiring = None
        mcp_supervisor: McpServerSupervisor | None = None
        mcp_ticket: McpReconcileTicket | None = None
        manifest_runtime_session_id: str | None = None
        close_manifest_if_publish_fails = runtime_session_id is None
        try:
            if repair_dangling_on_resume:
                if runtime_session_id is None:
                    raise RuntimeError("dangling repair requires a runtime_session_id")
                repair_dangling_runs_for_resume(
                    dsn=self.settings.storage.postgres_dsn,
                    runtime_session_id=runtime_session_id,
                    workspace_root=str(workspace.workspace_root),
                )
            lease = await self._attach_supervisor(workspace, host_session_id, conversation_id)
            mcp_supervisor, mcp_ticket = await self._build_mcp_supervisor(workspace)
            wiring = build_agent_runtime_wiring(
                self.settings,
                workspace.workspace_root,
                durable=self.durable,
                model_role=model_role,
                options=options,
                system_prompt=system_prompt,
                runtime_session_id=runtime_session_id,
                memory_domain=workspace.memory_domain,
                memory_reflection=memory_reflection,
                terminal_binding=BorrowedWorkspaceTerminalRuntime(
                    owner=TerminalOwnerContext(
                        host_session_id=host_session_id,
                        conversation_id=conversation_id,
                    ),
                    manager=lease.manager,
                ),
                permission_policy=permission_policy,
                retrieval_resources=await self._get_retrieval_resources() if self.durable else None,
                governance_coordinator=self._governance_coordinator if self.durable else None,
                mcp_supervisor=mcp_supervisor,
            )
            session = HostSession(
                host_session_id=host_session_id,
                conversation_id=conversation_id,
                workspace=workspace,
                wiring=wiring,
                terminal_lease=lease,
                mcp_supervisor=mcp_supervisor,
            )
            wiring.runtime_wiring.runtime_session.mcp_supervisor = mcp_supervisor
            if mcp_ticket is not None:
                await session.initialize_mcp(mcp_ticket)
            if self.durable:
                manifest_runtime_session_id = session.runtime_session_id
                self._manifest_store().upsert_open_manifest(
                    runtime_session_id=manifest_runtime_session_id,
                    conversation_id=conversation_id,
                    workspace=workspace,
                    model_role=model_role,
                    permission_policy=wiring.agent_runtime.permission_policy,
                    created_by=created_by,
                )
            # Publish under the lifecycle lock and re-check state: this is the
            # linearization point against shutdown/close. If shutdown won the
            # race, we roll back instead of leaking a session into a closed
            # HostCore (contract §0, audit P0-2).
            async with self._lifecycle_lock:
                if self._lifecycle is not HostCoreLifecycle.OPEN:
                    raise RuntimeError("HostCore is closing; open aborted")
                async with self._supervisor_lock:
                    supervisor = self._supervisors.get(lease.workspace_key)
                    if supervisor is None or not supervisor.is_current_lease(lease):
                        raise WorkspaceClosingError(
                            f"workspace {lease.workspace_key} closed while session was opening"
                        )
                    await self.registry.publish(reservation, session)
                    self._session_leases[host_session_id] = lease
            return session
        except BaseException as open_error:
            # Rollback exactly once: release the reservation, release the lease
            # (kills/prunes any owner state), and close partially-built runtime
            # resources so a failed open leaks nothing (contract §0, audit P0-4).
            if (
                manifest_runtime_session_id is not None
                and close_manifest_if_publish_fails
            ):
                await self.registry.retain_failed_open_manifest_close(
                    reservation,
                    runtime_session_id=manifest_runtime_session_id,
                )
                try:
                    self._manifest_store().mark_closed(manifest_runtime_session_id)
                except BaseException as manifest_error:
                    open_error.add_note(
                        "durable manifest close remains pending after failed open: "
                        f"{type(manifest_error).__name__}: {manifest_error}"
                    )
                else:
                    await self.registry.complete_manifest_close(
                        host_session_id=reservation.host_session_id,
                        runtime_session_id=manifest_runtime_session_id,
                    )
            if mcp_supervisor is not None:
                try:
                    await mcp_supervisor.aclose(timeout_seconds=5.0)
                except BaseException:
                    async with self._failed_open_mcp_cleanup_lock:
                        self._failed_open_mcp_supervisors[id(mcp_supervisor)] = (
                            mcp_supervisor
                        )
            await self.registry.release_reservation(reservation)
            if lease is not None:
                await self._release_supervisor_lease(lease)
            if wiring is not None:
                wiring.agent_runtime.close()
            raise

    # -- Read facades (allowed during CLOSING for diagnostics) ----------------

    async def get_session(self, host_session_id: str) -> HostSession:
        return await self.registry.get(host_session_id)

    async def find_by_conversation(self, conversation_id: str) -> HostSession | None:
        return await self.registry.find_by_conversation(conversation_id)

    async def replay_events(self, host_session_id: str, *, after_sequence: int | None = None):
        session = await self.get_session(host_session_id)
        return session.replay_events(after_sequence=after_sequence)

    async def get_pending_approval(self, host_session_id: str) -> PendingApproval | None:
        session = await self.get_session(host_session_id)
        return session.get_pending_approval()

    async def get_pending_interaction(self, host_session_id: str) -> PendingInteraction | None:
        session = await self.get_session(host_session_id)
        return session.get_pending_interaction()

    async def list_sessions(self) -> list[HostSessionSummary]:
        return await self.registry.list_sessions()

    async def list_workspace_terminal_snapshots(self) -> list[WorkspaceTerminalSnapshot]:
        async with self._supervisor_lock:
            supervisors = list(self._supervisors.values())
        return [supervisor.snapshot() for supervisor in supervisors]

    async def list_workspace_supervisors(self) -> list[dict[str, object]]:
        return [snapshot.to_payload() for snapshot in await self.list_workspace_terminal_snapshots()]

    # -- Execution facades (gated: never start/continue a run while closing) --

    async def resolve_approval(
        self,
        host_session_id: str,
        resolution: ApprovalResolution,
    ) -> AgentRunResult:
        self._raise_if_not_accepting("resolve an approval")
        session = await self.get_session(host_session_id)
        return await session.resolve_approval(resolution)

    async def resolve_plan_interaction(
        self,
        host_session_id: str,
        resolution: PlanInteractionResolution,
    ) -> AgentRunResult:
        self._raise_if_not_accepting("resolve a plan interaction")
        session = await self.get_session(host_session_id)
        return await session.resolve_plan_interaction(resolution)

    async def resolve_mcp_input_required(
        self,
        host_session_id: str,
        resolution: McpInputRequiredInteractionResolution,
    ) -> AgentRunResult:
        self._raise_if_not_accepting("resolve MCP input-required")
        session = await self.get_session(host_session_id)
        return await session.resolve_mcp_input_required(resolution)

    async def exit_plan_workflow(
        self,
        host_session_id: str,
        *,
        source: str,
        user_feedback: str = "",
    ) -> None:
        self._raise_if_not_accepting("exit plan workflow")
        session = await self.get_session(host_session_id)
        await session.exit_plan_workflow(source=source, user_feedback=user_feedback)

    async def stop_current_turn(
        self,
        host_session_id: str,
        *,
        reason: AbortKind = AbortKind.USER_STOP,
    ) -> AgentRunResult | None:
        self._raise_if_not_accepting("stop the current turn")
        session = await self.get_session(host_session_id)
        return await session.stop_current_turn(reason=reason)

    async def set_permission_mode(
        self,
        host_session_id: str,
        mode: str,
    ):
        self._raise_if_not_accepting("switch permission mode")
        session = await self.get_session(host_session_id)
        return session.set_permission_mode(mode)

    async def enter_plan(self, host_session_id: str, *, reason: str = ""):
        self._raise_if_not_accepting("enter plan")
        session = await self.get_session(host_session_id)
        return session.enter_plan(reason=reason)

    async def stream_approval_resolution(self, host_session_id: str, resolution: ApprovalResolution):
        self._raise_if_not_accepting("resolve an approval")
        session = await self.get_session(host_session_id)
        async for event in session.stream_approval_resolution(resolution):
            yield event

    async def stream_plan_interaction_resolution(
        self,
        host_session_id: str,
        resolution: PlanInteractionResolution,
    ):
        self._raise_if_not_accepting("resolve a plan interaction")
        session = await self.get_session(host_session_id)
        async for event in session.stream_plan_interaction_resolution(resolution):
            yield event

    # -- Idle sweep (HostCore is the sole closer; registry only discovers) ----

    async def sweep_idle(self, *, now: float | None = None) -> list[str]:
        candidates = await self.registry.list_idle_candidates(now=now)
        for host_session_id in candidates:
            await self.close_session(host_session_id)
        return candidates

    # -- Close paths (the three flows share one close primitive) --------------

    async def detach_session(self, host_session_id: str) -> None:
        await self.close_session(host_session_id, close_conversation=False)

    async def close_session(self, host_session_id: str, *, close_conversation: bool = False) -> None:
        """Sole session-close coordinator. Concurrent callers await one close."""
        claim = await self.registry.claim_close(
            host_session_id,
            close_conversation=close_conversation,
        )
        manifest_retry = claim.manifest_retry_attempt
        if manifest_retry is not None:
            if not claim.is_owner:
                await asyncio.shield(manifest_retry.completion)
                return
            manifest_error: BaseException | None = None
            try:
                self._manifest_store().mark_closed(manifest_retry.runtime_session_id)
            except BaseException as exc:
                manifest_error = exc
            try:
                await self.registry.finish_manifest_close_retry(
                    manifest_retry,
                    error=manifest_error,
                )
            except BaseException as exc:
                if not manifest_retry.completion.done():
                    await asyncio.shield(
                        self.registry.abort_manifest_close_retry(
                            manifest_retry,
                            error=exc,
                        )
                    )
                if manifest_retry.completion.done():
                    manifest_retry.completion.result()
                raise
            manifest_retry.completion.result()
            return
        attempt = claim.attempt
        if attempt is None:
            return
        if not claim.is_owner:
            await asyncio.shield(attempt.completion)
            if claim.requires_manifest_close_after_wait and self.durable:
                self._manifest_store().mark_closed(attempt.session.runtime_session_id)
                await self.registry.complete_manifest_close(
                    host_session_id=host_session_id,
                    runtime_session_id=attempt.session.runtime_session_id,
                )
            return
        session = attempt.session
        errors: list[BaseException] = []
        manifest_error: BaseException | None = None
        try:
            try:
                await session.aclose(reason=AbortKind.HOST_TEARDOWN)
            except BaseException as exc:
                # Child/run drain failure is not cleanup noise. Preserve the
                # indexed session, terminal lease, workspace, and closing
                # runtime so a later close call can retry the safe point.
                await self.registry.abort_close(attempt, error=exc)
                attempt.completion.result()
                raise AssertionError("unreachable")
            lease = self._session_leases.get(host_session_id)
            if lease is not None:
                preserve_lease = False
                try:
                    await self._release_supervisor_lease(lease)
                except PendingTerminalCompletionError as exc:
                    # A pending canonical terminal completion is physical close
                    # work, not cleanup noise. Keep the session, lease and
                    # workspace indexed so a later close attempt can retry.
                    preserve_lease = True
                    await self.registry.abort_close(attempt, error=exc)
                    attempt.completion.result()
                    raise AssertionError("unreachable")
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    if (
                        not preserve_lease
                        and self._session_leases.get(host_session_id) is lease
                    ):
                        self._session_leases.pop(host_session_id, None)
            try:
                self._cleanup_workspace_root(session.workspace)
            except BaseException as exc:
                errors.append(exc)
            merged_close_conversation = await self.registry.seal_close_intent(attempt)
            if merged_close_conversation and self.durable:
                try:
                    self._manifest_store().mark_closed(session.runtime_session_id)
                except BaseException as exc:
                    manifest_error = exc
                    errors.append(exc)
            error = errors[0] if errors else None
            await self.registry.finish_close(
                attempt,
                error=error,
                manifest_close_pending=manifest_error is not None,
            )
            attempt.completion.result()
        except BaseException as exc:
            # The drain-failure path already resolved the shared attempt. For
            # unexpected owner failures, resolve it here without deleting a
            # newer retry attempt (registry completion is identity-conditional).
            if not attempt.completion.done():
                await self.registry.abort_close(attempt, error=exc)
            # Consume/re-raise the exact shared result so owner and waiters see
            # the same outcome.
            attempt.completion.result()
            raise AssertionError("unreachable")

    async def close_workspace(self, workspace_key: str) -> None:
        async with self._supervisor_lock:
            supervisor = self._supervisors.get(workspace_key)
            if supervisor is not None:
                supervisor.mark_closing()
        for summary in await self.registry.list_sessions():
            try:
                session = await self.registry.get(summary.host_session_id)
            except KeyError:
                continue
            if session.workspace.workspace_key == workspace_key:
                await self.close_session(summary.host_session_id)
        await self._shutdown_supervisor(workspace_key)

    async def shutdown(self) -> None:
        async with self._lifecycle_lock:
            if self._lifecycle is HostCoreLifecycle.CLOSED:
                return
            if self._shutdown_attempt is not None:
                attempt = self._shutdown_attempt
                is_shutdown_owner = False
            else:
                self._lifecycle = HostCoreLifecycle.CLOSING
                attempt = _HostShutdownAttempt(
                    attempt_id=uuid4().hex,
                    completion=asyncio.get_running_loop().create_future(),
                )
                self._shutdown_attempt = attempt
                is_shutdown_owner = True
        if not is_shutdown_owner:
            await asyncio.shield(attempt.completion)
            return
        errors: list[BaseException] = []
        blocked_by_close_work = False
        try:
            pending_manifest_closes = await self.registry.list_manifest_close_tombstones()
            for host_session_id, _runtime_session_id in pending_manifest_closes:
                try:
                    await self.close_session(
                        host_session_id,
                        close_conversation=True,
                    )
                except BaseException as exc:
                    errors.append(exc)
            summaries = await self.registry.list_sessions()
            # Close every session first so each run's finally completes
            # (governance notify) and each terminal lease is released, before
            # retrieval providers close — no recoverable/startable borrower may
            # outlive the provider, and no terminal tool may outlive the all-kill
            # (contract §7.3).
            for summary in summaries:
                try:
                    await self.close_session(summary.host_session_id)
                except BaseException as exc:
                    errors.append(exc)
            try:
                await self._drain_failed_open_mcp_supervisors()
            except BaseException as exc:
                errors.append(exc)
            blocked_by_close_work = bool(
                await self.registry.list_sessions()
                or await self.registry.list_manifest_close_tombstones()
                or self._failed_open_mcp_supervisors
            )
            if blocked_by_close_work and not errors:
                errors.append(
                    RuntimeError(
                        "HostCore shutdown is blocked by incomplete session finalization"
                    )
                )
            if not blocked_by_close_work:
                async with self._supervisor_lock:
                    supervisors = list(self._supervisors.values())
                    self._supervisors.clear()
                for supervisor in supervisors:
                    supervisor.mark_closed()
                    try:
                        await asyncio.to_thread(supervisor.terminal_sessions.shutdown)
                    except BaseException as exc:
                        errors.append(exc)
                async with self._retrieval_lock:
                    resources = self._retrieval_resources
                    self._retrieval_resources = None
                    self._governance_coordinator = None
                if resources is not None:
                    try:
                        await resources.aclose()
                    except BaseException as exc:
                        errors.append(exc)
        except BaseException as exc:
            # Even an unexpected coordinator failure must not strand every
            # concurrent waiter behind a permanently-CLOSING HostCore.
            errors.append(exc)
        finally:
            async with self._lifecycle_lock:
                self._lifecycle = (
                    HostCoreLifecycle.OPEN
                    if blocked_by_close_work
                    else HostCoreLifecycle.CLOSED
                )
                if self._shutdown_attempt is attempt:
                    self._shutdown_attempt = None
                    error = errors[0] if errors else None
                    if error is None:
                        attempt.completion.set_result(None)
                    else:
                        attempt.completion.set_exception(error)
        attempt.completion.result()

    # -- Internal helpers -----------------------------------------------------

    async def _drain_failed_open_mcp_supervisors(self) -> None:
        """Retry bounded MCP cleanup retained by an unpublished open rollback."""

        async with self._failed_open_mcp_cleanup_lock:
            pending = tuple(self._failed_open_mcp_supervisors.items())
            first_error: BaseException | None = None
            for identity, supervisor in pending:
                try:
                    await supervisor.aclose(timeout_seconds=5.0)
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
                else:
                    if self._failed_open_mcp_supervisors.get(identity) is supervisor:
                        self._failed_open_mcp_supervisors.pop(identity, None)
            if first_error is not None:
                raise first_error

    async def _get_retrieval_resources(self) -> RetrievalRuntimeResources:
        async with self._retrieval_lock:
            if self._lifecycle is not HostCoreLifecycle.OPEN:
                raise RuntimeError("HostCore is closing; cannot start retrieval resources")
            if self._retrieval_resources is None:
                self._retrieval_resources = build_retrieval_runtime_resources(self.settings.retrieval)
                self._governance_coordinator = MemoryGovernanceCoordinator()
                self._retrieval_resources.attach_worker(self._governance_coordinator)
                if self._retrieval_resources.embedding is not None:
                    vector_worker = MemoryVectorIndexWorker(
                        MemoryVectorIndexSync(
                            dsn=self.settings.storage.postgres_dsn,
                            provider=self._retrieval_resources.embedding,
                            provider_name=self.settings.retrieval.embedding.provider,
                        )
                    )
                    self._governance_coordinator.on_commit = vector_worker.wake
                    self._retrieval_resources.attach_worker(vector_worker)
                self._retrieval_resources.start()
            return self._retrieval_resources

    async def _build_mcp_supervisor(
        self, workspace: ResolvedWorkspace
    ) -> tuple[McpServerSupervisor, McpReconcileTicket]:
        configs = load_mcp_server_configs(workspace_root=workspace.workspace_root)
        supervisor = McpServerSupervisor()
        ticket = supervisor.prepare(configs, trigger="initial")
        return supervisor, ticket

    async def _attach_supervisor(
        self,
        workspace: ResolvedWorkspace,
        host_session_id: str,
        conversation_id: str,
    ) -> WorkspaceTerminalLease:
        async with self._supervisor_lock:
            supervisor = self._supervisors.get(workspace.workspace_key)
            if supervisor is None:
                supervisor = WorkspaceTerminalSupervisor(
                    workspace_key=workspace.workspace_key,
                    workspace_root=workspace.workspace_root,
                )
                self._supervisors[workspace.workspace_key] = supervisor
            return supervisor.attach(
                TerminalOwnerContext(
                    host_session_id=host_session_id,
                    conversation_id=conversation_id,
                )
            )

    async def _release_supervisor_lease(self, lease: WorkspaceTerminalLease) -> None:
        """Release one lease: state transition under the lock, blocking kill in a
        thread off the lock, then prune the supervisor if it is empty and open."""
        async with self._supervisor_lock:
            supervisor = self._supervisors.get(lease.workspace_key)
            current = (
                supervisor is not None
                and supervisor.is_current_lease(lease)
            )
        if not current:
            return
        # Durable completion drain must succeed before dropping the supervisor
        # lease. A failure leaves the exact lease generation retryable.
        await asyncio.to_thread(
            lease.manager.release_owner,
            lease.owner.host_session_id,
        )
        async with self._supervisor_lock:
            supervisor = self._supervisors.get(lease.workspace_key)
            if supervisor is not None:
                supervisor.release_lease(lease)
            if (
                supervisor is not None
                and supervisor.is_accepting
                and supervisor.owner_count == 0
                and supervisor.terminal_sessions.live_process_count() == 0
            ):
                self._supervisors.pop(lease.workspace_key, None)

    async def _shutdown_supervisor(self, workspace_key: str) -> None:
        async with self._supervisor_lock:
            supervisor = self._supervisors.pop(workspace_key, None)
            if supervisor is None:
                return
            supervisor.mark_closed()
            manager = supervisor.terminal_sessions
        await asyncio.to_thread(manager.shutdown)

    def _cleanup_workspace_root(self, workspace: ResolvedWorkspace) -> None:
        # Transient root deletion is last, after every borrower/resource release
        # (contract §7.1): kill happened during lease release, so the directory is
        # safe to remove now.
        if workspace.cleanup_workspace_root_on_close:
            shutil.rmtree(workspace.workspace_root, ignore_errors=True)

    def _manifest_store(self) -> SessionManifestStore:
        return SessionManifestStore(self.settings.storage.postgres_dsn)
