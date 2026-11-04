"""Source-backed acceptance for the inexpensive overview contract."""

from fractions import Fraction
from dataclasses import replace
import io
import json
import math
from pathlib import Path

import av
from PIL import Image, ImageDraw
import pytest

from pulsara_agent.media import frames
from pulsara_agent.media.source import MediaSource, probe_media


def make_video(path: Path, states, *, rate=10, size=(320, 180), pts=None):
    with av.open(str(path), "w") as container:
        stream = container.add_stream("ffv1", rate=rate)
        stream.width, stream.height = size
        stream.pix_fmt = "bgr0"
        stream.time_base = Fraction(1, rate)
        for i, state in enumerate(states):
            image = state if isinstance(state, Image.Image) else Image.new("RGB", size, state)
            frame = av.VideoFrame.from_image(image)
            frame.pts, frame.time_base = i if pts is None else pts[i], Fraction(1, rate)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return path


def plan_and_read(path, **kwargs):
    with MediaSource.open(path) as source:
        info = probe_media(source)
        end = kwargs.pop("end", info.duration)
        plan = frames.prepare_frame_plan(source, info, start=0, end=end, **kwargs)
        result = frames.read_video_frames(
            source, info, start=0, end=end,
            budget=frames.FrameBudget(8_000_000, 8 * 1280 * 720, 16, frames.FRAME_ANALYSIS_FRAMES),
            **kwargs,
        )
    return plan, result, json.loads(result.text)


@pytest.mark.parametrize("count,rate,expected_probes", [(3, 10, 3), (30, 10, 4), (50, 1, 31)])
def test_short_and_long_overviews_probe_the_whole_video_without_prefix_scan(
    tmp_path, count, rate, expected_probes
):
    states = [(i * 41 % 256, i * 71 % 256, i * 113 % 256) for i in range(count)]
    path = make_video(tmp_path / "distributed.mkv", states, rate=rate)
    plan, _, body = plan_and_read(path)
    assert plan.probe_count == expected_probes
    assert body["sampling"] == "sparse"
    assert body["scanned_ranges"] == []
    assert body["observed_cut_times"] == []
    assert body["frames"][0]["t"] == 0
    assert body["frames"][-1]["t"] == pytest.approx((count - 1) / rate)
    assert len(body["frames"]) <= 16
    assert body["complete"] and body["next"] is None


def test_static_video_keeps_actual_endpoint_facts_and_stops_before_padding(tmp_path):
    path = make_video(tmp_path / "static.mkv", ["white"] * 50, rate=1)
    plan, _, body = plan_and_read(path)
    assert plan.probe_count == 31
    assert plan.observation_count == 2
    assert [frame["t"] for frame in body["frames"]] == [0, 49]
    single = make_video(tmp_path / "single.mkv", ["white"], rate=1)
    one_plan, _, one = plan_and_read(single)
    assert one_plan.observation_count == 1
    assert len(one["frames"]) == 1


def test_hour_long_static_source_remains_a_sparse_complete_overview(tmp_path):
    path = make_video(tmp_path / "hour.mkv", ["white"] * 30, rate=Fraction(1, 120))
    plan, _, body = plan_and_read(path)
    assert body["requested_range"] == [0, 3600]
    assert body["processed_range"] == [0, 3600]
    assert plan.probe_count <= 31
    assert plan.observation_count == 2
    assert [frame["t"] for frame in body["frames"]] == [0, 3480]
    assert body["scanned_ranges"] == []
    assert body["complete"] and body["next"] is None


@pytest.mark.parametrize("mode", ["overview", "detail", "motion"])
def test_delayed_video_track_does_not_probe_or_claim_scanning_the_empty_head(tmp_path, mode):
    path = make_video(tmp_path / "delayed.mkv", ["red", "blue"] * 5,
                      rate=1, pts=list(range(90, 100)))
    with MediaSource.open(path) as source:
        # The real stream begins at native PTS 90. An earlier audio stream
        # would establish this common origin; no fabricated video is inserted.
        info = replace(probe_media(source), timeline_origin=Fraction(0),
                       video_start=Fraction(90), video_end=Fraction(100), duration=100)
        plan = frames.prepare_frame_plan(source, info, start=0, end=100, mode=mode)
        result = frames.read_video_frames(
            source, info, start=0, end=100, mode=mode,
            budget=frames.FrameBudget(8_000_000, 8 * 1280 * 720, 16, frames.FRAME_ANALYSIS_FRAMES),
        )
    body = json.loads(result.text)
    assert body["requested_range"] == [0, 100]
    assert all(90 <= frame["t"] < 100 for frame in body["frames"])
    assert all(90 <= start < end <= 100 for start, end in body["scanned_ranges"])
    if mode == "overview":
        assert plan.probe_count == 10
        assert body["scanned_ranges"] == []


def test_gradual_drift_does_not_chain_all_observations_into_one_group(tmp_path):
    states = [Image.new("RGB", (320, 180), (i * 3,) * 3) for i in range(31)]
    path = make_video(tmp_path / "drift.mkv", states, rate=1)
    plan, _, body = plan_and_read(path)
    assert 2 < plan.observation_count <= 16
    assert body["frames"][0]["t"] == 0
    assert body["frames"][-1]["t"] == 30


def test_sampled_return_state_is_not_globally_deduplicated(tmp_path):
    path = make_video(tmp_path / "return.mkv", ["red", "blue", "red"], rate=10)
    plan, _, body = plan_and_read(path)
    assert plan.observation_count == 3
    assert [frame["t"] for frame in body["frames"]] == pytest.approx([0, 0.1, 0.2])
    assert [float(observation.t) for observation in plan.priority] == pytest.approx([0, 0.2, 0.1])
    assert body["scanned_ranges"] == [] and body["observed_cut_times"] == []


def test_unsampled_brief_return_is_not_invented_as_an_observed_event(tmp_path):
    path = make_video(tmp_path / "missed.mkv", ["red", "red", "blue"] + ["red"] * 7)
    _, _, body = plan_and_read(path)
    assert [frame["t"] for frame in body["frames"]] == pytest.approx([0, 0.9])
    assert body["scanned_ranges"] == [] and body["observed_cut_times"] == []
    assert body["limitations"]


def test_local_dialog_at_a_probe_survives_while_text_has_no_completeness_claim(tmp_path):
    base = Image.new("RGB", (320, 180), "white")
    ImageDraw.Draw(base).text((10, 10), "item 1", fill="black")
    dialog = base.copy()
    ImageDraw.Draw(dialog).rectangle((140, 70, 186, 106), fill="black")
    path = make_video(tmp_path / "dialog.mkv", [base, dialog, base])
    plan, result, body = plan_and_read(path)
    assert plan.observation_count == 3
    middle = next(frame for frame in body["frames"] if math.isclose(frame["t"], 0.1))
    image = Image.open(io.BytesIO(result.images[middle["image"] - 1].data)).convert("RGB")
    left, top, right, bottom = middle["cell_rect"]
    x = round((left + (right - left) * 0.51) * image.width)
    y = round((top + (bottom - top) * 0.50) * image.height)
    assert max(image.getpixel((x, y))) < 30
    assert body["limitations"]
    assert "transcript" not in body and "chapters" not in body


def test_tiny_text_changes_keep_the_sparse_evidence_limitation(tmp_path):
    images = []
    for numeral in ("1", "2", "1"):
        image = Image.new("RGB", (320, 180), "white")
        ImageDraw.Draw(image).text((110, 85), numeral, fill="black")
        images.append(image)
    path = make_video(tmp_path / "tiny-text.mkv", images)
    _, _, body = plan_and_read(path)
    times = [frame["t"] for frame in body["frames"]]
    assert times[0] == 0 and times[-1] == pytest.approx(0.2)
    assert all(any(math.isclose(time, actual) for actual in (0, 0.1, 0.2)) for time in times)
    assert body["sampling"] == "sparse" and body["scanned_ranges"] == []
    assert any("unobserved" in limitation.lower() for limitation in body["limitations"])
    assert "transcript" not in body and "chapters" not in body


@pytest.mark.parametrize("size", [(320, 180), (180, 320), (640, 80)])
def test_scene_target_preserves_both_axes_and_aspect_for_real_images(tmp_path, size):
    path = make_video(tmp_path / "geometry.mkv", ["red", "blue", "green"], size=size)
    _, result, body = plan_and_read(path)
    minimum = (320, 180) if size[0] >= size[1] else (180, 320)
    for frame in body["frames"]:
        image = Image.open(io.BytesIO(result.images[frame["image"] - 1].data))
        left, top, right, bottom = frame["cell_rect"]
        width, height = (right - left) * image.width, (bottom - top) * image.height
        assert width >= minimum[0] - 1e-6
        assert height >= minimum[1] - 1e-6
        assert abs(width - height * size[0] / size[1]) <= 1.01


def test_fast_cuts_remain_sparse_and_do_not_create_exact_cut_claims(tmp_path):
    colors = ["red", "blue", "green", "white", "black"] * 20
    path = make_video(tmp_path / "cuts.mkv", colors, rate=20)
    plan, _, body = plan_and_read(path)
    assert plan.probe_count == 6
    assert 2 <= len(body["frames"]) <= plan.probe_count
    assert body["scanned_ranges"] == [] and body["observed_cut_times"] == []
    assert all(frame["t"] * 20 == pytest.approx(round(frame["t"] * 20))
               for frame in body["frames"])
