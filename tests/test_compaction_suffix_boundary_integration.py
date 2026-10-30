"""Real adopted input boundaries preserve late messages and enforce final wire limits."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from io import BytesIO
from threading import Event

from PIL import Image
import pytest

from pulsara_agent.conversation_kernel.compaction.contracts import ResolvedCompactionPolicy
from pulsara_agent.conversation_kernel.direct_model import DirectKernelModelPort
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import PromptContent, PromptImagePart
from pulsara_agent.llm.errors import ModelTargetCapabilityMismatch
from pulsara_agent.model_input.contracts import StructuredModelInputCompileError
from pulsara_agent.workspace_identity import HostWorkspaceInput
from tests.support.model_config import test_model_binding, test_model_limits, test_model_runtime
from tests.test_compaction_request_start_integration import (
    _SimpleModel, _eventually, _render, _rows, _seed_history, _wait_summary,
)

pytestmark = pytest.mark.postgres


async def _open_root(tmp_path, database, monkeypatch):
    import pulsara_agent.conversation_kernel.host as host
    monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
    model = _SimpleModel()
    runtime = test_model_runtime(
        model_id="test-pro", wire_api="openai_responses",
        postgres_dsn=database.runtime_dsn, input_modalities=("text",),
        limits=test_model_limits(max_input_tokens=47_192, max_output_tokens=1_000,
            default_output_tokens=1_000, input_safety_margin_tokens=0),
    )
    model._model_runtime = runtime
    model._preparer = DirectKernelModelPort(model_runtime=runtime)
    monkeypatch.setattr(host, "DirectKernelModelPort", lambda **_: model)
    monkeypatch.setattr(host.LocalMcpManagementService, "load_configs", lambda *_a, **_k: ())
    core = KernelHostCore.production(model_runtime=runtime)
    root = await core.open_session(HostWorkspaceInput(workspace_kind="project", workspace_root=tmp_path))
    try:
        await root.update_model_call_binding(test_model_binding(runtime))
        root._compaction.policy = ResolvedCompactionPolicy(automatic_enabled=False, minimum_reclaim_tokens=1)
        _seed_history(root, repetitions=150)
        usage = await root.read_context_usage()
        ratio = usage["input_tokens"] / usage["input_budget_tokens"]
        root._compaction.policy = replace(root._compaction.policy, automatic_enabled=True,
            auto_trigger_ratio=ratio * 0.95, post_compaction_target_ratio=ratio * 0.85)
    except BaseException:
        await core.shutdown()
        raise
    return core, root, model


async def _begin_summary(root, model):
    command = random_id("command")
    await root.submit_prompt(command_id=command, content=PromptContent.text("EXACT_CURRENT_REQUEST"))
    await _wait_summary(model, root, command, None)
    turn_id = root.active_root_turn_id()
    assert turn_id is not None and not model.root_requests
    return command, turn_id


def test_postadopt_snapshot_keeps_late_steer_for_next_boundary(
    tmp_path, monkeypatch, stage2_migrated_postgres_database,
):
    async def scenario():
        core, root, model = await _open_root(tmp_path, stage2_migrated_postgres_database, monkeypatch)
        snapshot_started, release_snapshot = Event(), Event()
        original_read = root.repository.read_pending_prompt_steer_facts
        captured = False
        snapshot_ids = []
        def read_one_adopted_snapshot(*args, **kwargs):
            nonlocal captured
            facts = original_read(*args, **kwargs)
            revisions = _rows(root,
                "SELECT r.revision_ordinal FROM pulsara_v3.turns t "
                "JOIN pulsara_v3.turn_context_binding_revisions r "
                "ON r.session_id=t.session_id AND r.id=t.current_context_binding_revision_id "
                "WHERE t.session_id=%s AND t.id=%s",
                (root.session_id, kwargs["target_turn_id"]))
            if not captured and revisions and revisions[0]["revision_ordinal"] > 0:
                captured = True
                snapshot_ids.extend(fact.queue_item_id for fact in facts)
                snapshot_started.set()
                if not release_snapshot.wait(10):
                    raise TimeoutError("test adopted snapshot was not released")
            return facts
        monkeypatch.setattr(root.repository, "read_pending_prompt_steer_facts", read_one_adopted_snapshot)
        try:
            command, target = await _begin_summary(root, model)
            early = await root.steer_active_turn(command_id=random_id("command"),
                content=PromptContent.text("EARLY_SELECTED_STEER"), target_turn_id=target)
            assert early.status == "PENDING"
            model.summary.release.set()
            assert await asyncio.to_thread(snapshot_started.wait, 10)
            late = await root.steer_active_turn(command_id=random_id("command"),
                content=PromptContent.text("LATE_AFTER_SNAPSHOT_STEER"), target_turn_id=target)
            assert late.status == "PENDING"
            assert snapshot_ids == [early.prompt_delivery.queue_item_id]
            release_snapshot.set()
            async def finished():
                result = await root.query_command(command)
                assert result.status != "REJECTED", result
                return result.public_code == "TURN_COMPLETED"
            await _eventually(finished)
            assert len(model.summary.calls) == 1
            assert len(model.root_requests) == 2
            first, second = (request.compiled_input for request in model.root_requests)
            assert "EARLY_SELECTED_STEER" in _render(first.messages)
            assert "LATE_AFTER_SNAPSHOT_STEER" not in _render(first.messages)
            assert "LATE_AFTER_SNAPSHOT_STEER" in _render(second.messages)
            assert second.system_prompt == first.system_prompt and second.tools == first.tools
            assert second.messages[:len(first.messages)] == first.messages
            queues = _rows(root,
                "SELECT id,status,target_turn_id,consumed_entry_id FROM pulsara_v3.prompt_queue_items "
                "WHERE session_id=%s AND id=ANY(%s) ORDER BY queue_sequence",
                (root.session_id, [early.prompt_delivery.queue_item_id, late.prompt_delivery.queue_item_id]))
            assert [row["status"] for row in queues] == ["CONSUMED", "CONSUMED"]
            assert all(row["target_turn_id"] == target and row["consumed_entry_id"] for row in queues)
        finally:
            release_snapshot.set()
            model.summary.release.set()
            await core.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("suffix_kind", ["over_budget", "unsupported_image"])
def test_postadopt_illegal_suffix_never_opens_ordinary_provider(
    tmp_path, monkeypatch, stage2_migrated_postgres_database, suffix_kind,
):
    async def scenario():
        core, root, model = await _open_root(tmp_path, stage2_migrated_postgres_database, monkeypatch)
        try:
            command, target = await _begin_summary(root, model)
            execution = root._active_task
            assert execution is not None
            if suffix_kind == "over_budget":
                content = PromptContent.text("OVERSIZED_EXACT_STEER " + "z" * 300_000)
            else:
                buffer = BytesIO()
                Image.new("RGB", (8, 8), (21, 43, 65)).save(buffer, "PNG")
                content = PromptContent((PromptImagePart(buffer.getvalue(), "image/png"),))
            steer = await root.steer_active_turn(command_id=random_id("command"),
                content=content, target_turn_id=target)
            assert steer.status == "PENDING"
            model.summary.release.set()
            async def ended():
                result = await root.query_command(command)
                return result.status != "PENDING"
            await _eventually(ended)
            results = await asyncio.gather(execution, return_exceptions=True)
            expected_error = (StructuredModelInputCompileError if suffix_kind == "over_budget"
                              else ModelTargetCapabilityMismatch)
            assert isinstance(results[0], expected_error), results
            assert len(model.summary.calls) == 1
            assert model.root_requests == []
            state = _rows(root,
                "SELECT t.status,t.terminal_reason,r.revision_ordinal FROM pulsara_v3.turns t "
                "JOIN pulsara_v3.turn_context_binding_revisions r "
                "ON r.session_id=t.session_id AND r.id=t.current_context_binding_revision_id "
                "WHERE t.session_id=%s AND t.id=%s", (root.session_id, target))[0]
            assert state["status"] == "INTERRUPTED"
            assert state["terminal_reason"]
            if suffix_kind == "over_budget":
                assert state["terminal_reason"] == "PROVIDER_INPUT_RESOURCE_EXHAUSTED"
            assert state["revision_ordinal"] == 1
            queue = _rows(root,
                "SELECT status,terminal_reason,target_turn_id,consumed_entry_id "
                "FROM pulsara_v3.prompt_queue_items WHERE session_id=%s AND id=%s",
                (root.session_id, steer.prompt_delivery.queue_item_id))[0]
            assert queue["status"] == "REJECTED" and queue["terminal_reason"]
            assert queue["target_turn_id"] == target and queue["consumed_entry_id"] is None
            assert not _rows(root,
                "SELECT id FROM pulsara_v3.prompt_queue_items "
                "WHERE session_id=%s AND target_turn_id=%s AND status='PENDING'",
                (root.session_id, target))
        finally:
            model.summary.release.set()
            await core.shutdown()
    asyncio.run(scenario())
