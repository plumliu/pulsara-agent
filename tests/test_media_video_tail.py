"""Video/audio endpoints are distinct; sparse targets use the valid video range."""

import asyncio
from contextlib import contextmanager
from dataclasses import replace
from fractions import Fraction
import io
import re

import av
from PIL import Image
import pytest

from pulsara_agent.media.frames import (
    FrameBudget,
    prepare_frame_plan,
    render_frame_page,
)
from pulsara_agent.media.source import MediaError, MediaSource, probe_media
from tests.test_hardcut_media_contract import _execute


def padded_video(path, *, shift=0, known_metadata=False):
    """Real video at native 1s with a longer audio track; Matroska uses VFR."""
    with av.open(str(path), "w") as container:
        video = container.add_stream(
            "libx264" if known_metadata else "ffv1",
            rate=25 if known_metadata else 100,
        )
        video.width, video.height = 160, 90
        video.pix_fmt = "yuv420p" if known_metadata else "bgr0"
        video.time_base = Fraction(1, 100)
        audio = container.add_stream(
            "aac" if known_metadata else "pcm_s16le", rate=8000
        )
        audio.layout = "mono"
        for stamp, color in zip(
            (100, 104, 108, 112) if known_metadata else (100, 104, 111, 119),
            ("red", "blue", "green", "yellow"),
            strict=True,
        ):
            frame = av.VideoFrame.from_image(Image.new("RGB", (160, 90), color))
            frame.pts, frame.time_base = stamp + shift, Fraction(1, 100)
            for packet in video.encode(frame):
                container.mux(packet)
        for packet in video.encode():
            container.mux(packet)
        frame = av.AudioFrame(format="s16", layout="mono", samples=4000)
        frame.sample_rate, frame.pts, frame.time_base = 8000, 8000, Fraction(1, 8000)
        for plane in frame.planes:
            plane.update(bytes(plane.buffer_size))
        for packet in audio.encode(frame):
            container.mux(packet)
        for packet in audio.encode():
            container.mux(packet)
    return path


def source_endpoint(path):
    """Independent FFmpeg facts, without Pulsara selection or container duration."""
    with av.open(str(path)) as container:
        origin = Fraction(container.start_time, av.time_base)
        frames = list(container.decode(video=0))
        last = frames[-1]
        assert last.duration > 0
        return (last.pts + last.duration) * last.time_base - origin


@pytest.mark.parametrize("action", ["overview", "detail", "motion"])
@pytest.mark.parametrize("known_end", [False, True])
@pytest.mark.parametrize("shift", [0, 2])
def test_valid_intersection_preserves_selection_and_real_intervals(
    tmp_path, action, known_end, shift
):
    path = padded_video(tmp_path / "padded.mkv", shift=shift)
    video_end = source_endpoint(path)
    assert video_end == Fraction(shift + 20, 100)
    selected = []
    with MediaSource.open(path) as source:
        info = probe_media(source)
        assert info.duration == 0.5
        assert info.timeline_origin == 1
        assert info.video_end is None
        if known_end:
            info = replace(info, video_end=video_end)
        window = None
        while True:
            plan = prepare_frame_plan(
                source,
                info,
                start=0,
                end=info.duration,
                mode=action,
                window_start=window,
                analysis_frames=3,
            )
            assert plan.requested_range == (0, 0.5)
            selected.extend(plan.priority)
            rendered = render_frame_page(
                source,
                info,
                plan,
                offset=0,
                count=plan.observation_count,
                budget=FrameBudget(8_000_000, 4 * 1280 * 720, 4, 3),
            )
            assert rendered.images
            for item in rendered.index:
                encoded = rendered.images[item["image"] - 1]
                with Image.open(io.BytesIO(encoded.data)) as image:
                    assert image.size == (encoded.width, encoded.height)
            if plan.complete:
                assert plan.video_ended
                assert plan.processed_range[1] == video_end
                break
            assert action == "motion"
            assert plan.next_window_start is not None
            assert plan.next_window_start != window
            window = plan.next_window_start

    # Recomputing the midpoint matters: the original .25s target lies after
    # EOF; the correct effective midpoint selects .04s, not the .11s frame.
    offsets = (0, 4, 11, 19) if action == "motion" else (0, 4, 19)
    assert sorted(item.t for item in selected) == [
        Fraction(shift + offset, 100) for offset in offsets
    ]
    assert max(item.end for item in selected) == video_end
    assert all(item.t < item.end <= video_end for item in selected)
    assert all(item.position.absolute_time == item.t + 1 for item in selected)


@pytest.mark.parametrize("action", ["overview", "detail", "motion"])
@pytest.mark.parametrize("shift", [0, 2])
def test_complete_tool_returns_images_and_one_precise_tail_notice(
    tmp_path, monkeypatch, action, shift
):
    from pulsara_agent.media import execution

    # Force multiple physical motion batches, without changing its source range.
    monkeypatch.setattr(execution, "FRAME_ANALYSIS_FRAMES", 3)
    path = padded_video(tmp_path / "padded.mkv", shift=shift)
    result = asyncio.run(
        _execute(
            tmp_path,
            "read_video_frames",
            {
                "target": {"kind": "PATH", "path": path.name},
                "action": action,
                **({"from": 0, "to": 0.5} if action == "detail" else {}),
            },
        )
    )
    assert result.state == "SUCCESS", result.content.content
    parts = result.content.content
    assert result.pending_images
    indices = [part.value for part in parts if part.type == "object"]
    frames = (
        indices
        if action == "detail"
        else [frame for index in indices for frame in index["frames"]]
    )
    offsets = (0, 4, 11, 19) if action == "motion" else (0, 4, 19)
    assert [frame["t"] for frame in frames] == pytest.approx(
        [(shift + offset) / 100 for offset in offsets]
    )
    if action == "detail":
        assert frames[-1]["visible_range"][1] == (shift + 20) / 100
    elif action == "overview":
        assert (
            max(frame["representative_range"][1] for frame in frames)
            == (shift + 20) / 100
        )
    tail = [
        part.value for part in parts if part.type == "text" and "请求尾部" in part.value
    ]
    assert len(tail) == 1
    gap = re.search(r"\[([\d.]+),\s*([\d.]+)\)", tail[0])
    assert gap is not None
    assert tuple(map(float, gap.groups())) == ((shift + 20) / 100, 0.5)
    if shift:
        assert any(part.type == "text" and "开头缺少" in part.value for part in parts)


@pytest.mark.parametrize("action", ["overview", "detail", "motion"])
def test_tail_only_tool_request_fails_without_substituting_last_picture(
    tmp_path, action
):
    path = padded_video(tmp_path / "padded.mkv")
    result = asyncio.run(
        _execute(
            tmp_path,
            "read_video_frames",
            {
                "target": {"kind": "PATH", "path": path.name},
                "action": action,
                "from": 0.3,
                "to": 0.5,
            },
        )
    )
    assert result.state == "APPLICATION_ERROR"
    assert not result.pending_images
    assert not any(part.type in {"object", "image"} for part in result.content.content)


def test_known_endpoint_motion_reports_tail_only_after_its_final_batch(
    tmp_path, monkeypatch
):
    from pulsara_agent.media import execution

    monkeypatch.setattr(execution, "FRAME_ANALYSIS_FRAMES", 3)
    path = padded_video(tmp_path / "padded.mp4", known_metadata=True)
    with MediaSource.open(path) as source:
        info = probe_media(source)
    assert info.video_end == source_endpoint(path) < info.duration
    result = asyncio.run(
        _execute(
            tmp_path,
            "read_video_frames",
            {"target": {"kind": "PATH", "path": path.name}, "action": "motion"},
        )
    )
    assert result.state == "SUCCESS", result.content.content
    frames = [
        frame
        for part in result.content.content
        if part.type == "object"
        for frame in part.value["frames"]
    ]
    assert [frame["t"] for frame in frames] == pytest.approx(
        [float(1 + Fraction(i, 25) - info.timeline_origin) for i in range(4)]
    )
    notices = [
        part.value
        for part in result.content.content
        if part.type == "text" and "请求尾部" in part.value
    ]
    assert len(notices) == 1
    gap = re.search(r"\[([\d.]+),\s*([\d.]+)\)", notices[0])
    assert gap is not None
    assert tuple(map(float, gap.groups())) == (float(info.video_end), info.duration)


@pytest.mark.parametrize("action", ["overview", "detail", "motion"])
@pytest.mark.parametrize("failure", ["unknown_duration", "decode_failure"])
def test_unverified_or_failed_terminal_decode_is_not_a_successful_tail_gap(
    tmp_path, monkeypatch, action, failure
):
    path = padded_video(tmp_path / "padded.mkv")
    original = MediaSource.open_container
    with MediaSource.open(path) as source:
        info = probe_media(source)

        @contextmanager
        def terminal_fault(bound):
            with original(bound) as container:

                class FaultyDecode:
                    def __getattr__(self, name):
                        return getattr(container, name)

                    def decode(self, *args, **kwargs):
                        for frame in container.decode(*args, **kwargs):
                            if failure == "unknown_duration":
                                frame.duration = 0
                            yield frame
                        if failure == "decode_failure":
                            raise av.error.InvalidDataError(
                                1094995529, "broken video tail"
                            )

                yield FaultyDecode()

        monkeypatch.setattr(MediaSource, "open_container", terminal_fault)
        with pytest.raises(MediaError) as error:
            prepare_frame_plan(source, info, start=0, end=0.5, mode=action)
    if failure == "decode_failure":
        assert error.value.code == "MEDIA_DECODE_FAILED"
    else:
        assert error.value.code in {
            "MEDIA_FRAME_UNAVAILABLE",
            "MEDIA_TIMESTAMP_UNAVAILABLE",
        }


def test_tail_intersection_does_not_make_an_oversized_detail_request_legal(tmp_path):
    path = padded_video(tmp_path / "padded.mkv")
    with MediaSource.open(path) as source:
        info = replace(probe_media(source), duration=3)
        with pytest.raises(MediaError, match="at most 2 seconds") as error:
            prepare_frame_plan(source, info, start=0, end=2.1, mode="detail")
    assert error.value.code == "INVALID_RANGE"
