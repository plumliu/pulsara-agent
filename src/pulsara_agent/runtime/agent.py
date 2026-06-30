"""Claude Code-like main loop built on RuntimeSession."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import AsyncIterator, Literal
from uuid import uuid4

from pulsara_agent.capability.types import (
    CapabilityResolveContext,
    CapabilityResolver,
    NoopCapabilityResolver,
    ResolvedCapabilitySet,
)
from pulsara_agent.event import (
    AgentEvent,
    ConfirmResult,
    CustomEvent,
    EventContext,
    ExceedMaxItersEvent,
    PlanExitRequestedEvent,
    PlanExitResolvedEvent,
    PlanModeEnteredEvent,
    PlanModeExitedEvent,
    PlanQuestionAnsweredEvent,
    PlanQuestionAskedEvent,
    ProjectionFailedEvent,
    ProjectionReadyEvent,
    ProjectionRequestedEvent,
    RequireUserConfirmEvent,
    RunEndEvent,
    RunErrorEvent,
    RunStartEvent,
    ToolResultEndEvent,
    UserConfirmResultEvent,
)
from pulsara_agent.llm import LLMRuntime, ModelRole
from pulsara_agent.llm.request import LLMOptions
from pulsara_agent.memory.scope import MemoryDomainContext
from pulsara_agent.message import (
    Msg,
    ToolCallBlock,
    ToolCallState,
    ToolResultState,
    UserMsg,
)
from pulsara_agent.runtime.context import build_llm_context
from pulsara_agent.runtime.approval import ApprovalResolution
from pulsara_agent.runtime.hooks import MemoryHooks, NoopMemoryHooks, ToolResultPersistenceHook
from pulsara_agent.runtime.loop_helpers import (
    _accumulate_usage,
    _final_text,
    _projection_ids,
    _projection_summary,
)
from pulsara_agent.runtime.permission import (
    AllowAllPermissionGate,
    ApprovalPolicy,
    EffectivePermissionPolicy,
    PermissionMode,
    PermissionProfile,
    PermissionState,
    PolicyPermissionGate,
    PermissionDecisionKind,
    PermissionGate,
    TerminalAccess,
    default_permission_policy,
    mode_for_policy,
    parse_permission_mode,
    preset_to_policy,
)
from pulsara_agent.runtime.plan import (
    PlanExitResolution,
    PlanInteractionResolution,
    PlanQuestionResolution,
    PlanWorkflowState,
)
from pulsara_agent.runtime.recovery import (
    AbortKind,
    InRunRecoveryCause,
    InRunRecoveryState,
)
from pulsara_agent.runtime.session import RuntimeSession
from pulsara_agent.runtime.state import LoopBudget, LoopState, LoopStatus, LoopTransition
from pulsara_agent.runtime.tool_taxonomy import PLAN_WORKFLOW_TOOL_NAMES
from pulsara_agent.runtime.tool_loop import (
    _ToolBatchTap,
    _duplicate_tool_call_ids,
    _parse_tool_call,
    _remember_tool_result_event_span,
    _tool_batches,
    _tool_call_blocks,
    _tool_result_from_event_slice,
    build_tool_result_error_events,
)
from pulsara_agent.tools import ToolCall, ToolExecutionResult, ToolExecutor

WorkspaceKind = Literal["project", "transient"]

StopReason = Literal[
    "final",
    "max_turns",
    "model_error",
    "tool_error_budget",
    "plan_interaction_budget",
    "memory_hook_error",
    "waiting_user",
    "aborted",
]


def compose_system_prompt(
    base: str | None,
    *,
    memory_prompt: str | None = None,
    capability_prompt: str | None = None,
    active_skill_prompt: str | None = None,
) -> str | None:
    parts = [part for part in (base, memory_prompt, capability_prompt, active_skill_prompt) if part]
    if not parts:
        return None
    return "\n\n".join(parts)


def _with_memory_context_prompt(system_prompt: str | None, memory_prompt: str | None) -> str | None:
    return compose_system_prompt(system_prompt, memory_prompt=memory_prompt)


@dataclass(slots=True)
class AgentRunResult:
    status: LoopStatus
    stop_reason: StopReason | None
    state: LoopState
    messages: list[Msg]
    final_text: str
    error_message: str | None = None


class AgentRuntime:
    def __init__(
        self,
        *,
        runtime_session: RuntimeSession,
        llm_runtime: LLMRuntime,
        memory_hooks: MemoryHooks | None = None,
        tool_result_persistence_hook: ToolResultPersistenceHook | None = None,
        permission_gate: PermissionGate | None = None,
        model_role: ModelRole = ModelRole.PRO,
        options: LLMOptions | None = None,
        budget: LoopBudget | None = None,
        system_prompt: str | None = None,
        capability_resolver: CapabilityResolver | None = None,
        memory_domain: MemoryDomainContext | None = None,
        workspace_kind: WorkspaceKind = "transient",
        permission_policy: EffectivePermissionPolicy | None = None,
    ) -> None:
        self.runtime_session = runtime_session
        self.llm_runtime = llm_runtime
        self.memory_hooks = memory_hooks or NoopMemoryHooks()
        self.tool_result_persistence_hook = tool_result_persistence_hook
        policy = permission_policy or default_permission_policy()
        # Mutable holder shared by the gate and the terminal tools, so a
        # mid-conversation mode switch (set_permission_policy) is picked up by
        # everyone on the next turn without rebuilding the gate/executor/registry.
        self._permission_state = PermissionState.from_policy(policy)
        self.permission_gate = PolicyPermissionGate(
            self._permission_state,
            inner=permission_gate or AllowAllPermissionGate(),
        )
        self.model_role = model_role
        self.options = options
        self.budget = budget or LoopBudget()
        self.system_prompt = system_prompt
        self.capability_resolver = capability_resolver or NoopCapabilityResolver()
        self.memory_domain = memory_domain
        self.workspace_kind = workspace_kind
        self.tool_executor = runtime_session.create_tool_executor(
            memory_proposal_sink=getattr(self.memory_hooks, "memory_proposal_sink", None),
            memory_recall_service=getattr(self.memory_hooks, "recall", None),
            memory_query=getattr(self.memory_hooks, "memory_query", None),
            graph_id=getattr(self.memory_hooks, "graph_id", None),
            memory_read_scopes=getattr(self.memory_hooks, "read_scopes", None),
            permission_state=self._permission_state,
        )

    @property
    def permission_policy(self) -> EffectivePermissionPolicy:
        return self._permission_state.policy

    @property
    def permission_mode(self) -> PermissionMode | None:
        return self._permission_state.mode

    def set_permission_policy(
        self,
        policy: EffectivePermissionPolicy,
        *,
        mode: PermissionMode | None = None,
    ) -> None:
        """Swap the live permission policy. Takes effect on the next turn for
        the gate and on next execution for the terminal tools. Nothing is
        rebuilt — live terminal processes and the event log are unaffected."""
        self._permission_state.policy = policy
        self._permission_state.mode = mode if mode is not None else mode_for_policy(policy)

    async def run_task(
        self,
        user_input: str,
        *,
        prior_messages: list[Msg] | None = None,
        state: LoopState | None = None,
        active_skill_names: frozenset[str] | None = None,
    ) -> AgentRunResult:
        state = state or self.new_state()
        async for _event in self._stream_task(
            user_input,
            state,
            prior_messages=prior_messages,
            active_skill_names=active_skill_names,
        ):
            pass
        return self._run_result(state)

    async def stream_task(
        self,
        user_input: str,
        *,
        prior_messages: list[Msg] | None = None,
        state: LoopState | None = None,
        active_skill_names: frozenset[str] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        state = state or self.new_state()
        async for event in self._stream_task(
            user_input,
            state,
            prior_messages=prior_messages,
            active_skill_names=active_skill_names,
        ):
            yield event

    async def resume_after_approval(
        self,
        state: LoopState,
        resolution: ApprovalResolution,
    ) -> AgentRunResult:
        async for _event in self.stream_after_approval(state, resolution):
            pass
        return self._run_result(state)

    async def stream_after_approval(
        self,
        state: LoopState,
        resolution: ApprovalResolution,
    ) -> AsyncIterator[AgentEvent]:
        async for event in self._stream_approval_resolution(state, resolution):
            yield event

    async def resume_after_plan_interaction(
        self,
        state: LoopState,
        resolution: PlanInteractionResolution,
    ) -> AgentRunResult:
        async for _event in self.stream_after_plan_interaction(state, resolution):
            pass
        return self._run_result(state)

    async def stream_after_plan_interaction(
        self,
        state: LoopState,
        resolution: PlanInteractionResolution,
    ) -> AsyncIterator[AgentEvent]:
        async for event in self._stream_plan_interaction_resolution(state, resolution):
            yield event

    async def abort_run(
        self,
        state: LoopState,
        *,
        reason: AbortKind = AbortKind.USER_STOP,
    ) -> AgentRunResult:
        async for _event in self.stream_abort_run(state, reason=reason):
            pass
        return self._run_result(state)

    async def stream_abort_run(
        self,
        state: LoopState,
        *,
        reason: AbortKind = AbortKind.USER_STOP,
    ) -> AsyncIterator[AgentEvent]:
        if state.finalized:
            return
        if state.status in {LoopStatus.FINISHED, LoopStatus.FAILED, LoopStatus.ABORTED}:
            return
        state.status = LoopStatus.ABORTED
        state.stop_reason = "aborted"
        state.error_message = None
        state.pending_tool_calls = []
        state.pending_interaction_kind = None
        state.pending_interaction_payload = {}
        state.stop_request = None
        state.abort_kind = reason
        async for event in self._finalize_run(state):
            yield event

    def close(self) -> None:
        self.runtime_session.close()

    def new_state(self) -> LoopState:
        return LoopState(session_id=self.runtime_session.runtime_session_id, budget=self.budget)

    async def _stream_task(
        self,
        user_input: str,
        state: LoopState,
        *,
        prior_messages: list[Msg] | None = None,
        active_skill_names: frozenset[str] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        state.messages.extend(message.model_copy(deep=True) for message in (prior_messages or []))
        state.messages.append(UserMsg(name="user", content=user_input))
        yield await self.runtime_session.emit(
            RunStartEvent(
                **self._event_context(state).event_fields(),
                user_input_chars=len(user_input),
                metadata={"user_input": user_input},
            ),
            state=state,
        )
        async for event in self._emit_pending_plan_entry_audit(state):
            yield event
        ok, _result, error_event = await self._run_memory_hook(
            state,
            "on_turn_start",
            lambda: self._call_turn_start_hook(state, user_input),
        )
        if not ok:
            assert error_event is not None
            yield error_event
            async for event in self._finalize_run(state, run_session_end_hook=False):
                yield event
            return
        capabilities = self._resolve_capabilities(
            state,
            user_input=user_input,
            prior_messages=prior_messages,
            active_skill_names=active_skill_names,
        )
        state.scratchpad["capabilities"] = capabilities

        async for event in self._stream_model_loop(state, capabilities):
            yield event

    def _resolve_capabilities(
        self,
        state: LoopState,
        *,
        user_input: str,
        prior_messages: list[Msg] | None = None,
        active_skill_names: frozenset[str] | None = None,
    ) -> ResolvedCapabilitySet:
        return self.capability_resolver.resolve(
            CapabilityResolveContext(
                workspace_root=self.runtime_session.workspace_root,
                workspace_kind=self.workspace_kind,
                memory_domain=self.memory_domain,
                available_tool_names=frozenset(self.tool_executor.registry.names()),
                user_input=user_input,
                prior_messages=tuple(prior_messages or ()),
                active_skill_names=active_skill_names or frozenset(),
            )
        )

    async def _emit_pending_plan_entry_audit(self, state: LoopState) -> AsyncIterator[AgentEvent]:
        payload = state.scratchpad.get("plan_entry_audit")
        if not isinstance(payload, dict):
            return
        if state.scratchpad.get("plan_entry_audit_emitted"):
            return
        event = await self.runtime_session.emit(
            PlanModeEnteredEvent(
                **self._event_context(state).event_fields(),
                source="user",
                previous_permission_mode=payload.get("previous_permission_mode"),
                previous_permission_policy=dict(payload.get("previous_permission_policy") or {}),
                reason=str(payload.get("reason") or ""),
            ),
            state=state,
        )
        state.scratchpad["plan_entry_audit_emitted"] = True
        yield event

    async def _stream_model_loop(
        self,
        state: LoopState,
        capabilities: ResolvedCapabilitySet,
    ) -> AsyncIterator[AgentEvent]:
        while state.status is LoopStatus.RUNNING:
            if self._apply_stop_request(state):
                break
            if state.turn_index >= self.budget.max_turns:
                state.status = LoopStatus.FAILED
                state.stop_reason = "max_turns"
                state.transition(LoopTransition.EXCEED_MAX_ITERS)
                event = await self.runtime_session.emit(
                    ExceedMaxItersEvent(
                        **self._event_context(state).event_fields(),
                        name="agent_runtime",
                        max_iters=self.budget.max_turns,
                    ),
                    state=state,
                )
                yield event
                break

            async for event in self._project_memory(state):
                yield event

            context = build_llm_context(
                state=state,
                registry=self.tool_executor.registry,
                system_prompt=compose_system_prompt(
                    self.system_prompt,
                    memory_prompt=getattr(self.memory_hooks, "memory_context_prompt", lambda: None)(),
                    capability_prompt=capabilities.catalog_prompt,
                    active_skill_prompt=capabilities.active_skill_prompt,
                ),
                budget=self.budget,
            )

            reply_had_run_error = False
            try:
                async for event in self.llm_runtime.stream(
                    role=self.model_role,
                    context=context,
                    event_context=self._event_context(state),
                    options=self.options,
                ):
                    stored = await self.runtime_session.emit(event, state=state)
                    if isinstance(stored, RunErrorEvent):
                        reply_had_run_error = True
                    yield stored
            except Exception as exc:
                event = await self.runtime_session.emit(
                    RunErrorEvent(
                        **self._event_context(state).event_fields(),
                        message=f"{type(exc).__name__}: {exc}",
                        code="model_stream_error",
                    ),
                    state=state,
                )
                reply_had_run_error = True
                yield event

            if self._apply_stop_request(state):
                break
            if reply_had_run_error:
                if not self._recover_or_fail_model(state):
                    break
                state.begin_next_turn()
                continue

            assistant = self.runtime_session.event_log.replay(state.reply_id)
            state.messages.append(assistant)
            _accumulate_usage(state, assistant)
            ok, hook_events = await self._run_memory_hook_and_emit_events(
                state,
                "after_model_reply",
                lambda: self.memory_hooks.after_model_reply(state, assistant),
            )
            for event in hook_events:
                yield event
            if not ok:
                break

            tool_blocks = _tool_call_blocks(assistant)
            if not tool_blocks:
                state.status = LoopStatus.FINISHED
                state.stop_reason = "final"
                state.transition(LoopTransition.FINISH)
                break

            state.pending_tool_calls = tool_blocks
            state.transition(LoopTransition.CONTINUE_AFTER_MODEL)
            async for event in self._execute_tool_blocks(state, tool_blocks):
                yield event
            if self._apply_stop_request(state):
                break
            if state.status is not LoopStatus.RUNNING:
                break

            async for event in self._after_tool_results(state):
                yield event
            if self._apply_stop_request(state):
                break
            if state.status is not LoopStatus.RUNNING:
                break
            state.transition(LoopTransition.CONTINUE_AFTER_TOOL)
            state.begin_next_turn()

        if state.status is LoopStatus.WAITING_USER:
            return
        async for event in self._finalize_run(state):
            yield event

    def _apply_stop_request(self, state: LoopState) -> bool:
        request = state.stop_request
        if request is None:
            return False
        state.stop_request = None
        if state.status is not LoopStatus.RUNNING:
            return state.status is LoopStatus.ABORTED
        state.status = LoopStatus.ABORTED
        state.stop_reason = "aborted"
        state.error_message = None
        state.pending_tool_calls = []
        state.pending_interaction_kind = None
        state.pending_interaction_payload = {}
        state.abort_kind = request.reason
        return True

    async def _stream_approval_resolution(
        self,
        state: LoopState,
        resolution: ApprovalResolution,
    ) -> AsyncIterator[AgentEvent]:
        if state.status is not LoopStatus.WAITING_USER:
            raise ValueError("approval resolution requires a waiting state")
        pending_by_id = {call.id: call for call in state.pending_tool_calls}
        if not pending_by_id:
            raise ValueError("approval resolution requires pending tool calls")
        decisions_by_id = {decision.tool_call_id: decision for decision in resolution.decisions}
        unknown_ids = set(decisions_by_id).difference(pending_by_id)
        if unknown_ids:
            raise ValueError(f"approval resolution referenced unknown tool calls: {sorted(unknown_ids)}")
        missing_ids = set(pending_by_id).difference(decisions_by_id)
        if missing_ids:
            raise ValueError(f"approval resolution missing decisions for tool calls: {sorted(missing_ids)}")

        confirm_results = [
            ConfirmResult(
                confirmed=decisions_by_id[call.id].confirmed,
                tool_call=call.model_copy(deep=True),
                rules=list(decisions_by_id[call.id].rules) or None,
            )
            for call in state.pending_tool_calls
        ]
        event = await self.runtime_session.emit(
            UserConfirmResultEvent(**self._event_context(state).event_fields(), confirm_results=confirm_results),
            state=state,
        )
        yield event

        state.status = LoopStatus.RUNNING
        state.stop_reason = None
        async for event in self._stream_confirmed_tool_blocks(state, decisions_by_id):
            yield event
        if state.status is not LoopStatus.RUNNING:
            async for event in self._finalize_run(state):
                yield event
            return

        async for event in self._after_tool_results(state):
            yield event
        if state.status is not LoopStatus.RUNNING:
            async for event in self._finalize_run(state):
                yield event
            return
        state.transition(LoopTransition.CONTINUE_AFTER_TOOL)
        state.begin_next_turn()
        capabilities = state.scratchpad.get("capabilities")
        if not isinstance(capabilities, ResolvedCapabilitySet):
            capabilities = self._resolve_capabilities(
                state,
                user_input="",
                prior_messages=[],
                active_skill_names=frozenset(),
            )
            state.scratchpad["capabilities"] = capabilities
        async for event in self._stream_model_loop(state, capabilities):
            yield event

    async def _stream_plan_interaction_resolution(
        self,
        state: LoopState,
        resolution: PlanInteractionResolution,
    ) -> AsyncIterator[AgentEvent]:
        if state.status is not LoopStatus.WAITING_USER:
            raise ValueError("plan interaction resolution requires a waiting state")
        if state.pending_interaction_kind != "plan":
            raise ValueError("waiting state does not contain a pending plan interaction")
        payload = dict(state.pending_interaction_payload)
        if resolution.interaction_id != payload.get("interaction_id"):
            raise ValueError("plan interaction id does not match the pending interaction")
        kind = payload.get("kind")
        if kind == "question":
            if not isinstance(resolution, PlanQuestionResolution):
                raise ValueError("question interaction requires PlanQuestionResolution")
            async for event in self._resolve_plan_question(state, payload, resolution):
                yield event
        elif kind == "exit":
            if not isinstance(resolution, PlanExitResolution):
                raise ValueError("exit interaction requires PlanExitResolution")
            async for event in self._resolve_plan_exit(state, payload, resolution):
                yield event
        else:
            raise ValueError("pending plan interaction has invalid kind")

        state.pending_interaction_kind = None
        state.pending_interaction_payload = {}
        if state.status is LoopStatus.WAITING_USER:
            state.status = LoopStatus.RUNNING
            state.stop_reason = None

        async for event in self._after_tool_results(state):
            yield event
        if state.status is not LoopStatus.RUNNING:
            async for event in self._finalize_run(state):
                yield event
            return
        state.transition(LoopTransition.CONTINUE_AFTER_TOOL)
        state.begin_next_turn()
        capabilities = state.scratchpad.get("capabilities")
        if not isinstance(capabilities, ResolvedCapabilitySet):
            capabilities = self._resolve_capabilities(
                state,
                user_input="",
                prior_messages=[],
                active_skill_names=frozenset(),
            )
            state.scratchpad["capabilities"] = capabilities
        async for event in self._stream_model_loop(state, capabilities):
            yield event

    async def _resolve_plan_question(
        self,
        state: LoopState,
        payload: dict,
        resolution: PlanQuestionResolution,
    ) -> AsyncIterator[AgentEvent]:
        question_id = str(payload.get("question_id") or "")
        tool_call_id = str(payload["tool_call_id"])
        tool_name = "ask_plan_question"
        yield await self.runtime_session.emit(
            PlanQuestionAnsweredEvent(
                **self._event_context(state).event_fields(),
                question_id=question_id,
                answer_text=resolution.answer_text,
                selected_option=resolution.selected_option,
            ),
            state=state,
        )
        output = json.dumps(
            {
                "answer_text": resolution.answer_text,
                "selected_option": resolution.selected_option,
            },
            ensure_ascii=False,
        )
        async for event in self._emit_tool_result_and_record(
            state,
            tool_call_id=tool_call_id,
            tool_call_name=tool_name,
            output=output,
            result_state=ToolResultState.SUCCESS,
        ):
            yield event

    async def _resolve_plan_exit(
        self,
        state: LoopState,
        payload: dict,
        resolution: PlanExitResolution,
    ) -> AsyncIterator[AgentEvent]:
        exit_request_id = str(payload.get("exit_request_id") or "")
        tool_call_id = str(payload["tool_call_id"])
        yield await self.runtime_session.emit(
            PlanExitResolvedEvent(
                **self._event_context(state).event_fields(),
                exit_request_id=exit_request_id,
                tool_call_id=tool_call_id,
                decision=resolution.decision,
                user_feedback=resolution.user_feedback,
            ),
            state=state,
        )
        if resolution.decision == "revise":
            revisions = int(state.scratchpad.get("plan_exit_revisions", 0)) + 1
            state.scratchpad["plan_exit_revisions"] = revisions
            if revisions > state.budget.max_plan_exit_revisions_per_run:
                yield await self._mark_plan_budget_exceeded(state, kind="exit_revision")
        if resolution.decision == "approve":
            plan_state = self._plan_state(state)
            event_context = self._event_context(state)
            accepted_summary = str(payload.get("summary") or "")
            accepted_plan_text = str(payload.get("plan_text") or "")
            accepted_artifact_id = _accepted_plan_artifact_id(
                event_context.run_id,
                exit_request_id,
            )
            self.runtime_session.archive.put_text(
                accepted_artifact_id,
                accepted_plan_text,
                session_id=self.runtime_session.runtime_session_id,
                run_id=event_context.run_id,
                media_type="text/plain; charset=utf-8",
                metadata={
                    "kind": "accepted_plan",
                    "exit_request_id": exit_request_id,
                    "tool_call_id": tool_call_id,
                    "summary": accepted_summary,
                },
            )
            restored_mode = plan_state.pre_plan_permission_mode
            restored_policy = self._policy_from_plan_state(plan_state)
            self.set_permission_policy(
                restored_policy,
                mode=parse_permission_mode(restored_mode) if restored_mode is not None else None,
            )
            yield await self.runtime_session.emit(
                PlanModeExitedEvent(
                    **event_context.event_fields(),
                    source="approved_exit_plan",
                    exit_request_id=exit_request_id,
                    restored_permission_mode=restored_mode,
                    restored_permission_policy=restored_policy.to_dict(),
                    accepted_plan_summary=accepted_summary,
                    accepted_plan_artifact_id=accepted_artifact_id,
                ),
                state=state,
            )
            plan_state.finish(
                accepted_plan_summary=accepted_summary,
                accepted_plan_artifact_id=accepted_artifact_id,
            )
            _remove_plan_runtime_instructions(state)
        output = json.dumps(
            {
                "decision": resolution.decision,
                "user_feedback": resolution.user_feedback,
            },
            ensure_ascii=False,
        )
        async for event in self._emit_tool_result_and_record(
            state,
            tool_call_id=tool_call_id,
            tool_call_name="exit_plan",
            output=output,
            result_state=ToolResultState.SUCCESS,
        ):
            yield event

    async def _after_tool_results(self, state: LoopState) -> AsyncIterator[AgentEvent]:
        tool_error_count = sum(1 for result in state.tool_results if result.state is not ToolResultState.SUCCESS)
        if tool_error_count:
            state.consecutive_tool_failures += tool_error_count
            state.in_run_recovery = InRunRecoveryState(
                cause=InRunRecoveryCause.TOOL_FAILURE,
                consecutive_failures=state.consecutive_tool_failures,
            )
            if state.consecutive_tool_failures > self.budget.max_consecutive_tool_failures:
                state.status = LoopStatus.FAILED
                state.stop_reason = "tool_error_budget"
                state.error_message = "tool error budget exceeded"
                state.transition(LoopTransition.FAIL)
                return
        else:
            state.consecutive_tool_failures = 0
            state.in_run_recovery = None

        if self.tool_result_persistence_hook is not None:
            event = await self._run_tool_result_persistence_hook(state)
            if event is not None:
                yield event
        ok, hook_events = await self._run_memory_hook_and_emit_events(
            state,
            "after_tool_results",
            lambda: self.memory_hooks.after_tool_results(state, state.tool_results),
        )
        for event in hook_events:
            yield event
        if not ok:
            return
        ok, should_compact, error_event = await self._run_memory_hook(
            state,
            "should_compact",
            lambda: self.memory_hooks.should_compact(state),
        )
        if not ok:
            assert error_event is not None
            yield error_event
            return
        if should_compact:
            state.compacted = True
            yield await self.runtime_session.emit(
                CustomEvent(
                    **self._event_context(state).event_fields(),
                    name="compaction_requested",
                    value={},
                ),
                state=state,
            )

    async def _finalize_run(
        self,
        state: LoopState,
        *,
        run_session_end_hook: bool = True,
    ) -> AsyncIterator[AgentEvent]:
        if state.finalized:
            return
        state.finalized = True
        if run_session_end_hook:
            _ok, hook_events = await self._run_memory_hook_and_emit_events(
                state,
                "on_turn_end",
                lambda: self._call_turn_end_hook(state),
            )
            for event in hook_events:
                yield event
        yield await self.runtime_session.emit(
            RunEndEvent(
                **self._event_context(state).event_fields(),
                status=state.status.value,
                stop_reason=state.stop_reason,
                abort_kind=state.abort_kind.value if state.abort_kind is not None else None,
                error_message=state.error_message,
            ),
            state=state,
        )

    def _run_result(self, state: LoopState) -> AgentRunResult:
        return AgentRunResult(
            status=state.status,
            stop_reason=state.stop_reason,
            state=state,
            messages=list(state.messages),
            final_text=_final_text(state.messages),
            error_message=state.error_message,
        )

    async def _run_memory_hook(self, state: LoopState, hook_name: str, call):
        try:
            return True, await call(), None
        except Exception as exc:
            event = await self._mark_memory_hook_failed(state, hook_name, exc)
            return False, None, event

    async def _call_turn_start_hook(self, state: LoopState, user_input: str):
        hook = getattr(self.memory_hooks, "on_turn_start", None)
        if hook is not None and _is_overridden_hook(self.memory_hooks, "on_turn_start", NoopMemoryHooks):
            return await hook(state, user_input)
        return await self.memory_hooks.on_session_start(state, user_input)

    async def _call_turn_end_hook(self, state: LoopState):
        hook = getattr(self.memory_hooks, "on_turn_end", None)
        if hook is not None and _is_overridden_hook(self.memory_hooks, "on_turn_end", NoopMemoryHooks):
            return await hook(state)
        return await self.memory_hooks.on_session_end(state)

    async def _run_memory_hook_and_emit_events(
        self,
        state: LoopState,
        hook_name: str,
        call,
    ) -> tuple[bool, list[AgentEvent]]:
        ok, produced_events, error_event = await self._run_memory_hook(state, hook_name, call)
        if not ok:
            assert error_event is not None
            return False, [error_event]
        emitted_events: list[AgentEvent] = []
        try:
            for event in produced_events or ():
                emitted_events.append(await self.runtime_session.emit(event, state=state))
        except Exception as exc:
            emitted_events.append(await self._mark_memory_hook_failed(state, hook_name, exc))
            return False, emitted_events
        return True, emitted_events

    async def _run_tool_result_persistence_hook(self, state: LoopState) -> AgentEvent | None:
        assert self.tool_result_persistence_hook is not None
        try:
            await self.tool_result_persistence_hook.after_tool_results(state, state.tool_results)
            return None
        except Exception as exc:
            return await self.runtime_session.emit(
                CustomEvent(
                    **self._event_context(state).event_fields(),
                    name="tool_result_persistence_failed",
                    value={
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    },
                ),
                state=state,
            )

    async def _mark_memory_hook_failed(self, state: LoopState, hook_name: str, exc: Exception) -> AgentEvent:
        message = f"memory hook {hook_name} failed: {type(exc).__name__}: {exc}"
        state.status = LoopStatus.FAILED
        state.stop_reason = "memory_hook_error"
        state.error_message = message
        state.transition(LoopTransition.FAIL)
        return await self.runtime_session.emit(
            RunErrorEvent(
                **self._event_context(state).event_fields(),
                message=message,
                code="memory_hook_error",
                metadata={"hook": hook_name},
            ),
            state=state,
        )

    async def _mark_tool_budget_exceeded(self, state: LoopState, *, attempted_count: int) -> AgentEvent:
        message = (
            "tool call budget exceeded before execution: "
            f"current={state.tool_call_count}, attempted={attempted_count}, max={self.budget.max_tool_calls}"
        )
        state.status = LoopStatus.FAILED
        state.stop_reason = "tool_error_budget"
        state.error_message = message
        state.transition(LoopTransition.FAIL)
        return await self.runtime_session.emit(
            RunErrorEvent(
                **self._event_context(state).event_fields(),
                message=message,
                code="tool_budget_exceeded",
                metadata={
                    "current_tool_call_count": state.tool_call_count,
                    "attempted_tool_call_count": attempted_count,
                    "max_tool_calls": self.budget.max_tool_calls,
                },
            ),
            state=state,
        )

    async def _project_memory(self, state: LoopState) -> AsyncIterator[AgentEvent]:
        projection_id = f"projection:{state.turn_id}"
        context = self._event_context(state)
        yield await self.runtime_session.emit(
            ProjectionRequestedEvent(
                **context.event_fields(),
                projection_id=projection_id,
                role=self.model_role.value,
                scope=state.current_scope or "session",
                token_budget=self.budget.projection_token_budget,
            ),
            state=state,
        )
        try:
            projection = await asyncio.wait_for(
                self.memory_hooks.project(
                    state,
                    token_budget=self.budget.projection_token_budget,
                ),
                timeout=self.budget.recall_hard_timeout_ms / 1000,
            )
        except TimeoutError:
            state.memory_projection = None
            yield await self.runtime_session.emit(
                ProjectionFailedEvent(
                    **context.event_fields(),
                    projection_id=projection_id,
                    role=self.model_role.value,
                    scope=state.current_scope or "session",
                    token_budget=self.budget.projection_token_budget,
                    error="recall_timeout",
                ),
                state=state,
            )
            return
        except Exception as exc:
            state.memory_projection = None
            yield await self.runtime_session.emit(
                ProjectionFailedEvent(
                    **context.event_fields(),
                    projection_id=projection_id,
                    role=self.model_role.value,
                    scope=state.current_scope or "session",
                    token_budget=self.budget.projection_token_budget,
                    error=f"{type(exc).__name__}: {exc}",
                ),
                state=state,
            )
            return
        state.memory_projection = projection
        yield await self.runtime_session.emit(
            ProjectionReadyEvent(
                **context.event_fields(),
                projection_id=projection_id,
                role=self.model_role.value,
                scope=state.current_scope or "session",
                token_budget=self.budget.projection_token_budget,
                included_memory_ids=_projection_ids(projection),
                summary=_projection_summary(projection),
            ),
            state=state,
        )

    async def _execute_tool_blocks(
        self,
        state: LoopState,
        tool_blocks: list[ToolCallBlock],
    ) -> AsyncIterator[AgentEvent]:
        parsed_calls: list[ToolCall] = []
        for block in tool_blocks:
            try:
                parsed_calls.append(_parse_tool_call(block))
            except ValueError as exc:
                stored_events = await self.runtime_session.emit_many(
                    build_tool_result_error_events(
                        self._event_context(state),
                        tool_call_id=block.id,
                        tool_call_name=block.name,
                        message=str(exc),
                    ),
                    state=state,
                )
                for event in stored_events:
                    yield event
                result_block = _tool_result_from_event_slice(stored_events, block.id)
                _remember_tool_result_event_span(state, stored_events, block.id)
                state.tool_results.append(result_block)
                state.messages.append(
                    Msg(
                        role="tool_result",
                        name=block.name,
                        id=f"tool-result-message:{block.id}",
                        content=[result_block],
                    )
                )

        if not parsed_calls:
            return

        duplicate_ids = _duplicate_tool_call_ids(parsed_calls)
        if duplicate_ids:
            unique_calls: list[ToolCall] = []
            for call in parsed_calls:
                if call.id not in duplicate_ids:
                    unique_calls.append(call)
                    continue
                stored_events = await self.runtime_session.emit_many(
                    build_tool_result_error_events(
                        self._event_context(state),
                        tool_call_id=call.id,
                        tool_call_name=call.name,
                        message=f"Duplicate tool_call_id in assistant reply: {call.id}",
                    ),
                    state=state,
                )
                for event in stored_events:
                    yield event
                result_block = _tool_result_from_event_slice(stored_events, call.id)
                _remember_tool_result_event_span(state, stored_events, call.id)
                state.tool_results.append(result_block)
                state.messages.append(
                    Msg(
                        role="tool_result",
                        name=call.name,
                        id=f"tool-result-message:{call.id}",
                        content=[result_block],
                    )
                )
            parsed_calls = unique_calls
            if not parsed_calls:
                return

        if any(call.name in PLAN_WORKFLOW_TOOL_NAMES for call in parsed_calls):
            async for event in self._handle_workflow_tool_batch(state, parsed_calls):
                yield event
            return

        decision = await self.permission_gate.evaluate(parsed_calls)
        if decision.kind is PermissionDecisionKind.WAIT_FOR_USER:
            blocks = [
                ToolCallBlock(
                    id=call.id,
                    name=call.name,
                    input=json.dumps(call.arguments),
                    state=ToolCallState.ASKING,
                    suggested_rules=decision.suggested_rules,
                )
                for call in parsed_calls
            ]
            state.pending_tool_calls = blocks
            state.status = LoopStatus.WAITING_USER
            state.stop_reason = "waiting_user"
            state.transition(LoopTransition.WAIT_FOR_USER)
            event = await self.runtime_session.emit(
                RequireUserConfirmEvent(**self._event_context(state).event_fields(), tool_calls=blocks),
                state=state,
            )
            yield event
            return
        if decision.kind is PermissionDecisionKind.DENY:
            for call in parsed_calls:
                stored_events = await self.runtime_session.emit_many(
                    build_tool_result_error_events(
                        self._event_context(state),
                        tool_call_id=call.id,
                        tool_call_name=call.name,
                        message=decision.reason or "tool call denied by permission gate",
                        state=ToolResultState.DENIED,
                    ),
                    state=state,
                )
                for event in stored_events:
                    yield event
                result_block = _tool_result_from_event_slice(stored_events, call.id)
                _remember_tool_result_event_span(state, stored_events, call.id)
                state.tool_results.append(result_block)
                state.messages.append(
                    Msg(
                        role="tool_result",
                        name=call.name,
                        id=f"tool-result-message:{call.id}",
                        content=[result_block],
                    )
                )
            return

        async for event in self._stream_parsed_tool_calls(state, parsed_calls):
            yield event

    async def _handle_workflow_tool_batch(
        self,
        state: LoopState,
        parsed_calls: list[ToolCall],
    ) -> AsyncIterator[AgentEvent]:
        workflow_index = next(
            index for index, call in enumerate(parsed_calls) if call.name in PLAN_WORKFLOW_TOOL_NAMES
        )
        workflow_call = parsed_calls[workflow_index]
        try:
            if workflow_call.name == "enter_plan":
                async for event in self._execute_enter_plan(state, workflow_call):
                    yield event
            elif workflow_call.name == "ask_plan_question":
                async for event in self._execute_ask_plan_question(state, workflow_call):
                    yield event
            elif workflow_call.name == "exit_plan":
                async for event in self._execute_exit_plan(state, workflow_call):
                    yield event
            else:
                async for event in self._emit_tool_result_and_record(
                    state,
                    tool_call_id=workflow_call.id,
                    tool_call_name=workflow_call.name,
                    output=f"unknown workflow tool: {workflow_call.name}",
                    result_state=ToolResultState.ERROR,
                ):
                    yield event
        except Exception as exc:
            async for event in self._emit_tool_result_and_record(
                state,
                tool_call_id=workflow_call.id,
                tool_call_name=workflow_call.name,
                output=f"[TOOL_ERROR] {type(exc).__name__}: {exc}",
                result_state=ToolResultState.ERROR,
            ):
                yield event

        for index, call in enumerate(parsed_calls):
            if index == workflow_index:
                continue
            async for event in self._emit_tool_result_and_record(
                state,
                tool_call_id=call.id,
                tool_call_name=call.name,
                output=(
                    "not executed because a plan workflow control tool suspended or changed workflow state; "
                    "retry after the workflow step completes"
                ),
                result_state=ToolResultState.DENIED,
            ):
                yield event

    async def _execute_enter_plan(self, state: LoopState, call: ToolCall) -> AsyncIterator[AgentEvent]:
        plan_state = self._plan_state(state)
        if plan_state.active:
            output = json.dumps({"status": "already_active"}, ensure_ascii=False)
            async for event in self._emit_tool_result_and_record(
                state,
                tool_call_id=call.id,
                tool_call_name=call.name,
                output=output,
                result_state=ToolResultState.SUCCESS,
            ):
                yield event
            return
        reason = _optional_str(call.arguments.get("reason"))
        previous_mode = self.permission_mode
        previous_policy = self.permission_policy
        plan_state.begin(
            source="agent",
            previous_mode=previous_mode,
            previous_policy=previous_policy,
            reason=reason,
            pending_entry_audit=False,
        )
        self.set_permission_policy(
            preset_to_policy(PermissionMode.READ_ONLY),
            mode=PermissionMode.READ_ONLY,
        )
        yield await self.runtime_session.emit(
            PlanModeEnteredEvent(
                **self._event_context(state).event_fields(),
                source="agent",
                previous_permission_mode=(
                    previous_mode.value if isinstance(previous_mode, PermissionMode) else previous_mode
                ),
                previous_permission_policy=previous_policy.to_dict(),
                reason=reason,
            ),
            state=state,
        )
        output = json.dumps({"status": "entered", "permission_mode": PermissionMode.READ_ONLY.value}, ensure_ascii=False)
        async for event in self._emit_tool_result_and_record(
            state,
            tool_call_id=call.id,
            tool_call_name=call.name,
            output=output,
            result_state=ToolResultState.SUCCESS,
        ):
            yield event

    async def _execute_ask_plan_question(self, state: LoopState, call: ToolCall) -> AsyncIterator[AgentEvent]:
        if not self._plan_state(state).active:
            async for event in self._emit_tool_result_and_record(
                state,
                tool_call_id=call.id,
                tool_call_name=call.name,
                output="ask_plan_question can only be used while Plan workflow is active",
                result_state=ToolResultState.DENIED,
            ):
                yield event
            return
        if not self._consume_plan_interaction_budget(state):
            async for event in self._emit_plan_budget_error_result(state, call, kind="interaction"):
                yield event
            return
        question = _required_str(call.arguments.get("question"), "question")
        raw_options = call.arguments.get("options")
        if raw_options is None:
            raw_options = []
        if not isinstance(raw_options, list):
            raise ValueError("options must be a list of strings")
        options = tuple(str(option) for option in raw_options)
        allow_free_text = bool(call.arguments.get("allow_free_text", True))
        reason = _optional_str(call.arguments.get("reason"))
        question_id = f"plan_question:{uuid4().hex}"
        interaction_id = f"plan_interaction:{uuid4().hex}"
        yield await self.runtime_session.emit(
            PlanQuestionAskedEvent(
                **self._event_context(state).event_fields(),
                question_id=question_id,
                tool_call_id=call.id,
                question=question,
                options=list(options),
                allow_free_text=allow_free_text,
                reason=reason,
            ),
            state=state,
        )
        state.pending_tool_calls = []
        state.pending_interaction_kind = "plan"
        state.pending_interaction_payload = {
            "interaction_id": interaction_id,
            "kind": "question",
            "tool_call_id": call.id,
            "question_id": question_id,
            "question": question,
            "options": list(options),
            "allow_free_text": allow_free_text,
        }
        state.status = LoopStatus.WAITING_USER
        state.stop_reason = "waiting_user"
        state.transition(LoopTransition.WAIT_FOR_USER)

    async def _execute_exit_plan(self, state: LoopState, call: ToolCall) -> AsyncIterator[AgentEvent]:
        if not self._plan_state(state).active:
            async for event in self._emit_tool_result_and_record(
                state,
                tool_call_id=call.id,
                tool_call_name=call.name,
                output="exit_plan can only be used while Plan workflow is active",
                result_state=ToolResultState.DENIED,
            ):
                yield event
            return
        if not self._consume_plan_interaction_budget(state):
            async for event in self._emit_plan_budget_error_result(state, call, kind="interaction"):
                yield event
            return
        plan_text = _required_str(call.arguments.get("plan"), "plan")
        summary = _optional_str(call.arguments.get("summary"))
        exit_request_id = f"plan_exit:{uuid4().hex}"
        interaction_id = f"plan_interaction:{uuid4().hex}"
        yield await self.runtime_session.emit(
            PlanExitRequestedEvent(
                **self._event_context(state).event_fields(),
                exit_request_id=exit_request_id,
                tool_call_id=call.id,
                plan_text=plan_text,
                summary=summary,
            ),
            state=state,
        )
        state.pending_tool_calls = []
        state.pending_interaction_kind = "plan"
        state.pending_interaction_payload = {
            "interaction_id": interaction_id,
            "kind": "exit",
            "tool_call_id": call.id,
            "exit_request_id": exit_request_id,
            "plan_text": plan_text,
            "summary": summary,
        }
        state.status = LoopStatus.WAITING_USER
        state.stop_reason = "waiting_user"
        state.transition(LoopTransition.WAIT_FOR_USER)

    async def _emit_tool_result_and_record(
        self,
        state: LoopState,
        *,
        tool_call_id: str,
        tool_call_name: str,
        output: str,
        result_state: ToolResultState,
    ) -> AsyncIterator[AgentEvent]:
        stored_events = await self.runtime_session.emit_many(
            build_tool_result_error_events(
                self._event_context(state),
                tool_call_id=tool_call_id,
                tool_call_name=tool_call_name,
                message=output,
                state=result_state,
            ),
            state=state,
        )
        for event in stored_events:
            yield event
        result_block = _tool_result_from_event_slice(stored_events, tool_call_id)
        _remember_tool_result_event_span(state, stored_events, tool_call_id)
        state.tool_results.append(result_block)
        state.messages.append(
            Msg(
                role="tool_result",
                name=tool_call_name,
                id=f"tool-result-message:{tool_call_id}",
                content=[result_block],
            )
        )

    def _plan_state(self, state: LoopState) -> PlanWorkflowState:
        plan_state = state.scratchpad.get("plan_state")
        if isinstance(plan_state, PlanWorkflowState):
            return plan_state
        plan_state = PlanWorkflowState()
        state.scratchpad["plan_state"] = plan_state
        return plan_state

    def _consume_plan_interaction_budget(self, state: LoopState) -> bool:
        consumed = int(state.scratchpad.get("plan_interactions", 0))
        if consumed >= state.budget.max_plan_interactions_per_run:
            state.status = LoopStatus.FAILED
            state.stop_reason = "plan_interaction_budget"
            state.error_message = "plan interaction budget exceeded"
            state.transition(LoopTransition.FAIL)
            return False
        state.scratchpad["plan_interactions"] = consumed + 1
        return True

    async def _emit_plan_budget_error_result(
        self,
        state: LoopState,
        call: ToolCall,
        *,
        kind: str,
    ) -> AsyncIterator[AgentEvent]:
        message = f"plan {kind} budget exceeded"
        async for event in self._emit_tool_result_and_record(
            state,
            tool_call_id=call.id,
            tool_call_name=call.name,
            output=message,
            result_state=ToolResultState.ERROR,
        ):
            yield event
        yield await self.runtime_session.emit(
            RunErrorEvent(
                **self._event_context(state).event_fields(),
                message=message,
                code="plan_interaction_budget_exceeded",
            ),
            state=state,
        )

    async def _mark_plan_budget_exceeded(self, state: LoopState, *, kind: str) -> AgentEvent:
        message = f"plan {kind} budget exceeded"
        state.status = LoopStatus.FAILED
        state.stop_reason = "plan_interaction_budget"
        state.error_message = message
        state.transition(LoopTransition.FAIL)
        return await self.runtime_session.emit(
            RunErrorEvent(
                **self._event_context(state).event_fields(),
                message=message,
                code="plan_interaction_budget_exceeded",
            ),
            state=state,
        )

    def _policy_from_plan_state(self, plan_state: PlanWorkflowState) -> EffectivePermissionPolicy:
        payload = plan_state.pre_plan_permission_policy or {}
        if not payload:
            return default_permission_policy()
        return EffectivePermissionPolicy(
            profile=PermissionProfile(str(payload["profile"])),
            approval=ApprovalPolicy(str(payload["approval_policy"])),
            terminal=TerminalAccess(str(payload["terminal_access"])),
            execution_boundary="host",
            network_isolated=bool(payload.get("network_isolated", False)),
        )

    async def _stream_confirmed_tool_blocks(
        self,
        state: LoopState,
        decisions_by_id,
    ) -> AsyncIterator[AgentEvent]:
        parsed_calls: list[ToolCall] = []
        async def flush_parsed_calls() -> AsyncIterator[AgentEvent]:
            nonlocal parsed_calls
            if not parsed_calls:
                return
            calls = parsed_calls
            parsed_calls = []
            async for event in self._stream_parsed_tool_calls(state, calls):
                yield event

        for block in state.pending_tool_calls:
            decision = decisions_by_id[block.id]
            if not decision.confirmed:
                async for event in flush_parsed_calls():
                    yield event
                stored_events = await self.runtime_session.emit_many(
                    build_tool_result_error_events(
                        self._event_context(state),
                        tool_call_id=block.id,
                        tool_call_name=block.name,
                        message="tool call denied by user approval",
                        state=ToolResultState.DENIED,
                    ),
                    state=state,
                )
                for event in stored_events:
                    yield event
                result_block = _tool_result_from_event_slice(stored_events, block.id)
                _remember_tool_result_event_span(state, stored_events, block.id)
                state.tool_results.append(result_block)
                state.messages.append(
                    Msg(
                        role="tool_result",
                        name=block.name,
                        id=f"tool-result-message:{block.id}",
                        content=[result_block],
                    )
                )
                continue
            try:
                parsed_calls.append(_parse_tool_call(block))
            except ValueError as exc:
                async for event in flush_parsed_calls():
                    yield event
                stored_events = await self.runtime_session.emit_many(
                    build_tool_result_error_events(
                        self._event_context(state),
                        tool_call_id=block.id,
                        tool_call_name=block.name,
                        message=str(exc),
                    ),
                    state=state,
                )
                for event in stored_events:
                    yield event
                result_block = _tool_result_from_event_slice(stored_events, block.id)
                _remember_tool_result_event_span(state, stored_events, block.id)
                state.tool_results.append(result_block)
                state.messages.append(
                    Msg(
                        role="tool_result",
                        name=block.name,
                        id=f"tool-result-message:{block.id}",
                        content=[result_block],
                    )
                )
        async for event in flush_parsed_calls():
            yield event

    async def _stream_parsed_tool_calls(
        self,
        state: LoopState,
        parsed_calls: list[ToolCall],
    ) -> AsyncIterator[AgentEvent]:
        for batch in _tool_batches(parsed_calls, self.tool_executor):
            if state.tool_call_count + len(batch) > self.budget.max_tool_calls:
                yield await self._mark_tool_budget_exceeded(state, attempted_count=len(batch))
                return
            batch_events: list[AgentEvent] = []
            async for event in self._stream_tool_batch_events(state, batch, batch_events):
                yield event
            for call in batch:
                result_block = _tool_result_from_event_slice(batch_events, call.id)
                _remember_tool_result_event_span(state, batch_events, call.id)
                state.tool_results.append(result_block)
                state.messages.append(
                    Msg(role="tool_result", name=call.name, id=f"tool-result-message:{call.id}", content=[result_block])
                )
                state.tool_call_count += 1

    async def _stream_tool_batch_events(
        self,
        state: LoopState,
        batch: list[ToolCall],
        batch_events: list[AgentEvent],
    ) -> AsyncIterator[AgentEvent]:
        tap = _ToolBatchTap({call.id for call in batch})
        self.runtime_session.publisher.subscribe(tap)
        executor = ToolExecutor(
            registry=self.tool_executor.registry,
            record_event=self.runtime_session.make_thread_recorder(state=state),
            artifact_service=self.tool_executor.artifact_service,
            runtime_session_id=self.runtime_session.runtime_session_id,
        )
        async def execute_call(call: ToolCall) -> ToolExecutionResult:
            if executor.is_async(call):
                return await executor.execute_async(call, event_context=self._event_context(state))
            return await asyncio.to_thread(
                executor.execute,
                call,
                event_context=self._event_context(state),
            )

        tasks = [asyncio.create_task(execute_call(call)) for call in batch]
        pending = set(tasks)
        completed_tool_calls: set[str] = set()

        try:
            while pending or len(completed_tool_calls) < len(batch) or not tap.queue.empty():
                while not tap.queue.empty():
                    event = tap.queue.get_nowait()
                    batch_events.append(event)
                    if isinstance(event, ToolResultEndEvent):
                        completed_tool_calls.add(event.tool_call_id)
                    yield event
                if pending:
                    done, pending = await asyncio.wait(pending, timeout=0.05, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                    continue
                if len(completed_tool_calls) < len(batch):
                    event = await tap.queue.get()
                    batch_events.append(event)
                    if isinstance(event, ToolResultEndEvent):
                        completed_tool_calls.add(event.tool_call_id)
                    yield event
        finally:
            self.runtime_session.publisher.unsubscribe(tap)
            for task in pending:
                if not task.done():
                    task.cancel()

    def _recover_or_fail_model(self, state: LoopState) -> bool:
        state.consecutive_model_failures += 1
        state.in_run_recovery = InRunRecoveryState(
            cause=InRunRecoveryCause.MODEL_FAILURE,
            consecutive_failures=state.consecutive_model_failures,
        )
        if state.consecutive_model_failures > self.budget.max_consecutive_model_failures:
            state.status = LoopStatus.FAILED
            state.stop_reason = "model_error"
            state.error_message = "model error budget exceeded"
            state.transition(LoopTransition.FAIL)
            return False
        state.transition(LoopTransition.CONTINUE_AFTER_RECOVERY)
        return True

    def _event_context(self, state: LoopState) -> EventContext:
        return EventContext(run_id=state.run_id, turn_id=state.turn_id, reply_id=state.reply_id)


def _is_overridden_hook(instance: object, name: str, base: type) -> bool:
    method = getattr(type(instance), name, None)
    return method is not None and method is not getattr(base, name, None)


def _optional_str(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("expected a string")
    return value


def _required_str(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} is required")
    return value


def _remove_plan_runtime_instructions(state: LoopState) -> None:
    state.messages = [
        message
        for message in state.messages
        if message.metadata.get("runtime_instruction") not in {"plan_entry", "plan_active"}
    ]


def _accepted_plan_artifact_id(run_id: str, exit_request_id: str) -> str:
    return f"artifact:plan:{_sanitize_artifact_part(run_id)}:{_sanitize_artifact_part(exit_request_id)}:accepted"


def _sanitize_artifact_part(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value.strip()) or "unknown"
