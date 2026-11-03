"""A terminal observation owns complete selected text and one tail view."""

from __future__ import annotations

import os
import sys
from time import monotonic

import pytest

from pulsara_agent.identity import derive_id
from pulsara_agent.terminal_process.manager import ProcessRegistry
from pulsara_agent.terminal_process.output import (
    TerminalOutputOwner,
    TerminalOutputReadDisposition,
)


OWNER = derive_id("host", "terminal-public-output-tests", "owner")
PROCESS = derive_id("process", "terminal-public-output-tests", "process")


def test_small_omitted_body_is_complete_frozen_text_not_the_tail() -> None:
    owner = TerminalOutputOwner(owner_epoch=OWNER, process_id=PROCESS)
    text = "开始🙂 " + "middle " * 700 + "END\n"
    owner.append_raw(text.encode())

    preview, body = owner.snapshot_with_body_candidate(maximum_chars=512)
    owner.append_raw(b"later\n")

    assert len(text.encode()) < 8_000
    assert preview.text == text[-512:]
    assert preview.truncated_by_response_bound is True
    assert body.text == text
    assert body.source_format.value == "TEXT"
    assert body.source_coverage.value == "COMPLETE"
    assert owner.snapshot(
        maximum_chars=512, since_cursor=preview.output_cursor
    ).text == "later\n"


def test_capture_cursor_does_not_copy_retained_output(monkeypatch) -> None:
    owner = TerminalOutputOwner(owner_epoch=OWNER, process_id=PROCESS)
    owner.append_raw(b"old\n")

    def reject_output_read(*_args, **_kwargs):
        raise AssertionError("capturing a position must not read retained output")

    with monkeypatch.context() as patch:
        patch.setattr(owner, "_range_locked", reject_output_read)
        cursor = owner.capture_cursor()
    owner.append_raw("新🙂\n".encode())
    selected, body = owner.snapshot_with_body_candidate(
        maximum_chars=512, since_cursor=cursor
    )
    assert selected.disposition is TerminalOutputReadDisposition.EXACT_DELTA
    assert selected.text == body.text == "新🙂\n"


def test_exact_delta_body_stays_complete_after_earlier_output_was_evicted() -> None:
    owner = TerminalOutputOwner(
        owner_epoch=OWNER, process_id=PROCESS, maximum_bytes=100
    )
    owner.append_raw(b"old " * 50)
    cursor = owner.capture_cursor()
    owner.append_raw(b"current\n")
    selected, body = owner.snapshot_with_body_candidate(
        maximum_chars=512, since_cursor=cursor
    )

    assert selected.disposition is TerminalOutputReadDisposition.EXACT_DELTA
    assert selected.gap_before_output is False
    assert body.text == "current\n"
    assert body.source_coverage.value == "COMPLETE"
    assert body.source_coverage_reason is None


def _launch_interactive(registry: ProcessRegistry, path):
    registry.activate_owner(OWNER)
    attempt = derive_id("terminal-decision", "terminal-public-output-tests", str(monotonic()))
    state, _ = registry.exec_with_yield(
        command="interactive output fixture",
        cwd=path,
        yield_time_ms=0,
        tty=False,
        max_lifetime_seconds=None,
        owner_host_session_id=OWNER,
        shell_argv=(sys.executable, "-u", "-c", (
            "import sys; print('READY',flush=True); "
            "line=sys.stdin.readline(); print('REPLY:'+line,end='',flush=True)"
        )),
        env=dict(os.environ),
        decision_attempt_id=attempt,
        decision_deadline_monotonic=monotonic() + 5,
    )
    registry.settle_foreground_decision(attempt)
    state.output.wait_for_revision(0, 2)
    assert registry.poll(
        state.process_id, max_output_chars=512, owner_host_session_id=OWNER
    ).output == "READY\n"
    return state


def test_submit_returns_only_output_after_input_cut(tmp_path) -> None:
    registry = ProcessRegistry()
    try:
        state = _launch_interactive(registry, tmp_path)
        result = registry.write(
            state.process_id,
            "héllo",
            append_newline=True,
            yield_time_ms=2_000,
            max_output_chars=512,
            owner_host_session_id=OWNER,
        )
        assert result.status.value == "success"
        assert result.exit_code == 0
        assert result.output == "REPLY:héllo\n"
        assert result.output_body_candidate.text == result.output
        assert result.output_disposition is TerminalOutputReadDisposition.EXACT_DELTA
        assert registry.poll(
            state.process_id,
            since_cursor=result.output_cursor,
            max_output_chars=512,
            owner_host_session_id=OWNER,
        ).output == ""
    finally:
        registry.release_owner(OWNER, timeout_seconds=5)


@pytest.mark.parametrize("action", ("close_stdin", "kill"))
def test_control_does_not_select_or_freeze_logs(tmp_path, monkeypatch, action) -> None:
    registry = ProcessRegistry()
    try:
        state = _launch_interactive(registry, tmp_path)

        def reject_body(*_args, **_kwargs):
            raise AssertionError("control must not freeze an output observation")

        with monkeypatch.context() as patch:
            patch.setattr(state.output, "snapshot_with_body_candidate", reject_body)
            result = getattr(registry, action)(
                state.process_id, owner_host_session_id=OWNER
            )
        assert result.process_id == state.process_id
        assert result.output == ""
        assert result.output_body_candidate is None
        assert result.output_cursor is None
        assert result.retained_from_cursor is None
        later = registry.wait(
            state.process_id, timeout_seconds=2, max_output_chars=512,
            owner_host_session_id=OWNER,
        )
        assert later.status.value == ("killed" if action == "kill" else "success")
        assert "READY\n" in later.output_body_candidate.text
    finally:
        registry.release_owner(OWNER, timeout_seconds=5)
