"""Physical-frame adapter boundaries beyond the independent codec oracles."""

from contextlib import contextmanager
from fractions import Fraction

import av
from PIL import Image
import pytest

from pulsara_agent.media import frames_decode
from pulsara_agent.media.frames_decode import FrameTarget, VideoDecoder
from pulsara_agent.media.source import FramePosition, MediaError, MediaSource, probe_media


def _video(path, stamps, *, codec="ffv1"):
    with av.open(str(path), "w") as container:
        options = {"x264-params": "keyint=24:min-keyint=24:scenecut=0:bframes=3"} if codec == "libx264" else {}
        stream = container.add_stream(codec, rate=100, options=options)
        stream.width, stream.height = 160, 90
        stream.pix_fmt = "yuv420p" if codec == "libx264" else "bgr0"
        stream.time_base = Fraction(1, 100)
        for index, stamp in enumerate(stamps):
            frame = av.VideoFrame.from_image(Image.new("RGB", (160, 90), (index * 7 % 256, 20, 30)))
            frame.pts, frame.time_base = stamp, Fraction(1, 100)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return path


def test_bounded_scan_preserves_boundary_predecessor_and_exact_frontier(tmp_path):
    path = _video(tmp_path / "vfr.mkv", [100, 104, 111, 119])
    with MediaSource.open(path) as source:
        info = probe_media(source)
        with VideoDecoder(source, info) as decoder:
            first = list(decoder.scan(Fraction(2, 100), Fraction(1, 5), 2))
        assert [frame.t for frame in first] == [0, Fraction(4, 100)]
        frontier = first[-1].next_position
        assert frontier is not None
        assert frontier.absolute_time - info.timeline_origin == Fraction(11, 100)
        with VideoDecoder(source, info) as decoder:
            continued = list(decoder.scan(frontier.absolute_time - info.timeline_origin, Fraction(1, 5), 2))
        assert [frame.t for frame in continued] == [Fraction(11, 100), Fraction(19, 100)]
        assert continued[-1].next_position is None


def test_strict_before_first_frame_does_not_substitute_first_frame(tmp_path):
    path = _video(tmp_path / "first.mkv", [100, 104])
    with MediaSource.open(path) as source:
        info = probe_media(source)
        with VideoDecoder(source, info) as decoder:
            with pytest.raises(MediaError) as exc:
                next(decoder.visible_many([FrameTarget(Fraction(0), before=True)]))
        assert exc.value.code == "NO_VIDEO_IN_RANGE"


def test_dependency_frames_are_not_rasterized_as_requested_observations(tmp_path, monkeypatch):
    path = _video(tmp_path / "dependencies.mp4", range(50), codec="libx264")
    display = frames_decode.display_video_frame
    converted = []

    def observe(frame, info):
        converted.append(FramePosition.from_frame(frame))
        return display(frame, info)

    with MediaSource.open(path) as source:
        info = probe_media(source)
        monkeypatch.setattr(frames_decode, "display_video_frame", observe)
        with VideoDecoder(source, info) as decoder:
            selected = list(decoder.visible_many([Fraction(13, 100), Fraction(14, 100)]))
            native_work = decoder.decoded_frames
        assert converted == [frame.position for frame in selected]
        assert native_work > len(converted) == 2


def test_unknown_terminal_duration_never_becomes_held_coverage(tmp_path, monkeypatch):
    path = _video(tmp_path / "unknown-end.mkv", [0, 4])
    original = MediaSource.open_container
    with MediaSource.open(path) as source:
        info = probe_media(source)
        assert info.video_end is None

        @contextmanager
        def without_duration(bound):
            with original(bound) as container:
                class NoDuration:
                    def __getattr__(self, name):
                        return getattr(container, name)

                    def decode(self, *args, **kwargs):
                        for frame in container.decode(*args, **kwargs):
                            frame.duration = 0
                            yield frame
                yield NoDuration()

        monkeypatch.setattr(MediaSource, "open_container", without_duration)
        with VideoDecoder(source, info) as decoder:
            last = next(decoder.visible_many([Fraction(4, 100)]))
            assert last.end is None
        with VideoDecoder(source, info) as decoder:
            with pytest.raises(MediaError) as exc:
                list(decoder.scan(Fraction(0), Fraction(1), 10))
        assert exc.value.code == "MEDIA_TIMESTAMP_UNAVAILABLE"


def test_exact_source_version_is_invalidated_by_in_place_change(tmp_path):
    path = _video(tmp_path / "version.mkv", [0, 4])
    with MediaSource.open(path) as source:
        version = source.version
        assert version.size == source.size
        assert version.modified_ns == source.modified_ns
        assert version.dev == path.stat().st_dev and version.ino == path.stat().st_ino
        path.write_bytes(path.read_bytes() + b"changed")
        with pytest.raises(MediaError) as exc:
            _ = source.version
        assert exc.value.code == "MEDIA_SOURCE_CHANGED"


def test_native_position_requires_an_actual_source_boundary(tmp_path):
    path = _video(tmp_path / "boundary.mkv", [100, 104, 111])
    with MediaSource.open(path) as source:
        info = probe_media(source)
        interior = FramePosition(1020, Fraction(1, 1000))
        with VideoDecoder(source, info) as decoder:
            with pytest.raises(MediaError) as exc:
                next(decoder.visible_many([interior]))
        assert exc.value.code == "MEDIA_FRAME_UNAVAILABLE"
        with VideoDecoder(source, info) as decoder:
            with pytest.raises(MediaError) as exc:
                next(decoder.scan(interior, Fraction(1, 5), 1))
        assert exc.value.code == "MEDIA_FRAME_UNAVAILABLE"


def test_native_seek_timestamp_overflow_is_a_typed_protocol_boundary(tmp_path):
    path = _video(tmp_path / "overflow.mkv", [0, 4])
    with MediaSource.open(path) as source:
        info = probe_media(source)
        with VideoDecoder(source, info) as decoder:
            with pytest.raises(MediaError) as exc:
                next(decoder.visible_many([Fraction(10**50)]))
        assert exc.value.code == "RESOURCE_BOUNDARY"
        assert "native timestamp" in str(exc.value)
