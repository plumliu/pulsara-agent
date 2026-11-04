"""Detail source-grid positions progress from delivered evidence, not preparation."""

import asyncio
import copy
from fractions import Fraction
import io
import json
from time import monotonic
from types import SimpleNamespace

import av
from PIL import Image
import pytest

from pulsara_agent.media.frames import FrameBudget, FrameImage, FrameReadResult
from pulsara_agent.media.frames_paging import (
    FramePagePosition,
    make_frame_next,
    read_frame_continuation,
    trim_detail_page,
    with_frame_page_continuation,
)
from pulsara_agent.media.source import MediaError, MediaInfo, MediaSource


@pytest.fixture
def source_info(tmp_path):
    path = tmp_path / "detail.mkv"
    path.write_bytes(b"source descriptor for grid continuation")
    with MediaSource.open(path) as source:
        yield source, MediaInfo(10, Fraction(0), 0, None, (), 1280, 720)


def _query(source, info):
    return dict(source=source, info=info, start=0.0, end=10.0, mode="detail",
                crop=None, selection_revision="detail-grid-test")


def _plan(*, start=4, complete=False):
    observations = tuple(
        SimpleNamespace(t=Fraction(t), detail_grid_ordinal=ordinal,
                        detail_next_grid_ordinal=following)
        for t, ordinal, following in ((2, 4, 8), (4, 8, 10), (5, 10, 21))
    )
    return SimpleNamespace(
        priority=observations, observation_count=len(observations), mode="detail",
        crop=None, requested_range=(0.0, 10.0), next_window_start=None,
        detail_grid_start=start, detail_grid_count=21, complete=complete,
        video_ended=False, display_aspect=Fraction(16, 9),
    )


def _candidate(observations, payload=b"encoded image"):
    return FrameReadResult(
        (FrameImage(payload, "image/png", 1280, 720),),
        json.dumps({"video_frames": True, "frames": [
            {"n": n, "t": float(item.t), "image": 1}
            for n, item in enumerate(observations, 1)
        ]}), (2, 10), False,
    )


def test_detail_cursor_contains_only_global_grid_position_and_rejects_old_shape(source_info):
    source, info = source_info
    query = _query(source, info)
    call = make_frame_next(**query, position=FramePagePosition(next_global_grid_ordinal=8))
    cursor = call["arguments"]["continuation"]
    assert cursor["position"] == {"next_global_grid_ordinal": 8}
    assert read_frame_continuation(cursor, **query) == FramePagePosition(next_global_grid_ordinal=8)
    for position in ({"next_observation": 8}, {"next_global_grid_ordinal": 8, "window_start": None},
                     {"next_global_grid_ordinal": 0}, {"next_global_grid_ordinal": True},
                     {"next_global_grid_ordinal": None}, {"next_global_grid_ordinal": "8"}):
        changed = dict(cursor, position=position)
        with pytest.raises(MediaError) as failure:
            read_frame_continuation(changed, **query)
        assert failure.value.code == "INVALID_ARGUMENTS"


@pytest.mark.parametrize("change", ["range", "crop", "mode", "stream", "revision", "source"])
def test_detail_grid_position_retains_full_query_and_source_binding(source_info, change):
    source, info = source_info
    query = _query(source, info)
    cursor = copy.deepcopy(make_frame_next(
        **query, position=FramePagePosition(next_global_grid_ordinal=8),
    )["arguments"]["continuation"])
    if change == "range":
        query["end"] = 9.0
    elif change == "crop":
        query["crop"] = [0.1, 0, 1, 1]
    elif change == "mode":
        query["mode"] = "overview"
    elif change == "stream":
        cursor["stream"] = 1
    elif change == "revision":
        cursor["selection_revision"] = "obsolete"
    else:
        cursor["source_version"]["size"] += 1
    with pytest.raises(MediaError) as failure:
        read_frame_continuation(cursor, **query)
    assert failure.value.code == (
        "FRAME_CONTINUATION_UNAVAILABLE" if change == "revision" else
        "MEDIA_SOURCE_CHANGED" if change == "source" else "INVALID_ARGUMENTS"
    )


def test_next_uses_last_delivered_source_interval_not_prepared_tail(source_info):
    source, info = source_info
    plan = _plan()
    page = with_frame_page_continuation(
        _candidate(plan.priority[:1]), source=source, info=info, plan=plan,
        position=FramePagePosition(next_global_grid_ordinal=4), count=1,
        selection_revision="detail-grid-test",
    )
    body = json.loads(page.text)
    assert body["frames"] == [{"n": 1, "t": 2.0, "image": 1, "observation_position": 4}]
    assert body["next"]["arguments"]["continuation"]["position"] == {"next_global_grid_ordinal": 8}
    assert body["grid_delivery"] == {"position": 4, "next_position": 8, "targets": 21}
    assert "plan_delivery" not in body
    assert not body["complete"]


def test_final_detail_page_keeps_each_time_for_shared_image_and_closes_grid(source_info):
    source, info = source_info
    plan = _plan(complete=True)
    page = with_frame_page_continuation(
        _candidate(plan.priority), source=source, info=info, plan=plan,
        position=FramePagePosition(next_global_grid_ordinal=4), count=3,
        selection_revision="detail-grid-test",
    )
    body = json.loads(page.text)
    assert len(page.images) == 1
    assert [item["t"] for item in body["frames"]] == [2.0, 4.0, 5.0]
    assert [item["observation_position"] for item in body["frames"]] == [4, 8, 10]
    assert body["complete"] and body["next"] is None


def test_encoded_detail_trim_preserves_image_numbers_times_and_raw_limitations():
    frames = [
        {"n": n, "t": n / 2, "dt": 0.5, "image": image,
         "cell_rect": [0, 0, 1, 1]}
        for n, image in enumerate((1, 2, 1, 3), 1)
    ]
    raw = FrameReadResult(
        tuple(FrameImage(bytes([index]), "image/png", 1280, 720) for index in range(3)),
        json.dumps({"video_frames": True, "frames": frames, "next": None,
                    "limitations": ["raw sampling limitation"]}), (0, 2), False,
    )
    trimmed = trim_detail_page(raw, 3)
    assert json.loads(trimmed.text)["frames"] == frames[:3]
    assert trimmed.images == raw.images[:2]
    assert trimmed.images[0] is raw.images[0]
    assert json.loads(trimmed.text)["limitations"] == ["raw sampling limitation"]
    assert len(json.loads(raw.text)["frames"]) == 4
    for body in (dict(json.loads(raw.text), next={"tool": "read_video_frames"}),
                 dict(json.loads(raw.text), grid_delivery={"position": 0})):
        with pytest.raises(ValueError, match="unbound"):
            trim_detail_page(FrameReadResult(raw.images, json.dumps(body), (0, 2), False), 1)


@pytest.mark.parametrize("physical_prefix", [None, 2, "memory", "atom"])
def test_resource_requote_shrinks_detail_prefix_without_advancing_prepared_tail(source_info, physical_prefix):
    from pulsara_agent.conversation_kernel.tool_contracts import (
        FrozenImageToolResourceAllowance, FrozenImageToolResourceIncrement,
    )
    from pulsara_agent.identity import derive_id
    from pulsara_agent.llm.input import FrozenPromptContent, LLMImagePart, LLMTextPart
    from pulsara_agent.media.execution import render_prepared_frame_page
    from pulsara_agent.primitives.tool_result_budget import ToolResultPageBudget
    from pulsara_agent.tools.builtins.filesystem import ViewImageSource, ViewImageSourceKind

    source, info = source_info
    plan = _plan()
    encoded = io.BytesIO()
    Image.new("RGB", (1280, 720), "blue").save(encoded, format="PNG")
    payload = encoded.getvalue()
    attempts, quotes = [], []

    class Worker:
        async def run(self, operation, actual_source, actual_info, actual_plan, **kwargs):
            assert actual_plan is plan
            assert kwargs["offset"] == 0
            count = kwargs["count"]
            attempts.append(count)
            if physical_prefix == "atom" or (physical_prefix == "memory" and count > 1):
                raise MediaError("RESOURCE_BOUNDARY", "Worker-wide physical allocation")
            admitted = min(count, physical_prefix) if type(physical_prefix) is int else count
            return _candidate(plan.priority[:admitted], payload)

    class Validator:
        async def freeze(self, prompt, **kwargs):
            return FrozenPromptContent((LLMTextPart(prompt.parts[0].text),
                                        LLMImagePart("image/png", payload, 1280, 720)))

    class Quote:
        def quote(self, *, content, **kwargs):
            body = json.loads(content.parts[0].text)
            quotes.append(body)
            return FrozenImageToolResourceIncrement(0, 0, 0 if len(body["frames"]) == 1 else 1, 0)

    allowance = FrozenImageToolResourceAllowance(
        0, "detail-call", "binding", 100000, 100000, 0, 100000, Quote(),
    )
    async def run():
        return await render_prepared_frame_page(
            source=source, info=info, plan=plan,
            position=FramePagePosition(next_global_grid_ordinal=4),
            budget=FrameBudget(1 << 20, 1280 * 720, 1, 256), allowance=allowance,
            page_budget=ToolResultPageBudget("detail-call"),
            image_source=ViewImageSource(ViewImageSourceKind.PATH, str(source.path)),
            workspace_id=derive_id("workspace", "test", "detail-global-page"),
            worker=Worker(), image_validator=Validator(), deadline=monotonic() + 30,
        )

    if physical_prefix == "atom":
        with pytest.raises(MediaError) as failure:
            asyncio.run(run())
        assert failure.value.code == "RESOURCE_BOUNDARY"
        assert attempts == [3, 1]
        assert quotes == []
        return
    body = json.loads(asyncio.run(run()).parts[0].text)
    assert attempts == ([3, 1] if physical_prefix == "memory" else [3])
    assert [len(quote["frames"]) for quote in quotes] == (
        [3, 2, 1] if physical_prefix is None else [2, 1] if physical_prefix == 2 else [1]
    )
    assert all(len(quote["limitations"]) == int(quote["next"] is not None) for quote in quotes)
    assert body["next"]["arguments"]["continuation"]["position"] == {"next_global_grid_ordinal": 8}
    assert body["frames"][0]["observation_position"] == 4


def test_real_low_fps_continuation_skips_same_source_grid_targets_without_missing_distinct_pts(tmp_path):
    from pulsara_agent.media.frames import read_video_frames
    from pulsara_agent.media.source import probe_media

    path = tmp_path / "low-fps.mkv"
    with av.open(str(path), "w") as container:
        stream = container.add_stream("ffv1", rate=1)
        stream.width, stream.height, stream.pix_fmt = 320, 180, "bgr0"
        for ordinal, color in enumerate(("red", "green", "blue", "yellow")):
            frame = av.VideoFrame.from_image(Image.new("RGB", (320, 180), color))
            frame.pts, frame.time_base = ordinal, Fraction(1)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)

    arguments = {}
    delivered, positions, next_positions = [], [], []
    with MediaSource.open(path) as source:
        info = probe_media(source)
        while True:
            result = read_video_frames(
                source, info, start=0, end=4, mode="detail",
                continuation=arguments.get("continuation"),
                budget=FrameBudget(1 << 20, 1280 * 720, 1, 256),
            )
            body = json.loads(result.text)
            assert len(result.images) == len(body["frames"]) == 1
            assert body["frames"][0]["n"] == 1
            delivered.append(body["frames"][0]["t"])
            positions.append(body["frames"][0]["observation_position"])
            next_positions.append(body["grid_delivery"]["next_position"])
            if body["next"] is None:
                assert body["complete"]
                break
            assert not body["complete"]
            arguments = body["next"]["arguments"]
            assert set(arguments["continuation"]["position"]) == {"next_global_grid_ordinal"}
    assert delivered == [0.0, 1.0, 2.0, 3.0]
    assert positions == [0, 2, 4, 6]
    assert next_positions == [2, 4, 6, 9]


def test_full_256_observation_candidate_crossing_logical_boundary_renders_only_once(source_info):
    from pulsara_agent.conversation_kernel.tool_contracts import (
        FrozenImageToolResourceAllowance, FrozenImageToolResourceIncrement,
    )
    from pulsara_agent.identity import derive_id
    from pulsara_agent.llm.input import FrozenPromptContent, LLMImagePart, LLMTextPart
    from pulsara_agent.media.execution import render_prepared_frame_page
    from pulsara_agent.primitives.tool_result_budget import ToolResultPageBudget
    from pulsara_agent.tools.builtins.filesystem import ViewImageSource, ViewImageSourceKind

    source, _ = source_info
    info = MediaInfo(3600, Fraction(0), 0, None, (), 1280, 720)
    observations = tuple(
        SimpleNamespace(t=Fraction(7 * ordinal // 2, 7), detail_grid_ordinal=ordinal,
                        detail_next_grid_ordinal=ordinal + 1)
        for ordinal in range(256)
    )
    plan = SimpleNamespace(
        priority=observations, observation_count=256, mode="detail", crop=None,
        requested_range=(0.0, 3600.0), next_window_start=None,
        detail_grid_start=0, detail_grid_count=7201, complete=False,
        video_ended=False, display_aspect=Fraction(16, 9),
    )
    index = [
        {"n": ordinal + 1, "t": float(observation.t), "image": 1,
         "cell_rect": [0.0, 0.0, 1.0, 1.0],
         "visible_range": [float(observation.t), float(observation.t + Fraction(1, 7))],
         **({"dt": float(observation.t - observations[ordinal - 1].t)} if ordinal else {})}
        for ordinal, observation in enumerate(observations)
    ]
    encoded = io.BytesIO()
    Image.new("RGB", (1280, 720), "blue").save(encoded, format="PNG")
    payload = encoded.getvalue()
    raw = FrameReadResult(
        (FrameImage(payload, "image/png", 1280, 720),),
        json.dumps({
            "video_frames": True, "frames": index, "next": None,
            "target": {"kind": "PATH", "path": str(source.path)},
            "requested_range": [0.0, 3600.0], "processed_range": [0.0, 128.0],
            "display_size": [1280, 720], "crop": [0, 0, 1, 1],
            "sampling": "ordered", "probe_count": 256, "maximum_probe_gap": 4 / 7,
            "scanned_ranges": [], "observed_cut_times": [],
            "limitations": [
                "Sampling does not rule out unobserved brief changes; ordered does not mean every source frame.",
                "Detail uses exact half-second targets, or start/middle/end for short ranges; sampled frames may be blurred and intervening pages or text are not fully read. Identical rendered images can share one image number while retaining their distinct observation times.",
            ],
        }, separators=(",", ":")), (0, 128), False,
    )
    attempts, logical_quotes = [], []

    class Worker:
        async def run(self, operation, actual_source, actual_info, actual_plan, **kwargs):
            attempts.append(kwargs["count"])
            assert kwargs["offset"] == 0
            return raw

    class Validator:
        async def freeze(self, prompt, **kwargs):
            return FrozenPromptContent((LLMTextPart(prompt.parts[0].text),
                                        LLMImagePart("image/png", payload, 1280, 720)))

    class Quote:
        def quote(self, **kwargs):
            return FrozenImageToolResourceIncrement(0, 0, 0, 0)

    class RecordingBudget(ToolResultPageBudget):
        def fits_logical(self, body, **kwargs):
            accepted = super().fits_logical(body, **kwargs)
            logical_quotes.append((len(json.loads(body)["frames"]), accepted))
            return accepted

    allowance = FrozenImageToolResourceAllowance(
        0, "detail-call", "binding", 100000, 100000, 0, 100000, Quote(),
    )

    async def run():
        return await render_prepared_frame_page(
            source=source, info=info, plan=plan, position=FramePagePosition(),
            budget=FrameBudget(1 << 20, 1280 * 720, 1, 256), allowance=allowance,
            page_budget=RecordingBudget("detail-call"),
            image_source=ViewImageSource(ViewImageSourceKind.PATH, str(source.path)),
            workspace_id=derive_id("workspace", "test", "detail-logical-prefix"),
            worker=Worker(), image_validator=Validator(), deadline=monotonic() + 30,
        )

    body = json.loads(asyncio.run(run()).parts[0].text)
    assert logical_quotes[0] == (256, False)
    assert attempts == [256]
    assert 0 < len(body["frames"]) < 256
    assert body["frames"][-1]["observation_position"] == len(body["frames"]) - 1
    assert body["next"]["arguments"]["continuation"]["position"] == {
        "next_global_grid_ordinal": len(body["frames"]),
    }
    assert len(body["limitations"]) == 3
