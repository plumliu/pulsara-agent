"""A revision observer must not outrun its existing Host retention policy."""

import os
import sys
from threading import Event, current_thread
from time import monotonic

from pulsara_agent.identity import derive_id
from pulsara_agent.terminal_process.manager import ProcessRegistry


def test_revision_observation_settles_retention_when_append_callback_is_pending(
    tmp_path,
):
    entered, release = Event(), Event()

    class Registry(ProcessRegistry):
        armed = False

        def _retained_bytes_changed(self, output):
            if self.armed and current_thread().name.startswith("terminal-reader:"):
                entered.set()
                assert release.wait(5)
            return super()._retained_bytes_changed(output)

    registry = Registry(maximum_host_retained_bytes=160)
    owner = derive_id("host", "retention-observation", "owner")
    registry.activate_owner(owner)

    def launch(script, *, yield_time_ms):
        return registry.exec_with_yield(
            command="retention observer fixture",
            cwd=tmp_path,
            yield_time_ms=yield_time_ms,
            tty=False,
            max_lifetime_seconds=None,
            owner_host_session_id=owner,
            shell_argv=(sys.executable, "-c", script),
            env=dict(os.environ),
            decision_deadline_monotonic=monotonic() + 5,
        )[0]

    try:
        finished = launch("print('F' * 120, flush=True)", yield_time_ms=5000)
        assert finished.physical_completion.is_set()
        registry.armed = True
        live = launch(
            "import time;print('L' * 120, flush=True);time.sleep(30)", yield_time_ms=0
        )
        assert entered.wait(5)
        assert (
            finished.output.retained_utf8_bytes + live.output.retained_utf8_bytes == 242
        )
        live.output.wait_for_revision(0, 2)
        assert finished.output.retained_utf8_bytes < live.output.retained_utf8_bytes
        assert (
            finished.output.retained_utf8_bytes + live.output.retained_utf8_bytes <= 160
        )
    finally:
        release.set()
        registry.release_owner(owner, timeout_seconds=5)
