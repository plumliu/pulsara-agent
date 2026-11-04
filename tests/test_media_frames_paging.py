"""Real-source regressions for frozen multimodal frame pages.

The assertions concern delivered evidence, source/query binding and progress;
they do not reproduce the selector or the page renderer as a test oracle.
"""

from fractions import Fraction
import copy
import io
import json
from pathlib import Path

import av
from PIL import Image, ImageDraw
import pytest

from pulsara_agent.media import frames
from pulsara_agent.media.source import MediaError, MediaSource, probe_media


def make_video(path: Path, colors, *, rate=10, size=(320, 180)):
    with av.open(str(path), "w") as container:
        stream = container.add_stream("ffv1", rate=rate)
        stream.width, stream.height = size
        stream.pix_fmt = "bgr0"
        stream.time_base = Fraction(1, rate)
        for i, color in enumerate(colors):
            image = color if isinstance(color, Image.Image) else Image.new("RGB", size, color)
            frame = av.VideoFrame.from_image(image)
            frame.pts, frame.time_base = i, Fraction(1, rate)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return path


def budget(*, pixels=8 * 1280 * 720, images=16, encoded_bytes=8_000_000):
    return frames.FrameBudget(
        encoded_bytes, pixels, images, frames.FRAME_ANALYSIS_FRAMES
    )


def read(path, *, mode="overview", frame_budget=None, arguments=None, accepts=None):
    arguments = dict(arguments or {})
    with MediaSource.open(path) as source:
        info = probe_media(source)
        return frames.read_video_frames(
            source,
            info,
            start=arguments.get("from", 0),
            end=arguments.get("to", info.duration),
            mode=arguments.get("mode", mode),
            crop=arguments.get("crop"),
            continuation=arguments.get("continuation"),
            budget=frame_budget or budget(),
            accepts=accepts,
        )


def assert_image_index_consistent(result):
    body = json.loads(result.text)
    images = [Image.open(io.BytesIO(image.data)).convert("RGB") for image in result.images]
    assert images and body["frames"]
    assert [frame["n"] for frame in body["frames"]] == list(range(1, len(body["frames"]) + 1))
    times = [frame["t"] for frame in body["frames"]]
    assert times == sorted(times)
    assert len(times) == len(set(times))
    assert {frame["image"] for frame in body["frames"]} == set(range(1, len(images) + 1))
    rects = []
    for frame in body["frames"]:
        image = images[frame["image"] - 1]
        left, top, right, bottom = frame["cell_rect"]
        assert 0 <= left < right <= 1 and 0 <= top < bottom <= 1
        rect = tuple(round(value * size) for value, size in zip(
            (left, top, right, bottom),
            (image.width, image.height, image.width, image.height),
        ))
        assert rect[2] > rect[0] and rect[3] > rect[1]
        for other_page, (ol, ot, oright, obottom) in rects:
            if other_page == frame["image"]:
                assert rect[2] <= ol or oright <= rect[0] or rect[3] <= ot or obottom <= rect[1]
        rects.append((frame["image"], rect))
    return body, images


@pytest.mark.parametrize("mode", ["overview", "detail", "motion"])
def test_changing_page_allowances_deliver_the_same_real_observations_once(tmp_path, mode):
    colors = [(i * 47 % 256, i * 83 % 256, i * 131 % 256) for i in range(45)]
    path = make_video(tmp_path / f"{mode}.mkv", colors)
    with MediaSource.open(path) as source:
        info = probe_media(source)
        plan = frames.prepare_frame_plan(source, info, start=0, end=info.duration, mode=mode)
    expected = {float(observation.t) for observation in plan.priority}
    expected_ranges = {
        float(observation.t): [float(value) for value in observation.representative_range]
        if observation.representative_range is not None else None
        for observation in plan.priority
    }
    full = read(path, mode=mode)
    whole, _ = assert_image_index_consistent(full)
    assert {frame["t"] for frame in whole["frames"]}.issubset(expected)
    assert len(expected) > 2
    arguments = {"mode": mode}
    observed = []
    positions = []
    continuation_values = []
    page_number = 0
    while True:
        allowance = (1, 3, 2)[page_number % 3]
        page = read(
            path,
            mode=mode,
            arguments=arguments,
            frame_budget=budget(pixels=(1 + page_number % 2) * 1280 * 720),
            accepts=lambda result: len(json.loads(result.text)["frames"]) <= allowance,
        )
        body, _ = assert_image_index_consistent(page)
        delivered = [frame["t"] for frame in body["frames"]]
        assert len(delivered) <= allowance
        assert set(delivered).issubset(expected)
        assert not set(delivered).intersection(observed)
        for frame in body["frames"]:
            assert frame.get("representative_range") == expected_ranges[frame["t"]]
            positions.append(frame["observation_position"])
        observed.extend(delivered)
        page_number += 1
        if body["next"] is None:
            assert body["complete"] is True
            break
        assert body["complete"] is False
        next_call = body["next"]
        assert next_call["tool"] == "read_video_frames"
        assert next_call["arguments"]["mode"] == mode
        assert next_call["arguments"]["from"] == 0
        assert next_call["arguments"]["to"] == pytest.approx(4.5)
        cursor = next_call["arguments"]["continuation"]
        assert cursor not in continuation_values
        continuation_values.append(cursor)
        arguments = next_call["arguments"]
    assert set(observed) == expected
    assert len(observed) == len(expected)
    assert sorted(positions) == list(range(len(expected)))


def test_overview_priority_pages_keep_endpoints_then_add_earlier_evidence(tmp_path):
    colors = [(i * 47 % 256, i * 83 % 256, i * 131 % 256) for i in range(60)]
    path = make_video(tmp_path / "map.mkv", colors)
    first = read(path, accepts=lambda result: len(json.loads(result.text)["frames"]) <= 2)
    body, _ = assert_image_index_consistent(first)
    assert [frame["t"] for frame in body["frames"]] == pytest.approx([0, 5.9])
    assert body["complete"] is False and body["next"] is not None
    second = read(path, arguments=body["next"]["arguments"],
                  accepts=lambda result: len(json.loads(result.text)["frames"]) <= 2)
    later, _ = assert_image_index_consistent(second)
    assert all(0 < frame["t"] < 5.9 for frame in later["frames"])
    assert later["frames"][0]["t"] < body["frames"][-1]["t"]


def test_observed_motion_round_trip_can_cross_single_frame_pages(tmp_path):
    path = make_video(tmp_path / "aba.mkv", ["red", "blue", "red"])
    arguments = {"mode": "motion"}
    observations = []
    while True:
        result = read(path, arguments=arguments,
                      accepts=lambda page: len(json.loads(page.text)["frames"]) == 1)
        body, images = assert_image_index_consistent(result)
        frame = body["frames"][0]
        image = images[frame["image"] - 1]
        left, top, right, bottom = frame["cell_rect"]
        color = image.getpixel((round((left + right) * image.width / 2),
                                round((top + bottom) * image.height / 2)))
        observations.append((frame["t"], color))
        if body["next"] is None:
            assert body["complete"]
            break
        assert not body["complete"]
        arguments = body["next"]["arguments"]
    assert [time for time, _ in observations] == pytest.approx([0, 0.1, 0.2])
    assert observations[0][1][0] > 220
    assert observations[1][1][2] > 220
    assert observations[2][1][0] > 220


def test_quote_retries_reuse_one_plan_and_never_accept_an_empty_page(tmp_path, monkeypatch):
    path = make_video(tmp_path / "retry.mkv", ["red", "blue", "green", "white"] * 10)
    prepare = frames.prepare_frame_plan
    calls = []

    def record_prepare(*args, **kwargs):
        calls.append((args, kwargs))
        return prepare(*args, **kwargs)

    monkeypatch.setattr(frames, "prepare_frame_plan", record_prepare)
    attempts = []

    def accepts(result):
        body, _ = assert_image_index_consistent(result)
        attempts.append(len(body["frames"]))
        return len(body["frames"]) == 1

    result = read(path, accepts=accepts)
    body, _ = assert_image_index_consistent(result)
    assert len(calls) == 1
    assert max(attempts) > 1 and min(attempts) == 1
    assert not body["complete"] and body["next"] is not None
    calls.clear()
    with pytest.raises(MediaError) as error:
        read(path, accepts=lambda result: False)
    assert error.value.code == "RESOURCE_BOUNDARY"
    assert len(calls) == 1


def test_prepared_source_cannot_change_before_render(tmp_path):
    path = make_video(tmp_path / "changed.mkv", ["red", "blue", "green"])
    with MediaSource.open(path) as source:
        info = probe_media(source)
        plan = frames.prepare_frame_plan(source, info, start=0, end=info.duration)
        replacement = make_video(tmp_path / "replacement.mkv", ["white", "black", "white"])
        replacement.replace(path)
        with pytest.raises(MediaError) as error:
            frames.render_frame_page(source, info, plan, offset=0, count=1, budget=budget())
        assert error.value.code == "MEDIA_SOURCE_CHANGED"


def test_cancellation_does_not_return_a_prepared_or_rendered_success(tmp_path):
    path = make_video(tmp_path / "cancelled.mkv", ["red", "blue", "green"])
    with MediaSource.open(path) as source:
        info = probe_media(source)
        with pytest.raises(InterruptedError):
            frames.prepare_frame_plan(source, info, start=0, end=info.duration,
                                      cancelled=lambda: True)
        plan = frames.prepare_frame_plan(source, info, start=0, end=info.duration)
        with pytest.raises(InterruptedError):
            frames.render_frame_page(source, info, plan, offset=0, count=1,
                                     budget=budget(), cancelled=lambda: True)


def test_two_geometry_tiers_only_and_floor_failure_is_a_boundary(tmp_path):
    path = make_video(tmp_path / "floor.mkv", ["red", "blue", "green"])
    attempts = []

    def accepts(result):
        body, images = assert_image_index_consistent(result)
        if len(body["frames"]) != 1:
            return False
        image = images[0]
        attempts.append((image.width, image.height))
        return image.width <= 160 and image.height <= 90

    result = read(path, accepts=accepts)
    body, _ = assert_image_index_consistent(result)
    assert len(attempts) == 2
    assert attempts[0][0] >= 320 and attempts[0][1] >= 180
    assert attempts[-1] == (160, 90)
    assert not body["complete"] and body["next"] is not None
    assert any("readab" in item.lower() or "floor" in item.lower() for item in body["limitations"])
    with pytest.raises(MediaError) as error:
        read(path, frame_budget=budget(pixels=160 * 90 - 1))
    assert error.value.code == "RESOURCE_BOUNDARY"


def test_single_frame_detail_page_preserves_spatial_clarity_when_allowance_fits(tmp_path):
    path = make_video(tmp_path / "clear-detail.mkv", ["red", "blue", "green"], size=(1280, 720))
    result = read(path, mode="detail",
                  accepts=lambda page: len(json.loads(page.text)["frames"]) == 1)
    body, images = assert_image_index_consistent(result)
    assert len(images) == 1
    assert images[0].size == (1280, 720)
    assert body["next"] is not None and not body["complete"]
    assert not any("floor" in limitation.lower() for limitation in body["limitations"])


def test_detail_does_not_fall_back_to_scene_floor_when_clear_evidence_cannot_fit(tmp_path):
    path = make_video(tmp_path / "detail-boundary.mkv", ["red", "blue", "green"], size=(1280, 720))
    with pytest.raises(MediaError) as error:
        read(path, mode="detail", frame_budget=budget(pixels=160 * 90))
    assert error.value.code == "RESOURCE_BOUNDARY"


def test_detail_page_reduces_count_instead_of_shrinking_clear_source_images(tmp_path):
    path = make_video(tmp_path / "clear-pair.mkv", ["red", "blue", "green"], size=(1280, 720))
    result = read(path, mode="detail", frame_budget=budget(pixels=2 * 1280 * 720))
    body, images = assert_image_index_consistent(result)
    assert len(images) == 2
    assert all(image.size == (1280, 720) for image in images)
    assert len(body["frames"]) == 2
    assert body["next"] is not None and not body["complete"]


def test_too_few_analysis_slots_refuse_the_fixed_plan_instead_of_reselecting(tmp_path):
    path = make_video(tmp_path / "analysis-boundary.mkv", ["red", "blue", "green"])
    tiny = frames.FrameBudget(8_000_000, 1280 * 720, 4, 2)
    with pytest.raises(MediaError) as error:
        read(path, frame_budget=tiny)
    assert error.value.code == "RESOURCE_BOUNDARY"


def test_cropped_evidence_and_query_remain_identical_across_pages(tmp_path):
    image = Image.new("RGB", (320, 180), "red")
    ImageDraw.Draw(image).rectangle((80, 45, 239, 134), fill="blue")
    path = make_video(tmp_path / "cropped.mkv", [image] * 3)
    crop = [0.25, 0.25, 0.75, 0.75]
    arguments = {"crop": crop}
    observed = []
    while True:
        result = read(path, arguments=arguments,
                      accepts=lambda page: len(json.loads(page.text)["frames"]) == 1)
        body, images = assert_image_index_consistent(result)
        assert body["crop"] == crop
        for output in images:
            red, _, blue = output.getpixel((output.width * 3 // 4, output.height * 3 // 4))
            assert blue > 220 and red < 30
        observed.extend(frame["t"] for frame in body["frames"])
        if body["next"] is None:
            assert body["complete"]
            break
        assert body["next"]["arguments"]["crop"] == crop
        arguments = body["next"]["arguments"]
    assert observed == pytest.approx([0, 0.2])


@pytest.mark.parametrize("change", ["range", "mode", "policy", "ordinal"])
def test_real_continuation_refuses_a_changed_query_policy_or_invalid_position(tmp_path, change):
    path = make_video(tmp_path / "query.mkv", ["red", "blue", "green"])
    first = read(path, accepts=lambda page: len(json.loads(page.text)["frames"]) == 1)
    arguments = copy.deepcopy(json.loads(first.text)["next"]["arguments"])
    if change == "range":
        arguments["to"] = 0.2
    elif change == "mode":
        arguments["mode"] = "detail"
    elif change == "policy":
        arguments["continuation"]["selection_revision"] = "unavailable-policy"
    else:
        arguments["continuation"]["position"]["next_observation"] = 999
    with pytest.raises(MediaError) as error:
        read(path, arguments=arguments)
    assert error.value.code == (
        "FRAME_CONTINUATION_UNAVAILABLE" if change == "policy" else "INVALID_ARGUMENTS"
    )


def test_real_continuation_rejects_replaced_source_and_does_not_consume_position(tmp_path):
    path = make_video(tmp_path / "source.mkv", ["red", "blue", "green"])
    first = read(path, accepts=lambda page: len(json.loads(page.text)["frames"]) == 1)
    arguments = json.loads(first.text)["next"]["arguments"]
    again = read(path, arguments=arguments,
                 accepts=lambda page: len(json.loads(page.text)["frames"]) == 1)
    repeated = read(path, arguments=arguments,
                    accepts=lambda page: len(json.loads(page.text)["frames"]) == 1)
    left, right = json.loads(again.text), json.loads(repeated.text)
    assert left["frames"] == right["frames"]
    assert left["next"] == right["next"]
    make_video(tmp_path / "new-source.mkv", ["white", "black", "white"]).replace(path)
    with pytest.raises(MediaError) as error:
        read(path, arguments=arguments)
    assert error.value.code == "MEDIA_SOURCE_CHANGED"


def test_motion_final_page_advances_to_the_exact_next_native_window(tmp_path):
    from pulsara_agent.media.frames_paging import (
        FramePagePosition, with_frame_page_continuation,
    )

    count = frames.FRAME_ANALYSIS_FRAMES
    path = make_video(tmp_path / "windows.mkv", ["red"] * count + ["blue"], rate=100)
    with MediaSource.open(path) as source:
        info = probe_media(source)
        plan = frames.prepare_frame_plan(source, info, start=0, end=info.duration, mode="motion")
        assert plan.observation_count == count
        assert plan.next_window_start is not None and not plan.complete
        last = frames.render_frame_page(source, info, plan, offset=count - 1, count=1, budget=budget())
        final_page = with_frame_page_continuation(
            last, source=source, info=info, plan=plan,
            position=FramePagePosition(count - 1), count=1,
            selection_revision=frames.FRAME_SELECTION_REVISION,
        )
    body, _ = assert_image_index_consistent(final_page)
    assert body["frames"][0]["t"] == pytest.approx((count - 1) / 100)
    assert not body["complete"]
    arguments = body["next"]["arguments"]
    position = arguments["continuation"]["position"]
    assert position["next_observation"] == 0
    assert position["window_start"] == {
        "pts": plan.next_window_start.pts,
        "time_base": [plan.next_window_start.time_base.numerator,
                      plan.next_window_start.time_base.denominator],
    }
    following = read(path, arguments=arguments)
    final, images = assert_image_index_consistent(following)
    assert [frame["t"] for frame in final["frames"]] == pytest.approx([count / 100])
    assert final["complete"] and final["next"] is None
    assert images[0].getpixel((images[0].width // 2, images[0].height // 2))[2] > 220
