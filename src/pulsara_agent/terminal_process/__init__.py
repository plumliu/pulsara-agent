"""Host-scoped, process-local terminal execution.

This package intentionally owns only process-local execution and bounded join.
A process handle is valid only for the Host owner that created it and is
destroyed during owner close.
"""

from pulsara_agent.terminal_process.manager import (
    TerminalForegroundDecisionAttemptHandle,
    TerminalForegroundDecisionState,
    TerminalSessionManager,
)
from pulsara_agent.terminal_process.models import (
    TerminalPhysicalState,
    TerminalProcessInfo,
    TerminalProcessLog,
    TerminalProcessOrigin,
    TerminalRequest,
    TerminalResult,
    TerminalStatus,
)

__all__ = [
    "TerminalProcessInfo",
    "TerminalForegroundDecisionAttemptHandle",
    "TerminalForegroundDecisionState",
    "TerminalPhysicalState",
    "TerminalProcessLog",
    "TerminalProcessOrigin",
    "TerminalRequest",
    "TerminalResult",
    "TerminalSessionManager",
    "TerminalStatus",
]
