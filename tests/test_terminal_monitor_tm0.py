from __future__ import annotations

import errno
import threading
import time

import pytest

from pulsara_agent.primitives import context_fingerprint
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.terminal_observation import (
    TerminalOutputSpoolPolicyFact,
    TerminalProcessLifecycleOutcomeFact,
    UnavailableRecoveredTerminalOutputDeltaFact,
    build_terminal_lifecycle_outcome,
)
from pulsara_agent.runtime.terminal.output import (
    SanitizedOutputJournal,
    _SpoolAuthorityConflict,
    recover_terminal_output_delta,
)


def _spool_policy(
    *,
    maximum_bytes: int = 4096,
    page_bytes: int = 64,
    pending_bytes: int = 256,
) -> TerminalOutputSpoolPolicyFact:
    return build_frozen_fact(
        TerminalOutputSpoolPolicyFact,
        schema_version="terminal_output_spool_policy.v1",
        maximum_spool_utf8_bytes=maximum_bytes,
        page_utf8_bytes=page_bytes,
        maximum_pending_spool_utf8_bytes=pending_bytes,
        page_fsync_timeout_ms=50,
        terminal_drain_timeout_ms=500,
        overflow_policy="evict_oldest_complete_page_with_gap",
        file_permission_mode="0600",
        page_commit_contract_fingerprint=context_fingerprint(
            "tm0-test-page-commit:v1", "atomic"
        ),
        retention_horizon_policy_fingerprint=context_fingerprint(
            "tm0-test-retention:v1", "bounded"
        ),
    )


def test_tm0_chunk_ansi_and_secret_boundaries_match_one_shot() -> None:
    chunked = SanitizedOutputJournal(partial_line_quiet_seconds=5)
    one_shot = SanitizedOutputJournal(partial_line_quiet_seconds=5)
    payload = b"\x1b[31mhello\x1b[0m API_KEY=secret Bearer token-value\n"
    previous = 0
    for boundary in (1, 2, 5, 8, 13, 21, len(payload)):
        chunked.append(payload[previous:boundary])
        previous = boundary
    one_shot.append(payload)
    chunked.finish()
    one_shot.finish()

    assert chunked.full_text() == one_shot.full_text()
    assert chunked.full_text() == "hello API_KEY=[REDACTED] [REDACTED]\n"


def test_tm0_partial_line_is_observable_after_quiet_bound() -> None:
    journal = SanitizedOutputJournal(partial_line_quiet_seconds=0.02)
    revision = journal.revision
    journal.append(b"progress=73%")

    journal.wait_for_revision(revision, 0.2)
    time.sleep(0.04)

    assert journal.snapshot(max_chars=100).text == "progress=73%"


def test_tm0_retention_gap_and_typed_unavailable_recovery() -> None:
    journal = SanitizedOutputJournal(
        max_retained_segments=2,
        max_retained_chars=8,
        max_retained_utf8_bytes=8,
        max_segment_utf8_bytes=4,
        spool_policy=_spool_policy(maximum_bytes=8, page_bytes=4, pending_bytes=32),
    )
    initial = journal.initial_cursor
    journal.append(b"aaaa")
    journal.seal_for_observation()
    journal.append(b"bbbb")
    journal.seal_for_observation()
    journal.append(b"cccc")
    journal.finish()

    delta, _ = journal.snapshot_since(initial, max_chars=100, seal=False)
    assert delta.available_start_cursor.sanitized_char_offset > 0
    assert delta.truncated is True
    recovered = recover_terminal_output_delta(
        recovery_reference=journal.recovery_reference(),
        requested_start_cursor=initial,
        terminal_cursor=journal.end_cursor,
        max_chars=100,
    )
    assert isinstance(recovered, UnavailableRecoveredTerminalOutputDeltaFact)
    assert recovered.recovery_reason == "spool_range_evicted"
    assert journal.spool_retained_bytes <= 8


def test_tm0_restart_rehydrates_exact_retained_range() -> None:
    journal = SanitizedOutputJournal(
        spool_policy=_spool_policy(),
        max_segment_utf8_bytes=64,
    )
    initial = journal.initial_cursor
    journal.append(b"first\nsecond\n")
    journal.finish()

    recovered = recover_terminal_output_delta(
        recovery_reference=journal.recovery_reference(),
        requested_start_cursor=initial,
        terminal_cursor=journal.end_cursor,
        max_chars=100,
    )

    assert not isinstance(recovered, UnavailableRecoveredTerminalOutputDeltaFact)
    assert recovered.output_preview == "first\nsecond\n"
    assert recovered.truncated is False


def test_tm0_one_hundred_thousand_segments_keep_memory_bound() -> None:
    def fail_first(stage: str) -> None:
        if stage == "before_write":
            raise OSError(errno.ENOSPC, "full")

    journal = SanitizedOutputJournal(
        max_retained_segments=8,
        max_retained_chars=32,
        max_retained_utf8_bytes=32,
        max_segment_utf8_bytes=4,
        spool_policy=_spool_policy(),
        spool_fault_injector=fail_first,
    )
    for _ in range(100_000):
        journal.append(b"x\n")
    journal.finish()

    assert journal.retained_segment_count <= 8
    assert journal.retained_utf8_bytes <= 32


@pytest.mark.parametrize(
    ("failure", "expected_reason"),
    [
        (OSError(errno.ENOSPC, "full"), "write_enospc"),
        (PermissionError("denied"), "write_permission_denied"),
        (TimeoutError("slow fsync"), "fsync_timeout"),
    ],
)
def test_tm0_spool_faults_create_typed_gap_without_cursor_rollback(
    failure: BaseException,
    expected_reason: str,
) -> None:
    def inject(stage: str) -> None:
        if stage == (
            "before_fsync" if isinstance(failure, TimeoutError) else "before_write"
        ):
            raise failure

    journal = SanitizedOutputJournal(
        spool_policy=_spool_policy(),
        spool_fault_injector=inject,
    )
    journal.append(b"payload\n")
    journal.finish()
    state = journal.spool_writer_state()

    assert state.writer_state == "degraded"
    assert state.latest_gap is not None
    assert state.latest_gap.reason == expected_reason
    assert state.journal_end_cursor.sanitized_char_offset > 0
    assert state.successfully_spooled_cursor == journal.initial_cursor


def test_tm0_spool_queue_overflow_never_blocks_journal_reader() -> None:
    entered = threading.Event()
    release = threading.Event()

    def block_writer(stage: str) -> None:
        if stage == "before_write":
            entered.set()
            release.wait(1)

    journal = SanitizedOutputJournal(
        max_segment_utf8_bytes=8,
        spool_policy=_spool_policy(
            maximum_bytes=1024,
            page_bytes=8,
            pending_bytes=8,
        ),
        spool_fault_injector=block_writer,
    )
    journal.append(b"12345678")
    assert entered.wait(0.5)
    started = time.monotonic()
    journal.append(b"abcdefgh")
    elapsed = time.monotonic() - started
    release.set()
    journal.finish()

    state = journal.spool_writer_state()
    assert elapsed < 0.1
    assert state.latest_gap is not None
    assert state.latest_gap.reason == "writer_queue_overflow"
    assert state.journal_end_cursor.sanitized_char_offset == 16


def test_tm0_spool_hash_conflict_is_authority_untrusted() -> None:
    def inject(stage: str) -> None:
        if stage == "after_write_before_verify":
            raise _SpoolAuthorityConflict("conflict")

    journal = SanitizedOutputJournal(
        spool_policy=_spool_policy(),
        spool_fault_injector=inject,
    )
    journal.append(b"done\n")
    journal.finish()

    state = journal.spool_writer_state()
    assert state.writer_state == "authority_untrusted"
    assert state.journal_end_cursor.sanitized_char_offset == 5


@pytest.mark.parametrize(
    ("status", "exit_code", "kill_reason"),
    [
        ("success", 0, None),
        ("error", 1, None),
        ("timeout", 124, None),
        ("killed", -15, "user_tool_kill"),
        ("killed", -15, "teardown"),
        ("killed", -15, "lifetime_watchdog"),
    ],
)
def test_tm0_lifecycle_outcome_six_row_matrix(
    status: str,
    exit_code: int,
    kill_reason: str | None,
) -> None:
    outcome = build_terminal_lifecycle_outcome(
        status=status,
        exit_code=exit_code,
        kill_reason=kill_reason,
    )
    assert isinstance(outcome, TerminalProcessLifecycleOutcomeFact)
    assert "timed_out" not in type(outcome).model_fields


def test_tm0_invalid_lifecycle_outcome_is_rejected() -> None:
    with pytest.raises(ValueError, match="matrix"):
        build_terminal_lifecycle_outcome(
            status="success",
            exit_code=1,
            kill_reason=None,
        )
