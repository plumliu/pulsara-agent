"""The accepted body, its one native display, and exact source boundaries."""
from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
from time import monotonic

import pytest

from pulsara_agent.conversation_kernel.blob import CanonicalContentPublisher, MAXIMUM_BLOB_BYTES
from pulsara_agent.conversation_kernel.contracts import BlobContent, InlineContent
from pulsara_agent.conversation_kernel.tool_outputs import (
    KnownOutputPublicationFailure, PostgresToolOutputReadPort, ToolOutputProcessor,
    read_tool_output_content,
)
from pulsara_agent.identity import random_id
from pulsara_agent.ports.terminal_observation import CELL_OUTPUT_MEDIA_TYPE, CellOutputContent
from pulsara_agent.ports.tool_execution import (
    ToolOutputSourceCoverage as Coverage, ToolOutputSourceCoverageReason as CoverageReason,
    ToolOutputSourceFormatHint as Format,
)
from pulsara_agent.ports.tool_output import (
    ToolOutputBodyCandidate, ToolOutputContentError, ToolOutputUnavailableReason as Unavailable,
)


class Publisher:
    def __init__(self, failures=()):
        self.failures = iter(failures)
        self.calls = []

    def materialize(self, **arguments):
        self.calls.append(arguments)
        failure = next(self.failures, None)
        if failure:
            raise failure
        return CanonicalContentPublisher(object()).describe(
            workspace_id="workspace:" + "a" * 26,
            content=arguments["content"], media_type=arguments["media_type"], codec=arguments["codec"])


def materialize(text, *, publisher=None, candidate=None, format=Format.TEXT):
    processor = ToolOutputProcessor(object(), publisher=publisher or Publisher())
    return processor.materialize_body(session_id=random_id("session"), workspace_id=random_id("workspace"),
        result_entry_id=random_id("entry"), public_output=text, output_body_candidate=candidate,
        public_format=format, deadline_monotonic=monotonic() + 10)


@pytest.mark.parametrize("text", ["", "x" * 8000, "x" * 8001, "中😀\\\"\n" * 1000, "x" * 65536])
def test_small_complete_body_stays_inline_independently_of_omission(text):
    body = materialize(text)
    assert isinstance(body.output_body, InlineContent)
    assert body.output_body.canonical_bytes == text.encode()
    assert body.output_unavailability_reason is None
    projection = ToolOutputProcessor.prepare(body=body, native_output="selected preview", output_omitted=True)
    assert projection.canonical_preview.canonical_bytes == b"selected preview"
    assert projection.output_body == body.output_body
    assert projection.output_omitted


def test_long_body_uses_canonical_blob_and_never_rebuilds_the_supplied_preview():
    text = "head\n" + "中😀" * 20000 + "\ntail"
    body = materialize(text)
    assert isinstance(body.output_body, BlobContent)
    assert body.output_body.size == len(text.encode())
    assert body.output_body.digest == "sha256:" + sha256(text.encode()).hexdigest()
    projection = ToolOutputProcessor.prepare(body=body, native_output="tail", output_omitted=True)
    assert projection.canonical_preview.canonical_bytes == b"tail"


def test_terminal_primary_text_is_not_replaced_with_the_public_json_value():
    candidate = ToolOutputBodyCandidate("exact sanitized log\n", Format.TEXT, Coverage.COMPLETE)
    body = materialize('{"exit_code":1,"output":"exact sanitized log\\n"}', candidate=candidate,
                       format=Format.JSON)
    assert body.output_body.canonical_bytes == b"exact sanitized log\n"
    assert body.output_body.media_type == "text/plain"
    json_body = materialize('{"result":"response"}', format=Format.JSON)
    assert json_body.output_body.media_type == "application/json"


@pytest.mark.parametrize("reason", list(CoverageReason))
def test_retained_coverage_is_independent_of_body_storage_and_display(reason):
    candidate = ToolOutputBodyCandidate("retained", Format.TEXT, Coverage.RETAINED_SNAPSHOT, reason)
    body = materialize("public", candidate=candidate)
    assert body.source_coverage is Coverage.RETAINED_SNAPSHOT
    assert body.source_coverage_reason is reason
    assert isinstance(body.output_body, InlineContent)


@pytest.mark.parametrize("failures,reason", [
    ((KnownOutputPublicationFailure("failed"),), Unavailable.BODY_PUBLICATION_FAILED),
    ((TimeoutError(), TimeoutError()), Unavailable.BODY_PUBLICATION_UNCONFIRMED),
])
def test_unavailable_body_does_not_erase_the_real_display(failures, reason):
    publisher = Publisher(failures)
    body = materialize("known outcome", publisher=publisher)
    assert body.output_body is None and body.output_unavailability_reason is reason
    projection = ToolOutputProcessor.prepare(body=body, native_output="accepted; body unavailable", output_omitted=True)
    assert projection.canonical_preview.canonical_bytes == b"accepted; body unavailable"


def test_lost_body_ack_retries_only_identical_content():
    publisher = Publisher((TimeoutError(), None))
    body = materialize("accepted once", publisher=publisher)
    assert body.output_body.canonical_bytes == b"accepted once"
    assert publisher.calls[0] == publisher.calls[1]


def test_existing_blob_boundary_is_explicit_and_does_not_call_storage():
    publisher = Publisher()
    body = materialize("x" * (MAXIMUM_BLOB_BYTES + 1), publisher=publisher)
    assert body.output_unavailability_reason is Unavailable.BODY_TOO_LARGE
    assert body.output_body is None and not publisher.calls


def body_row(content=b"original", **changes):
    row = dict(workspace_id=random_id("workspace"), output_body_inline_content=content,
        output_body_blob_id=None, output_body_digest="sha256:" + sha256(content).hexdigest(),
        output_body_size=len(content), output_body_media_type="text/plain", output_body_codec="utf-8",
        output_unavailability_reason=None)
    row.update(changes)
    return row


class Connection:
    def __init__(self, row=None, blob=None):
        self.row, self.blob = row, blob
        self.calls = []

    def execute(self, query, arguments):
        self.calls.append((query, arguments))
        selected = self.blob if "FROM pulsara_v3.blobs" in query else self.row
        return type("Result", (), {"fetchone": lambda _: selected})()


class Provider:
    def __init__(self, connection):
        self.current = connection

    @contextmanager
    def connection(self, **_):
        yield self.current


@pytest.mark.parametrize("changes", [
    {"output_body_size": 99}, {"output_body_digest": "sha256:" + "0" * 64},
    {"output_body_codec": "binary"}, {"output_body_inline_content": None},
    {"output_unavailability_reason": "BODY_PUBLICATION_FAILED"},
])
def test_corrupt_or_unavailable_body_is_a_typed_error(changes):
    with pytest.raises(ToolOutputContentError):
        read_tool_output_content(Connection(), body_row(**changes))


def test_blob_reader_joins_workspace_and_verifies_the_frozen_descriptor():
    content = b"stored blob"
    blob_id = random_id("blob")
    row = body_row(content, output_body_inline_content=None, output_body_blob_id=blob_id)
    blob = dict(body=content, logical_digest=row["output_body_digest"], logical_size=len(content),
                media_type="text/plain", codec="utf-8")
    connection = Connection(blob=blob)
    assert read_tool_output_content(connection, row) == content
    assert connection.calls[0][1] == (blob_id, row["workspace_id"])
    blob["body"] = b"changed"
    with pytest.raises(ToolOutputContentError):
        read_tool_output_content(connection, row)


def test_exact_reader_binds_scope_and_inherits_memory_provenance():
    session, workspace, entry, memory = (random_id(kind) for kind in ("session", "workspace", "entry", "memory"))
    row = body_row()
    row.update(workspace_id=workspace, entry_kind="TOOL_RESULT", output_source_coverage="COMPLETE",
               output_source_coverage_reason=None, model_visible_memory_fact_ids=[memory], result_facts={"exit_code":23})
    connection = Connection(row=row)
    reader = PostgresToolOutputReadPort(Provider(connection), session_id=session, workspace_id=workspace)
    view = reader.read_body({"kind": "SESSION_ENTRY", "entry_id": entry})
    assert view.target == {"kind": "SESSION_ENTRY", "session_id": session, "entry_id": entry}
    assert view.content == b"original" and view.model_visible_memory_fact_ids == (memory,)
    assert view.required_facts == {"exit_code":23}
    with pytest.raises(KeyError):
        reader.read_body({"kind": "SESSION_ENTRY", "session_id": random_id("session"), "entry_id": entry})
    assert len(connection.calls) == 1


def test_formal_cell_output_preserves_the_exact_envelope_and_surrogate_encoding():
    session, workspace, entry = (random_id(kind) for kind in ("session", "workspace", "entry"))
    formal = CellOutputContent(random_id("cell"), random_id("turn"), random_id("attempt"), 1,
                               {"kind": "log", "text": "selected\ud800"})
    row = dict(workspace_id=workspace, entry_kind="TERMINAL_OBSERVATION", input_origin="CELL_OUTPUT",
               inline_content=formal.body, blob_id=None, content_digest="sha256:" + sha256(formal.body).hexdigest(),
               content_size=len(formal.body), content_media_type=CELL_OUTPUT_MEDIA_TYPE, content_codec="utf-8")
    reader = PostgresToolOutputReadPort(Provider(Connection(row=row)), session_id=session, workspace_id=workspace)
    assert reader.read_body({"kind": "SESSION_ENTRY", "entry_id": entry}).content == formal.body


def test_accepted_result_facts_freeze_business_metadata_independently_of_text():
    from datetime import datetime, timezone
    from pulsara_agent.conversation_kernel.contracts import HostWriterGuard
    from pulsara_agent.conversation_kernel.repository import build_prepared_tool_result_acceptance
    from pulsara_agent.primitives.tool_observation import ToolObservationOrigin

    facts = {"exit_code": 23, "target": {"kind": "PROCESS", "process_id": random_id("process")}}
    candidate = build_prepared_tool_result_acceptance(
        guard=HostWriterGuard(random_id("session"), 1, random_id("host")),
        workspace_id=random_id("workspace"), result_id=random_id("result"),
        result_entry_id=random_id("entry"), turn_id=random_id("turn"),
        assistant_entry_id=random_id("entry"), tool_call_id=random_id("call"), attempt_id=random_id("attempt"),
        result_state="SUCCESS", canonical_preview_content=InlineContent.from_bytes(b"tail"),
        output_body=InlineContent.from_bytes(b"full log"), output_unavailability_reason=None,
        source_coverage=Coverage.COMPLETE, source_coverage_reason=None, output_omitted=True,
        observed_at=datetime.now(timezone.utc), observation_duration_microseconds=None,
        observation_origin_kind=ToolObservationOrigin.BUILTIN, trusted_tool_reported_duration_microseconds=None,
        actor_id="tool", required_facts=facts)
    facts["exit_code"] = 0
    facts["target"]["process_id"] = random_id("process")
    assert candidate.required_facts["exit_code"] == 23
    assert candidate.output_body.canonical_bytes == b"full log"
    with pytest.raises(TypeError):
        candidate.required_facts["exit_code"] = 0


def test_plan_body_is_the_existing_public_projection_not_its_control_carrier():
    import json
    from pulsara_agent.conversation_kernel._repository.plans import _plan_public_output, _plan_public_facts

    storage = InlineContent.from_bytes(json.dumps({
        "status":"success", "plan_control":"QUESTION_ANSWERED", "interaction_id":random_id("plan-interaction"),
        "answer_kind":"OPTION", "selected_option_ordinal":1, "selected_label":"Approved answer",
    }).encode())
    body = _plan_public_output(storage,"PLAN_CONTROL")
    expected = {"status":"success", "plan_control":"QUESTION_ANSWERED",
                "answer":{"kind":"OPTION", "label":"Approved answer", "ordinal":1}}
    assert json.loads(body.canonical_bytes) == expected
    assert _plan_public_facts(storage,"PLAN_CONTROL") == expected
    assert b"interaction_id" not in body.canonical_bytes
    assert b"interaction_id" in storage.canonical_bytes


def _accept_test_body(stage2_migrated_postgres_database, text, *, repository_type=None, accept=True):
    from datetime import datetime, timezone
    from pulsara_agent.conversation_kernel.repository import ConversationKernelRepository, build_prepared_tool_result_acceptance
    from pulsara_agent.primitives.tool_observation import ToolObservationOrigin
    from tests.support.postgres import verified_postgres_provider
    from tests.support.tool_outputs import _install_tool_call

    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = (repository_type or ConversationKernelRepository)(provider)
    workspace, entry = random_id("workspace"), random_id("entry")
    lease, turn, assistant, call, attempt = _install_tool_call(repository, workspace)
    processor = ToolOutputProcessor(provider)
    body = processor.materialize_body(session_id=lease.guard.session_id, workspace_id=workspace,
        result_entry_id=entry, public_output=text, output_body_candidate=None, public_format=Format.TEXT,
        deadline_monotonic=monotonic() + 30)
    projection = processor.prepare(body=body, native_output="one supplied preview", output_omitted=True)
    candidate = build_prepared_tool_result_acceptance(guard=lease.guard, workspace_id=workspace,
        result_id=random_id("result"), result_entry_id=entry, turn_id=turn, assistant_entry_id=assistant,
        tool_call_id=call, attempt_id=attempt, result_state="SUCCESS",
        canonical_preview_content=projection.canonical_preview, output_body=projection.output_body,
        output_unavailability_reason=projection.output_unavailability_reason, source_coverage=projection.source_coverage,
        source_coverage_reason=projection.source_coverage_reason, output_omitted=True,
        required_facts={"exit_code":23},
        observed_at=datetime.now(timezone.utc), observation_duration_microseconds=None,
        observation_origin_kind=ToolObservationOrigin.BUILTIN, trusted_tool_reported_duration_microseconds=None, actor_id="tool")
    if accept:
        accepted = repository.accept_tool_result(lease.guard, candidate=candidate, deadline_monotonic=monotonic() + 30)
        assert repository.confirm_tool_result_winner(lease.guard, candidate=candidate,
            deadline_monotonic=monotonic() + 30) == accepted
    return provider,repository,lease,workspace,candidate


@pytest.mark.postgres
@pytest.mark.parametrize("text", ["small body", "中😀" * 20000])
def test_accepted_inline_and_blob_body_are_readable_by_original_entry(stage2_migrated_postgres_database, text):
    provider,repository,lease,workspace,candidate = _accept_test_body(stage2_migrated_postgres_database,text)
    entry = candidate.result_entry_id
    view = PostgresToolOutputReadPort(provider, session_id=lease.guard.session_id, workspace_id=workspace).read_body(
        {"kind": "SESSION_ENTRY", "entry_id": entry})
    assert view.content == text.encode()
    assert view.required_facts == {"exit_code":23}
    observation = repository.read_accepted_tool_result_observation(session_id=lease.guard.session_id,
        result_id=candidate.result_id, deadline_monotonic=monotonic() + 30)
    assert observation["result"] == "one supplied preview"
    assert observation["required_facts"] == {"exit_code":23}
    assert observation["read"]["arguments"]["target"]["entry_id"] == entry


@pytest.mark.postgres
def test_exact_database_body_rejects_foreign_scope_and_detects_blob_corruption(stage2_migrated_postgres_database):
    from tests.support.postgres import connect_postgres_test_database
    provider,_,lease,workspace,candidate = _accept_test_body(stage2_migrated_postgres_database,"长日志" * 30000)
    reader = PostgresToolOutputReadPort(provider,session_id=lease.guard.session_id,workspace_id=workspace)
    target = {"kind":"SESSION_ENTRY","entry_id":candidate.result_entry_id}
    with pytest.raises(KeyError):
        reader.read_body({**target,"session_id":random_id("session")})
    with pytest.raises(KeyError):
        PostgresToolOutputReadPort(provider,session_id=random_id("session"),workspace_id=workspace).read_body(target)
    with pytest.raises(KeyError):
        PostgresToolOutputReadPort(provider,session_id=lease.guard.session_id,workspace_id=random_id("workspace")).read_body(target)
    with connect_postgres_test_database(stage2_migrated_postgres_database.admin_dsn) as connection:
        connection.execute("UPDATE pulsara_v3.blobs SET body=%s WHERE id=%s",
                           (b"x" * candidate.output_body.size,candidate.output_body.blob_id))
    with pytest.raises(ToolOutputContentError,match="integrity"):
        reader.read_body(target)


@pytest.mark.postgres
def test_missing_saved_body_reports_unavailable_without_using_the_display(stage2_migrated_postgres_database):
    from tests.support.postgres import connect_postgres_test_database
    provider,_,lease,workspace,candidate = _accept_test_body(stage2_migrated_postgres_database,"full accepted output")
    with connect_postgres_test_database(stage2_migrated_postgres_database.admin_dsn) as connection:
        connection.execute("UPDATE pulsara_v3.tool_results SET output_body_inline_content=NULL,"
            "output_body_blob_id=NULL,output_body_digest=NULL,output_body_size=NULL,"
            "output_body_media_type=NULL,output_body_codec=NULL,output_unavailability_reason='BODY_PUBLICATION_FAILED' "
            "WHERE id=%s",(candidate.result_id,))
    reader = PostgresToolOutputReadPort(provider,session_id=lease.guard.session_id,workspace_id=workspace)
    with pytest.raises(ToolOutputContentError,match="BODY_PUBLICATION_FAILED"):
        reader.read_body({"kind":"SESSION_ENTRY","entry_id":candidate.result_entry_id})


@pytest.mark.postgres
def test_body_descriptor_and_result_entry_rollback_with_the_acceptance_occurrence(stage2_migrated_postgres_database):
    from pulsara_agent.conversation_kernel.repository import ConversationKernelRepository, ConversationKernelConflict
    from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
    class RejectOccurrence(ConversationKernelRepository):
        def _append_events(self,connection,guard,*,workspace_id,drafts):
            if any(item.event_type.value == "ToolResultAccepted" for item in drafts):
                raise ConversationKernelConflict("injected result occurrence conflict")
            return super()._append_events(connection,guard,workspace_id=workspace_id,drafts=drafts)
    provider,repository,lease,_,candidate = _accept_test_body(stage2_migrated_postgres_database,
        "large known outcome" * 5000,repository_type=RejectOccurrence,accept=False)
    with pytest.raises(ConversationKernelConflict,match="occurrence conflict"):
        repository.accept_tool_result(lease.guard,candidate=candidate,deadline_monotonic=monotonic()+30)
    with provider.connection(lane=PostgresConnectionLane.INSPECTOR,deadline_monotonic=monotonic()+30) as connection:
        assert connection.execute("SELECT count(*) FROM pulsara_v3.transcript_entries WHERE id=%s",(candidate.result_entry_id,)).fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM pulsara_v3.tool_results WHERE id=%s",(candidate.result_id,)).fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM pulsara_v3.agent_events WHERE event_id=%s",(candidate.tool_result_occurrence.event_id,)).fetchone() == (0,)
        # Content publication is immutable and independent; an unreferenced blob
        # is disposable, rather than evidence of a committed business result.
        assert connection.execute("SELECT count(*) FROM pulsara_v3.blobs WHERE id=%s",(candidate.output_body.blob_id,)).fetchone() == (1,)
