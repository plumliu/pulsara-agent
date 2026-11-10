"""Canonical annotation readback crosses sessions only inside the access domain."""

import json
from datetime import datetime, timezone
from time import monotonic

import pytest

from pulsara_agent.conversation_kernel import media_annotations
from pulsara_agent.conversation_kernel.repository import (
    build_prepared_tool_result_acceptance,
)
from pulsara_agent.conversation_kernel.tool_outputs import ToolOutputProcessor
from pulsara_agent.identity import random_id
from pulsara_agent.ports.tool_execution import ToolOutputSourceFormatHint
from pulsara_agent.primitives.tool_observation import ToolObservationOrigin
from tests.support.tool_outputs import _install_tool_call, prepare_output
from tests.test_conversation_fork import repo as repo, final, fork

pytestmark = pytest.mark.postgres


def test_committed_annotations_keyset_source_forks_and_permissions(repo, monkeypatch):
    # Force multiple source pages without changing any product budget.
    monkeypatch.setattr(media_annotations, "SESSION_QUERY_MAX_ITEMS", 1)
    text = "exact frozen text " * 1200
    path = "/clips/test.mkv"

    def create(
        *,
        modified="999000001234567890",
        source_path=path,
        state="SUCCESS",
        domain="u_local",
    ):
        workspace = random_id("workspace")
        lease, turn, request, call, attempt = _install_tool_call(
            repo,
            workspace,
            tool_name="annotate_media",
            memory_domain_id=domain,
            arguments={
                "target": {"kind": "PATH", "path": "test.mkv"},
                "from": 1,
                "to": 2,
                "text": text,
            },
        )
        entry = random_id("entry")
        body = json.dumps(
            {
                "canonical_path": source_path,
                "file_mtime_ns": modified,
                "media_annotation": "added",
            }
        )
        projection = prepare_output(
            ToolOutputProcessor(repo.connection_provider),
            session_id=lease.guard.session_id,
            workspace_id=workspace,
            result_entry_id=entry,
            public_output=body,
            native_output="added",
            output_body_candidate=None,
            output_omitted=True,
            public_format=ToolOutputSourceFormatHint.JSON,
            deadline_monotonic=monotonic() + 30,
        )
        candidate = build_prepared_tool_result_acceptance(
            guard=lease.guard,
            workspace_id=workspace,
            result_id=random_id("result"),
            result_entry_id=entry,
            turn_id=turn,
            assistant_entry_id=request,
            tool_call_id=call,
            attempt_id=attempt,
            result_state=state,
            canonical_preview_content=projection.canonical_preview,
            output_body=projection.output_body,
            source_coverage=projection.source_coverage,
            output_omitted=True,
            source_coverage_reason=projection.source_coverage_reason,
            output_unavailability_reason=None,
            required_facts={},
            actor_id="tool",
            observed_at=datetime.now(timezone.utc),
            observation_duration_microseconds=None,
            observation_origin_kind=ToolObservationOrigin.BUILTIN,
            trusted_tool_reported_duration_microseconds=None,
        )
        repo.accept_tool_result(
            lease.guard, candidate=candidate, deadline_monotonic=monotonic() + 30
        )
        return entry, lease, final(repo, lease.guard, turn)

    first, lease, anchor = create()
    assert fork(repo, lease.guard.session_id, anchor).created
    second, _, _ = create()
    create(modified="999000001234567891")
    create(source_path="/clips/renamed.mkv")
    create(state="APPLICATION_ERROR")
    create(domain="different_user")
    after = None
    notes = []
    while True:
        page = media_annotations.read_media_annotations(
            repo,
            memory_domain_id="u_local",
            path=path,
            modified_ns="999000001234567890",
            after=after,
            deadline_monotonic=monotonic() + 30,
        )
        notes.extend(page["items"])
        after = page["next"]
        if after is None:
            break
    assert len(notes) == 2
    assert all(set(n) == {"resultId", "from", "to", "text"} for n in notes)
    assert {n["resultId"] for n in notes} == {first, second}
    assert all(n["text"] == text and (n["from"], n["to"]) == (1, 2) for n in notes)

    # Archived ROOT sources remain canonical history, with the same access-domain
    # authorization; reviewing them never resumes or unarchives a Host.
    repo.archive_session(session_id=lease.guard.session_id, memory_domain_id="u_local",
        closed_writer=lease.guard, deadline_monotonic=monotonic()+30)
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from pulsara_agent.desktop_app.session_controller import LocalSessionController
    from pulsara_agent.conversation_kernel.host import default_permission_policy
    from tests.test_conversation_fork import rows
    async def inspect_archived():
        core = SimpleNamespace(_ensure_resources=AsyncMock(return_value=repo),
            _canonical_deadline=lambda:monotonic()+30, mcp_management=SimpleNamespace(lane=asyncio.Lock()),
            read_resumable_session=AsyncMock(side_effect=AssertionError("must not resume source")))
        controller = LocalSessionController(core=core, permission_policy=default_permission_policy(), active_skill_names=frozenset())
        snapshot = await controller.history_snapshot(lease.guard.session_id)
        assert snapshot.snapshot.snapshot.session_id == lease.guard.session_id
        window = await controller.history_window(lease.guard.session_id, {"anchor_entry_id": first})
        assert first in {entry["entry_id"] for entry in window["entries"]}
        assert controller._by_session == {}
        core.read_resumable_session.assert_not_awaited()
        foreign = LocalSessionController(core=core,memory_domain_id="different_user",permission_policy=default_permission_policy(),active_skill_names=frozenset())
        with pytest.raises(KeyError):
            await foreign.history_snapshot(lease.guard.session_id)
        with pytest.raises(KeyError):
            await foreign.history_window(lease.guard.session_id,{"anchor_entry_id":first})
    asyncio.run(inspect_archived())
    assert rows(repo,"SELECT lifecycle FROM pulsara_v3.sessions WHERE id=%s",(lease.guard.session_id,))[0]["lifecycle"] == "ARCHIVED"
