"""Disposable session products; no replay, durable jobs or recovery.

The Host calls admission/completion edges. HTTP only observes this memory.
The shared LLM executor owns transport; this owner alone may adopt a title.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pulsara_agent.llm.input import LLMMessage
from pulsara_agent.llm.model_connections import (
    ModelCallBinding,
    model_call_binding_to_dict,
)
from pulsara_agent.primitives.model_call import ModelCallPurpose

if TYPE_CHECKING:
    from pulsara_agent.conversation_kernel.direct_model import AcceptedProviderFinalSource
    from pulsara_agent.conversation_kernel.host import KernelHostSession
    from pulsara_agent.conversation_kernel.runner import KernelRunResult


TITLE_PROMPT = (
    "Generate a short, specific conversation title from the user's input. "
    "Write in the user's language. Return only a single-line title, without a prefix, "
    "explanation, or quotation marks. Do not answer or carry out the user's request."
)
SUGGESTION_PROMPT = (
    'Review the conversation for repeated constraints and unresolved tensions. You must '
    "write one unasked question as the user, in the user's language and voice (I/my means "
    'the user; you means the assistant), about how constraints fit together or what should'
    ' guide a tradeoff. Respect decisions already made; do not invent problems, merely '
    'restate requirements, or ask generically how to implement them. If no grounded '
    'unresolved issue remains, return empty text. Otherwise output only the concise '
    'question on one line, without quotes or explanation. Do not answer, continue the '
    'task, or call tools.'
)
CACHE_WARMING_PROMPT = (
    "This is a cache keep-alive request. Do not perform any work or call tools. "
    "Reply with exactly: OK."
)
# Optional warming policy, not limits on normal execution or session lifetime.
CACHE_WARMING_INTERVAL_SECONDS = 4 * 60
CACHE_WARMING_DURATION_SECONDS = 20 * 60

@dataclass(frozen=True, slots=True)
class AcceptedUserOrigin:
    turn_id: str
    entry_id: str
    binding: ModelCallBinding
    title_message: LLMMessage | None


@dataclass(slots=True)
class _Suggestion:
    origin_turn_id: str
    state: str = "awaiting_completion"
    turn_id: str | None = None
    final_entry_id: str | None = None
    binding: ModelCallBinding | None = None
    text: str | None = None
    origin: AcceptedUserOrigin | None = None
    admission: asyncio.Task | None = None
    request: asyncio.Task | None = None
    resolution_snapshot: object | None = None


class SessionEphemeralProducts:
    def __init__(self, host: KernelHostSession):
        self._host = host
        self._suggestion: _Suggestion | None = None
        self._tasks: set[asyncio.Task] = set()
        self._title_state = "absent"
        self._title_attempted = False
        self._closed = False
        self._warming_origin_turn_id: str | None = None
        self._warming_task: asyncio.Task | None = None

    def _start(self, coroutine, name: str) -> asyncio.Task:
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    @staticmethod
    def _cancel(task: asyncio.Task | None) -> None:
        if task is not None and not task.done() and not task.cancelling():
            task.cancel()

    def invalidate(self, *, preserve_running: bool = False) -> None:
        slot = self._suggestion
        if slot is None or (preserve_running and slot.state == "awaiting_completion"):
            return
        self._suggestion = None
        self._cancel(slot.request)

    def activate(self, turn_id: str) -> None:
        self.invalidate_cache_warming()
        if (
            not self._closed
            and self._host.root_session_id is None
            and self._warming_enabled()
        ):
            self._warming_origin_turn_id = turn_id
        self.invalidate()
        if not self._closed:
            # Installed before any canonical completion notification. This is
            # not provider admission: nonhuman origins will settle to absent.
            self._suggestion = _Suggestion(turn_id)

    def admitted(self, turn_id: str) -> None:
        slot = self._suggestion
        if (
            self._closed
            or slot is None
            or slot.origin_turn_id != turn_id
            or slot.admission is not None
        ):
            return
        try:
            slot.resolution_snapshot = (
                self._host._model_runtime.freeze_resolution_snapshot()
            )
        except Exception:
            slot.state = "absent"
            return
        if not self._title_attempted:
            self._title_state = "pending"
        slot.admission = self._start(
            self._read_origin(slot), f"ephemeral-origin:{turn_id}"
        )

    async def _read_origin(self, slot: _Suggestion) -> None:
        host = self._host
        try:
            origin = await host._io.run(
                host.repository.read_ephemeral_user_origin,
                host._lease.guard,
                turn_id=slot.origin_turn_id,
                deadline_monotonic=host._canonical_deadline(),
            )
            slot.origin = origin
            if origin is not None:
                slot.binding = origin.binding
                if (
                    origin.title_message is not None
                    and not self._title_attempted
                    and not self._closed
                ):
                    self._title_attempted = True
                    self._start(
                        self._generate_title(origin, slot.resolution_snapshot),
                        f"ephemeral-title:{origin.turn_id}",
                    )
                    return
            if origin is None and slot.state == "awaiting_completion":
                slot.state = "absent"
        except asyncio.CancelledError:
            raise
        except Exception:
            # Advisory origin reads must never fail canonical execution.
            slot.origin = None
            slot.state = "absent"
        finally:
            if not self._title_attempted:
                self._title_state = "absent"

    async def _generate_title(
        self, origin: AcceptedUserOrigin, resolution_snapshot
    ) -> None:
        from pulsara_agent.llm.ephemeral import (
            execute_ephemeral_request,
            prepare_text_ephemeral_request,
            validate_ephemeral_text,
        )

        host = self._host
        adopted = False
        try:
            prepared = prepare_text_ephemeral_request(
                runtime=host._model_runtime,
                binding=origin.binding,
                purpose=ModelCallPurpose.SESSION_TITLE,
                messages=(origin.title_message,),
                system_prompt=TITLE_PROMPT,
                timeout_policy=host._model.transport_timeout_policy,
                resolution_snapshot=resolution_snapshot,
            )
            result = await execute_ephemeral_request(
                runtime=host._model_runtime, prepared=prepared
            )
            title = (
                validate_ephemeral_text(result.text)
                if result.status == "COMPLETED"
                else None
            )
            if title and not self._closed:
                adopted = await host._io.run(
                    host.repository.adopt_generated_session_title,
                    session_id=host.session_id,
                    memory_domain_id=host._memory_domain_id,
                    title=title,
                    deadline_monotonic=host._canonical_deadline(),
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            self._title_state = "adopted" if adopted else "absent"

    def _suggestions_enabled(self) -> bool:
        from pulsara_agent.settings import LocalSettingsUnavailable

        try:
            return self._host._model_runtime.settings.read().input_suggestions_enabled
        except (LocalSettingsUnavailable, OSError, ValueError):
            # An unreadable preference must not fail a completed user turn.
            return False

    def completed(self, origin_turn_id: str, result: KernelRunResult | None) -> None:
        self._complete_warming(origin_turn_id, result)
        slot = self._suggestion
        if self._closed or slot is None or slot.origin_turn_id != origin_turn_id:
            return
        source = None if result is None else getattr(result, "final_source", None)
        if (
            not self._suggestions_enabled()
            or result is None
            or result.pending_plan_interaction_id
            or source is None
            or slot.admission is None
        ):
            slot.state = "absent"
            return
        slot.turn_id, slot.final_entry_id = result.turn_id, result.final_entry_id
        slot.state = "pending"
        slot.request = self._start(
            self._generate_suggestion(slot, source),
            f"ephemeral-suggestion:{result.turn_id}",
        )

    async def _generate_suggestion(self, slot: _Suggestion, source) -> None:
        from pulsara_agent.conversation_kernel.direct_model import (
            prepare_conversation_ephemeral_request,
        )
        from pulsara_agent.llm.ephemeral import (
            execute_ephemeral_request,
            validate_ephemeral_text,
        )

        host = self._host
        try:
            await asyncio.shield(slot.admission)
            slot.binding = source.call_target.model_call_binding
            if slot.origin is None or not await self._current(slot, source):
                return
            prepared = prepare_conversation_ephemeral_request(
                purpose=ModelCallPurpose.CONVERSATION_SUGGESTION,
                runtime=host._model_runtime,
                source=source,
                prompt=SUGGESTION_PROMPT,
                timeout_policy=host._model.transport_timeout_policy,
            )
            result = await execute_ephemeral_request(
                runtime=host._model_runtime, prepared=prepared
            )
            text = (
                validate_ephemeral_text(result.text)
                if result.status == "COMPLETED"
                else None
            )
            if text and await self._current(slot, source):
                slot.text, slot.state = text, "ready"
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            if slot.state != "ready":
                slot.state = "absent"

    def _warming_enabled(self) -> bool:
        from pulsara_agent.settings import LocalSettingsUnavailable

        try:
            return self._host._model_runtime.settings.read().cache_warming_enabled
        except (LocalSettingsUnavailable, OSError, ValueError):
            return False

    def invalidate_cache_warming(self, *, preserve_running: bool = False) -> None:
        if not preserve_running:
            self._warming_origin_turn_id = None
        # Keep the task reference until physical close; a new window can be
        # dropped rather than overlap a cancelled request still settling.
        self._cancel(self._warming_task)

    def _complete_warming(
        self, origin_turn_id: str, result: KernelRunResult | None
    ) -> None:
        if self._warming_origin_turn_id != origin_turn_id:
            return
        self._warming_origin_turn_id = None
        if (
            self._closed
            or self._host.root_session_id is not None
            or not self._warming_enabled()
            or result is None
            or result.pending_plan_interaction_id
            or result.final_source is None
            or (self._warming_task is not None and not self._warming_task.done())
        ):
            return
        self._warming_task = self._start(
            self._warm(result.final_source, asyncio.get_running_loop().time()),
            f"ephemeral-warming:{origin_turn_id}",
        )

    async def _warming_current(self, source: AcceptedProviderFinalSource) -> bool:
        task = asyncio.current_task()

        def current():
            return (
                not self._closed
                and self._host.root_session_id is None
                and self._warming_task is task
                and not task.cancelling()
                and self._warming_enabled()
            )

        return current() and await self._source_current(source) and current()

    async def _warm(self, source: AcceptedProviderFinalSource, started: float) -> None:
        from pulsara_agent.conversation_kernel.direct_model import (
            prepare_conversation_ephemeral_request,
        )
        from pulsara_agent.llm.ephemeral import execute_ephemeral_request

        host = self._host
        loop = asyncio.get_running_loop()
        deadline = started + CACHE_WARMING_DURATION_SECONDS
        due = started + CACHE_WARMING_INTERVAL_SECONDS
        try:
            if not await self._warming_current(source):
                return
            while due <= deadline:
                await asyncio.sleep(max(0, due - loop.time()))
                if loop.time() > deadline or not await self._warming_current(source):
                    return
                prepared = prepare_conversation_ephemeral_request(
                    runtime=host._model_runtime,
                    source=source,
                    purpose=ModelCallPurpose.CONVERSATION_CACHE_WARMING,
                    prompt=CACHE_WARMING_PROMPT,
                    timeout_policy=host._model.transport_timeout_policy,
                )
                if not await self._warming_current(source) or loop.time() > deadline:
                    return
                result = await execute_ephemeral_request(
                    runtime=host._model_runtime, prepared=prepared
                )
                if result.status != "COMPLETED" or asyncio.current_task().cancelling():
                    return
                # Only the next future slot; never catch up missed pings.
                due = started + (
                    int((loop.time() - started) // CACHE_WARMING_INTERVAL_SECONDS) + 1
                ) * CACHE_WARMING_INTERVAL_SECONDS
        except asyncio.CancelledError:
            raise
        except Exception:
            # Warming has no retry or recovery; normal conversation is unrelated.
            pass
        finally:
            if self._warming_task is asyncio.current_task():
                self._warming_task = None

    async def _current(self, slot: _Suggestion, source) -> bool:
        def current():
            return (
                not self._closed
                and self._suggestions_enabled()
                and self._suggestion is slot
            )

        return current() and await self._source_current(source) and current()

    async def _source_current(self, source: AcceptedProviderFinalSource) -> bool:
        from pulsara_agent.model_input.continuity import ProviderInputContinuityScope
        from pulsara_agent.model_input.contracts import ModelInputScopeKind

        host = self._host
        if (
            self._closed
            or host._closing
            or host._active_task is not None
        ):
            return False
        binding = await host.model_call_binding()
        if binding != source.call_target.model_call_binding:
            return False
        pending = await host._io.run(
            host.repository.has_pending_prompt,
            session_id=host.session_id,
            deadline_monotonic=host._canonical_deadline(),
        )
        scope = ProviderInputContinuityScope(
            host.session_id, ModelInputScopeKind.ROOT
        )
        cohort = host._input_continuity.current_cohort(scope)
        return (
            not pending
            and not self._closed
            and not host._closing
            and host._active_task is None
            and cohort is not None
            and cohort.view.wire_input_plan is source.wire_input_plan
            and not host._compaction.is_fenced(
                scope_kind=ModelInputScopeKind.ROOT
            )
        )

    def read(self) -> dict[str, object]:
        enabled = self._suggestions_enabled()
        if not enabled:
            self.invalidate(preserve_running=True)
        slot = self._suggestion if enabled else None
        return {
            "session_id": self._host.session_id,
            "host_session_id": self._host.host_session_id,
            "suggestion": {
                "state": "absent" if slot is None else slot.state,
                "origin_turn_id": None if slot is None else slot.origin_turn_id,
                "turn_id": None if slot is None else slot.turn_id,
                "final_entry_id": None if slot is None else slot.final_entry_id,
                "model_call_binding": model_call_binding_to_dict(
                    None if slot is None else slot.binding
                ),
                "text": None if slot is None else slot.text,
            },
            "title": {"state": self._title_state},
        }

    @property
    def physical_close_complete(self) -> bool:
        return self._closed and all(task.done() for task in self._tasks)

    async def aclose(self) -> None:
        self._closed = True
        self.invalidate_cache_warming()
        self.invalidate()
        tasks = tuple(self._tasks)
        for task in tasks:
            self._cancel(task)
        await asyncio.gather(*tasks, return_exceptions=True)
