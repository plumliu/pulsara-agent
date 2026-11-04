"""Detail sharing preserves source evidence and admits actual unique pictures."""

from dataclasses import replace
from fractions import Fraction
import io
from pathlib import Path
import weakref

import av
from PIL import Image, ImageDraw
import pytest

from pulsara_agent.media.frames import (
    FRAME_ANALYSIS_FRAMES,
    FrameBudget,
    prepare_frame_plan,
    read_video_frames,
)
from pulsara_agent.media.frames_detail_render import render_detail_images
from pulsara_agent.media import frames_detail_render
from pulsara_agent.media.source import MediaError, MediaSource, probe_media


def video(path: Path, states, *, size=(320, 180)):
    with av.open(str(path), "w") as container:
        stream = container.add_stream("ffv1", rate=2)
        stream.width, stream.height = size
        stream.pix_fmt = "bgr0"
        stream.time_base = Fraction(1, 2)
        for ordinal, state in enumerate(states):
            image = state if isinstance(state, Image.Image) else Image.new("RGB", size, state)
            frame = av.VideoFrame.from_image(image)
            frame.pts, frame.time_base = ordinal, Fraction(1, 2)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return path


def render(path, *, crop=None, images=8, pixels=8 * 1280 * 720, encoded_bytes=8_000_000,
           requested_range=None):
    with MediaSource.open(path) as source:
        info = probe_media(source)
        # Motion supplies all source observations, independently of detail's
        # target-selection policy. Only the shared renderer is under test.
        plan = prepare_frame_plan(source, info, start=0, end=info.duration, mode="motion", crop=crop)
        plan = replace(plan, mode="detail", requested_range=requested_range or plan.requested_range)
        return render_detail_images(
            source, info, plan, plan.priority,
            budget=FrameBudget(encoded_bytes, pixels, images, FRAME_ANALYSIS_FRAMES),
        )


def test_identical_pixels_share_one_picture_without_dropping_distinct_times(tmp_path):
    path = video(tmp_path / "repeat.mkv", ["red", "red", "blue", "red"])
    images, index = render(path, images=2, pixels=2 * 1280 * 720)
    assert len(images) == 2
    assert [item["n"] for item in index] == [1, 2, 3, 4]
    assert [item["image"] for item in index] == [1, 1, 2, 1]
    assert [item["t"] for item in index] == [0, 0.5, 1, 1.5]
    assert [item["dt"] for item in index[1:]] == [0.5] * 3
    assert [item["visible_range"] for item in index] == [[0, 0.5], [0.5, 1], [1, 1.5], [1.5, 2]]
    assert all(item["cell_rect"] == [0, 0, 1, 1] for item in index)
    decoded = [Image.open(io.BytesIO(image.data)).convert("RGB") for image in images]
    assert all(image.size == (1280, 720) for image in decoded)
    assert decoded[0].getpixel((640, 360))[0] > 240
    assert decoded[1].getpixel((640, 360))[2] > 240


def test_exact_comparison_is_before_observation_number_labels(tmp_path):
    path = video(tmp_path / "numbering.mkv", ["red", "red", "blue", "blue"])
    images, index = render(path)
    assert [item["image"] for item in index] == [1, 1, 2, 2]
    # The second unique picture is labelled 2, even though its first source
    # observation is n=3. Compare its visible glyph to an independent label.
    expected = Image.new("RGB", (1280, 720), "blue")
    draw = ImageDraw.Draw(expected)
    draw.rectangle((0, 0, 19, 17), fill="black")
    draw.text((4, 3), "2", fill="white")
    buffer = io.BytesIO()
    expected.save(buffer, format="JPEG", quality=90)
    expected_patch = Image.open(io.BytesIO(buffer.getvalue())).crop((4, 5, 10, 12))
    actual_patch = Image.open(io.BytesIO(images[1].data)).crop((4, 5, 10, 12))
    assert actual_patch.tobytes() == expected_patch.tobytes()


def test_one_rgb_pixel_difference_is_not_approximately_shared(tmp_path):
    first = Image.new("RGB", (1280, 720), (100, 100, 100))
    second = first.copy()
    second.putpixel((640, 360), (100, 101, 100))
    images, index = render(video(tmp_path / "one-pixel.mkv", [first, second], size=first.size))
    assert len(images) == 2
    assert [item["image"] for item in index] == [1, 2]


def test_exact_sharing_compares_the_final_crop_not_uncropped_source(tmp_path):
    first = Image.new("RGB", (1280, 720), "blue")
    second = Image.new("RGB", first.size, "green")
    for image in (first, second):
        ImageDraw.Draw(image).rectangle((320, 180, 959, 539), fill="red")
    images, index = render(
        video(tmp_path / "crop.mkv", [first, second], size=first.size),
        crop=(0.25, 0.25, 0.75, 0.75), images=1, pixels=1280 * 720,
    )
    assert len(images) == 1
    assert [item["image"] for item in index] == [1, 1]
    assert (images[0].width, images[0].height) == (1280, 720)


@pytest.mark.parametrize("limit", ["images", "pixels"])
def test_actual_unique_picture_resources_return_the_admitted_prefix(tmp_path, limit):
    path = video(tmp_path / f"limit-{limit}.mkv", ["red", "blue"])
    options = {
        "images": {"images": 1},
        "pixels": {"pixels": 1280 * 720},
    }[limit]
    images, index = render(path, **options)
    assert len(images) == len(index) == 1
    assert index[0]["t"] == 0


@pytest.mark.parametrize("options", [{"pixels": 1280 * 720 - 1}, {"encoded_bytes": 1}])
def test_an_inadmissible_first_picture_is_a_resource_boundary(tmp_path, options):
    path = video(tmp_path / "atom-limit.mkv", ["red"])
    with pytest.raises(MediaError) as error:
        render(path, **options)
    assert error.value.code == "RESOURCE_BOUNDARY"


def test_many_observations_fit_one_unique_picture_budget(tmp_path):
    path = video(tmp_path / "one-picture.mkv", ["red"] * 25)
    images, index = render(path, images=1, pixels=1280 * 720)
    assert len(images) == 1
    assert len(index) == 25
    assert [item["n"] for item in index] == list(range(1, 26))
    assert [item["image"] for item in index] == [1] * 25


def test_source_rgb_rasters_are_released_before_the_next_observation(tmp_path, monkeypatch):
    decoder_owner = frames_detail_render.VideoDecoder
    observed = []

    class TrackingDecoder:
        def __init__(self, *args, **kwargs):
            self.owner = decoder_owner(*args, **kwargs)

        def __enter__(self):
            self.owner.__enter__()
            return self

        def __exit__(self, *args):
            return self.owner.__exit__(*args)

        def visible_many(self, targets):
            for visible in self.owner.visible_many(targets):
                observed.append(weakref.ref(visible.image))
                assert sum(reference() is not None for reference in observed) == 1
                yield visible

    path = video(tmp_path / "streamed.mkv", ["red"] * 25)
    monkeypatch.setattr(frames_detail_render, "VideoDecoder", TrackingDecoder)
    images, index = render(path, images=1, pixels=1280 * 720)
    assert len(images) == 1 and len(index) == len(observed) == 25
    assert all(reference() is None for reference in observed)


def test_256_distinct_observations_convert_only_the_unique_capacity_plus_one(tmp_path, monkeypatch):
    decoder_owner = frames_detail_render.VideoDecoder
    observed = []

    class CountingDecoder:
        def __init__(self, *args, **kwargs):
            self.owner = decoder_owner(*args, **kwargs)

        def __enter__(self):
            self.owner.__enter__()
            return self

        def __exit__(self, *args):
            return self.owner.__exit__(*args)

        def visible_many(self, targets):
            for visible in self.owner.visible_many(targets):
                observed.append(visible.t)
                yield visible

    path = video(tmp_path / "physical-prefix.mkv", [(i, i // 2, 255 - i) for i in range(256)])
    monkeypatch.setattr(frames_detail_render, "VideoDecoder", CountingDecoder)
    images, index = render(path, images=18, pixels=18 * 1280 * 720)
    assert len(images) == len(index) == 18
    assert observed == [Fraction(i, 2) for i in range(19)]
    assert [item["t"] for item in index] == [i / 2 for i in range(18)]


def test_encoded_byte_limit_keeps_the_source_and_unique_image_prefix(tmp_path):
    path = video(tmp_path / "byte-prefix.mkv", ["red", "red", "blue", "blue", "green"])
    full_images, _ = render(path)
    allowance = len(full_images[0].data) + len(full_images[1].data) - 1
    images, index = render(path, encoded_bytes=allowance)
    assert len(images) == 1
    assert images[0].data == full_images[0].data
    assert [(item["n"], item["image"], item["t"]) for item in index] == [(1, 1, 0), (2, 1, 0.5)]


def test_detail_visible_intervals_are_always_actual_request_intersections(tmp_path):
    path = video(tmp_path / "intersections.mkv", ["red", "red"])
    images, index = render(path, requested_range=(0.1, 0.9))
    assert len(images) == 1
    assert [item["t"] for item in index] == [0, 0.5]
    assert [item["visible_range"] for item in index] == [[0.1, 0.5], [0.5, 0.9]]


def test_detail_geometry_admits_many_observations_that_share_one_image(tmp_path):
    import json

    path = video(tmp_path / "shared-admission.mkv", ["red"] * 25)
    with MediaSource.open(path) as source:
        info = probe_media(source)
        result = read_video_frames(
            source, info, start=0, end=info.duration, mode="detail",
            budget=FrameBudget(1 << 20, 1280 * 720, 1, FRAME_ANALYSIS_FRAMES),
        )
    body = json.loads(result.text)
    assert len(result.images) == 1
    assert len(body["frames"]) == 25
    assert [item["observation_position"] for item in body["frames"]] == list(range(25))
    assert body["complete"] and body["next"] is None


def test_unique_image_boundaries_shrink_only_the_delivery_prefix(tmp_path):
    import json

    path = video(tmp_path / "shared-prefix.mkv", ["red", "red", "blue", "red"])
    delivered, image_counts, page_observations = [], [], []
    continuation = None
    with MediaSource.open(path) as source:
        info = probe_media(source)
        while True:
            result = read_video_frames(
                source, info, start=0, end=info.duration, mode="detail",
                continuation=continuation,
                budget=FrameBudget(1 << 20, 1280 * 720, 1, FRAME_ANALYSIS_FRAMES),
            )
            body = json.loads(result.text)
            image_counts.append(len(result.images))
            page_observations.append(len(body["frames"]))
            delivered.extend((item["t"], item["observation_position"]) for item in body["frames"])
            if body["next"] is None:
                assert body["complete"]
                break
            continuation = body["next"]["arguments"]["continuation"]
    assert page_observations == [2, 1, 1]
    assert image_counts == [1, 1, 1]
    assert delivered == [(0, 0), (0.5, 1), (1, 2), (1.5, 3)]
