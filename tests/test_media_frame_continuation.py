"""Frame source positions bind a frozen selection without durable paging state."""
from dataclasses import replace
from fractions import Fraction
import json
import os
from types import SimpleNamespace

import pytest

from pulsara_agent.media.frames import FrameImage, FrameReadResult
from pulsara_agent.media.frames_paging import (
    FramePagePosition,
    make_frame_next,
    read_frame_continuation,
    with_frame_page_continuation,
)
from pulsara_agent.media.source import FramePosition, MediaError, MediaInfo, MediaSource


@pytest.fixture
def source_info(tmp_path):
    path = tmp_path / "source.mkv"
    path.write_bytes(b"descriptor-bound source")
    with MediaSource.open(path) as source:
        info = MediaInfo(10, Fraction(0), 0, None, (), 320, 180)
        yield source, info


def next_call(source, info, *, mode="overview", position=None, **changes):
    query = dict(source=source, info=info, start=0.0, end=10.0, mode=mode, crop=None,
                 selection_revision="frames-test-v1", position=position or FramePagePosition(1))
    query.update(changes)
    return make_frame_next(**query)


def parse(source, source_info, cursor, **changes):
    query = dict(source=source, info=source_info, start=0.0, end=10.0, mode="overview", crop=None,
                 selection_revision="frames-test-v1")
    query.update(changes)
    return read_frame_continuation(cursor, **query)


def test_complete_next_binds_canonical_query_and_existing_full_source_version(source_info):
    source, info = source_info
    call = next_call(source, info)
    arguments = call["arguments"]
    assert set(arguments) == {"target", "from", "to", "mode", "continuation"}
    assert call["tool"] == "read_video_frames"
    assert arguments["target"] == {"kind": "PATH", "path": str(source.path)}
    cursor = arguments["continuation"]
    assert set(cursor["source_version"]) == {"dev", "ino", "size", "modified_ns", "changed_ns"}
    assert cursor["stream"] == 0
    assert cursor["crop"] == [0, 0, 1, 1]
    assert parse(source, info, cursor) == FramePagePosition(1)
    assert parse(source, info, json.loads(json.dumps(cursor))) == FramePagePosition(1)
    assert parse(source, info, cursor) == FramePagePosition(1)  # positions are not consumed


@pytest.mark.parametrize("changes", [
    {"start": 0.1}, {"end": 9}, {"mode": "detail"}, {"crop": [0.1, 0, 1, 1]},
    {"info": MediaInfo(10, Fraction(0), 1, None, (), 320, 180)},
])
def test_position_cannot_be_reused_for_a_different_selection_query(source_info, changes):
    source, info = source_info
    cursor = next_call(source, info)["arguments"]["continuation"]
    with pytest.raises(MediaError, match="different query"):
        parse(source, info, cursor, **changes)


def test_obsolete_selection_policy_and_closed_source_facts_fail_explicitly(source_info):
    source, info = source_info
    cursor = next_call(source, info)["arguments"]["continuation"]
    with pytest.raises(MediaError) as error:
        parse(source, info, cursor, selection_revision="replacement-policy")
    assert error.value.code == "FRAME_CONTINUATION_UNAVAILABLE"
    for version in [dict(cursor["source_version"], unrelated=1),
                    dict(cursor["source_version"], dev=True)]:
        with pytest.raises(MediaError, match="source version"):
            parse(source, info, {**cursor, "source_version": version})
    with pytest.raises(MediaError, match="source version"):
        parse(source, info, {**cursor, "source_version": None})


@pytest.mark.parametrize("position", [None, {}, {"next_observation": 0},
    {"next_observation": True}, {"next_observation": -1},
    {"next_observation": 1, "window_start": None}])
def test_invalid_observation_positions_do_not_restart_or_create_same_next(source_info, position):
    source, info = source_info
    cursor = next_call(source, info)["arguments"]["continuation"]
    with pytest.raises(MediaError):
        parse(source, info, {**cursor, "position": position})


def test_same_size_restored_mtime_replacement_invalidates_source_position(source_info):
    source, info = source_info
    cursor = next_call(source, info)["arguments"]["continuation"]
    old = source.path.stat()
    replacement = source.path.with_suffix(".replacement")
    replacement.write_bytes(b"x" * old.st_size)
    os.utime(replacement, ns=(old.st_atime_ns, old.st_mtime_ns))
    replacement.replace(source.path)
    with MediaSource.open(source.path) as current:
        assert current.size == source.size and current.modified_ns == source.modified_ns
        with pytest.raises(MediaError) as error:
            parse(current, info, cursor)
        assert error.value.code == "MEDIA_SOURCE_CHANGED"


def test_motion_window_preserves_exact_native_coordinate_not_float_seconds(source_info):
    source, info = source_info
    position = FramePagePosition(0, FramePosition(1, Fraction(1, 3)))
    cursor = next_call(source, info, mode="motion", position=position)["arguments"]["continuation"]
    assert cursor["position"] == {"next_observation": 0,
                                  "window_start": {"pts": 1, "time_base": [1, 3]}}
    assert parse(source, info, cursor, mode="motion") == position
    for frame in [{"pts": 1, "time_base": [2, 6]},
                  {"pts": True, "time_base": [1, 3]},
                  {"pts": 1, "time_base": [1, 0]},
                  {"pts": 30, "time_base": [1, 3]}]:
        with pytest.raises(MediaError):
            parse(source, info, {**cursor, "position": {"next_observation": 0,
                  "window_start": frame}}, mode="motion")


def test_page_next_advances_priority_rank_with_only_delivered_image_index(source_info):
    source, info = source_info
    observations = tuple(SimpleNamespace(t=Fraction(t)) for t in [0, 9, 4])
    plan = SimpleNamespace(priority=observations, observation_count=3, next_window_start=None,
                           requested_range=(0.0, 10.0), mode="overview", crop=None,
                           complete=True, video_ended=False)
    candidate = FrameReadResult((FrameImage(b"jpeg", "image/jpeg", 320, 180),),
        json.dumps({"video_frames": True, "frames": [{"t": 0}, {"t": 9}],
                    "next": None, "limitations": []}), (0, 10), True)
    page = with_frame_page_continuation(candidate, source=source, info=info, plan=plan,
        position=FramePagePosition(), count=2, selection_revision="frames-test-v1")
    body = json.loads(page.text)
    assert page.images is candidate.images
    assert [frame["observation_position"] for frame in body["frames"]] == [0, 1]
    assert body["plan_delivery"] == {"position": 0, "next_position": 2,
                                    "observations": 3, "window_complete": False}
    assert not body["complete"]
    assert parse(source, info, body["next"]["arguments"]["continuation"]) == FramePagePosition(2)
    last = replace(candidate, text=json.dumps({"video_frames": True, "frames": [{"t": 4}]}))
    final = with_frame_page_continuation(last, source=source, info=info, plan=plan,
        position=FramePagePosition(2), count=1, selection_revision="frames-test-v1")
    final_body = json.loads(final.text)
    assert final_body["complete"] and final_body["next"] is None
    assert final_body["frames"] == [{"t": 4, "observation_position": 2}]


def test_resource_refresh_requotes_current_full_page_before_shrinking(source_info):
    content, attempts, old_quotes, fresh_quotes, refreshes = _run_refresh_page(source_info, fresh_limit=3)
    body = json.loads(content.parts[0].text)
    assert refreshes == 1
    assert attempts == [(3, False)]
    assert old_quotes == [3] and fresh_quotes == [3]
    assert body["complete"] and body["next"] is None
    assert body["plan_delivery"]["next_position"] == 3


def test_resource_refresh_then_smaller_page_preserves_remaining_plan(source_info):
    content, attempts, old_quotes, fresh_quotes, refreshes = _run_refresh_page(source_info, fresh_limit=1)
    body = json.loads(content.parts[0].text)
    assert refreshes == 1
    assert attempts == [(3, False), (2, False), (1, False)]
    assert old_quotes == [3] and fresh_quotes == [3, 2, 1]
    assert not body["complete"]
    assert body["frames"][0]["observation_position"] == 0
    assert body["next"]["arguments"]["continuation"]["position"] == {"next_observation": 1}


def test_resource_refresh_still_cannot_fit_minimum_page_is_typed(source_info):
    with pytest.raises(MediaError) as failure:
        _run_refresh_page(source_info, fresh_limit=0)
    assert failure.value.code == "RESOURCE_BOUNDARY"
    assert "minimum readable frame" in str(failure.value)


def test_source_change_while_refreshing_cannot_publish_an_old_frame_page(source_info):
    with pytest.raises(MediaError) as failure:
        _run_refresh_page(source_info, fresh_limit=3, mutate_source=True)
    assert failure.value.code == "MEDIA_SOURCE_CHANGED"


def _run_refresh_page(source_info, *, fresh_limit, mutate_source=False):
    """Exercise the real page controller/predicate; replace only decode/validation."""
    import asyncio
    import io
    from time import monotonic
    from PIL import Image
    from pulsara_agent.conversation_kernel.tool_contracts import (
        FrozenImageToolResourceAllowance, FrozenImageToolResourceIncrement,
    )
    from pulsara_agent.identity import derive_id
    from pulsara_agent.llm.input import FrozenPromptContent, LLMImagePart, LLMTextPart
    from pulsara_agent.media.execution import render_prepared_frame_page
    from pulsara_agent.media.frames import FrameBudget, FrameObservation, FramePlan
    from pulsara_agent.primitives.tool_result_budget import ToolResultPageBudget
    from pulsara_agent.tools.builtins.filesystem import ViewImageSource, ViewImageSourceKind

    source, info = source_info
    plan = FramePlan(
        priority=tuple(FrameObservation(FramePosition(t, Fraction(1)), Fraction(t), Fraction(t + 1)) for t in [0, 9, 4]),
        requested_range=(0.0, 10.0), processed_range=(Fraction(0), Fraction(10)),
        mode="overview", crop=None, display_aspect=Fraction(16, 9), complete=True, video_ended=False,
    )
    encoded = io.BytesIO()
    Image.new("RGB", (320, 180), "blue").save(encoded, format="PNG")
    payload = encoded.getvalue()
    attempts, old_quotes, fresh_quotes = [], [], []
    refreshes = 0

    class Worker:
        async def run(self, operation, actual_source, actual_info, actual_plan, **kwargs):
            assert operation.__name__ == "render_frame_page"
            assert actual_source is source and actual_info is info and actual_plan is plan
            offset, count = kwargs["offset"], kwargs["count"]
            attempts.append((count, kwargs["floor"]))
            observations = sorted(plan.priority[offset:offset + count], key=lambda item: item.t)
            body = {"video_frames": True, "frames": [{"t": float(item.t)} for item in observations],
                    "next": None, "limitations": []}
            return FrameReadResult((FrameImage(payload, "image/png", 320, 180),), json.dumps(body), (0, 10), False)

    class Validator:
        async def freeze(self, prompt, **kwargs):
            return FrozenPromptContent((
                LLMTextPart(prompt.parts[0].text),
                LLMImagePart("image/png", payload, 320, 180),
            ))

    class Quote:
        def __init__(self, limit, records):
            self.limit, self.records = limit, records

        def quote(self, *, content, **kwargs):
            count = len(json.loads(content.parts[0].text)["frames"])
            self.records.append(count)
            return FrozenImageToolResourceIncrement(0, 0, 0 if count <= self.limit else 1, 0)

    old = FrozenImageToolResourceAllowance(0, "refresh-call", "binding", 100000, 100000, 0, 100000, Quote(0, old_quotes))
    fresh = replace(old, quote_owner=Quote(fresh_limit, fresh_quotes))
    page_budget = ToolResultPageBudget("refresh-call")

    async def refresh():
        nonlocal refreshes
        refreshes += 1
        if mutate_source:
            source.path.write_bytes(b"changed while preparing the successor")
        return fresh, page_budget

    async def run():
        return await render_prepared_frame_page(
            source=source, info=info, plan=plan, position=FramePagePosition(),
            budget=FrameBudget(1 << 20, 1280 * 720, 10, 256), allowance=old,
            page_budget=page_budget, image_source=ViewImageSource(ViewImageSourceKind.PATH, str(source.path)),
            workspace_id=derive_id("workspace", "test", "frame-refresh"), worker=Worker(),
            image_validator=Validator(), deadline=monotonic() + 30,
            refresh_image_resources=refresh,
        )

    result = asyncio.run(run())
    return result, attempts, old_quotes, fresh_quotes, refreshes
