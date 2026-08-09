"""Python ownership boundary for the Protocol v3 Go terminal client.

Legacy launcher types remain explicit lazy compatibility exports.  Importing
this production facade must not initialize the Protocol v2 presentation graph.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any


_EXPORTS = {
    "TerminalClientBinary": (
        "pulsara_agent.terminal_client.binary",
        "TerminalClientBinary",
    ),
    "TerminalClientBinaryError": (
        "pulsara_agent.terminal_client.binary",
        "TerminalClientBinaryError",
    ),
    "resolve_terminal_client_binary": (
        "pulsara_agent.terminal_client.binary",
        "resolve_terminal_client_binary",
    ),
    "TerminalClientExit": (
        "pulsara_agent.terminal_client.launcher",
        "TerminalClientExit",
    ),
    "TerminalClientLaunchError": (
        "pulsara_agent.terminal_client.launcher",
        "TerminalClientLaunchError",
    ),
    "launch_terminal_client": (
        "pulsara_agent.terminal_client.v3_launcher",
        "launch_terminal_kernel_client",
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
    "TerminalClientBinary",
    "TerminalClientBinaryError",
    "TerminalClientExit",
    "TerminalClientLaunchError",
    "launch_terminal_client",
    "resolve_terminal_client_binary",
]
