"""Video source geometry, real temporal selection and layout quality."""

from fractions import Fraction
import itertools
from pathlib import Path

import av
from PIL import Image, ImageDraw, ImageFilter
import pytest

from pulsara_agent.media.source import MediaSource, probe_media, MediaError
from pulsara_agent.media.frames import (
    FrameBudget,
    prepare_frame_plan,
    render_frame_page,
)
from pulsara_agent.media.frames_layout import pack_rows, row_candidates
from pulsara_agent.media.frames_selection import Sample, detect_changes


def video(path: Path, states, pts=None, rate=100):
    with av.open(str(path), "w") as output:
        stream = output.add_stream("ffv1", rate=rate)
        stream.width, stream.height = 320, 180
        stream.pix_fmt = "bgr0"
        stream.time_base = Fraction(1, rate)
        for i, state in enumerate(states):
            image = (
                state
                if isinstance(state, Image.Image)
                else Image.new("RGB", (320, 180), state)
            )
            frame = av.VideoFrame.from_image(image)
            frame.pts = i if pts is None else pts[i]
            frame.time_base = Fraction(1, rate)
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)
    return path


def test_local_round_trip_after_a_different_scene():
    samples = [
        Sample(i / 100, Image.new("RGB", (320, 180), color), 0)
        for i, color in enumerate(["black", "white", "white", "red", "white", "white"])
    ]
    changes = detect_changes(samples)
    assert (2, 3, 4) in changes.round_trips
    assert {2, 3, 4}.issubset(changes.candidates)


def test_sparse_anchors_cannot_invent_round_trip():
    a = Image.new("RGB", (320, 180), "red")
    samples = [Sample(0, a, 0), Sample(0.1, a, 1)]
    changes = detect_changes(samples)
    assert not changes.round_trips and not changes.candidates and not changes.cuts
    assert changes.global_scores == (0, 0)


def test_equal_peaks_blurry_b_and_local_change_are_retained():
    a = Image.new("RGB", (320, 180), "black")
    b = a.copy()
    ImageDraw.Draw(b).rectangle((20, 20, 40, 40), fill="white")
    b = b.filter(ImageFilter.GaussianBlur(1))
    samples = [Sample(0, a, 0), Sample(0.04, b, 0), Sample(0.08, a, 0)]
    changes = detect_changes(samples)
    assert changes.round_trips == ((0, 1, 2),)
    assert changes.local_scores[1] >= 0.2
    assert changes.global_scores[1] < 10
    assert changes.candidates == (0, 1, 2)


def test_tier_two_never_exceeds_half_width_for_any_candidate():
    rows = row_candidates([2, 1, 2], 1280, 720, 16 / 9, (160, 90))
    for variants in rows:
        for row in variants:
            for index, (width, _) in enumerate(row.sizes, start=row.start):
                if index in (0, 2):
                    assert 427 <= width <= 640


def exhaustive(rows, width, height, penalty, max_pages=None):
    best = float("-inf")

    def visit(i, used, pages, score):
        nonlocal best
        if i == len(rows):
            best = max(best, score)
            return
        for row in rows[i]:
            if pages and used + row.height <= height:
                visit(
                    row.end,
                    used + row.height,
                    pages,
                    score + row.area / (width * height),
                )
            if max_pages is None or pages < max_pages:
                visit(
                    row.end,
                    row.height,
                    pages + 1,
                    score + row.area / (width * height) - penalty,
                )

    visit(0, 0, 0, 0.0)
    return best


@pytest.mark.parametrize("penalty", [1.02, 1.5, 2.5])
def test_dp_exhaustive_oracle_and_integer_geometry(penalty):
    for count in range(1, 5):
        for tiers in itertools.product((1, 2, 3), repeat=count):
            rows = row_candidates(tiers, 64, 36, 16 / 9, (8, 5))
            layout = pack_rows(rows, 64, 36, penalty=penalty)
            assert layout.score == pytest.approx(exhaustive(rows, 64, 36, penalty))
            assert [c.index for c in layout.cells] == list(range(count))
            for a, b in itertools.combinations(layout.cells, 2):
                if a.page == b.page:
                    al, at, ar, ab = a.rect
                    bl, bt, br, bb = b.rect
                    assert ar <= bl or br <= al or ab <= bt or bb <= at
            for pages in (1, 2):
                oracle = exhaustive(rows, 64, 36, penalty, pages)
                if oracle == float("-inf"):
                    with pytest.raises(ValueError):
                        pack_rows(rows, 64, 36, penalty=penalty, maximum_pages=pages)
                else:
                    assert pack_rows(
                        rows, 64, 36, penalty=penalty, maximum_pages=pages
                    ).score == pytest.approx(oracle)


def test_readable_floor_and_dp_cancel():
    rows = row_candidates([8] * 4, 1280, 720, 16 / 9, (160, 90))
    assert all(
        w >= 160 and h >= 90
        for candidates in rows
        for row in candidates
        for w, h in row.sizes
    )
    with pytest.raises(InterruptedError):
        pack_rows(rows, 1280, 720, cancelled=lambda: True)


def test_hard_cut_fade_motion_static_and_noise_features():
    black = Image.new("RGB", (320, 180), "black")
    white = Image.new("RGB", (320, 180), "white")
    cut = [
        Sample(i * 0.1, image, 0)
        for i, image in enumerate((black, black, white, white))
    ]
    assert detect_changes(cut).cuts == (2,)
    fade = [
        Sample(i * 0.1, Image.new("RGB", (320, 180), (i * 20,) * 3), 0)
        for i in range(12)
    ]
    changes = detect_changes(fade)
    assert changes.candidates and not changes.cuts
    static = [Sample(i * 0.1, black, 0) for i in range(12)]
    assert not detect_changes(static).candidates
    noise = [
        Sample(i * 0.1, Image.new("RGB", (320, 180), (i % 5,) * 3), 0)
        for i in range(12)
    ]
    assert not detect_changes(noise).round_trips
    # The accumulated variation must not cross a gap between observations.
    gapped = [
        Sample(
            i * 0.1,
            Image.new("RGB", (320, 180), (i * 10,) * 3),
            i,
        )
        for i in range(12)
    ]
    assert not detect_changes(gapped).candidates


def test_real_vfr_observations_keep_boundary_intersection_and_actual_intervals(
    tmp_path,
):
    path = video(
        tmp_path / "vfr.mkv", ["red", "blue", "red", "green"], pts=[0, 4, 11, 19]
    )
    with MediaSource.open(path) as source:
        info = probe_media(source)
        plan = prepare_frame_plan(source, info, start=0.02, end=0.2, mode="motion")
        result = render_frame_page(
            source,
            info,
            plan,
            offset=0,
            count=plan.observation_count,
            budget=FrameBudget(4_000_000, 16_777_216, 20, 256),
        )
    assert [item["t"] for item in result.index] == pytest.approx([0, 0.04, 0.11, 0.19])
    assert result.index[0]["visible_range"] == pytest.approx([0.02, 0.04])
    assert [item["dt"] for item in result.index[1:]] == pytest.approx(
        [0.04, 0.07, 0.08]
    )


def test_detail_targets_keep_endpoints_crop_and_sharpness_independently_of_display(
    tmp_path,
):
    sharp = Image.new("RGB", (320, 180), "white")
    ImageDraw.Draw(sharp).text((10, 30), "A", fill="black")
    blur = sharp.filter(ImageFilter.GaussianBlur(1))
    path = video(tmp_path / "detail.mkv", [blur] + [sharp] * 18 + [blur])
    with MediaSource.open(path) as source:
        plan = prepare_frame_plan(
            source,
            probe_media(source),
            start=0,
            end=0.2,
            mode="detail",
            crop=(0.25, 0.25, 0.75, 0.75),
        )
        result = render_frame_page(
            source,
            probe_media(source),
            plan,
            offset=0,
            count=plan.observation_count,
            budget=FrameBudget(4_000_000, 16_777_216, 20, 256),
        )
    assert [item["t"] for item in result.index] == pytest.approx([0, 0.1, 0.19])
    assert plan.crop == (0.25, 0.25, 0.75, 0.75)
    assert all(image.width == 1280 and image.height == 720 for image in result.images)


@pytest.mark.parametrize("mode", ["overview", "detail", "motion"])
def test_range_before_first_video_picture_never_delivers_later_source(tmp_path, mode):
    from dataclasses import replace

    path = video(tmp_path / "delayed.mkv", ["red", "blue"], pts=[100, 110])
    with MediaSource.open(path) as source:
        info = replace(
            probe_media(source),
            timeline_origin=0,
            duration=2,
            video_start=Fraction(1),
            video_end=Fraction(6, 5),
        )
        with pytest.raises(MediaError) as error:
            prepare_frame_plan(source, info, start=0, end=0.5, mode=mode)
    assert error.value.code == "NO_VIDEO_IN_RANGE"


@pytest.mark.parametrize("mode", ["overview", "detail", "motion"])
def test_actual_read_range_is_published_before_source_raster_analysis(
    tmp_path, monkeypatch, mode
):
    from pulsara_agent.media import frames_decode

    path = video(tmp_path / "progress.mkv", ["red"] * 30)
    events = []
    display = frames_decode.display_video_frame

    def observe(*args):
        events.append("display")
        return display(*args)

    monkeypatch.setattr(frames_decode, "display_video_frame", observe)
    with MediaSource.open(path) as source:
        plan = prepare_frame_plan(
            source,
            probe_media(source),
            start=0,
            end=0.3,
            mode=mode,
            progress=events.append,
        )
    assert events[0] == pytest.approx((0, 0.01 if mode == "motion" else 0.3))
    assert events[1] == "display"
    assert tuple(map(float, plan.processed_range)) == pytest.approx((0, 0.3))


def test_prepared_source_cannot_change_before_render(tmp_path):
    path = video(tmp_path / "changed.mkv", ["red", "blue", "green"])
    with MediaSource.open(path) as source:
        info = probe_media(source)
        plan = prepare_frame_plan(source, info, start=0, end=info.duration)
        replacement = video(tmp_path / "replacement.mkv", ["white", "black", "white"])
        replacement.replace(path)
        with pytest.raises(MediaError) as error:
            render_frame_page(
                source,
                info,
                plan,
                offset=0,
                count=1,
                budget=FrameBudget(4_000_000, 16_777_216, 20, 256),
            )
        assert error.value.code == "MEDIA_SOURCE_CHANGED"


def test_cancellation_never_returns_a_prepared_or_rendered_success(tmp_path):
    path = video(tmp_path / "cancelled.mkv", ["red", "blue", "green"])
    with MediaSource.open(path) as source:
        info = probe_media(source)
        with pytest.raises(InterruptedError):
            prepare_frame_plan(
                source, info, start=0, end=info.duration, cancelled=lambda: True
            )
        plan = prepare_frame_plan(source, info, start=0, end=info.duration)
        with pytest.raises(InterruptedError):
            render_frame_page(
                source,
                info,
                plan,
                offset=0,
                count=1,
                budget=FrameBudget(4_000_000, 16_777_216, 20, 256),
                cancelled=lambda: True,
            )
