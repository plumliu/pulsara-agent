"""Finite detail pages retain source observations through admission and continuation."""

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
    path.write_bytes(b"source descriptor for detail continuation")
    with MediaSource.open(path) as source:
        yield source, MediaInfo(10, Fraction(0), 0, None, (), 1280, 720)


def _query(source, info):
    return dict(source=source, info=info, start=0.0, end=2.0, mode="detail",
                crop=None, selection_revision="frames-v3")


def _plan():
    observations = tuple(SimpleNamespace(t=Fraction(t)) for t in (0, 1, Fraction(7, 4)))
    return SimpleNamespace(
        priority=observations, observation_count=3, mode="detail", crop=None,
        requested_range=(0.0, 2.0), next_window_start=None, complete=True,
        video_ended=False, display_aspect=Fraction(16, 9),
    )


def _candidate(observations, payload=b"encoded image"):
    return FrameReadResult(
        (FrameImage(payload, "image/png", 1280, 720),),
        json.dumps({"video_frames": True, "frames": [
            {"n": n, "t": float(item.t), "image": 1}
            for n, item in enumerate(observations, 1)
        ]}), (0, 2), False,
    )


def test_detail_cursor_uses_finite_observation_and_rejects_old_shape(source_info):
    source, info = source_info
    query = _query(source, info)
    cursor = make_frame_next(**query, position=FramePagePosition(1))["arguments"]["continuation"]
    assert cursor["position"] == {"next_observation": 1}
    assert read_frame_continuation(cursor, **query) == FramePagePosition(1)
    for position in ({"next_global_grid_ordinal": 1}, {"next_observation": 1, "window_start": None},
                     {"next_observation": 0}, {"next_observation": True},
                     {"next_observation": None}, {"next_observation": "1"}):
        with pytest.raises(MediaError) as failure:
            read_frame_continuation(dict(cursor, position=position), **query)
        assert failure.value.code == "INVALID_ARGUMENTS"
    with pytest.raises(MediaError) as failure:
        read_frame_continuation(dict(cursor, selection_revision="frames-v2"), **query)
    assert failure.value.code == "FRAME_CONTINUATION_UNAVAILABLE"


@pytest.mark.parametrize("change", ["range", "crop", "mode", "stream", "revision", "source"])
def test_detail_position_retains_full_query_and_source_binding(source_info, change):
    source, info = source_info
    query = _query(source, info)
    cursor = copy.deepcopy(make_frame_next(
        **query, position=FramePagePosition(1),
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


def test_next_uses_delivered_observation_not_prepared_tail(source_info):
    source, info = source_info
    plan = _plan()
    page = with_frame_page_continuation(
        _candidate(plan.priority[:1]), source=source, info=info, plan=plan,
        position=FramePagePosition(), count=1,
        selection_revision="frames-v3",
    )
    body = json.loads(page.text)
    assert body["frames"] == [{"n": 1, "t": 0.0, "image": 1, "observation_position": 0}]
    assert body["next"]["arguments"]["continuation"]["position"] == {"next_observation": 1}
    assert body["plan_delivery"] == {"position": 0, "next_position": 1, "observations": 3, "window_complete": False}
    assert not body["complete"]


def test_final_detail_page_keeps_each_time_for_shared_image_and_completes_plan(source_info):
    source, info = source_info
    plan = _plan()
    page = with_frame_page_continuation(
        _candidate(plan.priority), source=source, info=info, plan=plan,
        position=FramePagePosition(), count=3,
        selection_revision="frames-v3",
    )
    body = json.loads(page.text)
    assert len(page.images) == 1
    assert [item["t"] for item in body["frames"]] == [0.0, 1.0, 1.75]
    assert [item["observation_position"] for item in body["frames"]] == [0, 1, 2]
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
                 dict(json.loads(raw.text), plan_delivery={"position": 0})):
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
            position=FramePagePosition(),
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
    assert body["next"]["arguments"]["continuation"]["position"] == {"next_observation": 1}
    assert body["frames"][0]["observation_position"] == 0


def test_real_low_fps_continuation_delivers_three_distinct_source_observations(tmp_path):
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
                source, info, start=0.5, end=2.5, mode="detail",
                continuation=arguments.get("continuation"),
                budget=FrameBudget(1 << 20, 1280 * 720, 1, 256),
            )
            body = json.loads(result.text)
            assert len(result.images) == len(body["frames"]) == 1
            assert body["frames"][0]["n"] == 1
            delivered.append(body["frames"][0]["t"])
            positions.append(body["frames"][0]["observation_position"])
            next_positions.append(body["plan_delivery"]["next_position"])
            if body["next"] is None:
                assert body["complete"]
                break
            assert not body["complete"]
            arguments = body["next"]["arguments"]
            assert set(arguments["continuation"]["position"]) == {"next_observation"}
    assert delivered == [0.0, 1.0, 2.0]
    assert positions == [0, 1, 2]
    assert next_positions == [1, 2, 3]
