"""Versioned renderer-neutral local terminal protocol.

Protocol v3 is the package-default production surface.  The legacy v2 server
remains importable only from ``pulsara_agent.terminal_protocol.gateway`` until
its Stage 3 physical deletion.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any


def __getattr__(name: str) -> Any:
    if name not in {"TerminalProtocolServer", "TerminalKernelProtocolServer"}:
        raise AttributeError(name)
    value = getattr(
        import_module("pulsara_agent.terminal_protocol.v3_gateway"),
        "TerminalKernelProtocolServer",
    )
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


__all__ = ["TerminalKernelProtocolServer", "TerminalProtocolServer"]
