"""Test-only adapters for the typed child activation port."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, cast

from pulsara_agent.ports.run_execution import RunHandle

if TYPE_CHECKING:
    from pulsara_agent.ports.subagent import SubagentChildActivationPort
    from pulsara_agent.runtime.subagent.hydration import HydratedSubagentRunView
    from pulsara_agent.runtime.subagent.runtime import SubagentRuntime


TestChildActivationCallback = Callable[
    ["SubagentRuntime", "HydratedSubagentRunView"],
    Awaitable[None],
]


class CallbackSubagentChildActivationPort:
    """Adapt a test callback without adding a callable seam to production."""

    def __init__(
        self,
        callback: TestChildActivationCallback,
        *,
        delegate: "SubagentChildActivationPort | None" = None,
    ) -> None:
        self._callback = callback
        self._delegate = delegate
        self._runtime: SubagentRuntime | None = None

    def bind(self, runtime: SubagentRuntime) -> None:
        if self._runtime is not None and self._runtime is not runtime:
            raise RuntimeError("test child activation port is already bound")
        self._runtime = runtime

    async def activate_committed_child(
        self,
        subagent_run_id: str,
        *,
        deadline_monotonic: float,
    ) -> RunHandle:
        runtime = self._runtime
        if runtime is None:
            raise RuntimeError("test child activation port is not bound")
        view = await runtime.hydrate_child_activation_run(subagent_run_id)
        await self._callback(runtime, view)
        return cast(RunHandle, _CompletedTestRunHandle())

    async def terminalize_committed_child(
        self,
        subagent_run_id: str,
        *,
        termination_kind: str,
        deadline_monotonic: float,
    ):
        if self._delegate is None:
            raise RuntimeError(
                "test callback activation does not own a committed child RunOwner"
            )
        return await self._delegate.terminalize_committed_child(
            subagent_run_id,
            termination_kind=termination_kind,
            deadline_monotonic=deadline_monotonic,
        )

    def retire_child_activation(self, subagent_run_id: str) -> None:
        if self._delegate is not None:
            self._delegate.retire_child_activation(subagent_run_id)


class _CompletedTestRunHandle:
    pass


__all__ = ["CallbackSubagentChildActivationPort", "TestChildActivationCallback"]
