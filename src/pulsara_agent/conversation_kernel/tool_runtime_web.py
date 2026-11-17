"""Async read-only web dispatch on the ordinary tool result/settlement path."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from time import monotonic

from pulsara_agent.conversation_kernel.content_parts import ResultPart
from pulsara_agent.conversation_kernel.execution_watchdogs import KernelWatchdogOwner
from pulsara_agent.conversation_kernel.tool_contracts import KernelToolResult
from pulsara_agent.conversation_kernel.tool_runtime_results import (
    _await_mcp_operation, _freeze_physical_observation,
)
from pulsara_agent.ports.tool_execution import ToolBusinessValue


def _web_failure(tool_name, arguments, status, message):
    value = {"meta": {"status": status}, "content": [{"type": "text", "value": message}]}
    if tool_name == "web_fetch":
        from pulsara_agent.web.providers import validate_fetch
        with suppress(ValueError):
            request = validate_fetch(arguments)
            value["target"] = {"kind": "URL", "url": request.url}
    return value


class _WebToolOperations:
    async def _invoke_web(self, *, tool_name, arguments, invocation_context,
                          invocation_started, observation_origin):
        deadline = self._deadlines.deadline(KernelWatchdogOwner.NONTERMINAL_TOOL_INVOCATION)

        async def execute():
            try:
                if self._web_service is None:
                    return _web_failure(tool_name, arguments, "ERROR", "Web service is unavailable.")
                async with asyncio.timeout_at(deadline):
                    return await self._web_service.execute(tool_name, dict(arguments),
                        deadline_monotonic=deadline)
            except asyncio.CancelledError as error:
                if getattr(error, "result", None) is not None:
                    return error.result
                return _web_failure(tool_name, arguments, "CANCELLED", "Web request was cancelled.")
            except TimeoutError as error:
                message = "Web request exceeded the tool execution deadline."
                partial = getattr(error.__cause__, "result", None)
                if partial is not None:
                    return {**partial, "meta": {"status": "ERROR"},
                        "content": [*partial["content"], {"type": "text", "value": message}]}
                return _web_failure(tool_name, arguments, "ERROR", message)

        operation = asyncio.create_task(execute(), name=f"web-operation:{invocation_context.tool_call_id}")
        self._web_tasks.add(operation)
        cancel_event = invocation_context.cell_cancel_event
        cancellation = None if cancel_event is None else asyncio.create_task(cancel_event.wait())
        caller_cancelled = False
        try:
            try:
                await asyncio.wait((operation,) if cancellation is None else (operation, cancellation),
                    return_when=asyncio.FIRST_COMPLETED)
            except asyncio.CancelledError:
                caller_cancelled = True
            # A settled service value wins over a later Stop, including when
            # both wakeups arrive together. Only a still-running read is cancelled.
            if not operation.done():
                caller_cancelled = True
                operation.cancel()
            if operation.cancelled():
                value = _web_failure(tool_name, arguments, "CANCELLED", "Web request was cancelled.")
            else:
                try:
                    value, joined_cancelled = await _await_mcp_operation(operation)
                    caller_cancelled = caller_cancelled or joined_cancelled
                except asyncio.CancelledError:
                    if not operation.cancelled():
                        raise
                    value = _web_failure(tool_name, arguments, "CANCELLED", "Web request was cancelled.")
        finally:
            if cancellation is not None:
                cancellation.cancel()
                with suppress(asyncio.CancelledError):
                    await cancellation
            self._web_tasks.discard(operation)
        # Cleanup may itself receive Stop. Preserve the complete known value
        # while letting the common settlement owner interrupt its waiter.
        caller_cancelled = caller_cancelled or bool(asyncio.current_task().cancelling()) or (
            cancel_event is not None and cancel_event.is_set())
        meta = dict(value["meta"])
        status = meta.pop("status")
        fields = {key: item for key, item in value.items() if key not in {"meta", "content"}}
        if meta:
            fields["meta"] = meta
        return KernelToolResult(
            state="APPLICATION_ERROR" if status == "ERROR" else status,
            content=ToolBusinessValue(fields, tuple(ResultPart(part["type"], part["value"])
                for part in value["content"])),
            caller_cancelled_while_running=caller_cancelled,
            physical_timing="LATE_AFTER_WATCHDOG" if monotonic() >= deadline else "ON_TIME",
            effect_class="read_only",
            physical_observation=_freeze_physical_observation(invocation_started, observation_origin),
        )

    async def _stop_web_physical_owners(self):
        tasks = tuple(self._web_tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
