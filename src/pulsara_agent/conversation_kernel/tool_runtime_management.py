"""Execution adapters for session queries and user-level management tools."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from .capability_management_execution import CapabilityManagementOutcome
from pulsara_agent.capability.mcp_management import McpManagementConflict
from pulsara_agent.ports.tool_execution import ToolBusinessValue
from pulsara_agent.tools.builtins.todo import parse_todo_replacement
from pulsara_agent.conversation_kernel.content_parts import ResultPart
from pulsara_agent.conversation_kernel.io import PhysicalToolInvocationDisposition
from pulsara_agent.conversation_kernel.execution_watchdogs import KernelWatchdogOwner
from .tool_contracts import (
    KernelToolPhysicalInvocationError,
    KernelToolResult,
    ProcessLocalEffectSettlementToken,
)
from .tool_runtime_results import (
    _await_mcp_operation,
    _freeze_physical_observation,
    _kernel_tool_state,
)


class _ToolManagementOperations:
    """Category execution on the already-admitted direct tool owner."""

    async def _invoke_session_query(self, *, tool_name, arguments, tool_call_id, invocation_context, observation_origin):
        from pulsara_agent.conversation_kernel.content_parts import ResultPart
        from pulsara_agent.conversation_kernel.session_content import SessionQueryError
        from psycopg import Error as PostgresError
        if (self._session_content_query is None or invocation_context.conversation_scope_kind != "ROOT"
                or invocation_context.session_content_range is None):
            raise RuntimeError("session query lost its ROOT call-local owner")
        def query(*, deadline_monotonic):
            try:
                value = self._session_content_query.invoke(tool_name,dict(arguments),
                    current_range=invocation_context.session_content_range,
                    deadline_monotonic=deadline_monotonic)
                return "SUCCESS",value
            except (SessionQueryError,ValueError,RuntimeError,TimeoutError,PostgresError) as exc:
                return "APPLICATION_ERROR",ToolBusinessValue({},(ResultPart("text",str(exc)),))
        physical=await self._physical_io.run_tool_invocation(query,
            deadline_monotonic=self._deadlines.deadline(KernelWatchdogOwner.FOREGROUND_CANONICAL))
        if physical.disposition is PhysicalToolInvocationDisposition.RAISED:
            raise KernelToolPhysicalInvocationError(effect_class="read_only",error=physical.error,
                timing=physical.timing.value,caller_cancelled=physical.caller_cancelled,
                physical_observation=physical.observation)
        state,value=physical.value
        return KernelToolResult(state=state,content=value,
            caller_cancelled_while_running=physical.caller_cancelled,effect_class="read_only",
            physical_timing=physical.timing.value,
            physical_observation=None if physical.observation is None else replace(
                physical.observation,observation_origin_kind=observation_origin))


    async def _invoke_scheduled_management(self, *, tool_name, arguments, tool_call_id, invocation_context, invocation_started, observation_origin):
        if self._scheduled_tasks is None or invocation_context.conversation_scope_kind != "ROOT":
            raise RuntimeError("scheduled management lost its ROOT owner")
        async def manage():
            from pulsara_agent.scheduling.contracts import ScheduledTaskError
            try:
                method = (self._scheduled_tasks.invoke_query if tool_name == "query_schedules"
                          else self._scheduled_tasks.invoke_manage)
                observation = await method(arguments,
                    default_session_id=self._session_id,
                    memory_domain_id=self._memory_domain_id,
                    permission_mode=invocation_context.effective_permission_mode)
                return "SUCCESS", observation.value, observation.result_facts
            except (ScheduledTaskError, KeyError, ValueError) as exc:
                message = "该定时任务不存在或不属于当前会话。" if isinstance(exc, KeyError) else str(exc)
                return "APPLICATION_ERROR", ToolBusinessValue(content=(ResultPart("text", message),)), {}
        operation = asyncio.create_task(manage(), name=f"scheduled-management:{tool_call_id}")
        known, cancelled = await _await_mcp_operation(operation)
        state, value, result_facts = known
        return KernelToolResult(state=state, content=value, result_facts=result_facts,
            caller_cancelled_while_running=cancelled,
            effect_class="read_only" if tool_name == "query_schedules" else "unknown_effect",
            physical_observation=_freeze_physical_observation(invocation_started, observation_origin))

    async def _invoke_capability_management(self, *, turn_id, assistant_entry_id, tool_call_id, invocation_context, invocation_started, observation_origin):
        call = invocation_context.capability_call
        if (call is None or call.service is not self._capability_management
            or call.subject != (self._session_id, turn_id, assistant_entry_id, tool_call_id)
            or invocation_context.conversation_scope_kind != "ROOT"):
            raise RuntimeError("capability execution lost its exact prepared owner")
        try:
            outcome = await call.execute(cell_cancel_event=invocation_context.cell_cancel_event)
        except (McpManagementConflict, ValueError, OSError) as error:
            message = call._public_scrub(call.prepared).scrub_text(str(error))
            target = dict(call.prepared.intent.to_dict()["target"])
            if target["kind"] == "PATH":
                target = None
            outcome = CapabilityManagementOutcome("APPLICATION_ERROR", ToolBusinessValue(
                {} if target is None else {"target": target}, (ResultPart("text", message),)),False,{})
        if outcome.changed:
            try:
                adoption = await self._capability_reload.adopt_capability_management_change(workspace_root=call._root())
                if isinstance(adoption, dict) and adoption.get("status") != "RELOADED":
                    details = call._public_scrub(call.prepared).scrub_text(_reload_failure_text(adoption))
                    outcome = replace(outcome, state="APPLICATION_ERROR", business_value=ToolBusinessValue(
                        outcome.business_value.public_fields,(*outcome.business_value.content,ResultPart("text", "后续能力刷新未完成："+details))))
            except BaseException as error:
                if not isinstance(error,(Exception,asyncio.CancelledError)):
                    raise
                details = "等待已取消，刷新状态未确认。" if isinstance(error,asyncio.CancelledError) else call._public_scrub(call.prepared).scrub_text(str(error))
                outcome = replace(outcome,state="APPLICATION_ERROR",business_value=ToolBusinessValue(
                    outcome.business_value.public_fields,(*outcome.business_value.content,ResultPart("text","后续能力刷新未完成："+details))))
        return KernelToolResult(state=outcome.state,content=outcome.business_value,result_facts=outcome.result_facts,
            effect_class="read_only" if call.prepared.effects.read_only else "unknown_effect",
            physical_observation=_freeze_physical_observation(invocation_started,observation_origin))

    async def _invoke_capability_reload(self, *, invocation_started, observation_origin):
        if self._capability_reload is None:
            raise RuntimeError("Capability reload port is unavailable")
        try:
            values = await self._capability_reload.reload_capabilities(deadline_monotonic=self._deadlines.deadline(KernelWatchdogOwner.NONTERMINAL_TOOL_INVOCATION))
            cancelled = values.get("interruption") == "CANCELLED"
            state = "CANCELLED" if cancelled else "SUCCESS" if values.get("status") == "RELOADED" else "APPLICATION_ERROR"
            text = "已刷新当前能力来源。" if state == "SUCCESS" else _reload_failure_text(values)
        except asyncio.CancelledError:
            state,text = "CANCELLED","本次刷新等待已取消，刷新状态未确认。"
        except Exception as error:
            state,text = "APPLICATION_ERROR",str(error)
        scrub = self._capability_management.plugins._credential_boundary.capture_scrub_set()
        return KernelToolResult(state=state,content=ToolBusinessValue({},(ResultPart("text",scrub.scrub_text(text)),)),
            effect_class="read_only",physical_observation=_freeze_physical_observation(invocation_started,observation_origin))

    async def _invoke_todo_replacement(self, *, arguments, turn_id, attempt_id, invocation_context, invocation_started, observation_origin):
        candidate = parse_todo_replacement(arguments)
        counts = {
            "pending": candidate.pending_count,
            "in_progress": candidate.in_progress_count,
            "completed": candidate.completed_count,
            "total": len(candidate.ordered_items),
        }
        try:
            prepared = self._current_work_owner.prepare_replace(
                scope_kind=(
                    invocation_context.surface_borrow.prepared.access.conversation_scope_kind
                ),
                exact_turn_id=turn_id,
                attempt_id=attempt_id,
                proposed_result_entry_id=invocation_context.result_entry_id,
                candidate=candidate,
            )
        except LookupError:
            return KernelToolResult(
                # The attempt already exists; scope expiry is a known
                # rejection, not a no-dispatch TOOL_UNAVAILABLE result.
                state="APPLICATION_ERROR",
                content=ToolBusinessValue(content=(ResultPart("text", "当前工作的进度清单已不可更新。"),)),
                effect_class="read_only",
                physical_observation=_freeze_physical_observation(
                    invocation_started, observation_origin
                ),
            )
        token = ProcessLocalEffectSettlementToken(
            prepared.token_id,
            prepared,
        )
        self._todo_settlements[token.token_id] = token
        return KernelToolResult(
            state="SUCCESS",
            content=ToolBusinessValue(
                public_fields={"meta": {"counts": counts}},
                content=(ResultPart("text", "已更新进度清单。" if candidate.ordered_items else "已清空进度清单。"),),
            ),
            process_local_settlement=token,
            effect_class="read_only",
            physical_observation=_freeze_physical_observation(
                invocation_started, observation_origin
            ),
        )


    async def _invoke_presentation(self, *, tool, call, invocation_started, observation_origin):
        from pulsara_agent.conversation_kernel.presentation_targets import ResolvedPresentationObject

        async def resolve_object(kind, identity):
            if kind != "AGENT" or self._subagent is None:
                raise LookupError("Agent reader is unavailable")
            await self._subagent.resolve_visible_agent(identity)
            return ResolvedPresentationObject("AGENT", identity, self._session_id)

        result = await tool.execute_async(call, resolve_object=resolve_object)
        return KernelToolResult(
            state=_kernel_tool_state(result.status), content=result.business_value,
            result_facts=result.result_facts,
            effect_class="read_only",
            physical_observation=_freeze_physical_observation(invocation_started, observation_origin),
        )


def _reload_failure_text(values):
    parts=[]
    for key,label in (("plugin_view","插件来源"),("skill_producer","技能来源"),("mcp","MCP连接"),("local_hooks","本地Hook")):
        if key in values:
            parts.append(label+("已刷新。" if values[key] in {"RELOADED","COMPLETE","ADOPTED","UNCHANGED"} else "未完成刷新。"))
    for error in values.get("errors",()):
        parts.append(str(error))
    if values.get("attention"):
        parts.append(str(values["attention"]))
    if values.get("interruption") == "CANCELLED":
        parts.append("刷新等待已取消；此前已完成的刷新保留。")
    return "\n".join(parts) or "刷新状态未确认。"
