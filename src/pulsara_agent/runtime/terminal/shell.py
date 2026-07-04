"""Shell detection and argv construction for terminal runtime."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


_LOGIN_FLAG_SHELLS = {"bash", "zsh", "ksh"}
_FALLBACK_SHELLS = ("/bin/zsh", "/bin/bash", "/bin/sh")


@dataclass(frozen=True, slots=True)
class TerminalShellConfig:
    path: Path
    login: bool = False
    interactive_init: bool = False

    @property
    def name(self) -> str:
        return self.path.name

    def argv(self, command: str) -> list[str]:
        args = [str(self.path)]
        if self.login and self.name in _LOGIN_FLAG_SHELLS:
            args.append("-l")
        if self.interactive_init:
            args.append("-i")
        args.extend(["-c", command])
        return args

    def to_metadata(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "name": self.name,
            "login": self.login,
            "interactive_init": self.interactive_init,
        }


def detect_terminal_shell(
    env: Mapping[str, str] | None = None,
    *,
    login: bool = False,
    interactive_init: bool = False,
) -> TerminalShellConfig:
    env = env or os.environ
    candidates: list[str] = []
    configured = env.get("SHELL")
    if configured:
        candidates.append(configured)
    candidates.extend(_FALLBACK_SHELLS)
    for raw in candidates:
        path = Path(raw).expanduser()
        if _is_executable_file(path):
            return TerminalShellConfig(
                path=path.resolve(),
                login=login,
                interactive_init=interactive_init,
            )
    return TerminalShellConfig(
        path=Path("/bin/sh"),
        login=False,
        interactive_init=interactive_init,
    )


def _is_executable_file(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except OSError:
        return False
