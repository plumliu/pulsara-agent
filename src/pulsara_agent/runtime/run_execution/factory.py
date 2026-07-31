"""Common composition owner for Host and subagent run activations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pulsara_agent.runtime.run_execution.registry import RunExecutionRegistry
from pulsara_agent.runtime.run_execution.service import RunActivationService

if TYPE_CHECKING:
    from pulsara_agent.event_log import EventLog
    from pulsara_agent.runtime.agent import AgentRuntime


@dataclass(frozen=True, slots=True)
class RunActivationComposition:
    """One session-scoped runtime, registry, and activation-driver service."""

    agent_runtime: AgentRuntime
    registry: RunExecutionRegistry
    service: RunActivationService


class RunActivationFactory:
    """The sole production constructor for a runnable AgentRuntime composition."""

    def create(
        self,
        *,
        event_log: EventLog,
        runtime_session_id: str,
        agent_runtime_kwargs: dict[str, Any],
    ) -> RunActivationComposition:
        from pulsara_agent.runtime.agent import AgentRuntime

        if "run_execution_registry" in agent_runtime_kwargs:
            raise ValueError("RunActivationFactory owns the registry binding")
        registry = RunExecutionRegistry()
        agent_runtime = AgentRuntime(
            **agent_runtime_kwargs,
            run_execution_registry=registry,
        )
        service = RunActivationService(
            registry=registry,
            event_log=event_log,
            agent_runtime=agent_runtime,
            runtime_session_id=runtime_session_id,
        )
        return RunActivationComposition(
            agent_runtime=agent_runtime,
            registry=registry,
            service=service,
        )


__all__ = ["RunActivationComposition", "RunActivationFactory"]
