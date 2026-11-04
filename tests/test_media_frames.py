from fractions import Fraction
import io
import itertools
import json
from pathlib import Path

import av
from PIL import Image, ImageDraw, ImageFilter
import pytest

from pulsara_agent.media.source import MediaSource, probe_media, MediaError
from pulsara_agent.media.frames import FRAME_ANALYSIS_FRAMES, FrameBudget, prepare_frame_plan, read_video_frames
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


def read(path, mode="overview", budget=None, **kwargs):
    with MediaSource.open(path) as source:
        info = probe_media(source)
        return read_video_frames(
            source,
            info,
            start=kwargs.pop("start", 0),
            end=kwargs.pop("end", info.duration),
            mode=mode,
            budget=budget or FrameBudget(4_000_000, 1280 * 720, 10, FRAME_ANALYSIS_FRAMES),
            **kwargs,
        )


def read_pages(path, mode="overview", budget=None, accepts=None, **kwargs):
    """Copy the complete runtime next; do not infer time or candidate offsets."""
    pages = []
    seen = set()
    while True:
        page_budget = budget(len(pages)) if callable(budget) else budget
        result = read(path, mode, budget=page_budget, accepts=accepts, **kwargs)
        body = json.loads(result.text)
        assert body["frames"] and result.images
        assert [frame["n"] for frame in body["frames"]] == list(range(1, len(body["frames"]) + 1))
        pages.append((result, body))
        if body["next"] is None:
            break
        assert not body["complete"]
        assert body["next"]["tool"] == "read_video_frames"
        arguments = body["next"]["arguments"]
        assert arguments["target"] == {"kind": "PATH", "path": str(path)}
        assert [arguments["from"], arguments["to"]] == body["requested_range"]
        cursor = json.dumps(arguments["continuation"], sort_keys=True)
        assert cursor not in seen, "A successful source page must advance its continuation"
        seen.add(cursor)
        mode = arguments["mode"]
        kwargs = {"start": arguments["from"], "end": arguments["to"],
                  "continuation": arguments["continuation"]}
        if "crop" in arguments:
            kwargs["crop"] = arguments["crop"]
    return pages


def frame_colors(result, body):
    images = [Image.open(io.BytesIO(image.data)).convert("RGB") for image in result.images]
    colors = []
    for frame in body["frames"]:
        image = images[frame["image"] - 1]
        left, top, right, bottom = frame["cell_rect"]
        assert 0 <= left < right <= 1 and 0 <= top < bottom <= 1
        colors.append(image.getpixel((round((left + right) * image.width / 2),
                                      round((top + bottom) * image.height / 2))))
    return colors


def test_real_aba_images_and_time_facts(tmp_path):
    path = video(tmp_path / "aba.mkv", ["red"] * 4 + ["blue"] * 4 + ["red"] * 12)
    pages = read_pages(path, "motion")
    observed = []
    colors = []
    for result, body in pages:
        assert body["sampling"] == "ordered"
        times = [frame["t"] for frame in body["frames"]]
        assert [frame["dt"] for frame in body["frames"][1:]] == pytest.approx(
            [after - before for before, after in zip(times, times[1:])])
        observed.extend(times)
        colors.extend(frame_colors(result, body))
    assert observed == pytest.approx([i / 100 for i in range(20)])
    assert colors[4][2] > 220 and colors[0][0] > 220 and colors[8][0] > 220
    assert pages[-1][1]["complete"] is True


def test_motion_fixed_analysis_window_is_independent_of_page_budget_and_cancel(tmp_path):
    path = video(tmp_path / "long.mkv", ["red"] * 260)
    assert FRAME_ANALYSIS_FRAMES == 256
    with MediaSource.open(path) as source:
        info = probe_media(source)
        plan = prepare_frame_plan(source, info, start=0, end=info.duration, mode="motion")
    assert plan.observation_count == FRAME_ANALYSIS_FRAMES
    assert plan.processed_range == pytest.approx((0, 2.56))
    assert plan.next_window_start is not None
    assert plan.next_window_start.absolute_time == Fraction(256, 100)
    for pixels, count in ((320 * 180, 1), (2 * 320 * 180, 2)):
        body = json.loads(read(path, "motion", budget=FrameBudget(
            1_000_000, pixels, 1, FRAME_ANALYSIS_FRAMES)).text)
        assert len(body["frames"]) == count
        assert body["processed_range"] == pytest.approx(plan.processed_range)
        assert body["plan_delivery"]["observations"] == FRAME_ANALYSIS_FRAMES
        arguments = body["next"]["arguments"]
        assert arguments["from"] == 0 and arguments["to"] == pytest.approx(2.6)
        assert arguments["continuation"]["position"] == {
            "next_observation": count, "window_start": None}
    # Insufficient analysis working-set admission cannot silently select a
    # shorter plan and then make continuation depend on the page allocation.
    with pytest.raises(MediaError) as error:
        read(path, "motion", budget=FrameBudget(1_000_000, 1280 * 720, 2, 5))
    assert error.value.code == "RESOURCE_BOUNDARY"
    with pytest.raises(InterruptedError):
        read(path, "motion", cancelled=lambda: True)


def test_overview_full_range_sparse_not_prefix(tmp_path):
    path = video(tmp_path / "long.mkv", ["red"] * 50 + ["blue"] * 50)
    result = read(path)
    body = json.loads(result.text)
    assert body["processed_range"] == pytest.approx([0, 1])
    assert body["frames"][0]["t"] == 0
    assert body["frames"][-1]["t"] == pytest.approx(0.99)
    assert body["sampling"] == "sparse"
    assert "无音轨" in body["metadata"] and "无文字字幕" in body["metadata"]
    assert body["scanned_ranges"] == [] and body["observed_cut_times"] == []
    assert all(0 <= v <= 1 for f in body["frames"] for v in f["cell_rect"])


def test_detail_real_endpoints_crop_and_single_frame(tmp_path):
    path = video(
        tmp_path / "compare.mkv", ["red"] * 10 + ["blue"] * 10 + ["green"] * 10
    )
    pages = read_pages(path, "detail", crop=(0.25, 0.25, 0.75, 0.75),
                       budget=FrameBudget(4_000_000, 1280 * 720, 1, FRAME_ANALYSIS_FRAMES))
    times = []
    for result, body in pages:
        assert body["crop"] == [0.25, 0.25, 0.75, 0.75]
        assert "metadata" not in body
        assert len(result.images) == 1
        assert result.images[0].width == 1280 and result.images[0].height == 720
        times.extend(frame["t"] for frame in body["frames"])
    assert sorted(times) == pytest.approx([0, 0.15, 0.29])
    assert times[:2] == pytest.approx([0, 0.29])
    assert pages[-1][1]["complete"]
    single = video(tmp_path / "single.mkv", ["red"])
    assert len(json.loads(read(single, "detail").text)["frames"]) == 1


def test_range_before_first_video_picture_does_not_deliver_later_frame(tmp_path):
    from dataclasses import replace

    path = video(tmp_path / "delayed-track.mkv", ["red", "blue"], pts=[100, 110])
    with MediaSource.open(path) as source:
        # Simulate an earlier audio stream setting the common presentation origin.
        info = replace(
            probe_media(source), timeline_origin=0, duration=2,
            video_start=Fraction(1), video_end=Fraction(6, 5),
        )
        for mode in ("overview", "detail", "motion"):
            with pytest.raises(MediaError) as error:
                read_video_frames(
                    source,
                    info,
                    start=0,
                    end=0.5,
                    mode=mode,
                    budget=FrameBudget(1_000_000, 1280 * 720, 2, FRAME_ANALYSIS_FRAMES),
                )
            assert error.value.code == "NO_VIDEO_IN_RANGE"


def test_real_vfr_and_visible_boundary(tmp_path):
    path = video(
        tmp_path / "vfr.mkv", ["red", "blue", "red", "green"], pts=[0, 4, 11, 19]
    )
    result = read(path, "motion", start=0.02, end=0.2)
    body = json.loads(result.text)
    assert [f["t"] for f in body["frames"]] == pytest.approx([0, 0.04, 0.11, 0.19])
    assert body["frames"][0]["visible_range"] == pytest.approx([0.02, 0.04])
    assert [f["dt"] for f in body["frames"][1:]] == pytest.approx([0.04, 0.07, 0.08])


def test_union_quote_pages_fixed_sequence_without_dropping_observations(tmp_path):
    path = video(tmp_path / "quote.mkv", ["red"] * 20)
    attempts = []

    def accepts(result):
        body = json.loads(result.text)
        attempts.append(len(body["frames"]))
        return len(body["frames"]) <= 3

    pages = read_pages(path, "motion", accepts=accepts)
    assert max(attempts) > 3
    assert all(len(body["frames"]) <= 3 for _, body in pages)
    times = [frame["t"] for _, body in pages for frame in body["frames"]]
    assert times == pytest.approx([i / 100 for i in range(20)])
    assert pages[-1][1]["complete"]
    with pytest.raises(MediaError, match="fit") as error:
        read(path, "motion", accepts=lambda result: False)
    assert error.value.code == "RESOURCE_BOUNDARY"


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


def test_dense_motion_changing_page_budgets_preserve_original_query_and_all_frames(tmp_path):
    states = ["red", "blue"] * 15
    path = video(tmp_path / "dense.mkv", states)
    def budget(page):
        return FrameBudget(1_000_000, (2 + page % 2) * 320 * 180, 1, FRAME_ANALYSIS_FRAMES)

    end = len(states) / 100
    crop = [0.0, 0.0, 1.0, 1.0]
    observed = []
    pages = read_pages(path, "motion", start=0, end=end, budget=budget, crop=crop)
    for page, (_, body) in enumerate(pages):
        assert body["requested_range"] == [0, end]
        assert body["processed_range"] == [0, end]
        assert body["scanned_ranges"] == [[0, end]]
        assert all(0 <= t < end for t in body["observed_cut_times"])
        assert body["plan_delivery"]["position"] == len(observed)
        observed.extend(frame["t"] for frame in body["frames"])
        assert len(body["frames"]) <= 2 + page % 2
        if body["next"] is not None:
            arguments = body["next"]["arguments"]
            assert arguments["from"] == 0 and arguments["to"] == end
            assert arguments["mode"] == "motion" and arguments["crop"] == crop
            assert arguments["continuation"]["position"] == {
                "next_observation": len(observed), "window_start": None}
    assert pages[-1][1]["complete"]
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
    with MediaSource.open(path) as source:
        info = probe_media(source)
        plan = prepare_frame_plan(source, info, start=0, end=info.duration, mode="detail")
    assert plan.observation_count > 3
    pages = read_pages(path, "detail", budget=FrameBudget(
        8_000_000, 1280 * 720 * 4, 12, FRAME_ANALYSIS_FRAMES))
    times = [frame["t"] for _, body in pages for frame in body["frames"]]
    assert len(times) == len(set(times)) == plan.observation_count
    assert sorted(times) == pytest.approx(sorted(float(observation.t) for observation in plan.priority))
    assert min(times) == 0 and max(times) == pytest.approx(0.99)


@pytest.mark.parametrize("mode", ["overview", "detail", "motion"])
def test_range_published_before_raster_analysis(tmp_path, monkeypatch, mode):
    from pulsara_agent.media import frames_decode

    path = video(tmp_path / "progress.mkv", ["red"] * 30)
    events = []
    display = frames_decode.display_video_frame

    def record_display(*args):
        events.append("display")
        return display(*args)

    monkeypatch.setattr(frames_decode, "display_video_frame", record_display)
    result = read(path, mode, budget=FrameBudget(1_000_000, 1280 * 720, 3, FRAME_ANALYSIS_FRAMES),
                  progress=events.append)
    first_end = 0.01 if mode == "motion" else 0.3
    assert events[0] == pytest.approx((0, first_end))
    assert events[1] == "display"
    assert result.processed_range == pytest.approx((0, 0.3))


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


def test_aba_three_cell_pages_never_remove_the_excursion_or_return(tmp_path):
    path = video(tmp_path / "three-cell.mkv", ["red"] * 4 + ["blue"] * 4 + ["red"] * 12)
    pixels = 3 * 320 * 180
    pages = read_pages(path, "motion", budget=FrameBudget(
        1_000_000, pixels, 1, FRAME_ANALYSIS_FRAMES))
    times, colors = [], []
    for result, body in pages:
        assert 1 <= len(body["frames"]) <= 3
        assert sum(image.width * image.height for image in result.images) <= pixels
        assert body["media_version"]["size"] == path.stat().st_size
        assert body["media_version"]["modified_ns"] == str(path.stat().st_mtime_ns)
        times.extend(frame["t"] for frame in body["frames"])
        colors.extend(frame_colors(result, body))
    assert times == pytest.approx([i / 100 for i in range(20)])
    assert colors[3][0] > 220 and colors[4][2] > 220 and colors[8][0] > 220
    assert pages[-1][1]["complete"]
    with pytest.raises(MediaError) as error:
        read(path, "motion", budget=FrameBudget(
            1_000_000, 320 * 180 - 1, 1, FRAME_ANALYSIS_FRAMES))
    assert error.value.code == "RESOURCE_BOUNDARY"


def test_detail_clarity_search_stays_near_each_endpoint(tmp_path):
    sharp = Image.new("RGB", (320, 180), "white")
    ImageDraw.Draw(sharp).text((10, 30), "A", fill="black")
    blur = sharp.filter(ImageFilter.GaussianBlur(1))
    states = [blur] + [sharp] * 18 + [blur]
    path = video(tmp_path / "clarity.mkv", states)
    pages = read_pages(path, "detail", budget=FrameBudget(
        1_000_000, 1280 * 720, 1, FRAME_ANALYSIS_FRAMES))
    times = sorted(frame["t"] for _, body in pages for frame in body["frames"])
    assert times[0] == pytest.approx(0.01)
    assert times[-1] == pytest.approx(0.18)
    assert len(times) == 3
