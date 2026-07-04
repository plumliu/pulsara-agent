"""Interactive input support for the HostCore REPL."""

from __future__ import annotations

import asyncio
import errno
import sys
import termios
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TextIO

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.history import FileHistory, History, InMemoryHistory


class ReplPrompt(Protocol):
    async def read_line(self, message: str) -> str: ...


@dataclass(slots=True)
class BasicReplPrompt:
    """Fallback for redirected stdin and tests.

    Keeping this path synchronous is intentional: redirected input is already
    available and should retain ordinary ``input``/EOF semantics.
    """

    async def read_line(self, message: str) -> str:
        return input(message)


@dataclass(slots=True)
class InteractiveReplPrompt:
    session: PromptSession[str]

    async def read_line(self, message: str) -> str:
        while True:
            try:
                return await self.session.prompt_async(message, handle_sigint=True)
            except termios.error as exc:
                if not exc.args or exc.args[0] != errno.EINTR:
                    raise
                # tcsetattr() may be interrupted by SIGCONT when a suspended
                # REPL is brought back with `fg`. Once foreground ownership is
                # restored, retrying initializes prompt_toolkit cleanly.
                await asyncio.sleep(0)


def build_repl_prompt(*, history_path: Path, stdin: TextIO | None = None) -> ReplPrompt:
    stream = stdin or sys.stdin
    if not stream.isatty():
        return BasicReplPrompt()

    history = _history(history_path)
    return InteractiveReplPrompt(
        PromptSession(
            history=history,
            auto_suggest=AutoSuggestFromHistory(),
            enable_history_search=True,
            enable_suspend=True,
        )
    )


def _history(path: Path) -> History:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        # A read-only home directory should not make the REPL unusable.
        return InMemoryHistory()
    return FileHistory(str(path))
