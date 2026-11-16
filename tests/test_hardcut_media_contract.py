"""Media selection is complete before native display and JS composition."""

import asyncio
from fractions import Fraction
from time import monotonic
from types import SimpleNamespace
import wave

import av
from PIL import Image
import pytest

from pulsara_agent.conversation_kernel.image_validation import HostPromptImageValidator
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.media.execution import execute_media
from pulsara_agent.media.worker import MediaWorker
from pulsara_agent.ports.tool_execution import thaw_tool_json_object
from pulsara_agent.tools.builtins.media import (
    MediaTool,
    media_parameters_schema,
    validate_media_arguments,
)


def _video(path, colors, rate=10):
    with av.open(str(path), "w") as container:
        stream = container.add_stream("ffv1", rate=rate)
        stream.width, stream.height, stream.pix_fmt = 40, 24, "yuv420p"
        for i, color in enumerate(colors):
            frame = av.VideoFrame.from_image(Image.new("RGB", (40, 24), color))
            frame.pts, frame.time_base = i, Fraction(1, rate)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return path


async def _execute(path, name, arguments, **owners):
    io, worker, validator = KernelSessionIO(), MediaWorker(), HostPromptImageValidator()
    try:
        result = await execute_media(
            MediaTool(path, name=name),
            arguments,
            io=io,
            worker=worker,
            image_validator=validator,
            cache=owners.get("cache"),
            read_settings=owners.get("read_settings"),
            asr_runtime=owners.get("asr_runtime"),
            memory_domain_id="media-test",
            deadline=monotonic() + 60,
            reserve_read=lambda **facts: None,
            observe_read=lambda **facts: None,
        )
        from pulsara_agent.conversation_kernel.tool_outputs import (
            assemble_tool_business_value,
        )
        from tests.test_media_business_output_schema import validate_media_js_value

        await validate_media_js_value(
            name,
            assemble_tool_business_value(
                public_fields=result.content.public_fields,
                content_parts=result.content.content,
                result_state=result.state,
            ),
            result.state,
        )
        return result
    finally:
        await worker.aclose()
        await validator.aclose()
        await io.aclose(deadline_monotonic=monotonic() + 30)


def test_media_schemas_expose_only_current_parameters():
    from pulsara_agent.identity import random_id

    validate_media_arguments(
        "view_image", {"target": {"kind": "IMAGE_REF", "image_ref": random_id("image")}}
    )
    with pytest.raises(ValueError):
        validate_media_arguments(
            "view_image",
            {"target": {"kind": "PATH", "path": "pic.png"}, "crop": [0, 0, 1, 1]},
        )
    assert list(media_parameters_schema("read_video_frames")["properties"]) == [
        "target",
        "from",
        "to",
        "action",
        "crop",
    ]
    assert list(media_parameters_schema("read_transcript")["properties"]) == [
        "target",
        "from",
        "to",
    ]
    for invalid in ({"mode": "overview"}, {"continuation": {"old": True}}):
        with pytest.raises(ValueError):
            validate_media_arguments(
                "read_video_frames",
                {"target": {"kind": "PATH", "path": "v.mp4"}, **invalid},
            )
    with pytest.raises(ValueError, match="2 seconds"):
        validate_media_arguments(
            "read_video_frames",
            {
                "target": {"kind": "PATH", "path": "v.mp4"},
                "action": "detail",
                "from": 0,
                "to": 3,
            },
        )


def test_detail_keeps_every_observation_and_exact_shared_image(tmp_path):
    _video(tmp_path / "states.mkv", ["red"] * 4 + ["blue"] * 4 + ["red"] * 12)
    result = asyncio.run(
        _execute(
            tmp_path,
            "read_video_frames",
            {
                "target": {"kind": "PATH", "path": "states.mkv"},
                "action": "detail",
                "from": 0,
                "to": 1,
            },
        )
    )
    assert result.state == "SUCCESS"
    parts = result.content.content
    assert [p.type for p in parts] == [
        "text",
        "object",
        "image",
        "object",
        "image",
        "object",
        "image",
    ]
    assert [p.value["t"] for p in parts if p.type == "object"] == [0, 0.5, 0.9]
    refs = [p.value["image_ref"] for p in parts if p.type == "image"]
    assert refs[0] == refs[2] and refs[0] != refs[1]
    assert len(result.pending_images) == 2
    assert all(
        set(p.value) <= {"t", "visible_range", "dt"}
        for p in parts
        if p.type == "object"
    )
    assert thaw_tool_json_object(result.content.public_fields) == {
        "target": {"kind": "PATH", "path": "states.mkv"}
    }


@pytest.mark.parametrize(
    "size,crop,dimensions",
    [
        ((800, 450), [0, 0, 1, 0.25], (1280, 180)),
        ((450, 800), [0, 0, 0.25, 1], (180, 1280)),
        ((800, 450), [0, 0, 0.8, 0.2], (1280, 180)),
    ],
)
def test_detail_crop_at_readable_boundary_returns_validated_images(
    tmp_path, size, crop, dimensions
):
    from tests.test_media_detail_render import video

    video(tmp_path / "crop.mkv", ["red", "blue", "red"], size=size)
    result = asyncio.run(
        _execute(
            tmp_path,
            "read_video_frames",
            {
                "target": {"kind": "PATH", "path": "crop.mkv"},
                "action": "detail",
                "from": 0,
                "to": 1.5,
                "crop": crop,
            },
        )
    )

    assert result.state == "SUCCESS", result.content.content
    assert [part.value["t"] for part in result.content.content if part.type == "object"] == [0, 0.5, 1]
    assert len(result.pending_images) == 2
    assert all((image.width, image.height) == dimensions for image in result.pending_images)


def test_motion_runs_all_internal_windows_without_model_continuations(
    tmp_path, monkeypatch
):
    import pulsara_agent.media.execution as execution

    monkeypatch.setattr(execution, "FRAME_ANALYSIS_FRAMES", 7)
    _video(tmp_path / "motion.mkv", ["red", "blue", "green"] * 7)
    result = asyncio.run(
        _execute(
            tmp_path,
            "read_video_frames",
            {"target": {"kind": "PATH", "path": "motion.mkv"}, "action": "motion"},
        )
    )
    assert result.state == "SUCCESS"
    indices = [p.value for p in result.content.content if p.type == "object"]
    frames = [cell for item in indices for cell in item["frames"]]
    assert len(frames) == 21
    assert [cell["t"] for cell in frames] == pytest.approx([i / 10 for i in range(21)])
    assert len(indices) >= 3
    assert all("cell_rect" in cell and "n" in cell for cell in frames)
    assert [p.type for p in result.content.content[1:]] == [
        kind for _ in indices for kind in ("object", "image")
    ]


def test_overview_is_collage_with_local_grid_index_and_requested_crop(tmp_path):
    _video(tmp_path / "overview.mkv", ["red"] * 10 + ["blue"] * 10)
    result = asyncio.run(
        _execute(
            tmp_path,
            "read_video_frames",
            {
                "target": {"kind": "PATH", "path": "overview.mkv"},
                "crop": [0.25, 0, 0.75, 1],
            },
        )
    )
    assert result.state == "SUCCESS"
    pairs = result.content.content[1:]
    assert all(
        pairs[i].type == "object" and pairs[i + 1].type == "image"
        for i in range(0, len(pairs), 2)
    )
    assert all(
        cell["cell_rect"][0] < cell["cell_rect"][2] and "representative_range" in cell
        for part in pairs
        if part.type == "object"
        for cell in part.value["frames"]
    )


def test_unavailable_media_has_real_typed_failure_without_image(tmp_path):
    result = asyncio.run(
        _execute(
            tmp_path,
            "read_video_frames",
            {"target": {"kind": "PATH", "path": "missing.mkv"}},
        )
    )
    assert result.state == "APPLICATION_ERROR"
    assert result.content.public_fields["target"] == {
        "kind": "PATH",
        "path": "missing.mkv",
    }
    assert all(part.type == "text" for part in result.content.content)
    assert result.pending_images == ()


def test_transcript_acquires_more_than_180_seconds_in_upload_batches_without_display_budget(
    tmp_path,
):
    from datetime import datetime, timezone
    from pulsara_agent.media.asr import AsrTranscript, AsrTimestamp
    from pulsara_agent.media.asr_cache import AsrCacheBlock, asr_cache_id
    from pulsara_agent.media.source import MediaSource, probe_media
    from pulsara_agent.media.transcript import TranscriptService
    from pulsara_agent.media.transcript_work import TranscriptWork

    path = tmp_path / "long.wav"
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\0\0" * (200 * 16000))

    class Cache:
        def __init__(self):
            self.blocks = {}

        def query_ranges(self, key, start, end):
            return tuple(
                block
                for block in self.blocks.values()
                if block.key == key
                and block.from_seconds < end
                and block.to_seconds > start
            )

        def query_block(self, key, identity):
            return self.blocks.get(identity)

        def touch(self, key):
            return len(self.blocks)

        def save_success(self, key, start, end, provenance, payload):
            block = AsrCacheBlock(
                asr_cache_id(key, start, end),
                key,
                start,
                end,
                provenance,
                payload,
                datetime.now(timezone.utc),
            )
            self.blocks[block.id] = block
            return block

    async def scenario():
        calls = []

        async def execute(operation, *args, **kwargs):
            kwargs.pop("deadline_monotonic", None)
            return operation(*args, **kwargs)

        async def transcribe(config, audio, *, duration, **kwargs):
            calls.append(duration)
            wording = f"原始文字-{len(calls)}\n" + "完整" * 30_000
            return AsrTranscript(
                wording, (AsrTimestamp(0, duration, wording),), "segment", language="zh"
            )

        cache, work = Cache(), TranscriptWork()
        try:
            with MediaSource.open(path) as source:
                service = TranscriptService(
                    work=work,
                    cache=cache,
                    asr_config=SimpleNamespace(),
                    run_media=execute,
                    maximum_upload_bytes=44 + 32000 * 61,
                    maximum_response_bytes=4 << 20,
                    deadline_monotonic=monotonic() + 30,
                    transcribe=transcribe,
                )
                result = await service.read(
                    source,
                    probe_media(source),
                    {
                        "target": {"kind": "PATH", "path": str(path)},
                        "from": 0,
                        "to": 200,
                    },
                    memory_domain_id="media-test",
                )
                assert not result.errors
                assert result.processed_to == 200
                assert calls == [61, 61, 61, 17]
                assert len(result.entries) == 4
                assert sum(len(item.text) for item in result.entries) > 240_000
                assert [item.start for item in result.entries] == [0, 61, 122, 183]
                assert result.entries[-1].end == 200
        finally:
            await work.aclose()

    asyncio.run(scenario())


def test_subtitle_complete_text_and_boundary_wording_without_byte_fragments(tmp_path):
    from pulsara_agent.media.source import MediaSource, probe_media
    from pulsara_agent.media.transcript import TranscriptService

    text = "字幕原文" * 20
    path = tmp_path / "subtitles.srt"
    path.write_text(
        "\n".join(
            f"{i + 1}\n00:{(i * 2) // 60:02d}:{(i * 2) % 60:02d},000 --> 00:{(i * 2 + 2) // 60:02d}:{(i * 2 + 2) % 60:02d},000\n{i}: {text}\n"
            for i in range(1000)
        ),
        encoding="utf-8",
    )

    async def scenario():
        async def execute(operation, *args, **kwargs):
            return operation(*args, **kwargs)

        with MediaSource.open(path) as source:
            service = TranscriptService(
                work=None,
                cache=None,
                asr_config=None,
                run_media=execute,
                maximum_upload_bytes=1000,
                maximum_response_bytes=100,
                deadline_monotonic=monotonic() + 30,
            )
            value = await service.read(
                source,
                probe_media(source),
                {
                    "target": {"kind": "PATH", "path": str(path)},
                    "from": 0.5,
                    "to": 1999.5,
                },
                memory_domain_id="media-test",
            )
            assert value.source == "subtitle" and len(value.entries) == 1000
            assert [(item.start, item.end, item.text) for item in value.entries] == [
                (i * 2, i * 2 + 2, f"{i}: {text}") for i in range(1000)
            ]
            assert value.entries[0].public_value((0.5, 1999.5))["visible_range"] == {
                "from": 0.5,
                "to": 2,
            }
            assert value.entries[-1].public_value((0.5, 1999.5))["visible_range"] == {
                "from": 1998,
                "to": 1999.5,
            }

    asyncio.run(scenario())


def test_later_image_validation_failure_preserves_already_obtained_ordered_evidence(
    tmp_path, monkeypatch
):
    _video(tmp_path / "partial.mkv", ["red"] * 4 + ["blue"] * 4 + ["red"] * 12)
    original = HostPromptImageValidator.freeze_local_image
    calls = 0

    async def validate(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("later image validation unavailable")
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(HostPromptImageValidator, "freeze_local_image", validate)
    result = asyncio.run(
        _execute(
            tmp_path,
            "read_video_frames",
            {
                "target": {"kind": "PATH", "path": "partial.mkv"},
                "action": "detail",
                "from": 0,
                "to": 1,
            },
        )
    )
    assert result.state == "APPLICATION_ERROR"
    parts = result.content.content
    assert [part.type for part in parts] == ["text", "object", "image", "text"]
    assert parts[1].value["t"] == 0
    assert "later image validation unavailable" in parts[-1].value
    assert len(result.pending_images) == 1
    assert result.pending_images[0].image_ref == parts[2].value["image_ref"]
