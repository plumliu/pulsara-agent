"""Test-only ownership probe for explicitly composed AgentRuntime instances."""

from __future__ import annotations

from weakref import WeakKeyDictionary

from pulsara_agent.runtime.agent import AgentRuntime
from pulsara_agent.runtime.session_run_capabilities import (
    build_agent_runtime_session_capabilities,
)
from pulsara_agent.runtime.session import RuntimeSession
from pulsara_agent.runtime.run_execution.factory import RunActivationFactory
from pulsara_agent.runtime.subagent.activation import SubagentChildActivationService


_SESSIONS: WeakKeyDictionary[AgentRuntime, RuntimeSession] = WeakKeyDictionary()


def build_test_agent_runtime(
    *,
    runtime_session: RuntimeSession,
    **kwargs,
) -> AgentRuntime:
    agent = AgentRuntime(
        **build_agent_runtime_session_capabilities(runtime_session),
        **kwargs,
    )
    if (
        agent.subagent_runtime is not None
        and not agent.subagent_runtime.child_activation_port_bound
    ):
        child_service = SubagentChildActivationService(
            run_identity=agent._run_identity,
            run_ledger_port=agent._run_ledger,
            run_long_horizon_port=agent._run_long_horizon,
            llm_runtime=agent.llm_runtime,
            model_role=agent.model_role,
            options=agent.options,
            budget=agent.budget,
            system_prompt=agent.system_prompt,
            capability_runtime=agent.capability_runtime,
            workspace_kind=agent.workspace_kind,
            rollout_budget_feasibility_report=(
                agent.rollout_budget_feasibility_report
            ),
            activation_factory=RunActivationFactory(),
            subagent_runtime=agent.subagent_runtime,
        )
        agent.subagent_runtime.bind_child_activation_port(child_service)
    register_runtime_session_for_test(agent, runtime_session)
    return agent


def register_runtime_session_for_test(
    agent: AgentRuntime,
    runtime_session: RuntimeSession,
) -> None:
    existing = _SESSIONS.get(agent)
    if existing is not None and existing is not runtime_session:
        raise RuntimeError("test AgentRuntime is already bound to another session")
    _SESSIONS[agent] = runtime_session


def runtime_session_for_test(agent: AgentRuntime) -> RuntimeSession:
    try:
        return _SESSIONS[agent]
    except KeyError as exc:
        raise RuntimeError(
            "test must register the composition-owned RuntimeSession explicitly"
        ) from exc


__all__ = [
    "build_test_agent_runtime",
    "register_runtime_session_for_test",
    "runtime_session_for_test",
]
