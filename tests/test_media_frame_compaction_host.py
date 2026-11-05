"""A scripted business model sees real frame pages after Host compaction.

Only the provider's response and the test's resource limit are controlled.
The production Host, worker, decoder, frame owner, canonical image storage,
compaction transport and final normal-request assembly remain in the path.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from time import monotonic

import pytest

from pulsara_agent.conversation_kernel.compaction.contracts import ResolvedCompactionPolicy
from pulsara_agent.conversation_kernel.direct_model import DirectKernelModelPort
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.conversation_kernel.prompt_content import bind_prompt_images
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import LLMImagePart, LLMTextPart, MessageRole, PromptContent, PromptImagePart
from pulsara_agent.media import frames
from pulsara_agent.media.source import MediaSource, probe_media
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.tool_result_projection import conservative_tool_result_page_logical_utf8_bytes
from pulsara_agent.tools.builtins.media import parse_image_tool_source
from pulsara_agent.workspace_identity import HostWorkspaceInput
from tests.support.model_config import test_model_binding as model_binding, test_model_runtime as model_runtime
from tests.test_compaction_request_start_integration import _Model, _rows, _seed_history
from tests.test_media_overview_policy import make_video
from tests.test_round4_plan_host import _text, _tool_call

pytestmark = pytest.mark.postgres


def _image_signature(image):
    return image.media_type, image.width, image.height, image.image_ref, image.immutable_bytes


def _tool_public_result(request, call_id):
    messages = request.compiled_input.messages
    results = [message for message in messages
               if message.role is MessageRole.TOOL_RESULT and message.tool_call_id == call_id]
    assert len(results) == 1
    envelope = json.loads("".join(part.text for part in results[0].content if isinstance(part, LLMTextPart)))
    return envelope["pulsara_tool_result"]


def _delivered_page(request, call_id):
    messages = request.compiled_input.messages
    public = _tool_public_result(request, call_id)
    assert public["result_state"] == "SUCCESS"
    body_text = public["body"]
    body = json.loads(body_text)
    assert body["video_frames"] is True and body["frames"]
    attachments = []
    for message in messages:
        owner = None
        for part in message.content:
            if isinstance(part, LLMTextPart):
                try:
                    value = json.loads(part.text)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict) and "tool_image_source" in value:
                    owner = value["tool_image_source"]["tool_call_id"]
            elif isinstance(part, LLMImagePart) and owner == call_id:
                attachments.append(part)
    assert attachments
    assert {frame["image"] for frame in body["frames"]} == set(range(1, len(attachments) + 1))
    assert any(call.id == call_id for message in messages for call in message.tool_calls)
    return body_text, body, tuple(attachments)


class _FramePagingModel(_Model):
    def __init__(self, runtime, path, *, mode="overview"):
        super().__init__()
        self._model_runtime = runtime
        self._preparer = DirectKernelModelPort(model_runtime=runtime)
        self.path = path
        self.mode = mode
        self.rendered = {}
        self.pages = []
        self.observations = []
        self.last_call = None
        self.positions = []
        self.normal_after_page = []
        self.finished = False
        self.root = None
        self.first_binding = None
        self.summary.release.set()

    async def _stream(self, request):
        self.root_requests.append(request)
        if self.last_call is None:
            self.first_binding = request.cut.context_binding_revision_id
            self.last_call = random_id("call")
            events = _tool_call("read_video_frames", self.last_call, {
                "target": {"kind": "PATH", "path": str(self.path)},
                "from": 0, "to": 2 if self.mode == "detail" else 22, "mode": self.mode,
            })
        else:
            text, body, images = _delivered_page(request, self.last_call)
            expected_text, expected_images = self.rendered[self.last_call]
            assert text == expected_text
            assert tuple(map(_image_signature, images)) == tuple(map(_image_signature, expected_images))
            assert self.summary.calls, "No compaction occurred between the actual page and its normal request"
            assert request.cut.context_binding_revision_id != self.first_binding
            self.normal_after_page.append(request)
            self.pages.append(body)
            if self.mode == "detail":
                assert body["sampling"] == "sparse"
                assert body["scanned_ranges"] == body["observed_cut_times"] == []
                assert body["plan_delivery"]["observations"] == 3
                assert body["plan_delivery"]["position"] == len(self.observations)
                assert body["frames"][0]["observation_position"] == body["plan_delivery"]["position"]
            # Subsequent source pages exercise copied-next delivery rather than
            # repeatedly compacting already consumed pages.
            self.root._compaction.policy = replace(self.root._compaction.policy, automatic_enabled=False)
            for frame in body["frames"]:
                assert frame["observation_position"] not in self.positions
                self.positions.append(frame["observation_position"])
                if self.mode == "detail":
                    self.observations.append((frame["t"], frame["observation_position"], frame["visible_range"]))
            if body["next"] is not None:
                assert not body["complete"]
                assert body["next"]["tool"] == "read_video_frames"
                if self.mode == "detail":
                    arguments = body["next"]["arguments"]
                    assert arguments["mode"] == "detail"
                    assert (arguments["from"], arguments["to"]) == (0, 2)
                    assert arguments["target"] == {"kind": "PATH", "path": str(self.path)}
                    assert arguments["continuation"]["position"] == {
                        "next_observation": body["plan_delivery"]["next_position"],
                    }
                    assert body["plan_delivery"]["next_position"] == len(self.observations)
                self.last_call = random_id("call")
                events = _tool_call(body["next"]["tool"], self.last_call, body["next"]["arguments"])
            else:
                assert body["complete"]
                if self.mode == "detail":
                    assert body["plan_delivery"]["next_position"] == 3
                self.finished = True
                events = _text("FRAME_PAGES_RETAINED_AND_DRAINED")
        for event in events:
            yield event


@pytest.mark.parametrize("mode", ["overview", "detail"])
def test_host_compaction_keeps_the_real_current_page_and_copied_next(
    tmp_path, monkeypatch, isolated_migrated_postgres_database, mode,
):
    import pulsara_agent.conversation_kernel.host as host_module
    import pulsara_agent.media.execution as execution_module
    import pulsara_agent.primitives.tool_result_budget as budget_module

    if mode == "detail":
        # Start and midpoint share pixels, while the tail is a different image.
        path = make_video(tmp_path / "detail.mkv", ["red"] * 7 + ["blue"], rate=4)
    else:
        path = make_video(tmp_path / "map.mkv", [
            (i * 47 % 256, i * 83 % 256, i * 131 % 256) for i in range(22)
        ], rate=1)
    end = 2 if mode == "detail" else 22
    frame_budget = frames.FrameBudget(8_000_000, 8 * 1280 * 720, 16, frames.FRAME_ANALYSIS_FRAMES)
    with MediaSource.open(path) as source:
        info = probe_media(source)
        plan = frames.prepare_frame_plan(source, info, start=0, end=end, mode=mode)
        full = frames.read_video_frames(source, info, start=0, end=end, mode=mode, budget=frame_budget)
        one = frames.read_video_frames(source, info, start=0, end=end, mode=mode, budget=frame_budget,
                                      accepts=lambda result: len(json.loads(result.text)["frames"]) == 1)
    call_id = random_id("call")
    full_cost = conservative_tool_result_page_logical_utf8_bytes(
        tool_call_id=call_id, body=full.text, model_visible_memory_ids=(),
    )
    atom_cost = conservative_tool_result_page_logical_utf8_bytes(
        tool_call_id=call_id, body=one.text, model_visible_memory_ids=(),
    )
    if mode == "detail":
        # One physical image admits the first two shared observations, then next
        # delivers the third. Real source/rendering and provider quote remain.
        monkeypatch.setattr(execution_module, "MAXIMUM_PROMPT_IMAGE_PIXELS", 1280 * 720)
    else:
        assert atom_cost < full_cost
        monkeypatch.setattr(budget_module, "MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES",
                            (atom_cost + full_cost) // 2)


    async def scenario():
        monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
        runtime = model_runtime(postgres_dsn=isolated_migrated_postgres_database.runtime_dsn,
                                model_id="test-pro", wire_api="openai_chat_completions",
                                input_modalities=("text", "image"))
        model = _FramePagingModel(runtime, path, mode=mode)
        monkeypatch.setattr(host_module, "DirectKernelModelPort", lambda **_: model)
        monkeypatch.setattr(host_module.LocalMcpManagementService, "load_configs", lambda *_a, **_k: ())
        core = KernelHostCore.production(model_runtime=runtime)
        root = await core.open_session(HostWorkspaceInput(workspace_kind="project", workspace_root=tmp_path))
        model.root = root
        original_render = execution_module.render_prepared_frame_page
        force_compaction = True
        try:
            await root.update_model_call_binding(model_binding(runtime))
            root._compaction.policy = ResolvedCompactionPolicy(automatic_enabled=False, minimum_reclaim_tokens=1)
            _seed_history(root, segments=5, repetitions=150)
            usage = await root.read_context_usage()
            ratio = usage["input_tokens"] / usage["input_budget_tokens"]
            forced_policy = replace(root._compaction.policy, automatic_enabled=True,
                                    auto_trigger_ratio=ratio * 0.95, post_compaction_target_ratio=ratio * 0.85)

            async def observe_real_page(**kwargs):
                nonlocal force_compaction
                content = await original_render(**kwargs)
                current = kwargs["page_budget"].tool_call_id
                assert current not in model.rendered, "Compaction re-executed the logical frame tool"
                text = "".join(part.text for part in content.parts if isinstance(part, LLMTextPart))
                images = tuple(part for part in content.parts if isinstance(part, LLMImagePart))
                assert images and json.loads(text)["frames"]
                model.rendered[current] = text, images
                if force_compaction:
                    force_compaction = False
                    root._compaction.policy = forced_policy
                return content

            monkeypatch.setattr(execution_module, "render_prepared_frame_page", observe_real_page)
            async with asyncio.timeout(90):
                await root.run_turn(PromptContent.text("Inspect the exact supplied frame pages; do not use saved memory."),
                                    command_id=random_id("command"),
                                    requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            assert model.finished, _rows(
                root, "SELECT status,terminal_reason FROM pulsara_v3.turns WHERE session_id=%s ORDER BY accepted_at DESC LIMIT 1",
                (root.session_id,),
            )
            assert len(model.summary.calls) == 1
            assert len(model.rendered) == len(model.normal_after_page) > 1
            assert sorted(model.positions) == list(range(plan.observation_count))
            if mode == "detail":
                assert model.positions == [0, 1, 2]
                assert model.observations == [
                    (t, i, [t, t + 0.25]) for i, t in enumerate((0, 1, 1.75))
                ]
                # This is the page that actually crosses the compaction cut.
                # Sharing must survive without collapsing its independent PTS.
                first = model.pages[0]
                first_images = next(iter(model.rendered.values()))[1]
                assert len(first["frames"]) > len(first_images)
                assert first["frames"][0]["image"] == first["frames"][1]["image"] == 1
                assert [frame["t"] for frame in first["frames"][:2]] == [0, 1]
                assert [frame["observation_position"] for frame in first["frames"][:2]] == [0, 1]
                assert first["next"] is not None and not first["complete"]
            results = _rows(root, "SELECT tool_call_id,result_state FROM pulsara_v3.tool_results WHERE session_id=%s",
                            (root.session_id,))
            assert {row["tool_call_id"] for row in results} == set(model.rendered)
            assert len(results) == len(model.rendered)
            assert all(row["result_state"] == "SUCCESS" for row in results)
        finally:
            model.summary.release.set()
            await core.shutdown()

    asyncio.run(scenario())


class _RecoveringFrameModel(_Model):
    def __init__(self, runtime, paths, *, resource_case):
        super().__init__()
        self._model_runtime = runtime
        self._preparer = DirectKernelModelPort(model_runtime=runtime)
        self.paths = paths
        self.resource_case = resource_case
        self.root = None
        self.enable_recovery = None
        self.previous_calls = {}
        self.rendered = {}
        self.render_invocations = []
        self.positions = {str(path): [] for path in paths}
        self.pages = {str(path): [] for path in paths}
        self.finished = False
        self.first_binding = None

    async def _stream(self, request):
        self.root_requests.append(request)
        events = []
        if not self.previous_calls:
            self.first_binding = request.cut.context_binding_revision_id
            self.enable_recovery()
            for path in self.paths:
                call_id = random_id("call")
                self.previous_calls[call_id] = str(path)
                events.extend(_tool_call("read_video_frames", call_id, {
                    "target": {"kind": "PATH", "path": str(path)},
                    "from": 0, "to": 22, "mode": "overview",
                }))
        else:
            assert self.summary.calls
            assert request.cut.context_binding_revision_id != self.first_binding
            self.root._compaction.policy = replace(self.root._compaction.policy, automatic_enabled=False)
            following = {}
            for call_id, path in self.previous_calls.items():
                public = _tool_public_result(request, call_id)
                if self.resource_case == "minimum":
                    assert public["result_state"] == "APPLICATION_ERROR", public
                    body = json.loads(public["body"])
                    assert body["error"] == "RESOURCE_BOUNDARY"
                    assert body.get("next") is None
                    assert call_id not in self.rendered
                    assert not any(isinstance(part, LLMImagePart)
                                   for message in request.compiled_input.messages for part in message.content)
                    self.pages[path].append(body)
                    continue
                text, body, images = _delivered_page(request, call_id)
                expected_text, expected_images = self.rendered[call_id]
                assert text == expected_text
                assert tuple(map(_image_signature, images)) == tuple(map(_image_signature, expected_images))
                self.pages[path].append(body)
                for frame in body["frames"]:
                    position = frame["observation_position"]
                    assert position not in self.positions[path]
                    self.positions[path].append(position)
                if body["next"] is not None:
                    assert not body["complete"]
                    assert body["next"]["arguments"]["target"]["path"] == path
                    new_id = random_id("call")
                    following[new_id] = path
                    events.extend(_tool_call(body["next"]["tool"], new_id, body["next"]["arguments"]))
                else:
                    assert body["complete"]
            if following:
                self.previous_calls = following
            else:
                self.finished = True
                events.extend(_text("RECOVERED_REAL_FRAME_PAGES"))
        for event in events:
            yield event


async def _open_recovery_host(tmp_path, monkeypatch, database, *, paths, resource_case):
    import pulsara_agent.conversation_kernel.host as host_module
    import pulsara_agent.conversation_kernel.runner as runner_module
    import pulsara_agent.media.execution as execution_module

    monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
    runtime = model_runtime(postgres_dsn=database.runtime_dsn, model_id="test-pro",
                            wire_api="openai_chat_completions", input_modalities=("text", "image"))
    model = _RecoveringFrameModel(runtime, paths, resource_case=resource_case)
    monkeypatch.setattr(host_module, "DirectKernelModelPort", lambda **_: model)
    monkeypatch.setattr(host_module.LocalMcpManagementService, "load_configs", lambda *_a, **_k: ())
    core = KernelHostCore.production(model_runtime=runtime)
    root = await core.open_session(HostWorkspaceInput(workspace_kind="project", workspace_root=tmp_path))
    model.root = root
    records = {"old": {}, "fresh": {}, "preparations": [], "rejected_old": set()}
    all_old_rejected = asyncio.Event()
    try:
        await root.update_model_call_binding(model_binding(runtime))
        root._compaction.policy = ResolvedCompactionPolicy(automatic_enabled=False, minimum_reclaim_tokens=1)
        _seed_history(root, segments=5, repetitions=150)
        usage = await root.read_context_usage()
        ratio = usage["input_tokens"] / usage["input_budget_tokens"]
        recovery_policy = replace(root._compaction.policy, automatic_enabled=True,
                                  auto_trigger_ratio=ratio * 0.95, post_compaction_target_ratio=ratio * 0.85)
        model.enable_recovery = lambda: setattr(root._compaction, "policy", recovery_policy)

        hypothetical = {}
        frame_budget = frames.FrameBudget(8_000_000, 8 * 1280 * 720, 16, frames.FRAME_ANALYSIS_FRAMES)
        for path in paths:
            with MediaSource.open(path) as source:
                info = probe_media(source)
                whole = frames.read_video_frames(source, info, start=0, end=22, budget=frame_budget)
                one = frames.read_video_frames(source, info, start=0, end=22, budget=frame_budget,
                                              accepts=lambda candidate: len(json.loads(candidate.text)["frames"]) == 1)
            values = []
            for result in (whole, one):
                validated = await root._image_validator.freeze(PromptContent((
                    LLMTextPart(result.text),
                    *(PromptImagePart(image.data, image.mime) for image in result.images),
                )), deadline_monotonic=monotonic() + 20)
                values.append(bind_prompt_images(validated, root.workspace.workspace_key))
            hypothetical[str(path)] = tuple(values)

        quote_original = root._runner._quote_post_response_resources

        def exhaust_old_input_allocation(**kwargs):
            quote = quote_original(**kwargs)
            if quote.image_call_allowances and not records["old"]:
                # This is the controlled test boundary: the initial allocation
                # has no image-input tokens. The real quote owner is retained.
                allowances = tuple(replace(allowance, input_tokens=0)
                                   for allowance in quote.image_call_allowances)
                records["old"].update((allowance.tool_call_id, allowance) for allowance in allowances)
                return replace(quote, image_call_allowances=allowances)
            return quote

        monkeypatch.setattr(root._runner, "_quote_post_response_resources", exhaust_old_input_allocation)
        prepare_original = root._runner.compaction.prepare_pending_tool_batch

        async def prepare_real_pending_prefix(**kwargs):
            assert asyncio.current_task() is root._active_task
            records["preparations"].append(kwargs)
            return await prepare_original(**kwargs)

        monkeypatch.setattr(root._runner.compaction, "prepare_pending_tool_batch", prepare_real_pending_prefix)
        refresh_original = runner_module.refresh_tool_result_page_resources

        def refresh_real_resources(**kwargs):
            assert asyncio.current_task() is root._active_task
            allowances, budgets = refresh_original(**kwargs)
            changed = []
            for allowance in allowances:
                old = records["old"][allowance.tool_call_id]
                assert allowance.quote_owner is not old.quote_owner
                assert allowance.call_ordinal == old.call_ordinal
                assert allowance.executor_binding_fingerprint == old.executor_binding_fingerprint
                path = str(paths[allowance.call_ordinal])
                source = parse_image_tool_source("read_video_frames", {
                    "target": {"kind": "PATH", "path": path}, "from": 0, "to": 22, "mode": "overview",
                })
                whole, one = hypothetical[path]
                full_tokens = allowance.quote_owner.quote(tool_call_id=allowance.tool_call_id,
                    tool_name="read_video_frames", source=source, content=whole).input_tokens
                atom_tokens = allowance.quote_owner.quote(tool_call_id=allowance.tool_call_id,
                    tool_name="read_video_frames", source=source, content=one).input_tokens
                assert 0 < atom_tokens < full_tokens <= allowance.input_tokens
                if resource_case == "minimum":
                    allowance = replace(allowance, input_tokens=0)
                elif resource_case == "shrink" or (resource_case == "parallel" and allowance.call_ordinal == 0):
                    # This test-controlled fresh allocation exercises the real
                    # owner/renderer when compaction helps but a full page is
                    # still too expensive. It does not replace its quote.
                    allowance = replace(allowance, input_tokens=atom_tokens + (full_tokens - atom_tokens) // 3)
                records["fresh"][allowance.tool_call_id] = allowance
                changed.append(allowance)
            return tuple(changed), budgets

        monkeypatch.setattr(runner_module, "refresh_tool_result_page_resources", refresh_real_resources)
        quote_owner_original = runner_module._ToolResultResourceQuoteOwner.quote

        def observe_real_old_quote(owner, **kwargs):
            increment = quote_owner_original(owner, **kwargs)
            call_id = kwargs["tool_call_id"]
            old = records["old"].get(call_id)
            if old is not None and old.quote_owner is owner:
                records["rejected_old"].add(call_id)
                if len(records["rejected_old"]) == len(paths):
                    all_old_rejected.set()
            return increment

        monkeypatch.setattr(runner_module._ToolResultResourceQuoteOwner, "quote", observe_real_old_quote)
        render_original = execution_module.render_prepared_frame_page

        async def observe_real_render(**kwargs):
            call_id = kwargs["page_budget"].tool_call_id
            assert call_id not in model.render_invocations
            model.render_invocations.append(call_id)
            content = await render_original(**kwargs)
            text = "".join(part.text for part in content.parts if isinstance(part, LLMTextPart))
            images = tuple(part for part in content.parts if isinstance(part, LLMImagePart))
            model.rendered[call_id] = text, images
            return content

        monkeypatch.setattr(execution_module, "render_prepared_frame_page", observe_real_render)
        return core, root, model, records, all_old_rejected
    except BaseException:
        await core.shutdown()
        raise


@pytest.mark.parametrize("resource_case", ["full", "shrink", "minimum"])
def test_host_preaccept_recovery_requotes_real_dry_prefix_then_delivers_or_boundaries(
    tmp_path, monkeypatch, isolated_migrated_postgres_database, resource_case,
):
    path = make_video(tmp_path / "recovery.mkv", [
        (i * 47 % 256, i * 83 % 256, i * 131 % 256) for i in range(22)
    ], rate=1)

    async def scenario():
        core, root, model, records, _ = await _open_recovery_host(
            tmp_path, monkeypatch, isolated_migrated_postgres_database,
            paths=[path], resource_case=resource_case,
        )
        model.summary.release.set()
        try:
            async with asyncio.timeout(90):
                await root.run_turn(PromptContent.text("Inspect the actual frame pages; do not use saved memory."),
                                    command_id=random_id("command"),
                                    requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            assert model.finished
            assert len(model.summary.calls) == len(records["preparations"]) == 1
            assert set(records["old"]) == records["rejected_old"] == set(records["fresh"])
            first = model.pages[str(path)][0]
            if resource_case == "full":
                assert len(first["frames"]) == 16
                assert first["complete"] and first["next"] is None
                assert len(model.render_invocations) == 1
            elif resource_case == "shrink":
                assert 0 < len(first["frames"]) < 16
                assert not first["complete"] and first["next"] is not None
                assert sorted(model.positions[str(path)]) == list(range(16))
                assert len(model.rendered) == len(model.render_invocations) > 1
            else:
                assert first["error"] == "RESOURCE_BOUNDARY"
                assert not model.rendered and len(model.render_invocations) == 1
            results = _rows(root, "SELECT tool_call_id,result_state FROM pulsara_v3.tool_results WHERE session_id=%s",
                            (root.session_id,))
            assert len(results) == len(model.render_invocations)
            assert all(row["result_state"] == (
                "APPLICATION_ERROR" if resource_case == "minimum" else "SUCCESS"
            ) for row in results)
        finally:
            model.summary.release.set()
            await core.shutdown()

    asyncio.run(scenario())


def test_parallel_image_calls_wait_for_one_summary_and_receive_their_own_fresh_slots(
    tmp_path, monkeypatch, isolated_migrated_postgres_database,
):
    paths = [make_video(tmp_path / f"parallel-{index}.mkv", [
        ((i * 47 + index * 19) % 256, (i * 83 + index * 31) % 256,
         (i * 131 + index * 43) % 256) for i in range(22)
    ], rate=1) for index in range(2)]

    async def scenario():
        core, root, model, records, rejected = await _open_recovery_host(
            tmp_path, monkeypatch, isolated_migrated_postgres_database,
            paths=paths, resource_case="parallel",
        )
        running = None
        try:
            running = asyncio.create_task(root.run_turn(
                PromptContent.text("Inspect both videos' exact frame pages; do not use saved memory."),
                command_id=random_id("command"), requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
            ))
            async with asyncio.timeout(30):
                await model.summary.started.wait()
                await rejected.wait()
            assert not running.done()
            assert len(records["old"]) == len(records["rejected_old"]) == 2
            assert len(model.summary.calls) == len(records["preparations"]) == 1
            assert not records["fresh"]
            model.summary.release.set()
            async with asyncio.timeout(90):
                await running
            assert model.finished
            assert set(records["fresh"]) == set(records["old"])
            assert {allowance.call_ordinal for allowance in records["fresh"].values()} == {0, 1}
            smaller, full = (model.pages[str(path)][0] for path in paths)
            assert 0 < len(smaller["frames"]) < 16 and smaller["next"] is not None
            assert len(full["frames"]) == 16 and full["complete"] and full["next"] is None
            for path in paths:
                assert sorted(model.positions[str(path)]) == list(range(16))
            assert len(model.rendered) == len(model.render_invocations)
            assert len(model.summary.calls) == len(records["preparations"]) == 1
        finally:
            model.summary.release.set()
            if running is not None and not running.done():
                target = root.active_root_turn_id()
                if target is not None:
                    await root.request_stop_turn(
                        command_id=random_id("command"), target_turn_id=target,
                        admission_deadline_ms=root.control_admission_deadline_ms(),
                        expected_session_id=root.session_id, expected_host_session_id=root.host_session_id,
                    )
                await asyncio.gather(running, return_exceptions=True)
            await core.shutdown()

    asyncio.run(scenario())


def test_stop_while_parallel_image_refreshes_wait_for_summary_releases_the_batch(
    tmp_path, monkeypatch, isolated_migrated_postgres_database,
):
    paths = [make_video(tmp_path / f"cancel-{index}.mkv", [
        ((i * 47 + index * 19) % 256, (i * 83 + index * 31) % 256,
         (i * 131 + index * 43) % 256) for i in range(22)
    ], rate=1) for index in range(2)]

    async def scenario():
        core, root, model, records, rejected = await _open_recovery_host(
            tmp_path, monkeypatch, isolated_migrated_postgres_database,
            paths=paths, resource_case="parallel",
        )
        command_id = random_id("command")
        running = None
        try:
            running = asyncio.create_task(root.run_turn(
                PromptContent.text("Inspect both videos' exact frame pages; do not use saved memory."),
                command_id=command_id, requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
            ))
            async with asyncio.timeout(30):
                await model.summary.started.wait()
                await rejected.wait()
            target = root.active_root_turn_id()
            assert target is not None and not running.done()
            assert not records["fresh"] and not model.rendered
            await root.request_stop_turn(
                command_id=random_id("command"), target_turn_id=target,
                admission_deadline_ms=root.control_admission_deadline_ms(),
                expected_session_id=root.session_id, expected_host_session_id=root.host_session_id,
            )
            async with asyncio.timeout(30):
                await asyncio.gather(running, return_exceptions=True)
            query = await root.query_command(command_id)
            assert query.public_code == "USER_STOPPED"
            terminal = _rows(root, "SELECT status,terminal_reason FROM pulsara_v3.turns WHERE session_id=%s AND id=%s",
                             (root.session_id, target))
            assert terminal == [{"status": "INTERRUPTED", "terminal_reason": "USER_STOPPED"}]
            assert len(model.root_requests) == 1 and not model.finished
            assert not model.rendered and not records["fresh"]
            assert not root._compaction.has_active_work()
            assert not root._compaction_write_reservations
            assert not root._tools._media_worker._active
            assert not root._tools._media_worker._processes
            assert root._image_validator._active_process is None
            assert root._image_validator._active_connection is None
        finally:
            model.summary.release.set()
            if running is not None and not running.done():
                await asyncio.gather(running, return_exceptions=True)
            await core.shutdown()

    asyncio.run(scenario())
