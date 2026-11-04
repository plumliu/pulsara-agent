"""Independent native-media oracles for the exact frame decoder.

These checks compare actual decoded RGB observations, not nominal frame rates,
with the public adapter. Fixture sizes and GOPs are test inputs, never runtime
video/call limits. Real PyAV/FFmpeg performs muxing, seeking and decoding.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import contextmanager
from dataclasses import replace
from fractions import Fraction
import json
from pathlib import Path
from time import monotonic

import av
from PIL import Image, ImageChops, ImageDraw
import psutil
import pytest

from pulsara_agent.media.frames_decode import FrameTarget, VideoDecoder
from pulsara_agent.media.source import FramePosition, MediaError, MediaSource, probe_media


def _movie(
    path: Path,
    pts: list[int],
    *,
    codec: str = "ffv1",
    keyint: int = 24,
    open_gop: bool = False,
    options: dict[str, str] | None = None,
    size: tuple[int, int] = (160, 90),
) -> Path:
    """Emit known visual identities; verify persisted native facts in each test."""
    with av.open(str(path), "w", options=options or {}) as output:
        encoding = (
            {"x264-params": f"keyint={keyint}:min-keyint={keyint}:scenecut=0:open-gop={int(open_gop)}:bframes=3"}
            if codec == "libx264"
            else {}
        )
        stream = output.add_stream(codec, rate=100, options=encoding)
        stream.width, stream.height = size
        stream.pix_fmt = "yuv420p" if codec == "libx264" else "bgr0"
        stream.time_base = Fraction(1, 100)
        for index, stamp in enumerate(pts):
            image = Image.new(
                "RGB", size, ((index * 17) % 256, (index * 43) % 256, (index * 97) % 256)
            )
            ImageDraw.Draw(image).text((20, 30), str(index), fill="white")
            frame = av.VideoFrame.from_image(image)
            frame.pts, frame.time_base = stamp, Fraction(1, 100)
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)
    return path


def _sequential(path: Path):
    """Independent complete-decode oracle; collapse zero-length equal-PTS runs."""
    with av.open(str(path)) as container:
        origin = Fraction(container.start_time, av.time_base)
        rows = []
        for frame in container.decode(video=0):
            assert frame.pts is not None and frame.time_base is not None
            absolute = frame.pts * frame.time_base
            row = (absolute, FramePosition(frame.pts, frame.time_base), frame.to_image().convert("RGB"))
            if rows and rows[-1][0] == absolute:
                rows[-1] = row
            else:
                assert not rows or absolute > rows[-1][0]
                rows.append(row)
    return origin, rows


def _assert_pixels(actual: Image.Image, expected: Image.Image):
    assert actual.size == expected.size
    assert actual.mode == expected.mode == "RGB"
    assert ImageChops.difference(actual, expected).getbbox() is None


@pytest.mark.parametrize("kind", ["cfr", "vfr", "open_gop_vfr"])
def test_real_cfr_vfr_and_open_gop_match_full_rgb_sequential_oracle(tmp_path, kind):
    pts = list(range(100, 220))
    codec = "ffv1"
    if kind != "cfr":
        pts, stamp = [], 100
        for index in range(120):
            pts.append(stamp)
            stamp += (3, 5, 1, 7)[index % 4]
    if kind == "open_gop_vfr":
        codec = "libx264"
    path = _movie(tmp_path / ("movie.mp4" if codec == "libx264" else "movie.mkv"), pts,
                  codec=codec, open_gop=kind == "open_gop_vfr")
    origin, rows = _sequential(path)
    assert origin == 1
    assert len(rows) == 120
    if codec == "libx264":
        with av.open(str(path)) as container:
            assert container.streams.video[0].codec_context.has_b_frames
    targets = [(first[0] + second[0]) / 2 - origin for first, second in zip(rows, rows[1:])]
    with MediaSource.open(path) as source:
        info = probe_media(source)
        with VideoDecoder(source, info) as decoder:
            observed = list(decoder.visible_many(targets))
        assert len(observed) == len(targets)
        for actual, expected, successor in zip(observed, rows, rows[1:]):
            assert actual.position.absolute_time == expected[0]
            assert actual.t == expected[0] - origin
            assert actual.end == successor[0] - origin
            _assert_pixels(actual.image, expected[2])
        # Reopen/seek each previously failing point: correct batching alone must
        # not conceal an incorrect sparse seek near a B-frame/open-GOP boundary.
        if kind == "open_gop_vfr":
            for index in (20, 21, 22, 23, 68, 69, 70, 71):
                with VideoDecoder(source, info) as decoder:
                    actual = next(decoder.visible_many([targets[index]]))
                    assert actual.position.absolute_time == rows[index][0]
                    _assert_pixels(actual.image, rows[index][2])


def test_nonzero_origin_exact_boundary_and_exclusive_end(tmp_path):
    path = _movie(tmp_path / "nonzero.mkv", [100, 104, 111, 119])
    origin, rows = _sequential(path)
    with MediaSource.open(path) as source:
        info = probe_media(source)
        assert info.timeline_origin == origin == 1
        with VideoDecoder(source, info) as decoder:
            actual = list(decoder.visible_many([
                Fraction(4, 100), FrameTarget(Fraction(11, 100), before=True), Fraction(11, 100)
            ]))
        # Numeric request .04 must mean its documented decimal coordinate,
        # rather than 1.040's lossy subtraction from a float origin.
        with VideoDecoder(source, info) as decoder:
            decimal_target = next(decoder.visible_many([0.04]))
        assert [frame.position.absolute_time for frame in actual] == [rows[1][0], rows[1][0], rows[2][0]]
        assert decimal_target.position.absolute_time == rows[1][0]
        _assert_pixels(decimal_target.image, rows[1][2])


def test_valid_negative_mpegts_origin_and_native_pts_are_not_clamped(tmp_path):
    # MPEG-TS preserves negative native PTS with copyts; MP4 can rewrite the
    # same input, so assertions below establish the persisted fixture first.
    path = _movie(
        tmp_path / "negative.ts",
        [-20, -16, -9, -1, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110],
        codec="libx264",
        keyint=10,
        options={"avoid_negative_ts": "disabled", "mpegts_copyts": "1"},
    )
    origin, rows = _sequential(path)
    assert origin < 0 and rows[0][0] < 0
    assert any(first[0] < 0 < second[0] for first, second in zip(rows, rows[1:]))
    targets = [(first[0] + second[0]) / 2 - origin for first, second in zip(rows, rows[1:])]
    with MediaSource.open(path) as source:
        info = probe_media(source)
        assert info.timeline_origin == origin
        # Fresh seeks at all intervals establish correctness in the negative
        # portion independently from continuous-decoder reuse.
        for target, expected in zip(targets, rows):
            with VideoDecoder(source, info) as decoder:
                actual = next(decoder.visible_many([target]))
            assert actual.position.absolute_time == expected[0]
            _assert_pixels(actual.image, expected[2])


def test_duplicate_pts_analysis_and_exact_reread_use_last_visible_frame(tmp_path):
    path = _movie(tmp_path / "duplicates.mkv", [0, 4, 4, 11, 19])
    with av.open(str(path)) as container:
        raw = list(container.decode(video=0))
        assert raw[1].pts == raw[2].pts
        assert ImageChops.difference(raw[1].to_image(), raw[2].to_image()).getbbox() is not None
    origin, rows = _sequential(path)
    with MediaSource.open(path) as source:
        info = probe_media(source)
        with VideoDecoder(source, info) as decoder:
            scanned = list(decoder.scan(Fraction(0), Fraction(1, 5), 10))
        assert [row.position.absolute_time for row in scanned] == [row[0] for row in rows]
        with VideoDecoder(source, info) as decoder:
            reread = list(decoder.visible_many([row.position for row in scanned]))
        assert len(reread) == len(scanned) == 4
        for observed, selected, expected in zip(reread, scanned, rows):
            assert observed.t == selected.t == expected[0] - origin
            _assert_pixels(selected.image, expected[2])
            _assert_pixels(observed.image, expected[2])


def test_real_missing_pts_does_not_create_an_invented_motion_timeline(tmp_path):
    path = _movie(
        tmp_path / "missing.mkv", [-20, -16, -9, -1, 10, 20, 30, 40, 50],
        codec="libx264", options={"avoid_negative_ts": "disabled"},
    )
    with av.open(str(path)) as container:
        assert any(frame.pts is None for frame in container.decode(video=0))
    with MediaSource.open(path) as source:
        with pytest.raises(MediaError) as exc:
            info = probe_media(source)
            with VideoDecoder(source, info) as decoder:
                list(decoder.scan(Fraction(0), Fraction(1), 10))
    assert exc.value.code == "MEDIA_TIMESTAMP_UNAVAILABLE"


def test_decreasing_native_presentation_time_is_typed_not_sorted(tmp_path, monkeypatch):
    path = _movie(tmp_path / "ordered.mkv", [0, 4, 11, 19])
    original = MediaSource.open_container
    with MediaSource.open(path) as source:
        info = probe_media(source)

        @contextmanager
        def malformed_decode(bound):
            with original(bound) as container:
                class DecreasingPresentation:
                    def __getattr__(self, name):
                        return getattr(container, name)

                    def decode(self, *args, **kwargs):
                        for index, frame in enumerate(container.decode(*args, **kwargs)):
                            if index == 1:
                                frame.pts = -1
                            yield frame
                yield DecreasingPresentation()

        monkeypatch.setattr(MediaSource, "open_container", malformed_decode)
        with pytest.raises(MediaError) as exc:
            with VideoDecoder(source, info) as decoder:
                list(decoder.scan(Fraction(0), Fraction(1, 5), 10))
    assert exc.value.code in {"MEDIA_TIMESTAMP_UNAVAILABLE", "MEDIA_DECODE_FAILED"}
    assert "timestamp" in str(exc.value).lower() or "presentation" in str(exc.value).lower()


def test_sparse_and_dense_native_work_share_only_useful_decode_intervals(tmp_path, monkeypatch):
    path = _movie(tmp_path / "scheduling.mp4", list(range(600)), codec="libx264")
    origin, rows = _sequential(path)
    original = MediaSource.open_container
    counts = Counter()

    @contextmanager
    def observed_container(bound):
        counts["opens"] += 1
        with original(bound) as container:
            class ObservedContainer:
                def __getattr__(self, name):
                    return getattr(container, name)

                def seek(self, *args, **kwargs):
                    counts["seeks"] += 1
                    return container.seek(*args, **kwargs)

                def decode(self, *args, **kwargs):
                    for frame in container.decode(*args, **kwargs):
                        counts["decoded"] += 1
                        yield frame
            yield ObservedContainer()

    with MediaSource.open(path) as source:
        info = probe_media(source)
        monkeypatch.setattr(MediaSource, "open_container", observed_container)
        with VideoDecoder(source, info) as decoder:
            dense = list(decoder.visible_many([rows[i][0] - origin for i in range(240, 252)]))
        assert len(dense) == 12
        assert counts["opens"] == 1
        assert counts["seeks"] < len(dense)
        assert counts["decoded"] < 3 * len(dense)
        for actual, expected in zip(dense, rows[240:252]):
            _assert_pixels(actual.image, expected[2])
        counts.clear()
        indexes = [5, 105, 205, 305, 405, 505]
        with VideoDecoder(source, info) as decoder:
            sparse = list(decoder.visible_many([rows[i][0] - origin for i in indexes]))
        assert counts["opens"] == 1
        # This 600-frame fixture has indexed closed GOPs of 24; sparse probes
        # must decode local dependencies instead of all intervening 500 frames.
        assert counts["decoded"] < len(rows) // 2
        for actual, index in zip(sparse, indexes):
            assert actual.position.absolute_time == rows[index][0]
            _assert_pixels(actual.image, rows[index][2])


def test_source_change_between_fixed_plan_and_render_fails(tmp_path):
    from pulsara_agent.media.frames import FrameBudget, prepare_frame_plan, render_frame_page

    path = _movie(tmp_path / "changed.mkv", [0, 4, 11, 19])
    with MediaSource.open(path) as source:
        info = probe_media(source)
        plan = prepare_frame_plan(source, info, start=0, end=info.duration)
        path.write_bytes(path.read_bytes() + b"source changed")
        with pytest.raises(MediaError) as exc:
            render_frame_page(source, info, plan, offset=0, count=len(plan.priority),
                              budget=FrameBudget(4 << 20, 1280 * 720, 4, 256))
    assert exc.value.code == "MEDIA_SOURCE_CHANGED"


def test_native_worker_cancellation_after_decode_progress_reaps_process(tmp_path, monkeypatch):
    from pulsara_agent.media.frames import prepare_frame_plan
    from pulsara_agent.media.worker import MediaWorker

    path = _movie(tmp_path / "cancel.mkv", list(range(800)), size=(640, 360))

    async def scenario():
        owner = MediaWorker()
        children = []
        original_launch = asyncio.create_subprocess_exec

        async def capture_launch(*args, **kwargs):
            child = await original_launch(*args, **kwargs)
            children.append(child)
            return child

        monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_launch)
        progressed = []
        task = None

        def cancel_after_native_progress(span):
            progressed.append(span)
            assert children and children[-1].returncode is None
            task.cancel()

        try:
            with MediaSource.open(path) as source:
                info = probe_media(source)
                task = asyncio.create_task(owner.run(
                    prepare_frame_plan, source, info, start=0, end=info.duration,
                    mode="motion", analysis_frames=512,
                    on_progress=cancel_after_native_progress,
                    deadline_monotonic=monotonic() + 20,
                ))
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert progressed
                assert children and all(child.returncode is not None for child in children)
                assert not any(psutil.pid_exists(child.pid) for child in children)
                assert not owner._processes and not owner._active
                # Cancellation must also release the physical admission slot.
                repeated = await owner.run(probe_media, source, deadline_monotonic=monotonic() + 10)
                assert repeated.video_stream_index == info.video_stream_index
        finally:
            await owner.aclose()
        assert owner.physical_close_complete

    asyncio.run(scenario())


def test_authoritative_quote_retries_prepare_once_and_keep_plan_membership(tmp_path, monkeypatch):
    from pulsara_agent.conversation_kernel.image_validation import HostPromptImageValidator
    from pulsara_agent.conversation_kernel.tool_contracts import (
        FrozenImageToolResourceAllowance, FrozenImageToolResourceIncrement,
    )
    from pulsara_agent.llm.input import FrozenPromptContent, frozen_tool_result_public_text
    from tests.support.round3 import direct_tool_invocation_context
    from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
    from pulsara_agent.conversation_kernel.tool_policy import DefaultToolDispatchAuthorizationPolicy
    from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
    from pulsara_agent.identity import derive_id
    from tests.support.model_config import scheduled_task_service

    session = derive_id("session", "test", "independent-decode-quote")
    attempt = derive_id("attempt", "test", "independent-decode-quote")
    turn = derive_id("turn", "test", "independent-decode-quote")
    entry = derive_id("entry", "test", "independent-decode-quote")

    path = _movie(tmp_path / "quote-retries.mkv", [100 * index for index in range(18)])

    async def scenario():
        validator = HostPromptImageValidator()
        port = DirectKernelToolPort(
            workspace_root=tmp_path,
            host_owner_id=derive_id("host", "test", "independent-decode-quote"),
            session_id=session, live_bus=LiveAgentEventBus(),
            scheduled_tasks=scheduled_task_service(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
            image_validator=validator,
        )
        operations = Counter()
        plans = []
        quoted = []
        original_run = port._media_worker.run

        async def observe_operation(operation, *args, **kwargs):
            operations[operation.__name__] += 1
            value = await original_run(operation, *args, **kwargs)
            if operation.__name__ == "prepare_frame_plan":
                plans.append(value)
            return value

        monkeypatch.setattr(port._media_worker, "run", observe_operation)
        borrow, context = direct_tool_invocation_context(
            port, session_id=session, tool_name="read_video_frames",
            tool_call_id="independent-quote-retry", attempt_id=attempt,
            turn_id=turn, assistant_entry_id=entry,
        )

        class RejectFirstQuotes:
            def quote(self, **kwargs):
                assert isinstance(kwargs["content"], FrozenPromptContent)
                quoted.append(json.loads(frozen_tool_result_public_text(
                    kwargs["content"], tool_name="read_video_frames"
                )))
                return FrozenImageToolResourceIncrement(
                    canonical_bytes=8 << 20 if len(quoted) <= 2 else 0,
                    logical_bytes=0, wire_bytes=0, input_tokens=0,
                )

        try:
            allowance = FrozenImageToolResourceAllowance(
                0, context.tool_call_id,
                borrow.execution_binding("read_video_frames").executor_binding_fingerprint,
                4 << 20, 4 << 20, 8 << 20, 100_000, RejectFirstQuotes(),
            )
            invocation = replace(context, input_modalities=("text", "image"),
                                 image_resource_allowance=allowance)
            result = await port.invoke(
                tool_name="read_video_frames",
                arguments={"target": {"kind": "PATH", "path": str(path)}},
                tool_call_id=context.tool_call_id, attempt_id=attempt,
                turn_id=turn, assistant_entry_id=entry, invocation_context=invocation,
            )
            assert result.state == "SUCCESS", result.content
            assert operations["prepare_frame_plan"] == 1
            assert operations["render_frame_page"] >= 2
            assert len(plans) == 1 and len(quoted) >= 3
            priority = plans[0].priority
            assert len(priority) > 2
            for candidate in quoted:
                actual = [frame["t"] for frame in candidate["frames"]]
                assert actual == sorted(float(frame.t) for frame in priority[:len(actual)])
            assert len(quoted[-1]["frames"]) < len(quoted[0]["frames"])
        finally:
            borrow.close()
            await port.aclose()
            await validator.aclose()

    asyncio.run(scenario())
