"""Purpose-neutral port for RuntimeSession instances without a Host owner."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class NonHostRuntimeSessionTeardownPurpose(StrEnum):
    RESUME_RECOVERY = "resume_recovery"
    CHILD_TERMINAL = "child_terminal"


class NonHostRuntimeSessionTeardownRetryableError(RuntimeError):
    """The physical attempt exited and a later generation may retry."""


class NonHostRuntimeSessionTeardownReconciliationRequired(RuntimeError):
    """The physical attempt exited without a safe automatic retry."""


class NonHostRuntimeSessionTeardownPort(Protocol):
    runtime_session_id: str

    async def teardown_non_host_runtime_session(
        self,
        *,
        purpose: NonHostRuntimeSessionTeardownPurpose,
        deadline_monotonic: float,
    ) -> None: ...


class NonHostRuntimeSessionTeardownCapability(Protocol):
    """Purpose-bound capability that exposes no other RuntimeSession surface."""

    runtime_session_id: str
    purpose: NonHostRuntimeSessionTeardownPurpose

    async def teardown(self, *, deadline_monotonic: float) -> None: ...


@dataclass(frozen=True, slots=True)
class _BoundNonHostRuntimeSessionTeardownCapability:
    runtime_session_id: str
    purpose: NonHostRuntimeSessionTeardownPurpose
    _port: NonHostRuntimeSessionTeardownPort

    async def teardown(self, *, deadline_monotonic: float) -> None:
        await self._port.teardown_non_host_runtime_session(
            purpose=self.purpose,
            deadline_monotonic=deadline_monotonic,
        )


def bind_non_host_runtime_session_teardown_capability(
    port: NonHostRuntimeSessionTeardownPort,
    *,
    purpose: NonHostRuntimeSessionTeardownPurpose,
) -> NonHostRuntimeSessionTeardownCapability:
    """Narrow a RuntimeSession-like port to one closed teardown purpose."""

    runtime_session_id = port.runtime_session_id
    if not isinstance(runtime_session_id, str) or not runtime_session_id:
        raise ValueError("non-Host teardown port has no runtime-session identity")
    if not isinstance(purpose, NonHostRuntimeSessionTeardownPurpose):
        raise TypeError("non-Host teardown capability purpose is invalid")
    return _BoundNonHostRuntimeSessionTeardownCapability(
        runtime_session_id=runtime_session_id,
        purpose=purpose,
        _port=port,
    )


__all__ = [
    "NonHostRuntimeSessionTeardownCapability",
    "NonHostRuntimeSessionTeardownPort",
    "NonHostRuntimeSessionTeardownPurpose",
    "NonHostRuntimeSessionTeardownReconciliationRequired",
    "NonHostRuntimeSessionTeardownRetryableError",
    "bind_non_host_runtime_session_teardown_capability",
]
