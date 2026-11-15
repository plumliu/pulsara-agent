"""Actual media annotation production freezes its private binding with C."""

import asyncio
from datetime import datetime, timezone
import os
from time import monotonic
from types import SimpleNamespace

import pytest

from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.media_annotations import read_media_annotations
from pulsara_agent.conversation_kernel.repository import ConversationKernelRepository
from pulsara_agent.conversation_kernel.tool_outputs import (
    PostgresToolOutputReadPort,
    project_tool_result_export,
)
from pulsara_agent.conversation_kernel.tool_policy import DefaultToolDispatchAuthorizationPolicy
from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
from pulsara_agent.identity import random_id
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.primitives.tool_observation import ToolObservationOrigin
from tests.support.model_config import scheduled_task_service
from tests.support.postgres import verified_postgres_provider
from tests.support.round3 import direct_tool_invocation_context
from tests.support.tool_outputs import _install_tool_call
from tests.test_hardcut_entry_parts import read
from tests.test_hardcut_tool_settlement import executor
from tests.test_media_source import wav

pytestmark = pytest.mark.postgres


@pytest.fixture
def repository(stage2_migrated_postgres_database):
    return ConversationKernelRepository(
        verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    )


@pytest.mark.parametrize("valid_range", [True, False])
def test_actual_annotation_producer_acceptance_and_frozen_file_readback(repository, tmp_path, valid_range):
    path = tmp_path / "sound.wav"
    wav(path)
    original_mtime = str(path.stat().st_mtime_ns)
    text = "original frozen observation\nwith a second line"
    arguments = {"target": {"kind": "PATH", "path": "sound.wav"},
                 "from": 0.01, "to": 0.05 if valid_range else 2, "text": text}
    workspace = random_id("workspace")
    lease, turn, assistant, call, attempt = _install_tool_call(
        repository, workspace, tool_name="annotate_media", arguments=arguments)
    target = SimpleNamespace(session_id=lease.guard.session_id, workspace_id=workspace,
        turn_id=turn, assistant_entry_id=assistant, tool_call_id=call, attempt_id=attempt,
        result_id=random_id("result"), result_entry_id=random_id("entry"))

    async def scenario():
        port = DirectKernelToolPort(
            workspace_root=tmp_path, host_owner_id=random_id("host"),
            session_id=lease.guard.session_id, live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
            scheduled_tasks=scheduled_task_service())
        borrow, context = direct_tool_invocation_context(
            port, session_id=lease.guard.session_id, workspace_id=workspace,
            tool_name="annotate_media", tool_call_id=call, attempt_id=attempt,
            turn_id=turn, assistant_entry_id=assistant)
        try:
            result = await port.invoke(
                tool_name="annotate_media", arguments=arguments, tool_call_id=call,
                attempt_id=attempt, turn_id=turn, assistant_entry_id=assistant,
                invocation_context=context)
        finally:
            borrow.close()
            await port.aclose()
        # The saved binding describes the file the operation validated, even if
        # the user changes that file before the known outcome is accepted.
        os.utime(path, ns=(path.stat().st_atime_ns, int(original_mtime) + 1_000_000))
        execution = executor(repository, lease)
        await execution._settle_known_tool_result(
            session_id=target.session_id, turn_id=turn, assistant_entry_id=assistant,
            tool_name="annotate_media", tool_call_id=call, invocation_arguments=arguments,
            call_ordinal=0, scope_kind=ModelInputScopeKind.ROOT,
            result_id=target.result_id, result_entry_id=target.result_entry_id,
            attempt_id=attempt, workspace_id=workspace, result=result,
            observed_at=datetime.now(timezone.utc), observation_origin=ToolObservationOrigin.BUILTIN,
            live_sink=None, tool_result_block_id=random_id("block"), live_attribution={})
        return result

    result = asyncio.run(scenario())
    public = PostgresToolOutputReadPort(repository.connection_provider,
        session_id=target.session_id, workspace_id=workspace).read_result(
            {"kind": "SESSION_ENTRY", "entry_id": target.result_entry_id}).value
    exported = project_tool_result_export(public)
    with read(repository) as connection:
        stored = connection.execute(
            "SELECT result_facts,public_fields FROM pulsara_v3.tool_results WHERE id=%s",
            (target.result_id,)).fetchone()
        frozen_call = connection.execute(
            "SELECT tool_arguments FROM pulsara_v3.content_parts WHERE entry_id=%s AND tool_call_id=%s",
            (assistant, call)).fetchone()["tool_arguments"]
    assert frozen_call == arguments
    assert text.encode() not in exported
    assert b"canonical_path" not in exported and b"file_mtime_ns" not in exported
    assert str(path).encode() not in exported and original_mtime.encode() not in exported
    notes = read_media_annotations(repository, memory_domain_id="u_local", path=str(path),
        modified_ns=original_mtime, after=None, deadline_monotonic=monotonic()+30)
    if valid_range:
        assert result.state == "SUCCESS"
        assert stored["result_facts"] == {"canonical_path": str(path), "file_mtime_ns": original_mtime}
        assert public == {"target": {"kind": "PATH", "path": "sound.wav"},
            "meta": {"status": "SUCCESS"}, "content": ({"type": "text", "value": "已添加时间标注。"},)}
        assert stored["public_fields"] == {"target": {"kind": "PATH", "path": "sound.wav"}}
        assert notes == {"items": [{"resultId": target.result_entry_id, "from": 0.01, "to": 0.05, "text": text}], "next": None}
    else:
        assert result.state == "APPLICATION_ERROR"
        assert stored["result_facts"] == {} and notes == {"items": [], "next": None}
    changed = read_media_annotations(repository, memory_domain_id="u_local", path=str(path),
        modified_ns=str(path.stat().st_mtime_ns), after=None, deadline_monotonic=monotonic()+30)
    assert changed == {"items": [], "next": None}
