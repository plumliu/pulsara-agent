"""Best-effort session products; no replay, jobs or idle generation.

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

if TYPE_CHECKING:
    from pulsara_agent.conversation_kernel.host import KernelHostSession
    from pulsara_agent.conversation_kernel.runner import KernelRunResult


TITLE_PROMPT = (
    "Generate a short, specific conversation title from the user's input. "
    "Write in the user's language. Return only a single-line title, without a prefix, "
    "explanation, or quotation marks. Do not answer or carry out the user's request."
)
SUGGESTION_PROMPT = (
    "Review the whole conversation for repeated constraints and unresolved tensions. "
    "Find the unasked question about how those constraints fit together or what "
    "should guide a tradeoff. Respect explicit decisions; do not invent problems, "
    "restate requirements, or ask generically how to implement them. If the exchange "
    "is already resolved or lacks such evidence, return empty text. Otherwise, write "
    "one concise question as the user asking the assistant, in the user's language. "
    "Output only that question on one line, without explanation or quotes. "
    "Do not continue the task or call tools."
)


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
        from pulsara_agent.primitives.model_call import ModelCallPurpose

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

    def completed(self, origin_turn_id: str, result: KernelRunResult | None) -> None:
        slot = self._suggestion
        if self._closed or slot is None or slot.origin_turn_id != origin_turn_id:
            return
        source = None if result is None else getattr(result, "final_source", None)
        if (
            result is None
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
            prepare_suggestion_request,
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
            prepared = prepare_suggestion_request(
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

    async def _current(self, slot: _Suggestion, source) -> bool:
        from pulsara_agent.model_input.continuity import ProviderInputContinuityScope
        from pulsara_agent.model_input.contracts import ModelInputScopeKind

        host = self._host
        if (
            self._closed
            or self._suggestion is not slot
            or host._closing
            or host._active_task is not None
        ):
            return False
        binding = await host.model_call_binding()
        if self._suggestion is not slot or binding != slot.binding:
            return False
        pending = await host._io.run(
            host.repository.has_pending_prompt,
            session_id=host.session_id,
            deadline_monotonic=host._canonical_deadline(),
        )
        scope = ProviderInputContinuityScope(
            host.session_id, ModelInputScopeKind.ROOT, None
        )
        cohort = host._input_continuity.current_cohort(scope)
        return (
            not pending
            and not self._closed
            and self._suggestion is slot
            and host._active_task is None
            and cohort is not None
            and cohort.view.wire_input_plan is source.wire_input_plan
            and not host._compaction.is_fenced(
                scope_kind=ModelInputScopeKind.ROOT, scope_subagent_task_id=None
            )
        )

    def read(self) -> dict[str, object]:
        slot = self._suggestion
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

    async def aclose(self) -> None:
        self._closed = True
        self.invalidate()
        tasks = tuple(self._tasks)
        for task in tasks:
            self._cancel(task)
        await asyncio.gather(*tasks, return_exceptions=True)
