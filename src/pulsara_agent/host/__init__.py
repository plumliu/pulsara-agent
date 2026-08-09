"""Public Host boundary.

``HostCore`` resolves to the Stage 2 relational conversation kernel.  The
legacy EventLog core remains available only from its explicit implementation
module until the Stage 3--5 physical-delete pass; it is deliberately not a
package-default production entrypoint.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any


_EXPORTS = {
    "HostCore": ("pulsara_agent.conversation_kernel.host", "KernelHostCore"),
    "HostWorkspaceInput": (
        "pulsara_agent.workspace_identity",
        "HostWorkspaceInput",
    ),
    "ResolvedWorkspace": (
        "pulsara_agent.workspace_identity",
        "ResolvedWorkspace",
    ),
    "normalize_workspace_kind": (
        "pulsara_agent.workspace_identity",
        "normalize_workspace_kind",
    ),
    "resolve_workspace": ("pulsara_agent.workspace_identity", "resolve_workspace"),
    # Explicit compatibility exports remain lazy until the Stage 3--5 physical
    # deletion.  Asking for one opts into its legacy implementation module;
    # importing the production Host package does not load that graph.
    "HostCoreLifecycle": ("pulsara_agent.host.core", "HostCoreLifecycle"),
    "DuplicateHostSessionError": (
        "pulsara_agent.host.registry",
        "DuplicateHostSessionError",
    ),
    "HostSessionRegistry": (
        "pulsara_agent.host.registry",
        "HostSessionRegistry",
    ),
    "HostSessionSummary": (
        "pulsara_agent.host.registry",
        "HostSessionSummary",
    ),
    "SessionReservation": (
        "pulsara_agent.host.registry",
        "SessionReservation",
    ),
    "DanglingRunRepairResult": (
        "pulsara_agent.host.resume",
        "DanglingRunRepairResult",
    ),
    "HostSession": ("pulsara_agent.host.session", "HostSession"),
    "HostSessionBusyError": (
        "pulsara_agent.host.session",
        "HostSessionBusyError",
    ),
    "HostSessionLifecycle": (
        "pulsara_agent.host.session",
        "HostSessionLifecycle",
    ),
    "HostSessionPendingApprovalError": (
        "pulsara_agent.host.session",
        "HostSessionPendingApprovalError",
    ),
    "HostSessionPendingInteractionError": (
        "pulsara_agent.host.session",
        "HostSessionPendingInteractionError",
    ),
    "ResumableSessionSummary": (
        "pulsara_agent.host.session_manifest",
        "ResumableSessionSummary",
    ),
    "SessionManifest": ("pulsara_agent.host.session_manifest", "SessionManifest"),
    "DuplicateTerminalOwnerError": (
        "pulsara_agent.host.supervisor",
        "DuplicateTerminalOwnerError",
    ),
    "WorkspaceClosingError": (
        "pulsara_agent.host.supervisor",
        "WorkspaceClosingError",
    ),
    "WorkspaceLifecycleState": (
        "pulsara_agent.host.supervisor",
        "WorkspaceLifecycleState",
    ),
    "WorkspaceTerminalSnapshot": (
        "pulsara_agent.host.supervisor",
        "WorkspaceTerminalSnapshot",
    ),
    "WorkspaceTerminalSupervisor": (
        "pulsara_agent.host.supervisor",
        "WorkspaceTerminalSupervisor",
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
    "DuplicateHostSessionError",
    "DuplicateTerminalOwnerError",
    "DanglingRunRepairResult",
    "HostCore",
    "HostCoreLifecycle",
    "HostSession",
    "HostSessionBusyError",
    "HostSessionLifecycle",
    "HostSessionPendingApprovalError",
    "HostSessionPendingInteractionError",
    "HostSessionRegistry",
    "HostSessionSummary",
    "HostWorkspaceInput",
    "ResolvedWorkspace",
    "ResumableSessionSummary",
    "SessionReservation",
    "SessionManifest",
    "WorkspaceClosingError",
    "WorkspaceLifecycleState",
    "WorkspaceTerminalSnapshot",
    "WorkspaceTerminalSupervisor",
    "normalize_workspace_kind",
    "resolve_workspace",
]
