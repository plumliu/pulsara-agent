from fractions import Fraction
import io
import itertools
import json
from pathlib import Path

import av
from PIL import Image, ImageDraw, ImageFilter
import pytest

from pulsara_agent.media.source import MediaSource, probe_media, MediaError
from pulsara_agent.media.frames import FrameBudget, read_video_frames
from pulsara_agent.media.frames_layout import pack_rows, row_candidates
from pulsara_agent.media.frames_selection import Sample, detect_changes, select_samples


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


def read(path, mode="overview", budget=None, **kwargs):
    with MediaSource.open(path) as source:
        info = probe_media(source)
        return read_video_frames(
            source,
            info,
            start=kwargs.pop("start", 0),
            end=kwargs.pop("end", info.duration),
            mode=mode,
            budget=budget or FrameBudget(4_000_000, 1280 * 720, 10, 128),
            **kwargs,
        )


def test_real_aba_images_and_time_facts(tmp_path):
    path = video(tmp_path / "aba.mkv", ["red"] * 4 + ["blue"] * 4 + ["red"] * 12)
    result = read(path, "motion")
    body = json.loads(result.text)
    assert body["complete"] is True
    assert body["sampling"] == "ordered"
    assert [f["t"] for f in body["frames"]] == pytest.approx(
        [i / 100 for i in range(20)]
    )
    assert [f["dt"] for f in body["frames"][1:]] == pytest.approx([0.01] * 19)
    image = Image.open(io.BytesIO(result.images[0].data))
    colors = []
    for frame in body["frames"]:
        left, top, right, bottom = frame["cell_rect"]
        colors.append(
            image.getpixel(
                (
                    round((left + right) * image.width / 2),
                    round((top + bottom) * image.height / 2),
                )
            )
        )
    assert colors[4][2] > 220 and colors[0][0] > 220 and colors[8][0] > 220
    assert body["next"] is None


def test_motion_actual_prefix_next_and_cancel(tmp_path):
    path = video(tmp_path / "long.mkv", ["red"] * 30)
    budget = FrameBudget(1_000_000, 1280 * 720, 2, 5)
    result = read(path, "motion", budget=budget)
    body = json.loads(result.text)
    assert body["processed_range"] == pytest.approx([0, 0.05])
    assert not body["complete"]
    assert body["next"]["arguments"]["from"] == pytest.approx(0.05)
    result2 = read(
        path, "motion", budget=budget, start=body["next"]["arguments"]["from"]
    )
    assert json.loads(result2.text)["frames"][0]["t"] == pytest.approx(0.05)
    with pytest.raises(InterruptedError):
        read(path, "motion", cancelled=lambda: True)


def test_overview_full_range_sparse_not_prefix(tmp_path):
    path = video(tmp_path / "long.mkv", ["red"] * 50 + ["blue"] * 50)
    result = read(path, budget=FrameBudget(1_000_000, 1280 * 720, 2, 8))
    body = json.loads(result.text)
    assert body["processed_range"] == pytest.approx([0, 1])
    assert body["frames"][0]["t"] == 0
    assert body["frames"][-1]["t"] == pytest.approx(0.99)
    assert body["sampling"] == "sparse"
    assert "无音轨" in body["metadata"] and "无文字字幕" in body["metadata"]
    assert body["scanned_ranges"][0][1] < 1
    assert all(0 <= v <= 1 for f in body["frames"] for v in f["cell_rect"])


def test_detail_real_endpoints_crop_and_single_frame(tmp_path):
    path = video(
        tmp_path / "compare.mkv", ["red"] * 10 + ["blue"] * 10 + ["green"] * 10
    )
    result = read(path, "detail", crop=(0.25, 0.25, 0.75, 0.75))
    body = json.loads(result.text)
    assert [f["t"] for f in body["frames"]] == pytest.approx([0, 0.15, 0.29])
    assert body["crop"] == [0.25, 0.25, 0.75, 0.75]
    assert "metadata" not in body
    assert len(result.images) == 3
    single = video(tmp_path / "single.mkv", ["red"])
    assert len(json.loads(read(single, "detail").text)["frames"]) == 1
    limited = read(path, "detail", budget=FrameBudget(1_000_000, 1280 * 720, 2, 30))
    assert [f["t"] for f in json.loads(limited.text)["frames"]] == pytest.approx(
        [0, 0.29]
    )


def test_range_before_first_video_picture_does_not_deliver_later_frame(tmp_path):
    from dataclasses import replace

    path = video(tmp_path / "delayed-track.mkv", ["red", "blue"], pts=[100, 110])
    with MediaSource.open(path) as source:
        # Simulate an earlier audio stream setting the common presentation origin.
        info = replace(
            probe_media(source), timeline_origin=0, duration=2, video_end=1.2
        )
        for mode in ("overview", "detail", "motion"):
            with pytest.raises(MediaError):
                read_video_frames(
                    source,
                    info,
                    start=0,
                    end=0.5,
                    mode=mode,
                    budget=FrameBudget(1_000_000, 1280 * 720, 2, 8),
                )


def test_real_vfr_and_visible_boundary(tmp_path):
    path = video(
        tmp_path / "vfr.mkv", ["red", "blue", "red", "green"], pts=[0, 4, 11, 19]
    )
    result = read(path, "motion", start=0.02, end=0.2)
    body = json.loads(result.text)
    assert [f["t"] for f in body["frames"]] == pytest.approx([0, 0.04, 0.11, 0.19])
    assert body["frames"][0]["visible_range"] == pytest.approx([0.02, 0.04])
    assert [f["dt"] for f in body["frames"][1:]] == pytest.approx([0.04, 0.07, 0.08])


def test_union_quote_reselects_and_boundary_does_not_drop_endpoints(tmp_path):
    path = video(tmp_path / "quote.mkv", ["red"] * 20)
    attempts = []

    def accepts(result):
        body = json.loads(result.text)
        attempts.append(len(body["frames"]))
        return len(body["frames"]) <= 3

    result = read(path, "motion", accepts=accepts)
    body = json.loads(result.text)
    assert len(attempts) > 1 and len(body["frames"]) <= 3
    assert body["frames"][0]["t"] == 0 and body["frames"][-1]["t"] == pytest.approx(
        0.19
    )
    with pytest.raises(MediaError, match="fit"):
        read(path, "motion", accepts=lambda result: False)


def test_local_round_trip_after_a_different_scene():
    samples = [
        Sample(i / 100, Image.new("RGB", (320, 180), color), 0)
        for i, color in enumerate(["black", "white", "white", "red", "white", "white"])
    ]
    changes = detect_changes(samples)
    assert (2, 3, 4) in changes.round_trips
    selected = select_samples(samples, changes, 4, "motion")
    assert 3 in selected and any(index in selected for index in (4, 5))


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
    assert select_samples(samples, changes, 3, "motion") == [0, 1, 2]
    with pytest.raises(ValueError, match="round trip"):
        select_samples(samples, changes, 2, "motion")


def test_dense_motion_budget_delivers_ordered_prefixes_until_complete(tmp_path):
    states = ["red", "blue"] * 15
    path = video(tmp_path / "dense.mkv", states)
    budget = FrameBudget(1_000_000, 1280 * 720, 1, 30, maximum_frames=2)
    start, end = 0, len(states) / 100
    crop = [0.0, 0.0, 1.0, 1.0]
    observed = []
    while start < end:
        result = read(path, "motion", start=start, end=end, budget=budget, crop=crop)
        body = json.loads(result.text)
        left, right = body["processed_range"]
        assert left == start < right <= end
        assert body["scanned_ranges"] == [[left, right]]
        assert all(left <= t < right for t in body["observed_cut_times"])
        observed.extend(frame["t"] for frame in body["frames"])
        assert len(body["frames"]) <= 2
        if right < end:
            assert not body["complete"]
            assert body["next"] == {"tool": "read_video_frames", "arguments": {
                "target": {"kind": "PATH", "path": str(path)}, "from": right,
                "to": end, "mode": "motion", "crop": crop,
            }}
        else:
            assert body["complete"] and body["next"] is None
        start = right
    assert observed == pytest.approx([i / 100 for i in range(len(states))])


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


def test_detail_does_not_cap_supplementary_local_states_at_three(tmp_path):
    colors = ["red"] * 100
    colors[1:4] = ["blue", "green", "yellow"]
    colors[48:52] = ["blue", "green", "yellow", "purple"]
    colors[96:99] = ["blue", "green", "yellow"]
    path = video(tmp_path / "detail-states.mkv", colors)
    result = read(
        path, "detail", budget=FrameBudget(8_000_000, 1280 * 720 * 4, 12, 128)
    )
    body = json.loads(result.text)
    assert len(body["frames"]) > 3
    assert body["frames"][0]["t"] == 0 and body["frames"][-1]["t"] == pytest.approx(
        0.99
    )


@pytest.mark.parametrize("mode", ["overview", "detail", "motion"])
def test_range_published_before_raster_analysis(tmp_path, monkeypatch, mode):
    from pulsara_agent.media import frames

    path = video(tmp_path / "progress.mkv", ["red"] * 30)
    events = []
    display = frames.display_video_frame

    def record_display(*args):
        events.append("display")
        return display(*args)

    monkeypatch.setattr(frames, "display_video_frame", record_display)
    result = read(path, mode, budget=FrameBudget(1_000_000, 1280 * 720, 3, 5),
                  progress=events.append)
    first_end = 0.01 if mode == "motion" else 0.3
    assert events[0] == pytest.approx((0, first_end))
    assert events[1] == "display"
    assert result.processed_range == pytest.approx((0, 0.05 if mode == "motion" else 0.3))


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


def test_aba_can_fit_three_readable_cells_without_losing_return(tmp_path):
    path = video(tmp_path / "three-cell.mkv", ["red"] * 4 + ["blue"] * 4 + ["red"] * 12)
    result = read(
        path,
        "motion",
        budget=FrameBudget(1_000_000, 3 * 160 * 90, 1, 30, maximum_frames=3),
    )
    body = json.loads(result.text)
    assert len(body["frames"]) == 3
    assert [f["t"] for f in body["frames"]] == pytest.approx([0, 0.04, 0.19])
    assert sum(image.width * image.height for image in result.images) <= 3 * 160 * 90
    assert body["media_version"]["size"] == path.stat().st_size
    assert body["media_version"]["modified_ns"] == str(path.stat().st_mtime_ns)


def test_detail_clarity_search_stays_near_each_endpoint(tmp_path):
    sharp = Image.new("RGB", (320, 180), "white")
    ImageDraw.Draw(sharp).text((10, 30), "A", fill="black")
    blur = sharp.filter(ImageFilter.GaussianBlur(1))
    states = [blur] + [sharp] * 18 + [blur]
    path = video(tmp_path / "clarity.mkv", states)
    result = read(path, "detail", budget=FrameBudget(1_000_000, 1280 * 720, 3, 30))
    times = [f["t"] for f in json.loads(result.text)["frames"]]
    assert times[0] == pytest.approx(0.01)
    assert times[-1] == pytest.approx(0.18)
    assert len(times) == 3
