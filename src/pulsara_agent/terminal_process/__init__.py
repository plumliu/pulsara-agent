"""Host-scoped, process-local terminal execution.

This package intentionally owns only process-local execution and bounded join.
A process handle is valid only for the Host owner that created it and is
destroyed during owner close.
"""

from pulsara_agent.terminal_process.manager import TerminalSessionManager
from pulsara_agent.terminal_process.models import (
    TerminalProcessInfo,
    TerminalProcessLog,
    TerminalRequest,
    TerminalResult,
    TerminalStatus,
)

__all__ = [
    "TerminalProcessInfo",
    "TerminalProcessLog",
    "TerminalRequest",
    "TerminalResult",
    "TerminalSessionManager",
    "TerminalStatus",
]
