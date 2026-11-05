"""Local three-target detail contract; revision specification §3–5."""
from dataclasses import replace
from fractions import Fraction
import json

import av
from PIL import Image
import pytest

from pulsara_agent.media.frames import FrameBudget, prepare_frame_plan, read_video_frames
from pulsara_agent.media.source import MediaError, MediaSource, probe_media


def _video(path, colors, *, rate=1, pts=None):
    with av.open(str(path), "w") as output:
        stream = output.add_stream("ffv1", rate=rate)
        stream.width, stream.height = 160, 90
        stream.pix_fmt = "bgr0"
        stream.time_base = Fraction(1, rate)
        for i, color in enumerate(colors):
            frame = av.VideoFrame.from_image(Image.new("RGB", (160, 90), color))
            frame.pts = i if pts is None else pts[i]
            frame.time_base = Fraction(1, rate)
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)
    return path


@pytest.mark.parametrize("start,end,rate,pts,expected", [
    (0, 2, 4, None, [0, 1, 1.75]),
    (0.02, 0.2, 100, [0, 4, 11, 19], [0, 0.11, 0.19]),
    (0.01, 0.2, 4, None, [0]),
    (0, 2, 1, [0, 4], [0]),
])
def test_detail_resolves_exact_start_middle_and_strict_before_end(tmp_path, start, end, rate, pts, expected):
    count = len(pts) if pts else 8
    path = _video(tmp_path / "targets.mkv", ["red", "blue"] * (count // 2), rate=rate, pts=pts)
    with MediaSource.open(path) as source:
        info = probe_media(source)
        plan = prepare_frame_plan(source, info, start=start, end=end, mode="detail")
    assert [float(item.t) for item in plan.priority] == pytest.approx(expected)
    assert plan.probe_count == len(expected)
    assert plan.scanned_ranges == plan.observed_cut_times == ()
    assert plan.complete


def test_detail_range_limit_is_checked_before_video_intersection(tmp_path):
    path = _video(tmp_path / "short.mkv", ["red"], rate=4)
    with MediaSource.open(path) as source:
        info = probe_media(source)
        for end in (2.000001, 60):
            with pytest.raises(MediaError, match="at most 2 seconds") as error:
                prepare_frame_plan(source, info, start=0, end=end, mode="detail")
            assert error.value.code == "INVALID_RANGE"


def test_detail_reports_missing_video_prefix_and_tail(tmp_path):
    path = _video(tmp_path / "delayed.mkv", ["red", "blue", "green"], rate=100, pts=[100, 110, 119])
    with MediaSource.open(path) as source:
        info = replace(probe_media(source), timeline_origin=Fraction(0), duration=2,
                       video_start=Fraction(1), video_end=Fraction(6, 5))
        result = read_video_frames(source, info, start=0, end=2, mode="detail",
                                  budget=FrameBudget(1_000_000, 3 * 1280 * 720, 3, 3))
    body = json.loads(result.text)
    assert [f["t"] for f in body["frames"]] == pytest.approx([1, 1.1, 1.19])
    assert body["requested_range"] == [0, 2]
    assert body["processed_range"] == [1, 1.2]
    assert body["next"] is None and not body["complete"]
    assert any("starts after" in note for note in body["limitations"])
    assert any("ends before" in note for note in body["limitations"])


def test_detail_quote_shrink_trims_one_encoded_candidate(tmp_path, monkeypatch):
    from pulsara_agent.media import frames

    path = _video(tmp_path / "same-images.mkv", ["red"] * 8, rate=4)
    render = frames.render_frame_page
    calls = []

    def observed_render(*args, **kwargs):
        calls.append(kwargs["count"])
        return render(*args, **kwargs)

    monkeypatch.setattr(frames, "render_frame_page", observed_render)
    with MediaSource.open(path) as source:
        info = probe_media(source)
        result = read_video_frames(
            source, info, start=0, end=2, mode="detail",
            budget=FrameBudget(1_000_000, 1280 * 720, 1, 3),
            accepts=lambda candidate: len(json.loads(candidate.text)["frames"]) <= 2,
        )
    body = json.loads(result.text)
    assert calls == [3]
    assert len(result.images) == 1
    assert [frame["t"] for frame in body["frames"]] == [0, 1]
    assert body["plan_delivery"]["next_position"] == 2
    assert body["next"]["arguments"]["continuation"]["position"] == {"next_observation": 2}
