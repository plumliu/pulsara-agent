"""POSIX foreground-process-group ownership for the Go terminal child."""

from __future__ import annotations

import os
import signal
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ForegroundTerminalLease:
    fd: int
    previous_process_group: int
    child_process_group: int


def transfer_foreground_to_child(child_pid: int) -> ForegroundTerminalLease | None:
    """Give a session-leading child the controlling terminal when one exists."""

    fd = 0
    if not os.isatty(fd):
        return None
    previous = os.tcgetpgrp(fd)
    old_handler = signal.signal(signal.SIGTTOU, signal.SIG_IGN)
    try:
        os.tcsetpgrp(fd, child_pid)
    finally:
        signal.signal(signal.SIGTTOU, old_handler)
    return ForegroundTerminalLease(
        fd=fd,
        previous_process_group=previous,
        child_process_group=child_pid,
    )


def restore_foreground(lease: ForegroundTerminalLease | None) -> None:
    if lease is None or not os.isatty(lease.fd):
        return
    old_handler = signal.signal(signal.SIGTTOU, signal.SIG_IGN)
    try:
        os.tcsetpgrp(lease.fd, lease.previous_process_group)
    except OSError:
        # The outer shell may already have reclaimed the terminal.
        pass
    finally:
        signal.signal(signal.SIGTTOU, old_handler)


__all__ = [
    "ForegroundTerminalLease",
    "restore_foreground",
    "transfer_foreground_to_child",
]
