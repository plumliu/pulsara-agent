from __future__ import annotations

import asyncio
from fractions import Fraction
from pathlib import Path
from time import monotonic
import wave

import av
from PIL import Image
import pytest

from pulsara_agent.media.audio import extract_audio
from pulsara_agent.media.source import (
    MediaError,
    MediaSource,
    probe_media,
    validate_range,
)
from pulsara_agent.media.subtitles import read_subtitles
from pulsara_agent.media.worker import MediaWorker


def wav(path: Path, *, amplitude: int = 0, opposite: bool = False):
    from array import array

    samples = array(
        "h",
        [
            value
            for _ in range(1600)
            for value in (amplitude, -amplitude if opposite else amplitude)
        ],
    )
    with wave.open(str(path), "wb") as target:
        target.setnchannels(2)
        target.setsampwidth(2)
        target.setframerate(16000)
        target.writeframes(samples.tobytes())


def test_descriptor_version_and_independent_container_positions(tmp_path):
    path = tmp_path / "media.wav"
    wav(path)
    with MediaSource.open(path) as source:
        with source.open_container() as first, source.open_container() as second:
            assert next(first.decode(audio=0)).pts == next(second.decode(audio=0)).pts
        original = path.with_suffix(".old")
        path.rename(original)
        wav(path, amplitude=20)
        with pytest.raises(MediaError, match="modified or replaced") as exc:
            source.assert_unchanged()
        assert exc.value.code == "MEDIA_SOURCE_CHANGED"


def test_no_symlink_resolution_at_bound_source_open(tmp_path):
    target = tmp_path / "media.wav"
    wav(target)
    alias = tmp_path / "alias.wav"
    alias.symlink_to(target)
    with pytest.raises(OSError):
        MediaSource.open(alias)


def test_vfr_real_pts_and_nonzero_start(tmp_path):
    path = tmp_path / "vfr.mkv"
    with av.open(str(path), "w") as container:
        stream = container.add_stream("ffv1", rate=25)
        stream.width, stream.height, stream.pix_fmt = 32, 16, "yuv420p"
        stream.time_base = Fraction(1, 1000)
        for pts, color in [(5000, "red"), (5040, "blue"), (5160, "red")]:
            frame = av.VideoFrame.from_image(Image.new("RGB", (32, 16), color))
            frame.pts, frame.time_base = pts, Fraction(1, 1000)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    with MediaSource.open(path) as source:
        info = probe_media(source)
        assert info.timeline_origin == 5
        assert info.duration == pytest.approx(0.2)
        with source.open_container() as container:
            times = [info.frame_time(frame) for frame in container.decode(video=0)]
        assert times == pytest.approx([0, 0.04, 0.16])


@pytest.mark.parametrize(
    "start,end", [(True, 1), ("0", 1), (0, float("nan")), (-1, 1), (1, 1), (0, 3)]
)
def test_invalid_ranges(start, end):
    with pytest.raises(MediaError) as exc:
        validate_range(start, end, 2)
    assert exc.value.code == "INVALID_RANGE"


def test_unknown_duration_explicit_end_and_annotation():
    assert validate_range(0, 10, None).end == 10
    with pytest.raises(MediaError) as exc:
        validate_range(0, 1, None, annotation=True)
    assert exc.value.code == "MEDIA_DURATION_UNKNOWN"


@pytest.mark.parametrize(
    "amplitude,opposite,state",
    [
        (0, False, "near_silent"),
        (40, False, "sound_present"),
        (1000, True, "sound_present"),
    ],
)
def test_audio_sound_before_downmix(tmp_path, amplitude, opposite, state):
    path = tmp_path / "media.wav"
    wav(path, amplitude=amplitude, opposite=opposite)
    with MediaSource.open(path) as source:
        info = probe_media(source)
        result = extract_audio(source, info, 0.01, 0.09, 10000)
    assert result.sound.state == state
    assert len(result.data) == 44 + 1280 * 2


def test_text_subtitles_ass_control_and_empty_range(tmp_path):
    path = tmp_path / "captions.srt"
    path.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\n<i>Hello</i> world\n\n2\n00:00:03,000 --> 00:00:04,000\nAgain\n"
    )
    with MediaSource.open(path) as source:
        info = probe_media(source)
        result = read_subtitles(source, info, 0, 2.5)
        assert result.usable
        assert [(e.start, e.end, e.text) for e in result.entries] == [
            (1, 2, "Hello world")
        ]
        empty = read_subtitles(source, info, 4, 5)
        assert empty.usable and not empty.entries


def test_worker_cancel_kills_and_reaps(tmp_path):
    path = tmp_path / "media.wav"
    wav(path)

    async def check():
        owner = MediaWorker()
        with MediaSource.open(path) as source:
            info = await owner.run(
                probe_media, source, deadline_monotonic=monotonic() + 10
            )
            assert info.audio_stream_index == 0
            task = asyncio.create_task(
                owner.run(probe_media, source, deadline_monotonic=monotonic() + 10)
            )
            async with asyncio.timeout(5):
                while not owner._processes:
                    await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not owner._processes
        await owner.aclose()
        assert owner.physical_close_complete

    asyncio.run(check())


def test_external_playlist_io_denied(tmp_path):
    path = tmp_path / "outside.wav"
    wav(path, amplitude=1000)
    playlist = tmp_path / "evil.m3u8"
    playlist.write_text(
        f"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:0.1,\n{path.as_uri()}\n#EXT-X-ENDLIST\n"
    )
    with MediaSource.open(playlist) as source:
        with pytest.raises(MediaError):
            probe_media(source)


def test_subtitle_bounded_utf8_fragments_preserve_source_positions(tmp_path):
    path = tmp_path / "captions.srt"
    original = "你好 world\nAgain"
    path.write_text(
        f"1\n00:00:01,000 --> 00:00:02,000\n{original}\n\n2\n00:00:03,000 --> 00:00:04,000\nRepeat\n"
    )
    with MediaSource.open(path) as source:
        info = probe_media(source)
        position, offset, parts = None, 0, []
        while True:
            result = read_subtitles(
                source,
                info,
                0,
                5,
                after_position=position,
                text_offset=offset,
                maximum_text_bytes=7,
            )
            assert sum(len(entry.text.encode()) for entry in result.entries) <= 7
            parts.extend((entry.position, entry.text) for entry in result.entries)
            if result.next_position is None:
                break
            assert (result.next_position, result.next_text_offset) != (position, offset)
            position, offset = result.next_position, result.next_text_offset
    assert "".join(text for pos, text in parts if pos == 0) == original
    assert "".join(text for pos, text in parts if pos == 1) == "Repeat"


def test_media_source_clone_keeps_descriptor_owned_until_last_close(tmp_path):
    path = tmp_path / "media.wav"
    wav(path)
    with MediaSource.open(path) as original:
        clone = original.clone()
    with clone:
        assert probe_media(clone).audio_stream_index == 0


def test_still_image_rejected(tmp_path):
    path = tmp_path / "picture.jpg"
    Image.new("RGB", (16, 16), "red").save(path)
    with MediaSource.open(path) as source:
        with pytest.raises(MediaError) as exc:
            probe_media(source)
    assert exc.value.code == "MEDIA_FORMAT_UNSUPPORTED"


def test_unknown_source_finite_range_is_not_defaulted():
    with pytest.raises(MediaError):
        validate_range(None, None, None)


def test_worker_deadline_kills_without_late_resource(tmp_path):
    path = tmp_path / "media.wav"
    wav(path)

    async def check():
        owner = MediaWorker()
        with MediaSource.open(path) as source:
            with pytest.raises(TimeoutError):
                await owner.run(
                    probe_media, source, deadline_monotonic=monotonic() + 0.001
                )
            assert not owner._processes
        await owner.aclose()

    asyncio.run(check())


def test_rotation_and_pixel_aspect_are_applied_before_crop(tmp_path):
    from pulsara_agent.media.source import display_video_frame

    path = tmp_path / "rotated.mp4"
    with av.open(str(path), "w") as container:
        stream = container.add_stream("libx264", rate=25)
        stream.width, stream.height, stream.pix_fmt = 32, 16, "yuv420p"
        stream.sample_aspect_ratio = Fraction(2)
        stream.set_display_rotation(90)
        frame = av.VideoFrame.from_image(Image.new("RGB", (32, 16), "red"))
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    with MediaSource.open(path) as source:
        info = probe_media(source)
        assert (info.width, info.height) == (16, 64)
        with source.open_container() as container:
            frame = next(container.decode(video=0))
            assert display_video_frame(frame, info).size == (16, 64)


def test_external_concat_and_network_playlist_blocked(tmp_path):
    paths = [
        ("local.ffconcat", "ffconcat version 1.0\nfile /etc/passwd\n"),
        (
            "network.m3u8",
            "#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\nhttp://127.0.0.1:1/secret.ts\n#EXT-X-ENDLIST\n",
        ),
    ]
    for name, content in paths:
        path = tmp_path / name
        path.write_text(content)
        with MediaSource.open(path) as source:
            with pytest.raises(MediaError):
                probe_media(source)


def test_default_audio_track_selected_independent_of_provider(tmp_path):
    from array import array

    path = tmp_path / "two-tracks.mkv"
    with av.open(str(path), "w") as container:
        tracks = [container.add_stream("pcm_s16le", rate=16000) for _ in range(2)]
        tracks[0].disposition = av.stream.Disposition(0)
        tracks[1].disposition = av.stream.Disposition.default
        for index, track in enumerate(tracks):
            track.layout = "mono"
            frame = av.AudioFrame(format="s16", layout="mono", samples=1600)
            frame.sample_rate = 16000
            frame.pts = 0
            frame.time_base = Fraction(1, 16000)
            frame.planes[0].update(array("h", [1000 if index else 0] * 1600).tobytes())
            for packet in track.encode(frame):
                container.mux(packet)
            for packet in track.encode():
                container.mux(packet)
    with MediaSource.open(path) as source:
        info = probe_media(source)
        assert info.audio_stream_index == 1
        assert extract_audio(source, info, 0, 0.1, 10000).sound.state == "sound_present"


def test_audio_upload_boundary_before_decode(tmp_path):
    path = tmp_path / "sound.wav"
    wav(path)
    with MediaSource.open(path) as source:
        info = probe_media(source)
        with pytest.raises(MediaError) as exc:
            extract_audio(source, info, 0, 0.1, 44)
    assert exc.value.code == "RESOURCE_BOUNDARY"


def test_worker_async_start_and_precise_frame_progress(tmp_path):
    from pulsara_agent.media.frames import FrameBudget, read_video_frames

    path = tmp_path / "movie.mkv"
    with av.open(str(path), "w") as container:
        stream = container.add_stream("ffv1", rate=25)
        stream.width, stream.height, stream.pix_fmt = 160, 90, "yuv420p"
        for index in range(3):
            frame = av.VideoFrame.from_image(Image.new("RGB", (160, 90), "red"))
            frame.pts = index
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)

    async def check():
        events = []

        async def started():
            await asyncio.sleep(0)
            events.append("start")

        owner = MediaWorker()
        with MediaSource.open(path) as source:
            info = probe_media(source)
            result = await owner.run(
                read_video_frames,
                source,
                info,
                start=0,
                end=info.duration,
                mode="overview",
                crop=None,
                budget=FrameBudget(1000000, 1280 * 720, 2, 32),
                on_started=started,
                on_progress=lambda span: events.append(span),
                deadline_monotonic=monotonic() + 10,
            )
            assert events == ["start", result.processed_range]
        await owner.aclose()

    asyncio.run(check())


def test_metadata_probe_does_not_decode_tracks(tmp_path, monkeypatch):
    from contextlib import contextmanager
    import pulsara_agent.media.source as source_module

    path = tmp_path / "media.wav"
    wav(path)
    real = MediaSource.open_container

    @contextmanager
    def metadata_only(source):
        with real(source) as container:

            class MetadataContainer:
                streams = container.streams
                format = container.format
                start_time = container.start_time
                duration = container.duration
                demux = container.demux

                def decode(self, *args):
                    raise AssertionError("metadata probe decoded a track")

            yield MetadataContainer()

    monkeypatch.setattr(MediaSource, "open_container", metadata_only)
    with MediaSource.open(path) as source:
        assert (
            source_module.probe_media(source, display_facts=False).audio_stream_index
            == 0
        )


def test_worker_memory_formula_and_geometry_boundary(monkeypatch):
    from types import SimpleNamespace
    from pulsara_agent.media import resources

    monkeypatch.setattr(
        resources.psutil, "virtual_memory", lambda: SimpleNamespace(available=8000)
    )
    monkeypatch.setattr(resources, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(resources, "linux_cgroup_headroom", lambda: 4000)
    assert resources.available_worker_memory() == 4000 // 8
    token = resources.WORKER_MEMORY_BUDGET.set(1000)
    monkeypatch.setattr(
        resources.psutil,
        "Process",
        lambda: SimpleNamespace(memory_info=lambda: SimpleNamespace(rss=100)),
    )
    try:
        with pytest.raises(MediaError) as exc:
            resources.admit_video_geometry(100, 100, 2)
        assert exc.value.code == "RESOURCE_BOUNDARY"
    finally:
        resources.WORKER_MEMORY_BUDGET.reset(token)


def test_linux_cgroup_hierarchy_headroom(tmp_path):
    from pulsara_agent.media.resources import linux_cgroup_headroom

    root = tmp_path / "cgroup"
    child = root / "parent" / "child"
    child.mkdir(parents=True)
    for directory, limit, current in [
        (root, 10000, 1000),
        (child.parent, 4000, 3000),
        (child, "max", 500),
    ]:
        (directory / "memory.max").write_text(str(limit))
        (directory / "memory.current").write_text(str(current))
    membership = tmp_path / "membership"
    membership.write_text("0::/parent/child\n")
    assert linux_cgroup_headroom(root, membership) == 1000


def test_two_worker_owners_share_physical_gate(tmp_path, monkeypatch):
    import threading
    from pulsara_agent.media import worker

    monkeypatch.setattr(worker, "_DECODE_SLOTS", threading.BoundedSemaphore(1))
    path = tmp_path / "media.wav"
    wav(path)

    async def check():
        first, second = MediaWorker(), MediaWorker()
        started = asyncio.Event()
        release = asyncio.Event()
        second_started = asyncio.Event()

        async def hold():
            started.set()
            await release.wait()

        with MediaSource.open(path) as source:
            a = asyncio.create_task(
                first.run(
                    probe_media,
                    source,
                    on_started=hold,
                    deadline_monotonic=monotonic() + 10,
                )
            )
            await started.wait()
            b = asyncio.create_task(
                second.run(
                    probe_media,
                    source,
                    on_started=second_started.set,
                    deadline_monotonic=monotonic() + 10,
                )
            )
            await asyncio.sleep(0.05)
            assert not second_started.is_set()
            await first.aclose()
            with pytest.raises(asyncio.CancelledError):
                await a
            await b
            assert second_started.is_set()
        await second.aclose()

    asyncio.run(check())


def test_real_worker_over_memory_allocation_is_reaped(tmp_path, monkeypatch):
    from pulsara_agent.media import worker

    monkeypatch.setattr(worker, "available_worker_memory", lambda: 1)
    path = tmp_path / "media.wav"
    wav(path)

    async def check():
        owner = MediaWorker()
        with MediaSource.open(path) as source:
            with pytest.raises(MediaError) as exc:
                await owner.run(
                    probe_media, source, deadline_monotonic=monotonic() + 10
                )
            assert exc.value.code == "RESOURCE_BOUNDARY"
            assert not owner._processes
        await owner.aclose()

    asyncio.run(check())


def test_linux_incremental_address_space_allowance(monkeypatch):
    from types import SimpleNamespace
    import resource
    from pulsara_agent.media import resources

    captured = []
    monkeypatch.setattr(resources, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(
        resources.psutil,
        "Process",
        lambda: SimpleNamespace(memory_info=lambda: SimpleNamespace(rss=50, vms=100)),
    )
    monkeypatch.setattr(
        resource,
        "getrlimit",
        lambda _: (resource.RLIM_INFINITY, resource.RLIM_INFINITY),
    )
    monkeypatch.setattr(
        resource, "setrlimit", lambda kind, value: captured.append((kind, value))
    )
    token = resources.WORKER_MEMORY_BUDGET.set(None)
    try:
        resources.install_worker_memory_limit(200)
        assert captured == [(resource.RLIMIT_AS, (250, resource.RLIM_INFINITY))]
    finally:
        resources.WORKER_MEMORY_BUDGET.reset(token)
