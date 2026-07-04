"""Typed terminal ownership binding for a RuntimeSession.

These types live in the runtime layer so ``RuntimeSession`` can describe how it
obtained its ``TerminalSessionManager`` without depending on the host layer.
The host ``WorkspaceTerminalSupervisor`` builds a ``BorrowedWorkspaceTerminalRuntime``
from the lease it hands out; standalone runtimes use ``OwnedTerminalRuntime``.

Authorization principal is always ``TerminalOwnerContext.host_session_id``;
``conversation_id`` / ``runtime_session_id`` are diagnostics only (contract §1).
"""

from __future__ import annotations

from dataclasses import dataclass

from pulsara_agent.runtime.terminal.manager import TerminalSessionManager


@dataclass(frozen=True, slots=True)
class TerminalOwnerContext:
    host_session_id: str
    conversation_id: str
    runtime_session_id: str | None = None


@dataclass(frozen=True, slots=True)
class OwnedTerminalRuntime:
    """Standalone runtime owns a local manager.

    ``manager=None`` means the RuntimeSession builds a default local manager
    rooted at its workspace_root. ``RuntimeSession.close()`` shuts it down.
    """

    manager: TerminalSessionManager | None = None


@dataclass(frozen=True, slots=True)
class BorrowedWorkspaceTerminalRuntime:
    """HostCore path: the runtime borrows a workspace-shared manager via a lease.

    ``RuntimeSession.close()`` must NOT kill/detach/shutdown the shared manager;
    lease release (kill owner + prune owner sessions) is the supervisor/HostCore
    job and happens exactly once (contract §5).
    """

    owner: TerminalOwnerContext
    manager: TerminalSessionManager


TerminalRuntimeBinding = OwnedTerminalRuntime | BorrowedWorkspaceTerminalRuntime


@dataclass(frozen=True, slots=True)
class WorkspaceTerminalLease:
    """Unique, generation-stamped handle a supervisor hands to one borrower.

    Defined in the runtime layer (alongside the binding) only so the host
    supervisor and the runtime binding can share the type without a host->runtime
    import. The supervisor owns issuance and release; HostCore builds a
    ``BorrowedWorkspaceTerminalRuntime`` from ``owner`` + ``manager`` (contract §4).
    """

    workspace_key: str
    owner: TerminalOwnerContext
    generation: int
    manager: TerminalSessionManager
