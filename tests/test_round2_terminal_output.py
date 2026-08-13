from __future__ import annotations

import asyncio
import os
from pathlib import Path
from hashlib import sha256
import sys
from threading import Event, Thread
from time import monotonic, sleep

import pytest

from pulsara_agent.conversation_kernel.live import (
    LiveAgentEventBus,
    LiveChannelKind,
    LiveObservationKind,
)
from pulsara_agent.conversation_kernel.runner import _ToolResultLiveSink
from pulsara_agent.ports.tool_execution import ToolOutputSourceCoverageReason
from pulsara_agent.terminal_process.manager import ProcessRegistry
from pulsara_agent.terminal_process.manager import TerminalSessionManager
import pulsara_agent.terminal_process.manager as terminal_manager_module
from pulsara_agent.terminal_process.models import TerminalPhysicalState
from pulsara_agent.terminal_process.output import (
    IncrementalTerminalSanitizer,
    TerminalOutputOwner,
    TerminalOutputReadDisposition,
    TerminalOutputSourceCoverage,
)


def _streamed(raw: bytes, boundaries: tuple[int, ...]) -> bytes:
    sanitizer = IncrementalTerminalSanitizer()
    output = bytearray()
    start = 0
    for end in boundaries:
        output.extend(sanitizer.feed(raw[start:end]))
        start = end
    output.extend(sanitizer.feed(raw[start:]))
    output.extend(sanitizer.finalize())
    return bytes(output)


def test_round2_incremental_sanitizer_is_chunking_invariant_and_private() -> None:
    raw = (
        "开始\r\n"
        "\x1b[31mred\x1b[0m "
        "API_TOKEN=top-secret "
        "Authorization Bearer bearer-value "
        "结束🙂\n"
    ).encode()
    expected = IncrementalTerminalSanitizer.one_shot(raw)
    for step in (1, 2, 3, 7, 31):
        boundaries = tuple(range(step, len(raw), step))
        assert _streamed(raw, boundaries) == expected
    assert b"top-secret" not in expected
    assert b"bearer-value" not in expected
    assert b"\x1b" not in expected
    assert (
        "开始\nred API_TOKEN=<redacted> Authorization Bearer <redacted> 结束🙂\n"
        == expected.decode()
    )


def test_round2_sanitizer_bounds_undecided_secret_and_escape_payloads() -> None:
    secret = b"API_TOKEN=" + b"s" * 10_000 + b" done "
    escape = b"prefix \x1b]52;c;" + b"clipboard-secret" * 500 + b" suffix"
    sanitizer = IncrementalTerminalSanitizer()
    assert sanitizer.feed(secret[:100]) == b""
    assert sanitizer.quiet_boundary() == b""
    public = sanitizer.feed(secret[100:]) + sanitizer.finalize()
    assert b"s" * 64 not in public
    assert b"done" in public

    escaped = IncrementalTerminalSanitizer.one_shot(escape)
    assert b"clipboard-secret" not in escaped
    assert b"TERMINAL_ESCAPE_SEQUENCE_SUPPRESSED" in escaped


def test_round2_output_cursor_exact_delta_invalid_and_delivery_head_tail() -> None:
    owner = TerminalOutputOwner(owner_epoch="host:1", process_id="process:1")
    owner.append_raw("one 🙂 ".encode())
    first = owner.snapshot(maximum_chars=512)
    owner.append_raw("two 三 ".encode())
    delta = owner.snapshot(maximum_chars=512, since_cursor=first.output_cursor)
    assert delta.disposition is TerminalOutputReadDisposition.EXACT_DELTA
    assert delta.text == "two 三 "
    assert delta.output_cursor != first.output_cursor

    other = TerminalOutputOwner(owner_epoch="host:2", process_id="process:1")
    foreign = other.snapshot(maximum_chars=512).output_cursor
    with pytest.raises(ValueError, match="INVALID_CURSOR"):
        owner.snapshot(maximum_chars=512, since_cursor=foreign)
    with pytest.raises(ValueError, match="INVALID_CURSOR"):
        owner.snapshot(maximum_chars=512, since_cursor="not-a-cursor")

    start = owner.snapshot(maximum_chars=512).output_cursor
    body = "HEAD-" + "🙂" * 1_000 + "-TAIL "
    owner.append_raw(body.encode())
    bounded = owner.observation_slice(
        maximum_chars=512,
        maximum_bytes=1_500,
        since_cursor=start,
    )
    assert bounded.disposition is TerminalOutputReadDisposition.EXACT_DELTA
    assert bounded.text.startswith("HEAD-")
    assert bounded.text.endswith("-TAIL ")
    assert "omitted by delivery bound" in bounded.text
    assert bounded.omitted_by_delivery_bound_utf8_bytes > 0
    assert (
        bounded.included_source_utf8_bytes
        + bounded.omitted_by_delivery_bound_utf8_bytes
        == bounded.available_source_utf8_bytes
        == len(body.encode())
    )
    assert bounded.source_digest == f"sha256:{sha256(body.encode()).hexdigest()}"


def test_round2_cursor_snapshot_and_artifact_share_the_exact_selected_range() -> None:
    owner = TerminalOutputOwner(owner_epoch="host:cursor", process_id="process:cursor")
    owner.append_raw(b"old-output ")
    cursor = owner.snapshot(maximum_chars=512).output_cursor
    owner.append_raw("new-output 🙂\n".encode())

    snapshot, candidate = owner.snapshot_with_artifact_candidate(
        maximum_chars=512,
        since_cursor=cursor,
    )

    assert snapshot.disposition is TerminalOutputReadDisposition.EXACT_DELTA
    assert snapshot.text == "new-output 🙂\n"
    assert candidate.text == "new-output 🙂\n"
    assert "old-output" not in candidate.text
    assert candidate.source_coverage.value == "COMPLETE"


def test_round2_sanitizer_failure_cannot_claim_complete_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = TerminalOutputOwner(
        owner_epoch="host:sanitizer-failure",
        process_id="process:sanitizer-failure",
    )

    def fail_feed(_raw: bytes) -> bytes:
        raise RuntimeError("injected sanitizer failure")

    monkeypatch.setattr(owner._sanitizer, "feed", fail_feed)  # noqa: SLF001
    assert owner.append_raw(b"must-not-become-public") == b""
    snapshot, candidate = owner.snapshot_with_artifact_candidate(maximum_chars=512)

    assert snapshot.disposition is TerminalOutputReadDisposition.UNAVAILABLE
    assert snapshot.source_coverage is TerminalOutputSourceCoverage.UNAVAILABLE
    assert candidate.source_coverage.value == "RETAINED_SNAPSHOT"
    assert candidate.source_coverage_reason is (
        ToolOutputSourceCoverageReason.TERMINAL_SANITIZER_UNAVAILABLE
    )
    assert candidate.original_utf8_bytes is None


def test_round2_retention_gap_is_orthogonal_to_response_and_delivery_bounds() -> None:
    owner = TerminalOutputOwner(
        owner_epoch="host:1", process_id="process:1", maximum_bytes=100
    )
    before = owner.snapshot(maximum_chars=512).output_cursor
    owner.append_raw(("A" * 200 + " TAIL ").encode())
    public = owner.snapshot(maximum_chars=8, since_cursor=before)
    assert public.disposition is TerminalOutputReadDisposition.GAP
    assert public.source_coverage is TerminalOutputSourceCoverage.RETAINED_SNAPSHOT
    assert public.gap_before_output is True
    assert public.truncated_by_response_bound is True

    observed = owner.observation_slice(
        maximum_chars=80,
        maximum_bytes=128,
        since_cursor=before,
    )
    assert observed.disposition is TerminalOutputReadDisposition.GAP
    assert observed.gap_before_output is True
    assert observed.omitted_by_delivery_bound_utf8_bytes > 0
    assert observed.available_source_utf8_bytes == owner.retained_utf8_bytes


def test_round2_output_metadata_stays_bounded_after_many_tiny_segments() -> None:
    owner = TerminalOutputOwner(
        owner_epoch="host:1", process_id="process:1", maximum_bytes=4096
    )
    for _ in range(100_000):
        owner.append_raw(b"x ")
    snapshot = owner.snapshot(maximum_chars=512)
    assert owner.retained_utf8_bytes <= 4096
    assert snapshot.through_offset == 200_000
    assert snapshot.retained_from_offset > 0
    assert len(snapshot.output_cursor) < 1024


def test_round2_host_aggregate_evicts_finished_output_before_live_output(
    tmp_path: Path,
) -> None:
    registry = ProcessRegistry(maximum_host_retained_bytes=160)
    owner = "host:aggregate"
    registry.activate_owner(owner)
    completed, yielded, _cwd = registry.exec_with_yield(
        terminal_session_id="default",
        command="finished",
        cwd=tmp_path,
        yield_time_ms=5_000,
        tty=False,
        max_lifetime_seconds=None,
        owner_host_session_id=owner,
        shell_argv=(
            sys.executable,
            "-c",
            "print('F' * 120, flush=True)",
        ),
        decision_deadline_monotonic=monotonic() + 5,
        env=dict(os.environ),
    )
    assert yielded is False
    live, yielded, _cwd = registry.exec_with_yield(
        terminal_session_id="default",
        command="live",
        cwd=tmp_path,
        yield_time_ms=0,
        tty=False,
        max_lifetime_seconds=None,
        owner_host_session_id=owner,
        shell_argv=(
            sys.executable,
            "-u",
            "-c",
            "import time; print('L' * 120, flush=True); time.sleep(30)",
        ),
        decision_deadline_monotonic=monotonic() + 5,
        env=dict(os.environ),
    )
    assert yielded is True
    live.output.wait_for_revision(0, 2)
    assert completed.output.retained_utf8_bytes < live.output.retained_utf8_bytes
    assert completed.output.retained_utf8_bytes + live.output.retained_utf8_bytes <= 160
    results = registry.release_owner(owner, timeout_seconds=5)
    assert len(results) == 2


def test_round2_physical_retirement_joins_reader_watcher_timer_and_group(
    tmp_path: Path,
) -> None:
    registry = ProcessRegistry()
    owner = "host:physical-close"
    registry.activate_owner(owner)
    state, yielded, _cwd = registry.exec_with_yield(
        terminal_session_id="default",
        command="parent-and-child",
        cwd=tmp_path,
        yield_time_ms=0,
        tty=False,
        max_lifetime_seconds=60,
        owner_host_session_id=owner,
        shell_argv=(
            sys.executable,
            "-u",
            "-c",
            (
                "import subprocess,time; "
                "subprocess.Popen(['sleep','30']); "
                "print('running', flush=True); time.sleep(30)"
            ),
        ),
        decision_deadline_monotonic=monotonic() + 5,
        env=dict(os.environ),
    )
    assert yielded is True
    process_group_id = state.process.pid
    registry.release_owner(owner, timeout_seconds=5)
    assert state.reader.is_alive() is False
    assert state.watcher is not None and state.watcher.is_alive() is False
    assert state.deadline_timer is not None and state.deadline_timer.is_alive() is False
    assert state.physical_state is TerminalPhysicalState.PHYSICALLY_JOINED
    deadline = monotonic() + 1
    while monotonic() < deadline:
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            break
        sleep(0.01)
    else:
        pytest.fail("terminal process group survived owner release")


def test_round2_leader_exit_does_not_complete_or_release_capacity_until_group_exit(
    tmp_path: Path,
) -> None:
    completions: list[object] = []
    registry = ProcessRegistry(
        max_live_processes=1,
        completion_subscriber=lambda info, _snapshot: completions.append(info),
    )
    owner = "host:leader-group"
    registry.activate_owner(owner)
    state, yielded, _cwd = registry.exec_with_yield(
        terminal_session_id="default",
        command="background-descendant",
        cwd=tmp_path,
        yield_time_ms=0,
        tty=False,
        max_lifetime_seconds=None,
        owner_host_session_id=owner,
        shell_argv=(
            "/bin/sh",
            "-c",
            "sleep 0.8 >/dev/null 2>&1 & exit 0",
        ),
        decision_deadline_monotonic=monotonic() + 5,
        env=dict(os.environ),
    )
    assert yielded is True
    state.process.wait(timeout=1)
    deadline = monotonic() + 0.4
    while state.physical_state is TerminalPhysicalState.RUNNING:
        assert monotonic() < deadline
        sleep(0.005)

    assert state.physical_state is TerminalPhysicalState.TERMINALIZING
    assert registry.live_count(owner_host_session_id=owner) == 1
    assert completions == []
    with pytest.raises(terminal_manager_module.ProcessLimitError):
        registry.exec_with_yield(
            terminal_session_id="default",
            command="must-remain-blocked",
            cwd=tmp_path,
            yield_time_ms=0,
            tty=False,
            max_lifetime_seconds=None,
            owner_host_session_id=owner,
            shell_argv=("/bin/sh", "-c", "true"),
            decision_deadline_monotonic=monotonic() + 5,
            env=dict(os.environ),
        )

    deadline = monotonic() + 2
    while not completions:
        assert monotonic() < deadline
        sleep(0.01)
    assert registry.live_count(owner_host_session_id=owner) == 0
    registry.release_owner(owner, timeout_seconds=2)


def test_round2_wait_uses_physical_group_completion_not_shell_leader(
    tmp_path: Path,
) -> None:
    registry = ProcessRegistry()
    owner = "host:wait-group"
    registry.activate_owner(owner)
    state, yielded, _cwd = registry.exec_with_yield(
        terminal_session_id="default",
        command="background-descendant",
        cwd=tmp_path,
        yield_time_ms=0,
        tty=False,
        max_lifetime_seconds=None,
        owner_host_session_id=owner,
        shell_argv=("/bin/sh", "-c", "sleep 0.35 >/dev/null 2>&1 & exit 0"),
        decision_deadline_monotonic=monotonic() + 5,
        env=dict(os.environ),
    )
    assert yielded is True
    state.process.wait(timeout=1)

    started = monotonic()
    result = registry.wait(
        state.process_id,
        timeout_seconds=2,
        max_output_chars=512,
        owner_host_session_id=owner,
    )

    assert monotonic() - started >= 0.2
    assert result.status.value == "success"
    assert result.exit_code == 0
    assert state.physical_completion.is_set()
    assert state.physical_state is TerminalPhysicalState.PHYSICALLY_JOINED
    registry.release_owner(owner, timeout_seconds=2)


def test_round2_foreground_yield_waits_for_physical_group_completion(
    tmp_path: Path,
) -> None:
    registry = ProcessRegistry()
    owner = "host:foreground-group"
    registry.activate_owner(owner)
    started = monotonic()
    state, yielded, _cwd = registry.exec_with_yield(
        terminal_session_id="default",
        command="background-descendant",
        cwd=tmp_path,
        yield_time_ms=2_000,
        tty=False,
        max_lifetime_seconds=None,
        owner_host_session_id=owner,
        shell_argv=("/bin/sh", "-c", "sleep 0.35 >/dev/null 2>&1 & exit 0"),
        decision_deadline_monotonic=monotonic() + 5,
        env=dict(os.environ),
    )

    assert monotonic() - started >= 0.2
    assert yielded is False
    assert state.physical_completion.is_set()
    assert state.physical_state is TerminalPhysicalState.PHYSICALLY_JOINED
    registry.release_owner(owner, timeout_seconds=2)


def test_round2_process_admission_linearizes_launching_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = ProcessRegistry(max_live_processes=1)
    owner = "host:launch-capacity"
    registry.activate_owner(owner)
    entered_spawn = Event()
    release_spawn = Event()
    original_spawn = registry._spawn  # noqa: SLF001

    def delayed_spawn(**kwargs):
        entered_spawn.set()
        assert release_spawn.wait(2)
        return original_spawn(**kwargs)

    monkeypatch.setattr(registry, "_spawn", delayed_spawn)
    outcomes: list[object] = []

    def launch_first() -> None:
        try:
            outcomes.append(
                registry.exec_with_yield(
                    terminal_session_id="default",
                    command="first",
                    cwd=tmp_path,
                    yield_time_ms=0,
                    tty=False,
                    max_lifetime_seconds=None,
                    owner_host_session_id=owner,
                    shell_argv=("/bin/sh", "-c", "sleep 30"),
                    decision_deadline_monotonic=monotonic() + 5,
                    env=dict(os.environ),
                )
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            outcomes.append(exc)

    thread = Thread(target=launch_first)
    thread.start()
    assert entered_spawn.wait(1)
    with pytest.raises(terminal_manager_module.ProcessLimitError):
        registry.exec_with_yield(
            terminal_session_id="default",
            command="second",
            cwd=tmp_path,
            yield_time_ms=0,
            tty=False,
            max_lifetime_seconds=None,
            owner_host_session_id=owner,
            shell_argv=("/bin/sh", "-c", "sleep 30"),
            decision_deadline_monotonic=monotonic() + 5,
            env=dict(os.environ),
        )
    release_spawn.set()
    thread.join(timeout=3)

    assert thread.is_alive() is False
    assert len(outcomes) == 1 and isinstance(outcomes[0], tuple)
    assert registry.live_count(owner_host_session_id=owner) == 1
    assert registry._launching_by_owner == {}  # noqa: SLF001
    registry.release_owner(owner, timeout_seconds=3)


def test_round2_launch_reservation_releases_on_spawn_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = ProcessRegistry(max_live_processes=1)
    owner = "host:launch-failure"
    registry.activate_owner(owner)

    def fail_spawn(**_kwargs):
        raise OSError("injected spawn failure")

    monkeypatch.setattr(registry, "_spawn", fail_spawn)
    with pytest.raises(OSError, match="injected spawn failure"):
        registry.exec_with_yield(
            terminal_session_id="default",
            command="never-started",
            cwd=tmp_path,
            yield_time_ms=0,
            tty=False,
            max_lifetime_seconds=None,
            owner_host_session_id=owner,
            shell_argv=("/bin/sh", "-c", "true"),
            decision_deadline_monotonic=monotonic() + 5,
            env=dict(os.environ),
        )

    assert registry._launching_by_owner == {}  # noqa: SLF001
    registry.release_owner(owner, timeout_seconds=1)


def test_round2_owner_close_waits_unpublished_launch_and_physical_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = ProcessRegistry(max_live_processes=1)
    owner = "host:close-during-launch"
    registry.activate_owner(owner)
    spawned = Event()
    release_spawn = Event()
    original_spawn = registry._spawn  # noqa: SLF001
    physical_group: list[int] = []

    def delayed_publication(**kwargs):
        state = original_spawn(**kwargs)
        physical_group.append(state.process.pid)
        spawned.set()
        assert release_spawn.wait(2)
        return state

    monkeypatch.setattr(registry, "_spawn", delayed_publication)
    launch_errors: list[BaseException] = []
    close_errors: list[BaseException] = []

    def launch() -> None:
        try:
            registry.exec_with_yield(
                terminal_session_id="default",
                command="close-race",
                cwd=tmp_path,
                yield_time_ms=0,
                tty=False,
                max_lifetime_seconds=None,
                owner_host_session_id=owner,
                shell_argv=("/bin/sh", "-c", "sleep 30"),
                decision_deadline_monotonic=monotonic() + 5,
                env=dict(os.environ),
            )
        except BaseException as exc:
            launch_errors.append(exc)

    def close() -> None:
        try:
            registry.release_owner(owner, timeout_seconds=3)
        except BaseException as exc:  # pragma: no cover - asserted below
            close_errors.append(exc)

    launch_thread = Thread(target=launch)
    launch_thread.start()
    assert spawned.wait(1)
    close_thread = Thread(target=close)
    close_thread.start()
    sleep(0.05)
    assert close_thread.is_alive()
    release_spawn.set()
    launch_thread.join(timeout=3)
    close_thread.join(timeout=3)

    assert launch_thread.is_alive() is False
    assert close_thread.is_alive() is False
    assert len(launch_errors) == 1
    assert isinstance(launch_errors[0], RuntimeError)
    assert close_errors == []
    assert registry._launching_by_owner == {}  # noqa: SLF001
    assert physical_group
    with pytest.raises(ProcessLookupError):
        os.killpg(physical_group[0], 0)


def test_round2_pty_close_stdin_sends_eot_and_produces_real_eof(
    tmp_path: Path,
) -> None:
    registry = ProcessRegistry()
    owner = "host:pty-eof"
    registry.activate_owner(owner)
    state, yielded, _cwd = registry.exec_with_yield(
        terminal_session_id="default",
        command="cat",
        cwd=tmp_path,
        yield_time_ms=0,
        tty=True,
        max_lifetime_seconds=None,
        owner_host_session_id=owner,
        shell_argv=("/bin/sh", "-c", "cat"),
        decision_deadline_monotonic=monotonic() + 5,
        env=dict(os.environ),
    )
    assert yielded is True
    registry.close_stdin(
        state.process_id,
        max_output_chars=512,
        owner_host_session_id=owner,
    )
    assert state.stdin_closed is True
    terminal = registry.wait(
        state.process_id,
        timeout_seconds=2,
        max_output_chars=512,
        owner_host_session_id=owner,
    )
    assert terminal.status.value == "success"
    assert state.process.poll() == 0
    registry.release_owner(owner, timeout_seconds=2)


def test_round2_finished_process_reaches_joined_before_prunable_without_regression(
    tmp_path: Path,
) -> None:
    registry = ProcessRegistry(finished_ttl_seconds=0)
    owner = "host:retirement-state"
    registry.activate_owner(owner)
    state, yielded, _cwd = registry.exec_with_yield(
        terminal_session_id="default",
        command="true",
        cwd=tmp_path,
        yield_time_ms=2_000,
        tty=False,
        max_lifetime_seconds=None,
        owner_host_session_id=owner,
        shell_argv=(sys.executable, "-c", "print('done')"),
        decision_deadline_monotonic=monotonic() + 5,
        env=dict(os.environ),
    )
    assert yielded is False
    assert state.physical_state is TerminalPhysicalState.PHYSICALLY_JOINED
    # A later physical join is idempotent and cannot move PRUNABLE backwards.
    registry._prune_locked()  # noqa: SLF001
    assert state.physical_state is TerminalPhysicalState.PRUNABLE
    registry.release_owner(owner, timeout_seconds=2)


def test_round2_terminal_live_handoff_overflow_is_typed_gap_not_process_failure() -> (
    None
):
    async def scenario() -> None:
        bus = LiveAgentEventBus()
        observer_id, generation, revision = bus.subscribe()
        sink = _ToolResultLiveSink(
            live_bus=bus,
            session_id="session:live-overflow",
            turn_id="turn:live-overflow",
            draft_identity="entry:live-overflow",
            block_identity="block:live-overflow",
            attribution={
                "scope_kind": "ROOT",
                "scope_subagent_task_id": None,
                "channel_kind": LiveChannelKind.TOOL_RESULT,
                "channel_tool_call_id": "call:live-overflow",
                "channel_attempt_id": "attempt:live-overflow",
                "generation_id": "tool-result:live-overflow",
                "proposed_entry_id": "entry:live-overflow",
            },
        )
        sink._MAXIMUM_PENDING_BYTES = 32  # noqa: SLF001
        sink.offer_text("x" * 128)
        await sink.close()
        assert sink.overflowed is True
        assert bus.generation == generation + 1
        observed = bus.observe(observer_id, after_revision=revision, maximum_events=16)
        assert observed.kind is LiveObservationKind.GAP
        bus.close()

    asyncio.run(scenario())


def test_round2_exact_retention_boundary_and_artifact_coverage() -> None:
    owner = TerminalOutputOwner(
        owner_epoch="host:retention-hard",
        process_id="process:retention-hard",
    )
    owner.append_raw(b"a" * (16 * 1024 * 1024))
    complete = owner.artifact_candidate()
    assert owner.retained_utf8_bytes == 16 * 1024 * 1024
    assert complete.source_coverage.value == "COMPLETE"
    owner.append_raw("🙂".encode())
    retained = owner.artifact_candidate()
    assert owner.retained_utf8_bytes <= 16 * 1024 * 1024
    assert retained.source_coverage.value == "RETAINED_SNAPSHOT"
    assert retained.source_coverage_reason is not None
    assert retained.text.encode("utf-8").decode("utf-8") == retained.text


def test_round2_concurrent_append_and_snapshot_preserve_exact_utf8_stream() -> None:
    owner = TerminalOutputOwner(
        owner_epoch="host:concurrent",
        process_id="process:concurrent",
        maximum_bytes=1_000_000,
    )
    chunk = "line🙂\n".encode()

    def writer() -> None:
        for _ in range(1_000):
            owner.append_raw(chunk)

    threads = [Thread(target=writer) for _ in range(4)]
    for thread in threads:
        thread.start()
    observed_offsets: list[int] = []
    while any(thread.is_alive() for thread in threads):
        observed_offsets.append(owner.snapshot(maximum_chars=32_000).through_offset)
    for thread in threads:
        thread.join()
    final = owner.snapshot(maximum_chars=32_000)
    assert final.through_offset == len(chunk) * 4_000
    assert observed_offsets == sorted(observed_offsets)
    assert owner.artifact_candidate().text.count("line🙂\n") == 4_000


@pytest.mark.parametrize("fault_stage", ("subscribe", "reader", "timer"))
def test_round5_post_spawn_installation_fault_rolls_back_every_physical_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault_stage: str,
) -> None:
    registry = ProcessRegistry()
    owner = f"host:post-spawn-{fault_stage}"
    registry.activate_owner(owner)
    spawned: list[object] = []
    original_spawn = registry._spawn  # noqa: SLF001

    def capture_spawn(**kwargs: object):
        state = original_spawn(**kwargs)
        spawned.append(state)
        return state

    monkeypatch.setattr(registry, "_spawn", capture_spawn)
    subscriber = None
    lifetime = None
    if fault_stage == "subscribe":
        original_install = TerminalOutputOwner.install_subscription

        def fail_after_subscription(
            output: TerminalOutputOwner,
            token: str,
            callback,
        ):
            original_install(output, token, callback)
            raise RuntimeError("injected subscription installation failure")

        monkeypatch.setattr(
            TerminalOutputOwner,
            "install_subscription",
            fail_after_subscription,
        )

        def ignore_output(*_args: object) -> None:
            return

        subscriber = ignore_output
    elif fault_stage == "reader":

        def fail_after_reader_start(state) -> None:
            state.reader.start()
            state.reader_started = True
            raise RuntimeError("injected watcher installation failure")

        monkeypatch.setattr(
            registry,
            "_start_physical_threads",
            fail_after_reader_start,
        )
    else:
        original_timer_start = terminal_manager_module.Timer.start

        def fail_lifetime_timer(timer) -> None:
            if getattr(timer.function, "__name__", "") == "_expire":
                raise RuntimeError("injected lifetime timer installation failure")
            original_timer_start(timer)

        monkeypatch.setattr(
            terminal_manager_module.Timer,
            "start",
            fail_lifetime_timer,
        )
        lifetime = 10

    with pytest.raises(RuntimeError, match="injected"):
        registry.exec_with_yield(
            terminal_session_id="default",
            command="faulted launch",
            cwd=tmp_path,
            yield_time_ms=0,
            tty=False,
            max_lifetime_seconds=lifetime,
            owner_host_session_id=owner,
            shell_argv=(sys.executable, "-c", "import time; time.sleep(30)"),
            decision_deadline_monotonic=monotonic() + 5,
            env=dict(os.environ),
            output_subscriber=subscriber,
        )

    assert len(spawned) == 1
    state = spawned[0]
    assert state.physical_completion.is_set()
    assert state.process.poll() is not None
    assert state.output.observation_lease_count == 0
    assert registry._states == {}  # noqa: SLF001
    assert registry._launching_by_owner == {}  # noqa: SLF001
    assert registry._decision_attempts == {}  # noqa: SLF001
    registry.release_owner(owner, timeout_seconds=1)


def test_round2_observation_lease_and_join_failure_block_prune(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = ProcessRegistry(finished_ttl_seconds=0)
    owner = "host:prune-fence"
    registry.activate_owner(owner)
    state, yielded, _cwd = registry.exec_with_yield(
        terminal_session_id="default",
        command="finished",
        cwd=tmp_path,
        yield_time_ms=2_000,
        tty=False,
        max_lifetime_seconds=None,
        owner_host_session_id=owner,
        shell_argv=(sys.executable, "-c", "print('done')"),
        decision_deadline_monotonic=monotonic() + 5,
        env=dict(os.environ),
    )
    assert yielded is False
    token, _cursor = state.output.subscribe(lambda *_args: None)
    registry._prune_locked()  # noqa: SLF001
    assert state.process_id in registry._states  # noqa: SLF001
    state.output.unsubscribe(token)
    monkeypatch.setattr(
        terminal_manager_module, "_join_physical", lambda *_a, **_k: False
    )
    registry._prune_locked()  # noqa: SLF001
    assert state.process_id in registry._states  # noqa: SLF001
    assert state.physical_state is TerminalPhysicalState.PHYSICALLY_JOINED
    monkeypatch.undo()
    registry._prune_locked()  # noqa: SLF001
    assert state.process_id not in registry._states  # noqa: SLF001
    registry.release_owner(owner, timeout_seconds=2)


def test_round2_host_close_invalidates_process_and_cursor(tmp_path: Path) -> None:
    registry = ProcessRegistry()
    owner = "host:cursor-close"
    registry.activate_owner(owner)
    state, yielded, _cwd = registry.exec_with_yield(
        terminal_session_id="default",
        command="running",
        cwd=tmp_path,
        yield_time_ms=0,
        tty=False,
        max_lifetime_seconds=None,
        owner_host_session_id=owner,
        shell_argv=(sys.executable, "-c", "import time; time.sleep(30)"),
        decision_deadline_monotonic=monotonic() + 5,
        env=dict(os.environ),
    )
    assert yielded is True
    cursor = state.output.snapshot(maximum_chars=512).output_cursor
    registry.release_owner(owner, timeout_seconds=3)
    with pytest.raises(KeyError):
        registry.poll(
            state.process_id,
            max_output_chars=512,
            owner_host_session_id=owner,
            since_cursor=cursor,
        )


def test_round2_cwd_fallback_outside_rejection_and_probe_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    nested = workspace / "a" / "b"
    nested.mkdir(parents=True)
    manager = TerminalSessionManager(workspace)
    owner = "host:cwd-faults"
    manager.activate_owner(owner)
    manager.environment_owner.config = manager.environment_owner.config.__class__(
        enable_shell_snapshot=False
    )
    session = manager.get_or_create(owner_host_session_id=owner)
    session.state.current_cwd = nested
    nested.rmdir()
    result = session.execute(
        terminal_manager_module.TerminalRequest(command="pwd", yield_time_ms=2_000),
        decision_deadline_monotonic=monotonic() + 5,
    )
    assert result.status.value == "success"
    assert str(workspace / "a") in result.output
    with pytest.raises(ValueError, match="inside workspace"):
        session.execute(
            terminal_manager_module.TerminalRequest(
                command="pwd", workdir=str(tmp_path), yield_time_ms=2_000
            ),
            decision_deadline_monotonic=monotonic() + 5,
        )

    original_spawn = manager.process_registry._spawn  # noqa: SLF001

    def fail_spawn(**_kwargs):
        raise OSError("injected spawn failure")

    monkeypatch.setattr(manager.process_registry, "_spawn", fail_spawn)
    with pytest.raises(OSError, match="spawn failure"):
        session.execute(
            terminal_manager_module.TerminalRequest(command="true", yield_time_ms=1),
            decision_deadline_monotonic=monotonic() + 5,
        )
    monkeypatch.setattr(manager.process_registry, "_spawn", original_spawn)
    assert list(workspace.glob(".pulsara-cwd-*")) == []
    manager.release_owner(owner, timeout_seconds=3)
