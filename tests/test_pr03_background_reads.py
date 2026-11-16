from __future__ import annotations

import os
from pathlib import Path
import sys
from time import monotonic

import pytest

from pulsara_agent.terminal_process.manager import ProcessRegistry
from pulsara_agent.terminal_protocol.v3_gateway import (
    _decode_background_cursor,
    _encode_background_cursor,
)


def _launch(
    registry: ProcessRegistry,
    tmp_path: Path,
    *,
    owner: str,
    program: str,
    yield_time_ms: int,
):
    return registry.exec_with_yield(
        command=program,
        cwd=tmp_path,
        yield_time_ms=yield_time_ms,
        tty=False,
        max_lifetime_seconds=None,
        owner_host_session_id=owner,
        shell_argv=(sys.executable, "-c", program),
        decision_deadline_monotonic=monotonic() + 5,
        env=dict(os.environ),
    )


def test_pr03_background_inventory_uses_adoption_and_spooled_log_cursor(
    tmp_path: Path,
) -> None:
    registry = ProcessRegistry(maximum_host_retained_bytes=64)
    owner = 'host:pyugolwqgrjoyrgehpbxnbwvku'
    registry.activate_owner(owner)
    try:
        foreground, foreground_yielded = _launch(
            registry,
            tmp_path,
            owner=owner,
            program="raise SystemExit(0)",
            yield_time_ms=2_000,
        )
        background, background_yielded = _launch(
            registry,
            tmp_path,
            owner=owner,
            program="import time; time.sleep(30)",
            yield_time_ms=0,
        )
        assert foreground_yielded is False
        assert background_yielded is True
        assert [
            item.process_id
            for item in registry.list_background_processes(
                owner_host_session_id=owner
            )
        ] == [background.process_id]
        assert foreground.process_id != background.process_id

        first = registry.log(
            background.process_id,
            max_output_chars=512,
            owner_host_session_id=owner,
        )
        observed = background.output.observation_cursor()
        text = "真实后台输出🙂\n" * 30
        background.output.append_raw(text.encode())
        # The 64-byte Host threshold spills memory to disk; unobserved logs
        # remain complete, and background reads do not acknowledge delivery.
        assert sum(item.output.retained_utf8_bytes for item in (foreground, background)) <= 64
        incremental = registry.log(
            background.process_id,
            max_output_chars=512,
            owner_host_session_id=owner,
            since_cursor=first.output_cursor,
        )
        assert incremental.gap_before_output is False
        assert incremental.retained_from_cursor == first.retained_from_cursor
        assert incremental.output == text
        assert background.output.observation_cursor() == observed
        repeated = registry.log(
            background.process_id,
            max_output_chars=512,
            owner_host_session_id=owner,
            since_cursor=first.output_cursor,
        )
        assert repeated.output == text
        assert repeated.output_cursor == incremental.output_cursor
        assert background.output.inflight_read_count == 0
        registry.terminate_if_running(
            background.process_id,
            owner_host_session_id=owner,
        )
        assert [
            item.process_id
            for item in registry.list_background_processes(
                owner_host_session_id=owner
            )
        ] == [background.process_id]
    finally:
        registry.release_owner(owner, timeout_seconds=2)


def test_pr03_background_cursor_is_bound_to_exact_session_and_host() -> None:
    cursor = _encode_background_cursor(
        session_id='session:5dyf7lfzp7z22pphjtostkgsjm',
        host_session_id='host:gubylb5v44ganghpuxoqfvlrji',
        started_at=12.5,
        process_id='process:34npgaljkboupr4n456vrmou6a',
    )
    assert _decode_background_cursor(
        cursor, session_id='session:5dyf7lfzp7z22pphjtostkgsjm', host_session_id='host:gubylb5v44ganghpuxoqfvlrji'
    ) == (12.5, 'process:34npgaljkboupr4n456vrmou6a')
    with pytest.raises(ValueError, match="background cursor"):
        _decode_background_cursor(
            cursor, session_id='session:5dyf7lfzp7z22pphjtostkgsjm', host_session_id='host:eemam3abmyadtcyqhpde3rik7y'
        )
    with pytest.raises(ValueError, match="background cursor"):
        _decode_background_cursor(
            cursor, session_id='session:f2uxxwrmmavsqgv655dvvzytnu', host_session_id='host:gubylb5v44ganghpuxoqfvlrji'
        )
