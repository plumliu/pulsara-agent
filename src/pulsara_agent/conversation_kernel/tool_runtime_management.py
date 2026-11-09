"""Execution adapters for session queries and user-level management tools."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from .capability_management_execution import (
    capability_management_required_facts,
    capability_reload_required_facts,
    require_capability_reload_budget,
)
from pulsara_agent.capability.mcp_management import McpManagementConflict
from pulsara_agent.primitives.tool_result_budget import ToolResultPageBoundary
from pulsara_agent.ports.tool_execution import ToolOutputSourceFormatHint
from pulsara_agent.tools.builtins.todo import parse_todo_replacement
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
    _no_effect_budget_result,
    _kernel_tool_state,
)


class _ToolManagementOperations:
    """Category execution on the already-admitted direct tool owner."""

    async def _invoke_session_query(self, *, tool_name, arguments, tool_call_id, invocation_context, page_budget, observation_origin):
        from pulsara_agent.conversation_kernel.session_content import (
            SessionQueryError,
        )
        from pulsara_agent.conversation_kernel.repository_errors import (
            ConversationKernelConflict,
        )
        from pulsara_agent.ports.tool_output import ToolOutputContentError
        from pulsara_agent.terminal_protocol.canonical_v3 import (
            CanonicalProtocolGap,
        )
        from pulsara_agent.storage.migrations.errors import (
            PostgresSchemaError,
            PostgresSchemaFailureCode,
        )
        from psycopg import Error as PostgresError
        from psycopg.errors import QueryCanceled

        if (
            self._session_content_query is None
            or invocation_context.conversation_scope_kind != "ROOT"
            or invocation_context.session_content_range is None
        ):
            raise RuntimeError("session query lost its ROOT call-local owner")
        deadline = self._deadlines.deadline(
            KernelWatchdogOwner.FOREGROUND_CANONICAL
        )

        def query(*, deadline_monotonic):
            try:
                value = self._session_content_query.invoke(
                    tool_name,
                    dict(arguments),
                    current_range=invocation_context.session_content_range,
                    tool_call_id=tool_call_id,
                    page_budget=page_budget,
                    deadline_monotonic=deadline_monotonic,
                )
                return "SUCCESS", value
            except SessionQueryError as exc:
                return "APPLICATION_ERROR", {"code": exc.code, "message": str(exc)}
            except (TimeoutError, QueryCanceled):
                return "APPLICATION_ERROR", {
                    "code": "QUERY_TIMEOUT",
                    "message": "Session query timed out; narrow or retry it.",
                }
            except CanonicalProtocolGap:
                return "APPLICATION_ERROR", {
                    "code": "CURSOR_INVALID",
                    "message": "History cuts do not match; restart the query.",
                }
            except PostgresSchemaError as exc:
                code = (
                    "QUERY_TIMEOUT"
                    if exc.code is PostgresSchemaFailureCode.DEADLINE_EXCEEDED
                    else "QUERY_UNAVAILABLE"
                )
                return "APPLICATION_ERROR", {
                    "code": code,
                    "message": "Canonical session storage is unavailable or timed out.",
                }
            except (RuntimeError, ToolOutputContentError, ConversationKernelConflict):
                return "APPLICATION_ERROR", {
                    "code": "CONTENT_CORRUPT",
                    "message": "Saved session content could not be verified.",
                }
            except ToolResultPageBoundary:
                raise
            except ValueError:
                return "APPLICATION_ERROR", {
                    "code": "INVALID_REQUEST",
                    "message": "Invalid session query or cursor.",
                }
            except PostgresError:
                return "APPLICATION_ERROR", {
                    "code": "QUERY_UNAVAILABLE",
                    "message": "Canonical session storage is unavailable.",
                }

        physical = await self._physical_io.run_tool_invocation(
            query, deadline_monotonic=deadline
        )
        if physical.disposition is PhysicalToolInvocationDisposition.RAISED:
            if isinstance(physical.error, TimeoutError):
                state, value = (
                    "APPLICATION_ERROR",
                    {
                        "code": "QUERY_TIMEOUT",
                        "message": "Session query timed out; narrow or retry it.",
                    },
                )
            else:
                assert physical.error is not None
                raise KernelToolPhysicalInvocationError(
                    effect_class="read_only",
                    error=physical.error,
                    timing=physical.timing.value,
                    caller_cancelled=physical.caller_cancelled,
                    physical_observation=physical.observation,
                )
        else:
            state, value = physical.value
        return KernelToolResult(
            state=state,
            content=json.dumps(
                value, ensure_ascii=False, separators=(",", ":")
            ).encode(),
            page_complete=True,
            public_format=ToolOutputSourceFormatHint.JSON,
            caller_cancelled_while_running=physical.caller_cancelled,
            effect_class="read_only",
            physical_timing=physical.timing.value,
            physical_observation=(
                None
                if physical.observation is None
                else replace(
                    physical.observation, observation_origin_kind=observation_origin
                )
            ),
        )


    async def _invoke_scheduled_management(self, *, tool_name, arguments, tool_call_id, invocation_context, page_budget, invocation_started, observation_origin):
        if self._scheduled_tasks is None or invocation_context.conversation_scope_kind != "ROOT":
            raise RuntimeError("scheduled management lost its ROOT owner")
        async def manage():
            from pulsara_agent.scheduling.contracts import ScheduledTaskError
            try:
                method = (self._scheduled_tasks.invoke_query if tool_name == "query_schedules"
                          else self._scheduled_tasks.invoke_manage)
                value = await method(arguments,
                    default_session_id=self._session_id,
                    memory_domain_id=self._memory_domain_id,
                    permission_mode=invocation_context.effective_permission_mode,
                    page_budget=page_budget)
                return "SUCCESS", value
            except ToolResultPageBoundary:
                raise
            except (ScheduledTaskError, KeyError, ValueError) as exc:
                return "APPLICATION_ERROR", {"error": getattr(exc, "code", "INVALID_REQUEST"), "message": str(exc)}
        # Reuse the existing admitted-async-operation settlement: cancellation
        # waits for the management transaction and retains its actual result.
        operation = asyncio.create_task(manage(), name=f"scheduled-management:{tool_call_id}")
        known, cancelled = await _await_mcp_operation(operation)
        state, value = known
        return KernelToolResult(state=state,
            content=json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(),
            public_format=ToolOutputSourceFormatHint.JSON,
            page_complete=tool_name == "manage_schedule" or "target" not in arguments,
            required_facts=(value if tool_name == "manage_schedule" and state == "SUCCESS" else {
                key: value[key] for key in ("target", "status", "revision") if key in value
            }),
            caller_cancelled_while_running=cancelled,
            effect_class="read_only" if tool_name == "query_schedules" else "unknown_effect",
            physical_observation=_freeze_physical_observation(invocation_started, observation_origin))


    async def _invoke_capability_management(self, *, turn_id, assistant_entry_id, tool_call_id, invocation_context, page_budget, invocation_started, observation_origin):
        call = invocation_context.capability_call
        if (
            call is None
            or call.service is not self._capability_management
            or call.subject
            != (self._session_id, turn_id, assistant_entry_id, tool_call_id)
            or invocation_context.conversation_scope_kind != "ROOT"
        ):
            raise RuntimeError("capability execution lost its exact prepared owner")
        try:
            values = await call.execute(cell_cancel_event=invocation_context.cell_cancel_event, page_budget=page_budget)
        except ToolResultPageBoundary:
            raise
        except McpManagementConflict as exc:
            values = {
                "status": "CONFLICT",
                "message": str(exc),
                "adoption": "NOT_APPLICABLE",
            }
        except ValueError:
            # Private form values may be present in a native validator's
            # exception. Do not put that exception into tool/Hook context.
            values = {
                "status": "REJECTED",
                "message": "Capability operation could not be applied; review its current configuration.",
                "adoption": "NOT_APPLICABLE",
            }
        if values["status"] == "APPLIED":
            try:
                values[
                    "adoption"
                ] = await self._capability_reload.adopt_capability_management_change(
                    workspace_root=call._root()
                )
            except asyncio.CancelledError:
                # Source is already settled. Preserve that fact; a later
                # safe point may retry adoption, never replay the mutation.
                values["adoption"] = "PARTIAL"
            except Exception:
                values["adoption"] = "PARTIAL"
        return KernelToolResult(
            state="SUCCESS"
            if values["status"] in {"APPLIED", "OBSERVED"}
            else "CANCELLED" if values["status"] == "CANCELLED" else "APPLICATION_ERROR",
            content=json.dumps(
                values, ensure_ascii=False, separators=(",", ":")
            ).encode(),
            public_format=ToolOutputSourceFormatHint.JSON,
            required_facts=capability_management_required_facts(values),
            effect_class="read_only"
            if call.prepared.effects.read_only
            else "unknown_effect",
            physical_observation=_freeze_physical_observation(
                invocation_started, observation_origin
            ),
        )


    async def _invoke_capability_reload(self, *, page_budget, invocation_started, observation_origin):
        if self._capability_reload is None:
            raise RuntimeError("Capability reload port is unavailable")
        try:
            require_capability_reload_budget(page_budget)
        except ToolResultPageBoundary:
            return _no_effect_budget_result()
        values = await self._capability_reload.reload_capabilities(
            deadline_monotonic=self._deadlines.deadline(
                KernelWatchdogOwner.NONTERMINAL_TOOL_INVOCATION
            )
        )
        content = json.dumps(
            dict(values),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return KernelToolResult(
            state="SUCCESS",
            content=content,
            public_format=ToolOutputSourceFormatHint.JSON,
            required_facts=capability_reload_required_facts(values),
            effect_class="read_only",
            physical_observation=_freeze_physical_observation(
                invocation_started, observation_origin
            ),
        )


    async def _invoke_todo_replacement(self, *, arguments, turn_id, attempt_id, invocation_context, page_budget, invocation_started, observation_origin):
        candidate = parse_todo_replacement(arguments)
        counts = {
            "pending": candidate.pending_count,
            "in_progress": candidate.in_progress_count,
            "completed": candidate.completed_count,
            "total": len(candidate.ordered_items),
        }
        acknowledgement = json.dumps(
            {
                "status": ("CLEARED" if not candidate.ordered_items else "UPDATED"),
                "counts": counts,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        try:
            fits_receipt = page_budget.fits(acknowledgement.decode("utf-8"))
        except ToolResultPageBoundary:
            return _no_effect_budget_result()
        if not fits_receipt:
            return _no_effect_budget_result()
        try:
            prepared = self._current_work_owner.prepare_replace(
                scope_kind=(
                    invocation_context.surface_borrow.prepared.access.conversation_scope_kind
                ),
                exact_turn_id=turn_id,
                attempt_id=attempt_id,
                proposed_result_entry_id=invocation_context.result_entry_id,
                candidate=candidate,
                acknowledgement=acknowledgement,
            )
        except LookupError:
            return KernelToolResult(
                # The attempt already exists; scope expiry is a known
                # rejection, not a no-dispatch TOOL_UNAVAILABLE result.
                state="APPLICATION_ERROR",
                content=b'{"error":"todo scope is no longer active"}',
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
            content=acknowledgement,
            public_format=ToolOutputSourceFormatHint.JSON,
            page_complete=True,
            required_facts=json.loads(acknowledgement),
            process_local_settlement=token,
            effect_class="read_only",
            physical_observation=_freeze_physical_observation(
                invocation_started, observation_origin
            ),
        )


    async def _invoke_presentation(self, *, tool, call, invocation_started, observation_origin):
        from pulsara_agent.conversation_kernel.presentation_targets import (
            ResolvedPresentationObject, resolve_visible_cell,
        )

        async def resolve_object(kind, identity):
            if kind == "AGENT":
                if self._subagent is None:
                    raise LookupError("Agent reader is unavailable")
                await self._subagent.resolve_visible_agent(identity)
                return ResolvedPresentationObject("AGENT", identity, self._session_id)
            resolve_visible_cell(self, cell_id=identity)
            return ResolvedPresentationObject("CELL", identity, self._session_id, self._host_owner_id)

        result = await tool.execute_async(call, resolve_object=resolve_object)
        return KernelToolResult(
            state=_kernel_tool_state(result.status), content=result.output.encode("utf-8"),
            public_format=result.public_format, page_complete=result.page_complete,
            native_output=None if result.native_output is None else result.native_output.encode("utf-8"),
            required_facts=result.required_facts, output_body_candidate=result.output_body_candidate,
            effect_class="read_only",
            physical_observation=_freeze_physical_observation(invocation_started, observation_origin),
        )
