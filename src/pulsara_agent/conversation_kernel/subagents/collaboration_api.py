"""Collaboration operations over the common session/request owners.

This facade never executes a turn or stores Work state. Wait subscriptions only
wake reads of canonical requests and the receiving turn's common input queue.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
import inspect
from time import monotonic
from typing import Callable, Mapping

from pulsara_agent.code_mode.contracts import (
    CellParticipationObserver,
    CellParticipationReference,
)
from pulsara_agent.conversation_kernel.subagents.collaboration_contracts import (
    AGENT_TOOLS,
    agent_target,
    work_target,
    parse_batch_members,
    target_id,
    validate_collaboration_parameters,
)
from pulsara_agent.conversation_kernel.tool_contracts import KernelToolResult
from pulsara_agent.ports.tool_execution import ToolBusinessValue
from pulsara_agent.conversation_kernel.content_parts import ResultPart
from pulsara_agent.primitives.permission import PermissionMode

TERMINAL = frozenset({"completed", "stopped"})


def _business(parts=(), *, meta=None, state="SUCCESS", outcome=None, delivered=None):
    return KernelToolResult(
        state=state,
        content=ToolBusinessValue(
            public_fields={"meta": dict(meta or {})}, content=tuple(parts)
        ),
        delivery_succeeded=delivered,
        physical_timing="ON_TIME" if outcome is None else outcome.timing.value,
        caller_cancelled_while_running=False
        if outcome is None
        else outcome.caller_cancelled,
        physical_observation=None if outcome is None else outcome.observation,
    )


def _run_record(row):
    return {
        "agent": agent_target(row["agent_id"]),
        "work": work_target(row["work_id"]),
        "work_status": row["status"],
    }


class CollaborationToolPort:
    def __init__(
        self,
        *,
        repository,
        root_session_id: str,
        session_id: str,
        runtime,
        admission_facts: Callable,
        io,
    ):
        self.repository = repository
        self.root_session_id = root_session_id
        self.session_id = session_id
        self.runtime = runtime
        self.admission_facts = admission_facts
        self.io = io
        self._cell_observer: CellParticipationObserver | None = None

    def bind_cell_observer(self, observer: CellParticipationObserver) -> None:
        self._cell_observer = observer

    def _observe_cell(
        self, context, references: tuple[CellParticipationReference, ...]
    ) -> None:
        cell_id = getattr(context, "code_cell_id", None)
        observer = self._cell_observer
        if cell_id is None or observer is None:
            return
        try:
            observer.observe_agent_participation(cell_id, tuple(references))
        except Exception:
            # UI observations do not affect the tool's business settlement.
            try:
                observer.mark_cell_participation_incomplete(cell_id)
            except Exception:
                pass

    @property
    def tool_names(self) -> frozenset[str]:
        return AGENT_TOOLS

    def validate_arguments(self, *, tool_name, arguments) -> str | None:
        try:
            validate_collaboration_parameters(tool_name, arguments)
        except (TypeError, ValueError, KeyError) as error:
            return str(error)
        return None

    async def freeze_compaction_handoff(
        self, *, cursor: str | None = None
    ) -> tuple[dict[str, object], ...]:
        # Fifty is the existing per-read boundary. The renderer asks for another
        # active page only while its observation budget still needs candidates.
        rows = await self._read(
            self.repository.list_query_agent_sessions,
            root_session_id=self.root_session_id,
            observer_session_id=self.session_id,
            active_only=True,
            cursor=cursor,
            limit=50,
            deadline_monotonic=self.runtime.read_deadline(),
        )
        return tuple(
            {
                "agent": agent_target(row["agent_id"]),
                "current_work": (
                    None
                    if row["current_work_id"] is None
                    else work_target(row["current_work_id"])
                ),
                "queued_count": row["queued_count"],
            }
            for row in rows
        )

    async def resolve_visible_agent(self, agent_id: str):
        from pulsara_agent.conversation_kernel.presentation_targets import (
            resolve_visible_agent,
        )

        return await self._read(
            resolve_visible_agent,
            repository=self.repository,
            root_session_id=self.root_session_id,
            agent_session_id=agent_id,
            deadline_monotonic=self.runtime.read_deadline(),
        )

    async def _facts(self, context, requests=()) -> dict:
        facts = self.admission_facts(context, requests)
        if inspect.isawaitable(facts):
            facts = await facts
        return dict(facts)

    async def _read(self, method, **kwargs):
        # Repository owns synchronous PostgreSQL transactions; IO dispatch does
        # not retain a database connection across a wait.
        return await self.io.run(method, **kwargs)

    async def _work_status(self, work_id: str) -> dict:
        row = await self._read(
            self.repository.read_agent_work_graph_facts,
            root_session_id=self.root_session_id,
            work_id=work_id,
            deadline_monotonic=self.runtime.read_deadline(),
        )
        if row is None:
            raise ValueError("WORK does not exist in this collaboration")
        return row

    async def read_cell_work_page(self, *, source_exec_attempt_id, cursor, limit):
        return await self._read(
            self.repository.list_cell_agent_works,
            root_session_id=self.root_session_id,
            source_exec_attempt_id=source_exec_attempt_id,
            cursor=cursor,
            limit=limit,
            deadline_monotonic=self.runtime.read_deadline(),
        )

    async def visible_cell_participants(self, participants):
        visible = []
        for item in participants:
            if item["work_id"] is None:
                state = await self._read(
                    self.repository.read_agent_runtime_state,
                    session_id=item["agent_id"],
                    deadline_monotonic=self.runtime.read_deadline(),
                )
                present = (
                    state is not None
                    and state["root_session_id"] == self.root_session_id
                )
            else:
                fact = await self._read(
                    self.repository.read_agent_work_graph_facts,
                    root_session_id=self.root_session_id,
                    work_id=item["work_id"],
                    deadline_monotonic=self.runtime.read_deadline(),
                )
                present = fact is not None and fact["agent_id"] == item["agent_id"]
            if present:
                visible.append(item)
        return visible

    @staticmethod
    def _public_work(row: Mapping, *, detail=True) -> dict:
        result = {
            "target": work_target(row["work_id"]),
            "agent": agent_target(row["agent_id"]),
            "work_status": row["status"],
            "work_title": row.get("work_title"),
        }
        if detail:
            for field in ("instruction", "final", "diagnostic"):
                if field in row and row[field] is not None:
                    result[field] = row[field]
        reason = row.get("terminal_reason")
        if isinstance(reason, str) and reason.startswith("DEPENDENCY_NOT_SUCCESSFUL:"):
            result["failed_dependency"] = work_target(
                reason.removeprefix("DEPENDENCY_NOT_SUCCESSFUL:")
            )
        return result

    @staticmethod
    def _work_summary(row):
        return CollaborationToolPort._public_work(row, detail=False)

    async def _require_agent(self, agent_id: str):
        row = await self._read(
            self.repository.read_agent_runtime_state,
            session_id=agent_id,
            deadline_monotonic=self.runtime.read_deadline(),
        )
        if row is None or row["root_session_id"] != self.root_session_id:
            raise ValueError("AGENT does not exist in this collaboration")

    async def _run(self, requests, context):
        facts = await self._facts(context, requests)
        snapshot = context.agent_invocation_snapshot
        if snapshot is None:
            raise ValueError(
                "delegation requires the accepted ROOT invocation snapshot"
            )
        facts["model_call_binding"] = snapshot.model_call_binding
        facts["permission_snapshot"] = snapshot.permission_snapshot
        actual_requests = tuple(
            replace(
                r,
                initial_context=tuple(
                    unit
                    for turns in r.context_turns
                    for unit in snapshot.initial_context_units[-turns:]
                ),
            )
            for r in requests
        )
        root_guard = facts.pop("root_guard")
        admission = dict(
            requests=actual_requests,
            source_turn_id=context.turn_id,
            source_tool_attempt_id=context.attempt_id,
            **facts,
        )
        async with self.runtime.request_admission(
            self.root_session_id, actual_requests
        ):
            outcome = await self.io.run_tool_invocation(
                self.repository.admit_agent_requests,
                root_guard,
                occurred_at=datetime.now(timezone.utc),
                **admission,
            )
            rows = outcome.value
            if outcome.error is not None:
                confirmation = {k: v for k, v in admission.items() if k != "actor_id"}
                confirmation["deadline_monotonic"] = self.runtime.read_deadline()
                rows = await self.io.run(
                    self.repository.confirm_agent_requests, root_guard, **confirmation
                )
                if rows is None:
                    raise outcome.error
        await self.runtime.wake_agent_requests(self.root_session_id)
        return rows, outcome

    async def invoke(self, *, tool_name, arguments, invocation_context):
        error = self.validate_arguments(tool_name=tool_name, arguments=arguments)
        if error:
            raise ValueError(error)
        context = invocation_context
        is_root = context.session_id == self.root_session_id
        if context.effective_permission_mode != PermissionMode.BYPASS_PERMISSIONS:
            raise ValueError("collaboration operations require bypass permissions")
        if (
            tool_name not in {"query_agents", "send_agent_message", "wait_message"}
            and not is_root
        ):
            raise ValueError("operation is available only to ROOT")
        source_kind = getattr(getattr(context, "invocation_source", None), "kind", None)
        if tool_name in {"run_agent", "wait_agent"} and source_kind != "CODE_MODE_CALL":
            raise ValueError("operation requires a live Code Mode invocation")
        if tool_name == "wait_message" and (
            is_root or source_kind != "MODEL_TOOL_CALL"
        ):
            raise ValueError(
                "message waiting is available only to native subagent calls"
            )
        if tool_name == "run_agent":
            rows, outcome = await self._run(parse_batch_members(arguments), context)
            return _business(
                (ResultPart("object", _run_record(row)) for row in rows),
                outcome=outcome,
            )
        if tool_name == "query_agents":
            target = arguments.get("target")
            if target is None:
                rows = await self._read(
                    self.repository.list_query_agent_sessions,
                    root_session_id=self.root_session_id,
                    observer_session_id=context.session_id,
                    limit=None,
                    deadline_monotonic=self.runtime.read_deadline(),
                )
                parts = [ResultPart("object", self._public_agent(row)) for row in rows]
                return _business(parts or [ResultPart("text", "没有可查询的 Agent。")])
            if target["kind"] == "WORK":
                row = await self._read(
                    self.repository.query_visible_agent_work,
                    root_session_id=self.root_session_id,
                    observer_session_id=context.session_id,
                    work_id=target_id(target, "WORK"),
                    deadline_monotonic=self.runtime.read_deadline(),
                )
                if row is None:
                    raise ValueError("WORK does not exist in this collaboration")
                self._observe_cell(
                    context, ((row["agent_id"], row["work_id"], "query"),)
                )
                return _business([ResultPart("object", self._public_work(row))])
            identity = target_id(target, "AGENT")
            rows = await self._read(
                self.repository.list_query_agent_works,
                root_session_id=self.root_session_id,
                observer_session_id=context.session_id,
                agent_id=identity,
                limit=None,
                deadline_monotonic=self.runtime.read_deadline(),
            )
            self._observe_cell(context, ((identity, None, "query"),))
            parts = []
            for row in rows:
                parts.append(ResultPart("object", self._work_summary(row)))
                if row["status"] == "stopped" and row.get("terminal_reason"):
                    parts.append(
                        ResultPart(
                            "text",
                            "工作已停止：" + row["terminal_reason"].split(":", 1)[0],
                        )
                    )
            return _business(
                parts or [ResultPart("text", "该 Agent 没有可查询的 Work。")]
            )
        if tool_name == "send_agent_message":
            identity = target_id(arguments["target"], "AGENT", allow_root=True)
            identity = self.root_session_id if identity == "ROOT" else identity
            if identity != self.root_session_id:
                await self._require_agent(identity)
                self._observe_cell(context, ((identity, None, "message"),))
            result = await self.runtime.send_agent_steer(
                agent_id=identity, message=arguments["message"], source_context=context
            )
            success = result["delivery"] == "queued"
            return _business(
                [
                    ResultPart(
                        "text",
                        "消息已加入接收方当前轮次的输入队列。"
                        if success
                        else result["message"],
                    )
                ],
                state="SUCCESS" if success else "APPLICATION_ERROR",
            )
        if tool_name == "stop_agent":
            rows, outcome, message = await self._stop(arguments["target"], context)
            meta = {
                state + "_work_count": sum(row["status"] == state for row in rows)
                for state in ("queued", "running", "completed", "stopped")
            }
            return _business(
                [
                    ResultPart("text", message),
                    *[
                        ResultPart(
                            "object",
                            {
                                "target": work_target(row["work_id"]),
                                "work_status": row["status"],
                            },
                        )
                        for row in rows
                    ],
                ],
                meta=meta,
                outcome=outcome,
            )
        if tool_name == "wait_agent":
            return await self._wait_agents(arguments, context)
        state = await self._wait_message(arguments, context)
        return _business(
            [
                ResultPart(
                    "text",
                    "有新消息待处理，正文将通过正常输入路径交付。"
                    if state["state"] == "message_available"
                    else "本次等待已取消。"
                    if state["state"] == "interrupted"
                    else "本次等待超时，尚未发现新消息。",
                )
            ],
            state="CANCELLED" if state["state"] == "interrupted" else "SUCCESS",
        )

    @staticmethod
    def _public_agent(row):
        result = {
            "target": agent_target(row["agent_id"]),
            "current_work": None
            if row["ui_current_work_id"] is None
            else work_target(row["ui_current_work_id"]),
            "current_work_title": row["ui_current_work_title"],
            "queued_work_count": row["ui_queued_work_count"],
        }
        if row.get("title") is not None:
            result["name"] = row["title"]
        return result

    async def _stop(self, target, context):
        if target["kind"] == "WORK":
            work = await self._work_status(target_id(target, "WORK"))
            identity, work_id = work["agent_id"], work["work_id"]
            self._observe_cell(context, ((identity, work_id, "stop"),))
            if work["status"] in TERMINAL:
                return [work], None, "工作已结束，本次未执行停止。"
        else:
            identity, work_id = target_id(target, "AGENT"), None
            await self._require_agent(identity)
            self._observe_cell(context, ((identity, None, "stop"),))
        facts = await self._facts(context)
        outcome = await self.io.run_tool_invocation(
            self.repository.cancel_agent_requests,
            facts["root_guard"],
            agent_id=identity,
            work_id=work_id,
            reason="USER_CANCELLED",
            occurred_at=datetime.now(timezone.utc),
            actor_id=facts["actor_id"],
            deadline_monotonic=facts["deadline_monotonic"],
        )
        if outcome.error is not None:
            raise outcome.error
        for row in outcome.value:
            if row["status"] == "running":
                await self.runtime.stop_active_work(
                    agent_id=identity, work_id=row["work_id"]
                )
        actual = [await self._work_status(row["work_id"]) for row in outcome.value]
        return (
            actual,
            outcome,
            "已请求停止工作，结果反映实际结算。"
            if actual
            else "Agent 没有未结束工作。",
        )

    @asynccontextmanager
    async def _suspend(self, context):
        async with self.runtime.suspend_execution(context):
            yield

    @staticmethod
    def _interrupted(context) -> bool:
        return (
            context.cell_cancel_event is not None and context.cell_cancel_event.is_set()
        )

    async def _wait_change(self, revision, context, *, timeout=None):
        """Race observations with caller control, without cancelling target work."""
        changed = asyncio.create_task(self.runtime.wait_for_agent_change(revision))
        control = (
            None
            if context.cell_cancel_event is None
            else asyncio.create_task(context.cell_cancel_event.wait())
        )
        observers = (changed,) if control is None else (changed, control)
        try:
            done, _ = await asyncio.wait(
                observers, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if control is not None and control in done:
                return "interrupted"
            if changed in done:
                changed.result()
                return "changed"
            return "timeout"
        finally:
            for observer in observers:
                if not observer.done():
                    observer.cancel()
            await asyncio.gather(*observers, return_exceptions=True)

    async def _wait_message(self, arguments, context):
        deadline = monotonic() + arguments.get("timeout_seconds", 30)
        revision = self.runtime.agent_change_revision()
        if await self.runtime.has_pending_agent_message(invocation_context=context):
            return {"state": "message_available"}
        if monotonic() >= deadline:
            return {"state": "timeout"}
        # Do not yield a physical slot for a poll or an already available input.
        # Keep the pre-query revision so arrivals before suspension still wake us.
        async with self._suspend(context):
            while True:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return {"state": "timeout"}
                change = await self._wait_change(revision, context, timeout=remaining)
                if change in {"timeout", "interrupted"}:
                    return {"state": change}
                revision = self.runtime.agent_change_revision()
                if await self.runtime.has_pending_agent_message(
                    invocation_context=context
                ):
                    return {"state": "message_available"}

    async def _agent_wait_snapshot(self, identities, bound_work_ids):
        rows = await self._read(
            self.repository.read_agent_wait_states,
            root_session_id=self.root_session_id,
            agent_ids=identities,
            bound_work_ids=bound_work_ids,
            deadline_monotonic=self.runtime.read_deadline(),
        )
        if tuple(row["agent_id"] for row in rows) != identities:
            raise ValueError("AGENT does not exist in this collaboration")
        for row, work_id in zip(rows, bound_work_ids, strict=True):
            if work_id is not None and row["bound_work_id"] != work_id:
                raise ValueError("bound Work no longer exists for this Agent")
        return rows

    async def _wait_agents(self, arguments, context):
        works_branch = "works" in arguments
        key, kind = ("works", "WORK") if works_branch else ("agents", "AGENT")
        identities = tuple(target_id(t, kind) for t in arguments[key])
        settle, deadline = (
            arguments.get("settle", "all"),
            monotonic() + arguments.get("timeout_seconds", 30),
        )
        owner_cursors = getattr(context, "agent_message_cursors", None)
        after = dict(owner_cursors or {})
        rows = ()
        revision = self.runtime.agent_change_revision()
        self._observe_cell(
            context,
            tuple((i, None, "wait") for i in identities) if not works_branch else (),
        )

        def records():
            if works_branch:
                return [ResultPart("object", _run_record(row)) for row in rows]
            return [
                ResultPart(
                    "object",
                    {
                        "agent": agent_target(row["agent_id"]),
                        "is_idle": not row["has_running_turn"]
                        and not row["has_pending_work"],
                    },
                )
                for row in rows
            ]

        async def evaluate():
            nonlocal rows, revision
            if self._interrupted(context):
                return _business(
                    [
                        *records(),
                        ResultPart(
                            "text", "本次等待已取消，目标工作继续沿原生命周期结算。"
                        ),
                    ],
                    state="CANCELLED",
                )
            if works_branch:
                rows = await self._read(
                    self.repository.read_work_wait_states,
                    root_session_id=self.root_session_id,
                    work_ids=identities,
                    deadline_monotonic=self.runtime.read_deadline(),
                )
                if tuple(row["work_id"] for row in rows) != identities:
                    raise ValueError("WORK does not exist in this collaboration")
                self._observe_cell(
                    context,
                    tuple((row["agent_id"], row["work_id"], "wait") for row in rows),
                )
                satisfied = [row["status"] in TERMINAL for row in rows]
            else:
                rows = await self._agent_wait_snapshot(
                    identities, (None,) * len(identities)
                )
                satisfied = [
                    not row["has_running_turn"] and not row["has_pending_work"]
                    for row in rows
                ]
            if self._interrupted(context):
                return _business(
                    [
                        *records(),
                        ResultPart("text", "本次等待已取消，目标工作未被停止。"),
                    ],
                    state="CANCELLED",
                )
            sources = (
                {}
                if works_branch
                else await self.runtime.pending_message_sources(
                    recipient_session_id=context.session_id,
                    source_agent_ids=identities,
                    after_by_sender=after,
                    invocation_context=context,
                )
            )
            if self._interrupted(context):
                return _business(
                    [
                        *records(),
                        ResultPart("text", "本次等待已取消，目标工作未被停止。"),
                    ],
                    state="CANCELLED",
                )
            reason = (
                "message_available"
                if sources
                else "condition_met"
                if (all(satisfied) if settle == "all" else any(satisfied))
                else "timeout"
                if monotonic() >= deadline
                else None
            )
            if reason is None:
                return None
            meta = {"wait_reason": reason}
            callback = None
            if sources:
                meta["message_sources"] = [
                    agent_target(identity)
                    for identity in identities
                    if identity in sources
                ]
                captured = dict(sources)

                async def delivered():
                    if owner_cursors is not None:
                        for sender, sequence in captured.items():
                            owner_cursors[sender] = max(
                                owner_cursors.get(sender, 0), sequence
                            )

                callback = delivered
            return _business(records(), meta=meta, delivered=callback)

        async def observe():
            try:
                return await evaluate()
            except Exception as error:
                return _business(
                    [*records(), ResultPart("text", str(error))],
                    state="APPLICATION_ERROR",
                )

        result = await observe()
        if result is not None:
            return result
        async with self._suspend(context):
            while True:
                change = await self._wait_change(
                    revision, context, timeout=max(0, deadline - monotonic())
                )
                revision = self.runtime.agent_change_revision()
                if change == "interrupted":
                    return _business(
                        [
                            *records(),
                            ResultPart("text", "本次等待已取消，目标工作未被停止。"),
                        ],
                        state="CANCELLED",
                    )
                result = await observe()
                if result is not None:
                    return result
