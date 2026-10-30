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

from pulsara_agent.code_mode.contracts import CellParticipationObserver, CellParticipationReference
from pulsara_agent.conversation_kernel.subagents.collaboration_contracts import (
    AGENT_TOOLS,
    agent_target,
    work_target,
    parse_batch_members,
    prepared_request,
    target_id,
    validate_collaboration_parameters,
)
from pulsara_agent.conversation_kernel.tool_contracts import KernelToolResult
from pulsara_agent.ports.tool_execution import ToolOutputSourceFormatHint
from pulsara_agent.primitives.context import canonical_json_bytes
from pulsara_agent.primitives.permission import PermissionMode

TERMINAL = frozenset({"completed", "stopped"})


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

    def _observe_cell(self, context, references: tuple[CellParticipationReference, ...]) -> None:
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
                    None if row["current_work_id"] is None
                    else work_target(row["current_work_id"])
                ),
                "queued_count": row["queued_count"],
            }
            for row in rows
        )

    async def resolve_visible_agent(self, agent_id: str):
        from pulsara_agent.conversation_kernel.presentation_targets import resolve_visible_agent
        return await self._read(
            resolve_visible_agent, repository=self.repository,
            root_session_id=self.root_session_id, agent_session_id=agent_id,
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

    async def _work(self, work_id: str) -> dict:
        row = await self._read(
            self.repository.query_agent_work,
            root_session_id=self.root_session_id,
            work_id=work_id,
            deadline_monotonic=self.runtime.read_deadline(),
        )
        if row is None:
            raise ValueError("WORK does not exist in this collaboration")
        return self._public_work(row)

    async def _work_status(self, work_id: str) -> dict:
        row = await self._read(self.repository.read_agent_work_graph_facts,
            root_session_id=self.root_session_id, work_id=work_id,
            deadline_monotonic=self.runtime.read_deadline())
        if row is None:
            raise ValueError("WORK does not exist in this collaboration")
        return self._public_work(row)

    async def read_cell_work_page(self, *, source_exec_attempt_id, cursor, limit):
        return await self._read(self.repository.list_cell_agent_works,
            root_session_id=self.root_session_id, source_exec_attempt_id=source_exec_attempt_id,
            cursor=cursor, limit=limit, deadline_monotonic=self.runtime.read_deadline())

    async def visible_cell_participants(self, participants):
        visible = []
        for item in participants:
            if item["work_id"] is None:
                state = await self._read(self.repository.read_agent_runtime_state,
                    session_id=item["agent_id"], deadline_monotonic=self.runtime.read_deadline())
                present = state is not None and state["root_session_id"] == self.root_session_id
            else:
                fact = await self._read(self.repository.read_agent_work_graph_facts,
                    root_session_id=self.root_session_id, work_id=item["work_id"],
                    deadline_monotonic=self.runtime.read_deadline())
                present = fact is not None and fact["agent_id"] == item["agent_id"]
            if present:
                visible.append(item)
        return visible

    @staticmethod
    def _public_work(row: Mapping) -> dict:
        result = {
            "target": work_target(row["work_id"]),
            "agent": agent_target(row["agent_id"]),
            "status": row["status"],
            "terminal_reason": row.get("terminal_reason"),
        }
        for field in (
            "instruction",
            "final",
            "diagnostic",
            "failed_dependency",
            "name",
        ):
            if row.get(field) is not None:
                result[field] = row[field]
        reason = row.get("terminal_reason")
        if isinstance(reason, str) and reason.startswith("DEPENDENCY_NOT_SUCCESSFUL:"):
            result["failed_dependency"] = work_target(
                reason.removeprefix("DEPENDENCY_NOT_SUCCESSFUL:")
            )
        return result

    async def _agent_rows(self, agent_id: str, *, cursor=None, limit=20):
        # The repository checks ownership even when no work rows are returned.
        rows = await self._read(
            self.repository.list_agent_works,
            root_session_id=self.root_session_id,
            agent_id=agent_id,
            cursor=cursor,
            limit=limit,
            deadline_monotonic=self.runtime.read_deadline(),
        )
        if rows is None:
            raise ValueError("AGENT does not exist in this collaboration")
        return rows

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
                    snapshot.initial_context_units[-r.context_turns :]
                ),
            )
            if r.create_agent and r.context_mode == "last_n"
            else r
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
        return tuple(
            {
                "agent": agent_target(row["agent_id"]),
                "work": work_target(row["work_id"]),
                "status": row["status"],
            }
            for row in rows
        ), outcome

    async def invoke(self, *, tool_name, arguments, invocation_context):
        error = self.validate_arguments(tool_name=tool_name, arguments=arguments)
        if error:
            raise ValueError(error)
        context = invocation_context
        is_root = context.session_id == self.root_session_id
        if context.effective_permission_mode != PermissionMode.BYPASS_PERMISSIONS:
            raise ValueError("collaboration operations require bypass permissions")
        if tool_name not in {"query_agents", "send_agent_message", "wait_agent"} and not is_root:
            raise ValueError("operation is available only to ROOT")
        if (
            tool_name in {"run_agent.batch", "wait_agent.works"}
            and getattr(getattr(context, "invocation_source", None), "kind", None)
            != "CODE_MODE_CALL"
        ):
            raise ValueError("operation requires a live Code Mode invocation")
        outcome = None
        if tool_name == "run_agent":
            rows, outcome = await self._run((prepared_request(arguments),), context)
            result = rows[0]
        elif tool_name == "run_agent.batch":
            members = parse_batch_members(arguments)
            rows, outcome = await self._run(tuple(r for _, r in members), context)
            result = {key: row for (key, _), row in zip(members, rows, strict=True)}
        elif tool_name == "query_agents" and "target" not in arguments:
            limit = arguments.get("max_items", 50)
            rows = await self._read(
                self.repository.list_query_agent_sessions,
                root_session_id=self.root_session_id,
                observer_session_id=context.session_id,
                cursor=arguments.get("cursor"),
                limit=limit + 1,
                deadline_monotonic=self.runtime.read_deadline(),
            )
            selected = rows[:limit]
            result = {
                "root": agent_target("ROOT"),
                "agents": [self._public_agent(row) for row in selected],
                "next_cursor": self._agent_cursor(selected[-1])
                if len(rows) > limit
                else None,
            }
        elif tool_name == "query_agents":
            target = arguments["target"]
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
                result = self._public_work(row)
                self._observe_cell(context, ((result["agent"]["agent_id"], result["target"]["work_id"], "query"),))
            else:
                identity = target_id(target, "AGENT")
                limit = arguments.get("max_items", 50)
                rows = await self._read(
                    self.repository.list_query_agent_works,
                    root_session_id=self.root_session_id,
                    observer_session_id=context.session_id,
                    agent_id=identity,
                    cursor=arguments.get("cursor"),
                    limit=limit + 1,
                    deadline_monotonic=self.runtime.read_deadline(),
                )
                selected = rows[:limit]
                self._observe_cell(context, ((identity, None, "query"),))
                result = {
                    "agent": agent_target(identity),
                    "works": [self._public_work(row) for row in selected],
                    "next_cursor": self._work_cursor(selected[-1])
                    if len(rows) > limit
                    else None,
                }
        elif tool_name == "send_agent_message":
            identity = target_id(arguments["target"], "AGENT", allow_root=True)
            identity = self.root_session_id if identity == "ROOT" else identity
            if identity != self.root_session_id:
                await self._agent_rows(identity, limit=1)
                self._observe_cell(context, ((identity, None, "message"),))
            result = await self.runtime.send_agent_steer(
                agent_id=identity, message=arguments["message"], source_context=context
            )
        elif tool_name == "stop_agent":
            result, outcome = await self._stop(arguments["target"], context)
        elif tool_name == "wait_agent":
            result = await self._wait_agents(arguments, context)
        else:
            result = await self._wait_works(arguments, context)
        return KernelToolResult(
            state="SUCCESS",
            content=canonical_json_bytes(result),
            public_format=ToolOutputSourceFormatHint.JSON,
            physical_timing="ON_TIME" if outcome is None else outcome.timing.value,
            caller_cancelled_while_running=False
            if outcome is None
            else outcome.caller_cancelled,
            physical_observation=None if outcome is None else outcome.observation,
        )

    @staticmethod
    def _agent_cursor(row):
        return row.get("cursor", row["agent_id"])

    @staticmethod
    def _work_cursor(row):
        return row.get("cursor", row["work_id"])

    @staticmethod
    def _public_agent(row):
        result = {
            "agent": agent_target(row["agent_id"]),
            "name": row.get("name", row.get("title")),
            **{
                k: row[k]
                for k in ("current_work", "queued_count")
                if k in row
            },
        }
        if "current_work_id" in row:
            result["current_work"] = (
                None
                if row["current_work_id"] is None
                else work_target(row["current_work_id"])
            )
        return result

    async def _stop(self, target, context):
        if target["kind"] == "WORK":
            work = await self._work(target_id(target, "WORK"))
            identity, work_id = work["agent"]["agent_id"], work["target"]["work_id"]
            self._observe_cell(context, ((identity, work_id, "stop"),))
            if work["status"] in TERMINAL:
                return {
                    "target": target,
                    "stopped": False,
                    "works": [work],
                    "message": "Work already ended; no stop was performed.",
                }, None
        else:
            identity, work_id = target_id(target, "AGENT"), None
            await self._agent_rows(identity, limit=1)
            self._observe_cell(context, ((identity, None, "stop"),))
        facts = await self._facts(context)
        guard = facts["root_guard"]
        outcome = await self.io.run_tool_invocation(
            self.repository.cancel_agent_requests,
            guard,
            agent_id=identity,
            work_id=work_id,
            reason="USER_CANCELLED",
            occurred_at=datetime.now(timezone.utc),
            actor_id=facts["actor_id"],
            deadline_monotonic=facts["deadline_monotonic"],
        )
        if outcome.error is not None:
            raise outcome.error
        rows = outcome.value
        for row in rows:
            if row["status"] == "running":
                await self.runtime.stop_active_work(
                    agent_id=identity, work_id=row["work_id"]
                )
        actual = [await self._work(row["work_id"]) for row in rows]
        return {
            "target": target,
            "stop_requested": bool(actual),
            "stopped": bool(actual)
            and all(work["status"] == "stopped" for work in actual),
            "works": actual,
            "message": "Stop requested; outcomes reflect actual settlement."
            if actual
            else "Agent has no unfinished work.",
        }, outcome

    @asynccontextmanager
    async def _suspend(self, context):
        async with self.runtime.suspend_execution(context):
            yield

    @staticmethod
    def _interrupted(context) -> bool:
        return context.cell_cancel_event is not None and context.cell_cancel_event.is_set()

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

    async def _wait_works(self, arguments, context):
        identities = tuple(target_id(t, "WORK") for t in arguments["targets"])
        async with self._suspend(context):
            while True:
                revision = self.runtime.agent_change_revision()
                works = [await self._work_status(identity) for identity in identities]
                self._observe_cell(context, tuple(
                    (work["agent"]["agent_id"], work["target"]["work_id"], "wait") for work in works))
                pending = [
                    work["target"] for work in works if work["status"] not in TERMINAL
                ]
                if self._interrupted(context):
                    return {
                        "state": "interrupted",
                        "works": works,
                        "pending_targets": pending,
                    }
                if not pending or (
                    arguments.get("settle", "all") == "first"
                    and len(pending) < len(works)
                ):
                    return {
                        "state": "settled",
                        "works": works,
                        "pending_targets": pending,
                    }
                change = await self._wait_change(revision, context)
                if change == "interrupted":
                    return {
                        "state": "interrupted",
                        "works": works,
                        "pending_targets": pending,
                    }

    async def _bind(self, identities, context):
        bindings, unavailable = [], []
        for identity in identities:
            target = agent_target(identity)
            if identity == self.root_session_id:
                bindings.append({"target": target, "work": None})
                continue
            row = await self._read(
                self.repository.read_agent_runtime_state,
                session_id=identity,
                deadline_monotonic=self.runtime.read_deadline(),
            )
            if row is None or row["root_session_id"] != self.root_session_id:
                raise ValueError("AGENT does not exist in this collaboration")
            active = row["current_work_id"]
            if active is not None:
                if identity == context.session_id:
                    raise ValueError("cannot wait for your own current Work")
                bindings.append({"target": target, "work": work_target(active)})
            elif row["has_pending_work"]:
                unavailable.append({"target": target, "reason": "not_started"})
                bindings.append({"target": target, "work": None})
            elif row["latest_work_id"] is not None:
                bindings.append(
                    {"target": target, "work": work_target(row["latest_work_id"])}
                )
            else:
                unavailable.append({"target": target, "reason": "inactive"})
                bindings.append({"target": target, "work": None})

        return bindings, unavailable

    async def _wait_agents(self, arguments, context):
        identities = tuple(
            self.root_session_id
            if (identity := target_id(t, "AGENT", allow_root=True)) == "ROOT"
            else identity
            for t in arguments["targets"]
        )
        if len(set(identities)) != len(identities):
            raise ValueError("duplicate resolved AGENT targets")
        # The cell owns its sender-specific observation cursors. Native waits
        # rely only on the receiving context's normal consumed input position.
        owner_cursors = getattr(context, "agent_message_cursors", None)
        after = dict(owner_cursors or {})
        deadline = monotonic() + arguments.get("timeout_seconds", 30)
        async with self._suspend(context):
            revision = self.runtime.agent_change_revision()
            bindings, unavailable = await self._bind(identities, context)
            self._observe_cell(context, tuple(
                (binding["target"]["agent_id"],
                 None if binding["work"] is None else binding["work"]["work_id"], "wait")
                for binding in bindings if binding["target"]["agent_id"] != self.root_session_id))
            while True:
                sources = await self.runtime.pending_message_sources(
                    recipient_session_id=context.session_id,
                    source_agent_ids=identities,
                    after_by_sender=after,
                    invocation_context=context,
                )
                works = [
                    await self._work_status(binding["work"]["work_id"])
                    for binding in bindings
                    if binding["work"] is not None
                ]
                done = {
                    work["target"]["work_id"]
                    for work in works
                    if work["status"] in TERMINAL
                }
                pending = [
                    binding["target"]
                    for binding in bindings
                    if binding["work"] is None or binding["work"]["work_id"] not in done
                ]
                result = {
                    "bindings": bindings,
                    "works": works,
                    "pending_targets": pending,
                }
                if self._interrupted(context):
                    return {"state": "interrupted", **result}
                if sources:
                    if owner_cursors is not None:
                        for sender, sequence in sources.items():
                            owner_cursors[sender] = max(
                                owner_cursors.get(sender, 0), sequence
                            )
                    return {
                        "state": "message_available",
                        **result,
                        "message_sources": [
                            agent_target(identity) for identity in sources
                        ],
                    }
                if unavailable:
                    return {
                        "state": "unavailable",
                        **result,
                        "unavailable_targets": unavailable,
                    }
                if not pending or (arguments.get("settle", "all") == "first" and done):
                    return {"state": "settled", **result}
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return {"state": "timeout", **result}
                change = await self._wait_change(revision, context, timeout=remaining)
                if change in {"timeout", "interrupted"}:
                    return {"state": change, **result}
                revision = self.runtime.agent_change_revision()
