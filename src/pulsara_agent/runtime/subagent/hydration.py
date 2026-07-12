"""Bounded archive and child-ledger hydration for subagent graph facts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal

from pulsara_agent.event import RunEndEvent, RunStartEvent
from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.runtime.subagent.facts import (
    SubagentResultFact,
    SubagentRunFact,
    SubagentTaskFact,
)
from pulsara_agent.runtime.subagent.projection import EventLogLocator


@dataclass(frozen=True, slots=True)
class SubagentHydrationDiagnostic:
    code: str
    severity: Literal["warning", "error"]
    entity_kind: Literal["task", "run", "result"]
    entity_id: str
    artifact_id: str | None = None
    child_runtime_session_id: str | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class HydratedSubagentTaskView:
    fact: SubagentTaskFact
    objective_text: str | None
    objective_text_complete: bool
    diagnostics: tuple[SubagentHydrationDiagnostic, ...] = ()


@dataclass(frozen=True, slots=True)
class HydratedSubagentRunView:
    fact: SubagentRunFact
    task_text: str | None
    task_text_complete: bool
    child_run_id: str | None
    child_terminal_status: str | None
    diagnostics: tuple[SubagentHydrationDiagnostic, ...] = ()


@dataclass(frozen=True, slots=True)
class HydratedSubagentResultView:
    fact: SubagentResultFact
    result_text: str | None
    result_text_complete: bool
    diagnostics: tuple[SubagentHydrationDiagnostic, ...] = ()


class SubagentGraphHydrator:
    def __init__(
        self,
        *,
        archive: ArtifactStore,
        parent_runtime_session_id: str,
        event_log_locator: EventLogLocator,
    ) -> None:
        self._archive = archive
        self._parent_runtime_session_id = parent_runtime_session_id
        self._event_log_locator = event_log_locator

    async def hydrate_task(
        self,
        fact: SubagentTaskFact,
        *,
        max_chars: int,
    ) -> HydratedSubagentTaskView:
        text, complete, diagnostics = await self._read_artifact(
            fact.objective_artifact_id,
            entity_kind="task",
            entity_id=fact.task_id,
            max_chars=max_chars,
        )
        return HydratedSubagentTaskView(
            fact=fact,
            objective_text=text,
            objective_text_complete=complete,
            diagnostics=diagnostics,
        )

    async def hydrate_run(
        self,
        fact: SubagentRunFact,
        *,
        include_task_text: bool,
        include_child_native: bool,
        max_chars: int,
    ) -> HydratedSubagentRunView:
        task_text: str | None = None
        task_complete = not include_task_text
        diagnostics: list[SubagentHydrationDiagnostic] = []
        if include_task_text:
            if fact.task_artifact_id is None:
                diagnostics.append(
                    SubagentHydrationDiagnostic(
                        code="subagent_task_artifact_missing",
                        severity="error",
                        entity_kind="run",
                        entity_id=fact.subagent_run_id,
                        message="Run has no durable spawn-task artifact reference.",
                    )
                )
                task_complete = False
            else:
                task_text, task_complete, artifact_diagnostics = await self._read_artifact(
                    fact.task_artifact_id,
                    entity_kind="run",
                    entity_id=fact.subagent_run_id,
                    max_chars=max_chars,
                )
                diagnostics.extend(artifact_diagnostics)

        child_run_id: str | None = None
        child_terminal_status: str | None = None
        if include_child_native:
            child_run_id, child_terminal_status, child_diagnostics = await asyncio.to_thread(
                self._hydrate_child_native,
                fact,
            )
            diagnostics.extend(child_diagnostics)

        return HydratedSubagentRunView(
            fact=fact,
            task_text=task_text,
            task_text_complete=task_complete,
            child_run_id=child_run_id,
            child_terminal_status=child_terminal_status,
            diagnostics=tuple(diagnostics),
        )

    async def hydrate_result(
        self,
        fact: SubagentResultFact,
        *,
        max_chars: int,
    ) -> HydratedSubagentResultView:
        text, complete, diagnostics = await self._read_artifact(
            fact.final_message_artifact_id,
            entity_kind="result",
            entity_id=fact.result_id,
            max_chars=max_chars,
        )
        return HydratedSubagentResultView(
            fact=fact,
            result_text=text,
            result_text_complete=complete,
            diagnostics=diagnostics,
        )

    async def _read_artifact(
        self,
        artifact_id: str,
        *,
        entity_kind: Literal["task", "run", "result"],
        entity_id: str,
        max_chars: int,
    ) -> tuple[str | None, bool, tuple[SubagentHydrationDiagnostic, ...]]:
        if max_chars < 1:
            raise ValueError("max_chars must be >= 1")
        try:
            result = await asyncio.to_thread(
                self._archive.read_text,
                artifact_id,
                session_id=self._parent_runtime_session_id,
                max_chars=max_chars,
            )
        except Exception as exc:
            return (
                None,
                False,
                (
                    SubagentHydrationDiagnostic(
                        code="subagent_artifact_unavailable",
                        severity="error",
                        entity_kind=entity_kind,
                        entity_id=entity_id,
                        artifact_id=artifact_id,
                        message=type(exc).__name__,
                    ),
                ),
            )
        diagnostics: tuple[SubagentHydrationDiagnostic, ...] = ()
        if result.has_more:
            diagnostics = (
                SubagentHydrationDiagnostic(
                    code="subagent_artifact_clipped",
                    severity="warning",
                    entity_kind=entity_kind,
                    entity_id=entity_id,
                    artifact_id=artifact_id,
                    message="Artifact hydration was clipped to the requested hard cap.",
                ),
            )
        return result.text, not result.has_more, diagnostics

    def _hydrate_child_native(
        self,
        fact: SubagentRunFact,
    ) -> tuple[str | None, str | None, tuple[SubagentHydrationDiagnostic, ...]]:
        try:
            child_log = self._event_log_locator.event_log_for_runtime_session(
                fact.child_runtime_session_id
            )
            events = child_log.iter()
        except Exception as exc:
            return (
                None,
                None,
                (
                    SubagentHydrationDiagnostic(
                        code="child_event_log_unavailable",
                        severity="error",
                        entity_kind="run",
                        entity_id=fact.subagent_run_id,
                        child_runtime_session_id=fact.child_runtime_session_id,
                        message=type(exc).__name__,
                    ),
                ),
            )

        starts = [
            event
            for event in events
            if isinstance(event, RunStartEvent)
            and _child_attribution_matches(
                event.metadata,
                subagent_run_id=fact.subagent_run_id,
                parent_runtime_session_id=fact.parent_runtime_session_id,
            )
        ]
        if not starts:
            return (
                None,
                None,
                (
                    SubagentHydrationDiagnostic(
                        code="child_native_run_missing",
                        severity="warning",
                        entity_kind="run",
                        entity_id=fact.subagent_run_id,
                        child_runtime_session_id=fact.child_runtime_session_id,
                    ),
                ),
            )
        if len(starts) != 1:
            return (
                None,
                None,
                (
                    SubagentHydrationDiagnostic(
                        code="multiple_child_native_runs",
                        severity="error",
                        entity_kind="run",
                        entity_id=fact.subagent_run_id,
                        child_runtime_session_id=fact.child_runtime_session_id,
                    ),
                ),
            )
        child_run_id = starts[0].run_id
        if fact.reported_child_run_id is not None and fact.reported_child_run_id != child_run_id:
            return (
                child_run_id,
                None,
                (
                    SubagentHydrationDiagnostic(
                        code="child_run_attribution_mismatch",
                        severity="error",
                        entity_kind="run",
                        entity_id=fact.subagent_run_id,
                        child_runtime_session_id=fact.child_runtime_session_id,
                    ),
                ),
            )
        ends = [
            event
            for event in events
            if isinstance(event, RunEndEvent) and event.run_id == child_run_id
        ]
        status = ends[-1].status if ends else None
        return child_run_id, status, ()


def _child_attribution_matches(
    metadata: dict[str, object],
    *,
    subagent_run_id: str,
    parent_runtime_session_id: str,
) -> bool:
    raw = metadata.get("subagent")
    return (
        isinstance(raw, dict)
        and raw.get("subagent_run_id") == subagent_run_id
        and raw.get("parent_runtime_session_id") == parent_runtime_session_id
    )
