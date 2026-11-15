"""Accept, exactly confirm and read the same complete frozen business value."""
from dataclasses import replace
from datetime import datetime, timezone
from time import monotonic

import pytest
from psycopg.rows import dict_row

from pulsara_agent.conversation_kernel.content_parts import ResultPart, read_content_parts
from pulsara_agent.conversation_kernel.repository import (
    AssistantToolCallBlock, ConversationKernelConflict, ConversationKernelRepository,
    build_prepared_tool_result_acceptance,
)
from pulsara_agent.conversation_kernel.tool_outputs import PostgresToolOutputReadPort, project_tool_result_export
from pulsara_agent.identity import random_id
from pulsara_agent.ports.tool_execution import thaw_tool_json_object
from pulsara_agent.primitives.context import freeze_json
from pulsara_agent.primitives.tool_observation import ToolObservationOrigin
from tests.support.postgres import verified_postgres_provider
from tests.test_hardcut_entry_parts import open_turn, read

pytestmark = pytest.mark.postgres


def prepare(repository, state="SUCCESS", parts=None):
    lease, intent, _, cut = open_turn(repository, text="run\n")
    assistant, call = random_id("entry"), "provider-call"
    repository.commit_assistant_message(lease.guard, cut=cut, entry_id=assistant,
        blocks=(AssistantToolCallBlock(random_id("block"), call, "test_explicit_result", freeze_json({"input": 7})),),
        occurred_at=datetime.now(timezone.utc), actor_id="model:test", complete_turn=False,
        deadline_monotonic=monotonic()+30)
    with read(repository) as connection:
        row = connection.execute("SELECT workspace_id,permission_snapshot_fingerprint FROM pulsara_v3.turns WHERE id=%s", (intent.turn_id,)).fetchone()
    attempt = repository.accept_tool_attempt(lease.guard, attempt_id=random_id("attempt"),
        assistant_entry_id=assistant, tool_call_id=call, authorization_kind="policy", authorization_reference="allow",
        actor_kind="runtime", actor_id="test", remote_idempotency_key=None, retry_of_attempt_id=None,
        permission_snapshot_fingerprint=row["permission_snapshot_fingerprint"], occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic()+30)
    candidate = build_prepared_tool_result_acceptance(guard=lease.guard, workspace_id=row["workspace_id"],
        result_id=random_id("result"), result_entry_id=random_id("entry"), turn_id=intent.turn_id,
        assistant_entry_id=assistant, tool_call_id=call, attempt_id=attempt.attempt_id, result_state=state,
        public_fields={"meta": {"exit_code": 23}, "structuredContent": None, "external": {"next": [2, 1]}},
        content_parts=parts if parts is not None else (ResultPart("text", "a\r\nb\n"), ResultPart("object", {"n": [1, 2.5, False, None]})),
        result_facts={"canonical_path": "/frozen/path", "file_mtime_ns": 123},
        observed_at=datetime.now(timezone.utc), observation_duration_microseconds=1200,
        observation_origin_kind=ToolObservationOrigin.BUILTIN, trusted_tool_reported_duration_microseconds=None, actor_id="test")
    return lease, candidate


@pytest.fixture
def repository(stage2_migrated_postgres_database):
    return ConversationKernelRepository(verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn))


@pytest.mark.parametrize("state,public_status", [("SUCCESS", "SUCCESS"), ("APPLICATION_ERROR", "ERROR"), ("CANCELLED", "CANCELLED")])
def test_accept_confirm_read_and_export_use_identical_frozen_value(repository, state, public_status):
    lease, candidate = prepare(repository, state)
    accepted = repository.accept_tool_result(lease.guard, candidate=candidate, deadline_monotonic=monotonic()+30)
    assert repository.confirm_tool_result_winner(lease.guard, candidate=candidate, deadline_monotonic=monotonic()+30) == accepted
    reader = PostgresToolOutputReadPort(repository.connection_provider, session_id=lease.guard.session_id, workspace_id=candidate.workspace_id)
    view = reader.read_result({"kind": "SESSION_ENTRY", "entry_id": candidate.result_entry_id})
    value = thaw_tool_json_object(view.value)
    assert value["meta"] == {"status": public_status, "exit_code": 23}
    assert value["structuredContent"] is None and value["external"]["next"] == [2, 1]
    assert value["content"] == [{"type": "text", "value": "a\r\nb\n"}, {"type": "object", "value": {"n": [1, 2.5, False, None]}}]
    assert b"/frozen/path" not in project_tool_result_export(value)
    with pytest.raises(ConversationKernelConflict):
        repository.confirm_tool_result_winner(lease.guard, candidate=replace(candidate, public_fields={"changed": True}), deadline_monotonic=monotonic()+30)
    with read(repository) as connection:
        assert len(read_content_parts(connection, entry_id=candidate.result_entry_id)) == 2
        facts = connection.execute("SELECT result_facts FROM pulsara_v3.tool_results WHERE id=%s", (candidate.result_id,)).fetchone()["result_facts"]
        assert facts == {"canonical_path": "/frozen/path", "file_mtime_ns": 123}


def test_content_failure_rolls_back_result_entry_occurrence_and_all_parts(repository, monkeypatch):
    lease, candidate = prepare(repository)
    from pulsara_agent.conversation_kernel._repository import tools
    original = tools.write_content_parts
    def fail_after_publication(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected content acceptance failure")
    monkeypatch.setattr(tools, "write_content_parts", fail_after_publication)
    with pytest.raises(RuntimeError, match="acceptance failure"):
        repository.accept_tool_result(lease.guard, candidate=candidate, deadline_monotonic=monotonic()+30)
    assert repository.confirm_tool_result_winner(lease.guard, candidate=candidate, deadline_monotonic=monotonic()+30) is None
    with read(repository) as connection:
        assert connection.execute("SELECT 1 FROM pulsara_v3.transcript_entries WHERE id=%s", (candidate.result_entry_id,)).fetchone() is None
        assert connection.execute("SELECT 1 FROM pulsara_v3.tool_results WHERE id=%s", (candidate.result_id,)).fetchone() is None
        assert connection.execute("SELECT 1 FROM pulsara_v3.agent_events WHERE event_id=%s", (candidate.tool_result_occurrence.event_id,)).fetchone() is None
        assert read_content_parts(connection, entry_id=candidate.result_entry_id) == ()


def test_single_text_beyond_removed_blob_cap_is_complete(repository):
    text = "中" * (6*1024*1024)
    lease, candidate = prepare(repository, parts=(ResultPart("text", text),))
    repository.accept_tool_result(lease.guard, candidate=candidate, deadline_monotonic=monotonic()+60)
    view = PostgresToolOutputReadPort(repository.connection_provider, session_id=lease.guard.session_id, workspace_id=candidate.workspace_id).read_result({"kind": "SESSION_ENTRY", "entry_id": candidate.result_entry_id})
    assert view.value["content"][0]["value"] == text


def test_failed_business_value_retains_image_target_object_and_text_in_order(repository):
    from io import BytesIO
    from PIL import Image
    from pulsara_agent.conversation_kernel.prompt_content import freeze_canonical_prompt
    from pulsara_agent.conversation_kernel.prompt_storage import publish_prompt_images
    from pulsara_agent.llm.input import FrozenPromptContent, LLMImagePart
    from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
    stream = BytesIO()
    Image.new("RGB", (7, 5), (11, 23, 41)).save(stream, format="PNG")
    prompt = freeze_canonical_prompt(FrozenPromptContent((LLMImagePart("image/png", stream.getvalue(), 7, 5),)))
    with repository.connection_provider.connection(lane=PostgresConnectionLane.ARTIFACT, row_factory=dict_row,
            deadline_monotonic=monotonic()+30) as connection:
        (image_ref,) = publish_prompt_images(connection, prompt=prompt)
    parts = (ResultPart("text", "failure before\r\n"),
             ResultPart("image", {"kind": "IMAGE_REF", "image_ref": image_ref}),
             ResultPart("target", {"kind": "PATH", "path": "/actual/file"}),
             ResultPart("object", {"partial_failure": True, "obtained": [1, None]}),
             ResultPart("text", "after\n"))
    lease, candidate = prepare(repository, "APPLICATION_ERROR", parts)
    repository.accept_tool_result(lease.guard, candidate=candidate, deadline_monotonic=monotonic()+30)
    reader = PostgresToolOutputReadPort(repository.connection_provider, session_id=lease.guard.session_id, workspace_id=candidate.workspace_id)
    view = reader.read_result({"kind": "SESSION_ENTRY", "entry_id": candidate.result_entry_id})
    assert [part["type"] for part in view.value["content"]] == ["text", "image", "target", "object", "text"]
    assert view.value["meta"]["status"] == "ERROR"
    assert view.value["content"][1]["value"]["image_ref"] == image_ref
    assert repository.confirm_tool_result_winner(lease.guard, candidate=candidate, deadline_monotonic=monotonic()+30) is not None


def test_actual_empty_result_is_readable_but_save_failure_is_not(repository):
    from pulsara_agent.ports.tool_output import ToolOutputContentError, ToolOutputUnavailableReason
    lease, candidate = prepare(repository, parts=())
    repository.accept_tool_result(lease.guard, candidate=candidate, deadline_monotonic=monotonic()+30)
    reader = PostgresToolOutputReadPort(repository.connection_provider, session_id=lease.guard.session_id, workspace_id=candidate.workspace_id)
    assert reader.read_result({"kind": "SESSION_ENTRY", "entry_id": candidate.result_entry_id}).value["content"] == ()
    for reason in ToolOutputUnavailableReason:
        failed_lease, failed = prepare(repository, "SYSTEM_ERROR", (ResultPart("text", "effect occurred; complete result unavailable"),))
        failed = replace(failed, output_unavailability_reason=reason)
        repository.accept_tool_result(failed_lease.guard, candidate=failed, deadline_monotonic=monotonic()+30)
        failed_reader = PostgresToolOutputReadPort(repository.connection_provider,
            session_id=failed_lease.guard.session_id, workspace_id=failed.workspace_id)
        with pytest.raises(ToolOutputContentError, match=reason.value):
            failed_reader.read_result({"kind": "SESSION_ENTRY", "entry_id": failed.result_entry_id})
        assert repository.confirm_tool_result_winner(failed_lease.guard, candidate=failed, deadline_monotonic=monotonic()+30) is not None


@pytest.mark.parametrize("owner", ["machine", "human"])
def test_policy_and_human_denials_store_one_common_text_value(repository, owner):
    from pulsara_agent.conversation_kernel.contracts import InlineContent
    lease, intent, _, cut = open_turn(repository, text="denial request\n")
    assistant, call, entry, result_id = random_id("entry"), "denied-call", random_id("entry"), random_id("result")
    repository.commit_assistant_message(lease.guard, cut=cut, entry_id=assistant,
        blocks=(AssistantToolCallBlock(random_id("block"), call, "write_file", freeze_json({"path": "/actual"})),),
        occurred_at=datetime.now(timezone.utc), actor_id="model:test", complete_turn=False,
        deadline_monotonic=monotonic()+30)
    with read(repository) as connection:
        row = connection.execute("SELECT workspace_id,permission_snapshot_fingerprint FROM pulsara_v3.turns WHERE id=%s", (intent.turn_id,)).fetchone()
    arguments = dict(decision_id=random_id("decision"), assistant_entry_id=assistant,
        tool_call_id=call, decision="DENY", attempt_id=None, result_id=result_id,
        result_entry_id=entry, denial_content=InlineContent.from_bytes("操作未获授权\n".encode()),
        redacted_subject="/actual", actor_id="test", occurred_at=datetime.now(timezone.utc),
        permission_snapshot_fingerprint=row["permission_snapshot_fingerprint"], deadline_monotonic=monotonic()+30)
    if owner == "machine":
        repository.accept_tool_capability_decision(lease.guard, **arguments,
            authorization_reference="policy", denial_result_state="PERMISSION_DENIED")
    else:
        command = random_id("command")
        accepted = repository.accept_tool_interaction_decision(lease.guard, **arguments, command_id=command)
        assert repository.confirm_tool_interaction_decision(lease.guard, **arguments, command_id=command) == accepted
    value = PostgresToolOutputReadPort(repository.connection_provider,
        session_id=lease.guard.session_id, workspace_id=row["workspace_id"]).read_result({"kind": "SESSION_ENTRY", "entry_id": entry}).value
    assert value["meta"] == {"status": "ERROR"}
    assert value["content"] == ({"type": "text", "value": "操作未获授权\n"},)


def test_complete_json_strings_roundtrip_confirm_export_and_fork(repository):
    import json
    from pulsara_agent.conversation_kernel.repository import AssistantTextBlock
    from pulsara_agent.conversation_kernel.contracts import InlineContent

    structured = {"normal_unicode": "中文😀", "nul": "a\x00b", "surrogate": "\ud800",
        "nested": {"array": [None, "\udfff", "\x00", 2.5, False], "empty": ""}}
    object_value = {"nul": "object\x00body", "surrogate": "\ud801", "ordinary": "图文😀",
        "nested": {"values": ["\x00", "\udfff", "原文"]}}
    lease, candidate = prepare(repository, parts=(ResultPart("object", object_value),))
    candidate = replace(candidate, public_fields={"structuredContent": structured,
        "business_value": {"explicit_null": None, "text": "值\x00\udfff"}})
    accepted = repository.accept_tool_result(lease.guard, candidate=candidate, deadline_monotonic=monotonic()+30)
    assert repository.confirm_tool_result_winner(lease.guard, candidate=candidate, deadline_monotonic=monotonic()+30) == accepted

    def verify(session_id, entry_id):
        view = PostgresToolOutputReadPort(repository.connection_provider,
            session_id=session_id, workspace_id=candidate.workspace_id).read_result({"kind": "SESSION_ENTRY", "entry_id": entry_id})
        value = thaw_tool_json_object(view.value)
        assert value["structuredContent"] == structured
        assert value["business_value"] == {"explicit_null": None, "text": "值\x00\udfff"}
        assert "missing" not in value["business_value"]
        assert value["content"] == [{"type": "object", "value": object_value}]
        exported = project_tool_result_export(value).decode("utf-8")
        decoder = json.JSONDecoder()
        fields, boundary = decoder.raw_decode(exported)
        body, end = decoder.raw_decode(exported[boundary:].lstrip())
        assert fields["structuredContent"] == structured
        assert fields["business_value"]["text"] == "值\x00\udfff"
        assert body == object_value
        assert end > 0
        return value, exported

    original_value, original_export = verify(candidate.session_id, candidate.result_entry_id)
    cut = repository.prepare_provider_input_cut(lease.guard, turn_id=candidate.turn_id, deadline_monotonic=monotonic()+30)
    final_entry = random_id("entry")
    repository.commit_assistant_message(lease.guard, cut=cut, entry_id=final_entry,
        blocks=(AssistantTextBlock(random_id("block"), InlineContent.from_bytes(b"done")),),
        complete_turn=True, occurred_at=datetime.now(timezone.utc), actor_id="model:test",
        deadline_monotonic=monotonic()+30)
    child = random_id("session")
    fork = repository.fork_conversation(source_session_id=candidate.session_id,
        anchor_entry_id=final_entry, child_session_id=child, memory_domain_id="u_local",
        deadline_monotonic=monotonic()+30)
    assert fork.created
    with read(repository) as connection:
        copied = connection.execute("SELECT result_entry_id,result_facts FROM pulsara_v3.tool_results WHERE session_id=%s", (child,)).fetchone()
        assert copied["result_facts"] == {"canonical_path": "/frozen/path", "file_mtime_ns": 123}
    child_value, child_export = verify(child, copied["result_entry_id"])
    assert child_value == original_value and child_export == original_export


@pytest.mark.parametrize("field", ["public_fields", "content_parts", "result_facts"])
@pytest.mark.parametrize("saved,replacement", [(True, 1), (False, 0), (1, 1.25), (10**30, 10**30 + 1)])
def test_exact_confirmation_rejects_json_scalar_type_drift(repository, field, saved, replacement):
    lease, candidate = prepare(repository)

    def with_value(value):
        payload = {"nested": [value, None, {"unchanged": "中文"}]}
        return replace(candidate, **{field: (ResultPart("object", payload),)
                                     if field == "content_parts" else payload})

    original = with_value(saved)
    accepted = repository.accept_tool_result(lease.guard, candidate=original,
                                             deadline_monotonic=monotonic()+30)
    assert repository.confirm_tool_result_winner(lease.guard, candidate=original,
                                                deadline_monotonic=monotonic()+30) == accepted
    with pytest.raises(ConversationKernelConflict, match="different winner"):
        repository.confirm_tool_result_winner(lease.guard, candidate=with_value(replacement),
                                              deadline_monotonic=monotonic()+30)
