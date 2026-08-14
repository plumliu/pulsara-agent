"""Exact process-local foreground cancellation intent.

The carrier is owned by one live task slot.  It is never serialized and adds
no durable cancellation generation or recovery promise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
from threading import Lock

from pulsara_agent.model_input.contracts import ModelInputScopeKind


class ForegroundCancellationCause(StrEnum):
    USER_REQUEST = "USER_REQUEST"
    HOST_SESSION_CLOSE = "HOST_SESSION_CLOSE"


@dataclass(slots=True)
class ActiveTurnCancellationIntent:
    turn_id: str
    scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    _cause: ForegroundCancellationCause | None = None
    _lock: Lock | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.turn_id:
            raise ValueError("cancellation intent turn identity is empty")
        if (self.scope_kind is ModelInputScopeKind.ROOT) != (
            self.scope_subagent_task_id is None
        ):
            raise ValueError("cancellation intent scope union is invalid")
        self._lock = Lock()

    @property
    def cause(self) -> ForegroundCancellationCause | None:
        assert self._lock is not None
        with self._lock:
            return self._cause

    def install_cause(
        self, cause: ForegroundCancellationCause
    ) -> ForegroundCancellationCause:
        assert self._lock is not None
        with self._lock:
            if self._cause is None:
                self._cause = cause
            return self._cause

    def require_exact(
        self,
        *,
        turn_id: str,
        scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
    ) -> None:
        if (
            self.turn_id != turn_id
            or self.scope_kind is not scope_kind
            or self.scope_subagent_task_id != scope_subagent_task_id
        ):
            raise RuntimeError("cancellation intent does not exact-join turn")


def stable_subagent_turn_id(*, session_id: str, task_id: str) -> str:
    if not session_id or not task_id:
        raise ValueError("subagent turn identity input is empty")
    digest = sha256(f"{session_id}\0{task_id}".encode()).hexdigest()
    return f"subagent-turn:{digest}"


__all__ = [
    "ActiveTurnCancellationIntent",
    "ForegroundCancellationCause",
    "stable_subagent_turn_id",
]
