"""Terminal runtime compatibility exports.

The Stage 2 kernel imports narrow terminal submodules directly.  Keep this
package facade lazy so that doing so does not initialize the legacy monitor,
notification, or EventLog completion graph.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any


_EXPORTS = {
    "BorrowedWorkspaceTerminalRuntime": (
        "pulsara_agent.runtime.terminal.binding",
        "BorrowedWorkspaceTerminalRuntime",
    ),
    "OwnedTerminalRuntime": (
        "pulsara_agent.runtime.terminal.binding",
        "OwnedTerminalRuntime",
    ),
    "TerminalOwnerContext": (
        "pulsara_agent.runtime.terminal.binding",
        "TerminalOwnerContext",
    ),
    "TerminalRuntimeBinding": (
        "pulsara_agent.runtime.terminal.binding",
        "TerminalRuntimeBinding",
    ),
    "WorkspaceTerminalLease": (
        "pulsara_agent.runtime.terminal.binding",
        "WorkspaceTerminalLease",
    ),
    "TerminalSessionManager": (
        "pulsara_agent.runtime.terminal.manager",
        "TerminalSessionManager",
    ),
    "TerminalBackendType": (
        "pulsara_agent.runtime.terminal.models",
        "TerminalBackendType",
    ),
    "TerminalIOMode": (
        "pulsara_agent.runtime.terminal.models",
        "TerminalIOMode",
    ),
    "TerminalProcessInfo": (
        "pulsara_agent.runtime.terminal.models",
        "TerminalProcessInfo",
    ),
    "TerminalProcessLog": (
        "pulsara_agent.runtime.terminal.models",
        "TerminalProcessLog",
    ),
    "TerminalRequest": (
        "pulsara_agent.runtime.terminal.models",
        "TerminalRequest",
    ),
    "TerminalResult": (
        "pulsara_agent.runtime.terminal.models",
        "TerminalResult",
    ),
    "TerminalSessionState": (
        "pulsara_agent.runtime.terminal.models",
        "TerminalSessionState",
    ),
    "TerminalStatus": (
        "pulsara_agent.runtime.terminal.models",
        "TerminalStatus",
    ),
    "ExecPolicyDecision": (
        "pulsara_agent.runtime.terminal.policy",
        "ExecPolicyDecision",
    ),
    "ExecPolicyDecisionKind": (
        "pulsara_agent.runtime.terminal.policy",
        "ExecPolicyDecisionKind",
    ),
    "TerminalExecPolicy": (
        "pulsara_agent.runtime.terminal.policy",
        "TerminalExecPolicy",
    ),
    "PendingTerminalCompletionError": (
        "pulsara_agent.runtime.terminal.process",
        "PendingTerminalCompletionError",
    ),
    "ProcessRegistry": (
        "pulsara_agent.runtime.terminal.process",
        "ProcessRegistry",
    ),
    "TerminalProcessState": (
        "pulsara_agent.runtime.terminal.process",
        "TerminalProcessState",
    ),
    "TerminalSession": (
        "pulsara_agent.runtime.terminal.session",
        "TerminalSession",
    ),
}


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    value = getattr(import_module(target[0]), target[1])
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


__all__ = [
    "BorrowedWorkspaceTerminalRuntime",
    "ExecPolicyDecision",
    "ExecPolicyDecisionKind",
    "OwnedTerminalRuntime",
    "PendingTerminalCompletionError",
    "TerminalBackendType",
    "TerminalIOMode",
    "TerminalOwnerContext",
    "TerminalProcessInfo",
    "TerminalProcessLog",
    "TerminalRequest",
    "TerminalResult",
    "TerminalRuntimeBinding",
    "TerminalSession",
    "TerminalSessionManager",
    "TerminalSessionState",
    "TerminalStatus",
    "ProcessRegistry",
    "TerminalProcessState",
    "TerminalExecPolicy",
    "WorkspaceTerminalLease",
]
