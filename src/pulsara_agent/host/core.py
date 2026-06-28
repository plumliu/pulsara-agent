"""Reusable host facade for web, desktop, and thin CLI drivers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from pulsara_agent.host.identity import HostWorkspaceInput, ResolvedWorkspace, resolve_workspace
from pulsara_agent.host.registry import HostSessionRegistry, HostSessionSummary
from pulsara_agent.host.session import HostSession
from pulsara_agent.host.supervisor import WorkspaceTerminalSupervisor
from pulsara_agent.llm import ModelRole
from pulsara_agent.llm.request import LLMOptions
from pulsara_agent.runtime.approval import ApprovalResolution, PendingApproval
from pulsara_agent.runtime.agent import AgentRunResult
from pulsara_agent.runtime.permission import EffectivePermissionPolicy
from pulsara_agent.runtime.plan import PendingInteraction, PlanInteractionResolution
from pulsara_agent.runtime.recovery import AbortKind
from pulsara_agent.runtime.wiring import build_agent_runtime_wiring
from pulsara_agent.settings import PulsaraSettings


@dataclass(slots=True)
class HostCore:
    settings: PulsaraSettings
    durable: bool = True
    scratch_root: Path | None = None
    registry: HostSessionRegistry = field(default_factory=HostSessionRegistry)
    use_workspace_supervisor: bool = True
    _supervisors: dict[str, WorkspaceTerminalSupervisor] = field(default_factory=dict, init=False, repr=False)
    _supervisor_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

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
        workspace = resolve_workspace(workspace_input, scratch_root=self.scratch_root)
        host_session_id = host_session_id or f"host:{uuid4().hex}"
        conversation_id = conversation_id or f"conversation:{uuid4().hex}"
        supervisor = await self._attach_supervisor(workspace, host_session_id) if self.use_workspace_supervisor else None
        wiring = build_agent_runtime_wiring(
            self.settings,
            workspace.workspace_root,
            durable=self.durable,
            model_role=model_role,
            options=options,
            system_prompt=system_prompt,
            memory_domain=workspace.memory_domain,
            memory_reflection=memory_reflection,
            terminal_session_manager=supervisor.terminal_sessions if supervisor is not None else None,
            terminal_owner_host_session_id=host_session_id,
            owns_terminal_session_manager=supervisor is None,
            permission_policy=permission_policy,
        )
        session = HostSession(
            host_session_id=host_session_id,
            conversation_id=conversation_id,
            workspace=workspace,
            wiring=wiring,
        )
        return await self.registry.add(session)

    async def get_session(self, host_session_id: str) -> HostSession:
        return await self.registry.get(host_session_id)

    async def find_by_conversation(self, conversation_id: str) -> HostSession | None:
        return await self.registry.find_by_conversation(conversation_id)

    async def close_session(self, host_session_id: str) -> None:
        session = await self.registry.get(host_session_id)
        await self.registry.close_session(host_session_id)
        await self._detach_supervisor(session.workspace.workspace_key, host_session_id)

    async def replay_events(self, host_session_id: str, *, after_sequence: int | None = None):
        session = await self.get_session(host_session_id)
        return session.replay_events(after_sequence=after_sequence)

    async def get_pending_approval(self, host_session_id: str) -> PendingApproval | None:
        session = await self.get_session(host_session_id)
        return session.get_pending_approval()

    async def get_pending_interaction(self, host_session_id: str) -> PendingInteraction | None:
        session = await self.get_session(host_session_id)
        return session.get_pending_interaction()

    async def resolve_approval(
        self,
        host_session_id: str,
        resolution: ApprovalResolution,
    ) -> AgentRunResult:
        session = await self.get_session(host_session_id)
        return await session.resolve_approval(resolution)

    async def resolve_plan_interaction(
        self,
        host_session_id: str,
        resolution: PlanInteractionResolution,
    ) -> AgentRunResult:
        session = await self.get_session(host_session_id)
        return await session.resolve_plan_interaction(resolution)

    async def stop_current_turn(
        self,
        host_session_id: str,
        *,
        reason: AbortKind = AbortKind.USER_STOP,
    ) -> AgentRunResult | None:
        session = await self.get_session(host_session_id)
        return await session.stop_current_turn(reason=reason)

    async def set_permission_mode(
        self,
        host_session_id: str,
        mode: str,
    ):
        session = await self.get_session(host_session_id)
        return session.set_permission_mode(mode)

    async def enter_plan(self, host_session_id: str, *, reason: str = ""):
        session = await self.get_session(host_session_id)
        return session.enter_plan(reason=reason)

    async def stream_approval_resolution(self, host_session_id: str, resolution: ApprovalResolution):
        session = await self.get_session(host_session_id)
        async for event in session.stream_approval_resolution(resolution):
            yield event

    async def stream_plan_interaction_resolution(
        self,
        host_session_id: str,
        resolution: PlanInteractionResolution,
    ):
        session = await self.get_session(host_session_id)
        async for event in session.stream_plan_interaction_resolution(resolution):
            yield event

    async def list_sessions(self) -> list[HostSessionSummary]:
        return await self.registry.list_sessions()

    async def list_workspace_supervisors(self) -> list[dict[str, object]]:
        async with self._supervisor_lock:
            supervisors = list(self._supervisors.values())
        return [supervisor.summary() for supervisor in supervisors]

    async def close_workspace(self, workspace_key: str) -> None:
        summaries = await self.list_sessions()
        for summary in summaries:
            session = await self.get_session(summary.host_session_id)
            if session.workspace.workspace_key == workspace_key:
                await self.registry.close_session(summary.host_session_id)
        async with self._supervisor_lock:
            supervisor = self._supervisors.pop(workspace_key, None)
        if supervisor is not None:
            supervisor.shutdown()

    async def shutdown(self) -> None:
        summaries = await self.list_sessions()
        for summary in summaries:
            await self.close_session(summary.host_session_id)
        async with self._supervisor_lock:
            supervisors = list(self._supervisors.values())
            self._supervisors.clear()
        for supervisor in supervisors:
            supervisor.shutdown()

    async def _attach_supervisor(
        self,
        workspace: ResolvedWorkspace,
        host_session_id: str,
    ) -> WorkspaceTerminalSupervisor:
        async with self._supervisor_lock:
            supervisor = self._supervisors.get(workspace.workspace_key)
            if supervisor is None:
                supervisor = WorkspaceTerminalSupervisor(
                    workspace_key=workspace.workspace_key,
                    workspace_root=workspace.workspace_root,
                )
                self._supervisors[workspace.workspace_key] = supervisor
            supervisor.attach(host_session_id)
            return supervisor

    async def _detach_supervisor(self, workspace_key: str, host_session_id: str) -> None:
        async with self._supervisor_lock:
            supervisor = self._supervisors.get(workspace_key)
            if supervisor is None:
                return
            supervisor.detach(host_session_id, kill_owned=True)
            if not supervisor.owner_sessions and supervisor.terminal_sessions.live_process_count() == 0:
                self._supervisors.pop(workspace_key, None)
