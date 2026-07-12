import asyncio
import errno
import threading
import termios
from pathlib import Path

from prompt_toolkit.history import FileHistory, InMemoryHistory

from pulsara_agent import repl


class _Stream:
    def __init__(self, *, tty: bool) -> None:
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


def test_redirected_repl_uses_basic_prompt(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("builtins.input", lambda message: f"{message}hello")
    prompt = repl.build_repl_prompt(
        history_path=tmp_path / "history",
        stdin=_Stream(tty=False),  # type: ignore[arg-type]
    )

    assert isinstance(prompt, repl.BasicReplPrompt)
    assert asyncio.run(prompt.read_line("pulsara> ")) == "pulsara> hello"


def test_redirected_repl_input_does_not_block_background_event_loop(
    monkeypatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    def blocking_input(_message: str) -> str:
        entered.set()
        release.wait(timeout=1)
        return "done"

    monkeypatch.setattr("builtins.input", blocking_input)

    async def run() -> None:
        prompt = repl.BasicReplPrompt()
        reading = asyncio.create_task(prompt.read_line("pulsara> "))
        await asyncio.wait_for(asyncio.to_thread(entered.wait), timeout=0.2)
        background_progressed = asyncio.Event()
        asyncio.get_running_loop().call_soon(background_progressed.set)
        await asyncio.wait_for(background_progressed.wait(), timeout=0.2)
        assert not reading.done()
        release.set()
        assert await reading == "done"

    asyncio.run(run())


def test_interactive_repl_enables_async_history_and_suspend(tmp_path: Path) -> None:
    history_path = tmp_path / "nested" / "history"
    prompt = repl.build_repl_prompt(
        history_path=history_path,
        stdin=_Stream(tty=True),  # type: ignore[arg-type]
    )

    assert isinstance(prompt, repl.InteractiveReplPrompt)
    assert isinstance(prompt.session.history, FileHistory)
    assert history_path.parent.is_dir()
    assert prompt.session.enable_history_search is True
    assert prompt.session.enable_suspend is True


def test_repl_history_falls_back_when_home_is_read_only(monkeypatch, tmp_path: Path) -> None:
    def _deny(*_args, **_kwargs):
        raise PermissionError("read only")

    monkeypatch.setattr(Path, "mkdir", _deny)

    assert isinstance(repl._history(tmp_path / "history"), InMemoryHistory)


def test_interactive_repl_retries_termios_setup_interrupted_by_sigcont() -> None:
    class InterruptedSession:
        def __init__(self) -> None:
            self.calls = 0

        async def prompt_async(self, _message: str, *, handle_sigint: bool) -> str:
            assert handle_sigint is True
            self.calls += 1
            if self.calls == 1:
                raise termios.error(errno.EINTR, "Interrupted system call")
            return "resumed"

    session = InterruptedSession()
    prompt = repl.InteractiveReplPrompt(session)  # type: ignore[arg-type]

    assert asyncio.run(prompt.read_line("pulsara> ")) == "resumed"
    assert session.calls == 2
