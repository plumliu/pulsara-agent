"""Host-scoped, process-local terminal execution.

This package intentionally owns only process-local execution and bounded join.
A process handle is valid only for the Host owner that created it and is
destroyed during owner close.
"""

from pulsara_agent.terminal_process.manager import (
    TerminalForegroundDecisionAttemptHandle,
    TerminalForegroundDecisionState,
    TerminalManager,
)
from pulsara_agent.terminal_process.models import (
    TerminalCwdScope,
    TerminalPhysicalState,
    TerminalProcessInfo,
    TerminalProcessLog,
    TerminalProcessOrigin,
    TerminalRequest,
    TerminalResult,
    TerminalStatus,
)

__all__ = [
    "TerminalCwdScope",
    "TerminalProcessInfo",
    "TerminalForegroundDecisionAttemptHandle",
    "TerminalForegroundDecisionState",
    "TerminalPhysicalState",
    "TerminalProcessLog",
    "TerminalProcessOrigin",
    "TerminalRequest",
    "TerminalResult",
    "TerminalManager",
    "TerminalStatus",
]
