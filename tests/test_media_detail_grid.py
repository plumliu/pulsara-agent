from fractions import Fraction
import json

import av
from PIL import Image
import pytest

from pulsara_agent.media.frames import FrameBudget, prepare_frame_plan, read_video_frames
from pulsara_agent.media.frames_detail import DETAIL_TARGET_STEP, DetailGrid
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


def test_detail_grid_is_half_second_lazy_and_preserves_short_three_roles():
    assert DETAIL_TARGET_STEP == Fraction(1, 2)
    short = DetailGrid(Fraction(10), Fraction(11))
    assert short.count == 3
    assert [(short.target(i).t, short.target(i).before) for i in range(3)] == [
        (10, False), (Fraction(21, 2), False), (11, True),
    ]
    long = DetailGrid(Fraction(0), Fraction(3600))
    assert long.count == 7201
    assert long.target(7199).t == Fraction(7199, 2)
    assert long.target(7200).before
    with pytest.raises(MediaError, match="outside"):
        long.target(7201)


def test_detail_grid_jump_preserves_half_open_boundary_and_terminal():
    grid = DetailGrid(Fraction(0), Fraction(10))
    assert grid.after_interval(0, Fraction(1)) == 2
    assert grid.target(2).t == 1
    assert grid.after_interval(0, Fraction(3, 4)) == 2
    assert grid.after_interval(19, Fraction(10)) == grid.count
    short = DetailGrid(Fraction(0), Fraction(1))
    assert short.after_interval(0, Fraction(1, 2)) == 1
    assert short.after_interval(1, Fraction(1)) == 3


def test_detail_real_long_hold_jumps_grid_without_duplicate_source(tmp_path):
    path = _video(tmp_path / "hold.mkv", ["red", "blue"], pts=[0, 3600])
    with MediaSource.open(path) as source:
        info = probe_media(source)
        plan = prepare_frame_plan(source, info, start=0, end=info.duration, mode="detail")
    assert plan.detail_grid_count == 7203
    assert [observation.t for observation in plan.priority] == [0, 3600]
    assert [observation.detail_grid_ordinal for observation in plan.priority] == [0, 7200]
    assert plan.priority[-1].detail_next_grid_ordinal == plan.detail_grid_count
    assert plan.complete and plan.observation_count == 2
    assert plan.scanned_ranges == () and plan.observed_cut_times == ()


def test_detail_physical_chunk_uses_distinct_sources_not_grid_roles(tmp_path):
    path = _video(tmp_path / "many.mkv", ["red"] * 300)
    with MediaSource.open(path) as source:
        info = probe_media(source)
        plan = prepare_frame_plan(source, info, start=0, end=info.duration, mode="detail")
        next_plan = prepare_frame_plan(
            source, info, start=0, end=info.duration, mode="detail",
            detail_grid_start=plan.priority[-1].detail_next_grid_ordinal,
        )
    assert plan.observation_count == 256 and not plan.complete
    assert plan.priority[-1].detail_next_grid_ordinal == 512
    assert next_plan.priority[0].t == 256
    assert next_plan.observation_count == 44 and next_plan.complete


@pytest.mark.parametrize("ordinal,span,before_display", [
    (0, (0.0, 1.0), True),
    (1, (0.5, 1.0), True),
    (2, (0.75, 1.0), False),
])
def test_detail_progress_is_early_once_or_actual_for_terminal(
    tmp_path, monkeypatch, ordinal, span, before_display,
):
    from pulsara_agent.media import frames_decode

    path = _video(tmp_path / "terminal.mkv", ["red", "blue", "green", "white"], rate=4)
    events = []
    display = frames_decode.display_video_frame

    def record_display(*args):
        events.append("display")
        return display(*args)

    with MediaSource.open(path) as source:
        info = probe_media(source)
        monkeypatch.setattr(frames_decode, "display_video_frame", record_display)
        plan = prepare_frame_plan(
            source, info, start=0, end=1, mode="detail",
            detail_grid_start=ordinal, progress=events.append,
        )
    assert [event for event in events if event != "display"] == [span]
    assert events[0] == (span if before_display else "display")
    if ordinal == 2:
        assert plan.priority[0].t == Fraction(3, 4)
        assert plan.processed_range == (Fraction(3, 4), 1)


def test_detail_budget_pages_resume_global_grid_from_last_delivered(tmp_path):
    path = _video(tmp_path / "pages.mkv", ["red", "blue"] * 4)
    budget = FrameBudget(1_000_000, 1280 * 720, 1, 2)
    continuation = None
    observations, positions, cursors = [], [], []
    while True:
        with MediaSource.open(path) as source:
            info = probe_media(source)
            result = read_video_frames(
                source, info, start=0, end=info.duration, mode="detail",
                budget=budget, continuation=continuation,
            )
        body = json.loads(result.text)
        observations.extend(frame["t"] for frame in body["frames"])
        positions.extend(frame["observation_position"] for frame in body["frames"])
        assert len(result.images) == 1
        assert "plan_delivery" not in body
        if body["next"] is None:
            assert body["complete"]
            break
        continuation = body["next"]["arguments"]["continuation"]
        ordinal = continuation["position"]["next_global_grid_ordinal"]
        assert ordinal > (cursors[-1] if cursors else 0)
        cursors.append(ordinal)
    assert observations == list(range(8))
    assert positions == list(range(0, 16, 2))
    assert cursors == list(range(2, 16, 2))


def test_detail_large_physical_prefix_is_rendered_once(tmp_path, monkeypatch):
    from pulsara_agent.media import frames

    path = _video(tmp_path / "many-different.mkv", ["red", "blue"] * 150)
    render = frames.render_frame_page
    calls = []

    def observed_render(*args, **kwargs):
        calls.append(kwargs["count"])
        return render(*args, **kwargs)

    monkeypatch.setattr(frames, "render_frame_page", observed_render)
    with MediaSource.open(path) as source:
        info = probe_media(source)
        result = read_video_frames(
            source, info, start=0, end=info.duration, mode="detail",
            budget=FrameBudget(1_000_000, 1280 * 720, 1, 256),
        )
    body = json.loads(result.text)
    assert calls == [256]
    assert [frame["t"] for frame in body["frames"]] == [0]
    assert body["next"]["arguments"]["continuation"]["position"] == {
        "next_global_grid_ordinal": 2,
    }


def test_detail_quote_shrink_trims_one_encoded_candidate(tmp_path, monkeypatch):
    from pulsara_agent.media import frames

    path = _video(tmp_path / "same-images.mkv", ["red"] * 30)
    render = frames.render_frame_page
    calls = []

    def observed_render(*args, **kwargs):
        calls.append(kwargs["count"])
        return render(*args, **kwargs)

    monkeypatch.setattr(frames, "render_frame_page", observed_render)
    with MediaSource.open(path) as source:
        info = probe_media(source)
        result = read_video_frames(
            source, info, start=0, end=info.duration, mode="detail",
            budget=FrameBudget(1_000_000, 1280 * 720, 1, 256),
            accepts=lambda candidate: len(json.loads(candidate.text)["frames"]) <= 2,
        )
    body = json.loads(result.text)
    assert calls == [30]
    assert len(result.images) == 1 and len(body["frames"]) == 2
    assert [frame["t"] for frame in body["frames"]] == [0, 1]
    assert body["grid_delivery"]["next_position"] == 4


@pytest.mark.parametrize("singleton_fits", [False, True])
def test_detail_unknown_resource_failure_only_retries_singleton(
    tmp_path, monkeypatch, singleton_fits,
):
    from pulsara_agent.media import frames

    path = _video(tmp_path / "resource.mkv", ["red"] * 300)
    render = frames.render_frame_page
    calls = []

    def observed_render(*args, **kwargs):
        calls.append(kwargs["count"])
        if kwargs["count"] > 1 or not singleton_fits:
            raise MediaError("RESOURCE_BOUNDARY", "A resource owner rejected this request")
        return render(*args, **kwargs)

    monkeypatch.setattr(frames, "render_frame_page", observed_render)
    with MediaSource.open(path) as source:
        info = probe_media(source)
        arguments = dict(
            start=0, end=info.duration, mode="detail",
            budget=FrameBudget(1_000_000, 1280 * 720, 1, 256),
        )
        if singleton_fits:
            result = read_video_frames(source, info, **arguments)
            assert len(json.loads(result.text)["frames"]) == 1
        else:
            with pytest.raises(MediaError, match="resource owner"):
                read_video_frames(source, info, **arguments)
    assert calls == [256, 1]
