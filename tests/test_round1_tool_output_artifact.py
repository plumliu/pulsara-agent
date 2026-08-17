from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import shlex
import sys
from time import monotonic
from uuid import uuid4

import psycopg
import pytest

from pulsara_agent.conversation_kernel.contracts import BlobContent, InlineContent
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.memory.contracts import (
    FrozenMemoryProposal,
    MemoryKindHint,
    MemoryProducerKind,
    prepare_memory_candidate,
)
from pulsara_agent.conversation_kernel.repository import (
    AssistantToolCallBlock,
    ConversationKernelConflict,
    ConversationKernelRepository,
    PreparedMemoryProposalSideBranch,
    build_prepared_tool_result_acceptance,
)
from pulsara_agent.conversation_kernel.runner import (
    ConversationKernelRunner,
    KernelToolAuthorization,
    KernelToolAuthorizationKind,
    KernelToolResult,
)
from pulsara_agent.conversation_kernel.tool_artifacts import (
    ARTIFACT_READ_HARD_CHARS,
    CANONICAL_TOOL_RESULT_PREVIEW_HARD_BYTES,
    KnownArtifactPublicationFailure,
    PostgresToolArtifactReadPort,
    ToolOutputArtifactProcessor,
)
from pulsara_agent.conversation_kernel.tool_policy import (
    DefaultToolDispatchAuthorizationPolicy,
)
from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
from pulsara_agent.conversation_kernel.vocabulary import (
    APPEND_GUARDS,
    COMMITTED_EVENT_DESCRIPTORS,
    LIVE_EVENT_TYPES,
    SUBJECT_SLOTS,
)
from tests.support.round3 import (
    ScriptedKernelModel,
    StaticContextSourceCollector,
    StructuredToolPort,
    direct_tool_invocation_context,
)
from pulsara_agent.primitives.context import freeze_json, thaw_json
from pulsara_agent.primitives.tool_observation import (
    MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES,
    ToolObservationOrigin,
)
from pulsara_agent.primitives.tool_result_projection import (
    conservative_artifact_page_logical_utf8_bytes,
)
from pulsara_agent.primitives.permission import DEFAULT_PERMISSION_MODE
from pulsara_agent.primitives.run_permission import (
    RunPermissionAdmissionSource,
    build_run_permission_snapshot,
)
from pulsara_agent.message import ToolResultState
from pulsara_agent.ports.artifact import (
    ArtifactContentError,
    ToolOutputArtifactDisposition,
    ToolOutputArtifactUnavailabilityReason,
    ToolResultDisplayKind,
)
from pulsara_agent.ports.live_agent_event import (
    TextDeltaPayload,
    TextEndPayload,
    TextStartPayload,
    ToolCallDeltaPayload,
    ToolCallEndPayload,
    ToolCallStartPayload,
    live_digest,
)
from pulsara_agent.ports.tool_execution import (
    ToolCall,
    ToolOutputSourceCoverage,
    ToolOutputSourceCoverageReason,
)
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from pulsara_agent.memory.scope import CTX_USER, MemoryScopeKind
from pulsara_agent.storage.migrations.manifest import CONVERSATION_KERNEL_RELATIONS
from pulsara_agent.conversation_kernel.jobs import JOB_HANDLER_CATALOG
from pulsara_agent.terminal_process.output import TerminalOutputOwner
from pulsara_agent.tools.builtins.artifact import ArtifactReadTool
from tests.support.postgres import verified_postgres_provider


def _name(prefix: str) -> str:
    return f"{prefix}:{uuid4().hex}"


def _terminal_output(maximum_bytes: int) -> TerminalOutputOwner:
    return TerminalOutputOwner(
        owner_epoch="round1-test-host",
        process_id=_name("terminal-process"),
        maximum_bytes=maximum_bytes,
    )


@dataclass(slots=True)
class _RecordingPublisher:
    failure: Exception | None = None
    calls: list[bytes] = field(default_factory=list)

    def publish(
        self,
        *,
        workspace_id: str,
        content: bytes,
        media_type: str,
        codec: str,
        deadline_monotonic: float,
    ) -> BlobContent:
        del deadline_monotonic
        value = bytes(content)
        self.calls.append(value)
        if self.failure is not None:
            raise self.failure
        digest = "sha256:" + sha256(value).hexdigest()
        return BlobContent(
            f"blob:{workspace_id}:{digest.removeprefix('sha256:')}",
            digest,
            len(value),
            media_type,
            codec,
        )


def _processor(publisher: _RecordingPublisher) -> ToolOutputArtifactProcessor:
    return ToolOutputArtifactProcessor(object(), publisher=publisher)  # type: ignore[arg-type]


def test_round1_static_authority_and_count_oracles_remain_closed() -> None:
    assert len(CONVERSATION_KERNEL_RELATIONS) == 25
    assert "tool_result_artifacts" not in CONVERSATION_KERNEL_RELATIONS
    assert len(COMMITTED_EVENT_DESCRIPTORS) == 31
    assert len(LIVE_EVENT_TYPES) == 23
    assert len(SUBJECT_SLOTS) == 13
    assert len(APPEND_GUARDS) == 2
    assert len(JOB_HANDLER_CATALOG) == 1

    root = Path(__file__).resolve().parents[1]
    sql = (
        root
        / "src/pulsara_agent/storage/migrations/sql/0000_conversation_kernel_baseline.sql"
    ).read_text(encoding="utf-8")
    tool_results = sql.split("CREATE TABLE pulsara_v3.tool_results (", 1)[1].split(
        "CREATE TABLE pulsara_v3.prompt_queue_items (", 1
    )[0]
    for column in (
        "output_artifact_disposition",
        "output_source_coverage",
        "output_display_kind",
    ):
        assert f"{column} text NOT NULL" in tool_results
    assert "output_source_coverage_reason" in tool_results
    assert "output_artifact_unavailability_reason" in tool_results
    assert "output_artifact_blob_id, workspace_id" in tool_results
    assert "uq_pulsara_v3_tool_result_output_artifact_id" in tool_results
    assert "CREATE TABLE pulsara_v3.tool_result_artifacts" not in sql
    assert "transcript_entries_tool_result_inline_ck" in sql
    assert "entry_kind <> 'TOOL_RESULT'" in sql

    production = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (root / "src/pulsara_agent").rglob("*.py")
    )
    assert "head_tail_huge" not in production
    assert "ToolResultPreviewMetadata" not in production
    assert "ToolResultArtifactRef" not in production


@pytest.mark.parametrize(
    ("text", "expected_disposition", "expected_display", "published"),
    (
        pytest.param(
            "",
            ToolOutputArtifactDisposition.NOT_REQUIRED,
            ToolResultDisplayKind.COMPLETE,
            0,
            id="empty",
        ),
        pytest.param(
            "a" * 7_999,
            ToolOutputArtifactDisposition.NOT_REQUIRED,
            ToolResultDisplayKind.COMPLETE,
            0,
            id="archive-before",
        ),
        pytest.param(
            "a" * 8_000,
            ToolOutputArtifactDisposition.NOT_REQUIRED,
            ToolResultDisplayKind.COMPLETE,
            0,
            id="archive-at",
        ),
        pytest.param(
            "a" * 8_001,
            ToolOutputArtifactDisposition.AVAILABLE,
            ToolResultDisplayKind.COMPLETE,
            1,
            id="archive-after",
        ),
        pytest.param(
            "a" * 39_999,
            ToolOutputArtifactDisposition.AVAILABLE,
            ToolResultDisplayKind.COMPLETE,
            1,
            id="display-before",
        ),
        pytest.param(
            "a" * 40_000,
            ToolOutputArtifactDisposition.AVAILABLE,
            ToolResultDisplayKind.COMPLETE,
            1,
            id="display-at",
        ),
        pytest.param(
            "a" * 40_001,
            ToolOutputArtifactDisposition.AVAILABLE,
            ToolResultDisplayKind.HEAD_TAIL,
            1,
            id="display-after",
        ),
    ),
)
def test_round1_preview_threshold_matrix(
    text: str,
    expected_disposition: ToolOutputArtifactDisposition,
    expected_display: ToolResultDisplayKind,
    published: int,
) -> None:
    publisher = _RecordingPublisher()
    projection = _processor(publisher).prepare(
        workspace_id="workspace",
        result_entry_id="entry",
        public_output=text,
        candidate=None,
        artifact_source_read=False,
        deadline_monotonic=monotonic() + 10,
    )
    assert projection.artifact_disposition is expected_disposition
    assert projection.display_kind is expected_display
    assert len(publisher.calls) == published
    assert projection.canonical_preview.size <= CANONICAL_TOOL_RESULT_PREVIEW_HARD_BYTES
    if expected_display is ToolResultDisplayKind.COMPLETE:
        assert projection.canonical_preview.canonical_bytes.decode().startswith(text)
        assert projection.omitted_middle_chars == 0
    else:
        preview = projection.canonical_preview.canonical_bytes.decode()
        assert preview.startswith(text[: projection.visible_head_chars])
        assert preview.endswith(text[-projection.visible_tail_chars :])
        assert projection.omitted_middle_chars == (
            len(text) - projection.visible_head_chars - projection.visible_tail_chars
        )
        assert len(preview) <= 8_000


@pytest.mark.parametrize(
    ("text", "expected_display"),
    (
        ("a" * 39_999, ToolResultDisplayKind.COMPLETE),
        ("a" * 40_000, ToolResultDisplayKind.COMPLETE),
        ("a" * 40_001, ToolResultDisplayKind.HEAD_TAIL),
        ("中" * 13_333, ToolResultDisplayKind.COMPLETE),
        (("中" * 13_333) + "a", ToolResultDisplayKind.COMPLETE),
        (("中" * 13_333) + "aa", ToolResultDisplayKind.HEAD_TAIL),
        ("🙂" * 9_999, ToolResultDisplayKind.COMPLETE),
        ("🙂" * 10_000, ToolResultDisplayKind.COMPLETE),
        (("🙂" * 10_000) + "a", ToolResultDisplayKind.HEAD_TAIL),
        (("\\\"" * 20_000), ToolResultDisplayKind.COMPLETE),
        (("\\\"" * 20_000) + "a", ToolResultDisplayKind.HEAD_TAIL),
    ),
)
def test_round7_1_canonical_complete_uses_candidate_utf8_bytes(
    text: str,
    expected_display: ToolResultDisplayKind,
) -> None:
    projection = _processor(_RecordingPublisher()).prepare(
        workspace_id="workspace",
        result_entry_id="entry",
        public_output=text,
        candidate=None,
        artifact_source_read=False,
        deadline_monotonic=monotonic() + 10,
    )
    assert projection.candidate_utf8_bytes == len(text.encode("utf-8"))
    assert projection.display_kind is expected_display
    assert projection.canonical_preview.size <= CANONICAL_TOOL_RESULT_PREVIEW_HARD_BYTES
    projection.canonical_preview.canonical_bytes.decode("utf-8")


def test_round2_cursor_artifact_replaces_only_the_selected_terminal_delta() -> None:
    owner = _terminal_output(16 * 1024 * 1024)
    owner.append_raw(b"old-output\n")
    cursor = owner.snapshot(maximum_chars=512).output_cursor
    owner.append_raw(b"new-output\n")
    snapshot, candidate = owner.snapshot_with_artifact_candidate(
        maximum_chars=512,
        since_cursor=cursor,
    )
    public_output = json.dumps(
        {"status": "success", "output": snapshot.text},
        ensure_ascii=False,
    )

    projection = _processor(_RecordingPublisher()).prepare(
        workspace_id="workspace",
        result_entry_id="entry:cursor-delta",
        public_output=public_output,
        candidate=candidate,
        artifact_source_read=False,
        deadline_monotonic=monotonic() + 10,
    )
    rendered = json.loads(projection.canonical_preview.canonical_bytes)
    assert rendered["output"] == "new-output\n"
    assert "old-output" not in projection.canonical_preview.canonical_bytes.decode()


def test_round1_archive_bytes_and_display_chars_are_independent() -> None:
    text = "中" * 2_667
    publisher = _RecordingPublisher()
    projection = _processor(publisher).prepare(
        workspace_id="workspace",
        result_entry_id="entry",
        public_output=text,
        candidate=None,
        artifact_source_read=False,
        deadline_monotonic=monotonic() + 10,
    )
    assert len(text) < 8_000
    assert len(text.encode("utf-8")) == 8_001
    assert projection.artifact_disposition is ToolOutputArtifactDisposition.AVAILABLE
    assert projection.display_kind is ToolResultDisplayKind.COMPLETE
    assert publisher.calls == [text.encode("utf-8")]


def test_round1_multibyte_preview_and_unavailable_wording_are_exact() -> None:
    text = "🙂" * 20_000
    publisher = _RecordingPublisher(
        failure=KnownArtifactPublicationFailure("storage unavailable")
    )
    projection = _processor(publisher).prepare(
        workspace_id="workspace",
        result_entry_id="entry",
        public_output=text,
        candidate=None,
        artifact_source_read=False,
        deadline_monotonic=monotonic() + 10,
    )
    preview = projection.canonical_preview.canonical_bytes.decode("utf-8")
    assert projection.artifact_disposition is ToolOutputArtifactDisposition.UNAVAILABLE
    assert projection.display_kind is ToolResultDisplayKind.HEAD_TAIL
    assert projection.artifact_unavailability_reason is (
        ToolOutputArtifactUnavailabilityReason.BLOB_PUBLICATION_FAILED
    )
    assert projection.canonical_preview.size <= 65_536
    assert "omitted output could not be retained" in preview
    assert "artifact_id=" not in preview

    complete = _processor(
        _RecordingPublisher(failure=KnownArtifactPublicationFailure("no storage"))
    ).prepare(
        workspace_id="workspace",
        result_entry_id="entry:complete",
        public_output="x" * 9_000,
        candidate=None,
        artifact_source_read=False,
        deadline_monotonic=monotonic() + 10,
    )
    complete_preview = complete.canonical_preview.canonical_bytes.decode()
    assert complete.display_kind is ToolResultDisplayKind.COMPLETE
    assert "complete candidate output is shown inline" in complete_preview
    assert "omitted output could not be retained" not in complete_preview


@dataclass(slots=True)
class _LostPublicationAckPublisher:
    calls: list[bytes] = field(default_factory=list)

    def publish(
        self,
        *,
        workspace_id: str,
        content: bytes,
        media_type: str,
        codec: str,
        deadline_monotonic: float,
    ) -> BlobContent:
        del deadline_monotonic
        value = bytes(content)
        self.calls.append(value)
        if len(self.calls) == 1:
            raise psycopg.OperationalError("injected lost blob acknowledgement")
        digest = "sha256:" + sha256(value).hexdigest()
        return BlobContent(
            f"blob:{workspace_id}:{digest.removeprefix('sha256:')}",
            digest,
            len(value),
            media_type,
            codec,
        )


def test_round1_blob_ack_unknown_reissues_only_the_exact_candidate() -> None:
    publisher = _LostPublicationAckPublisher()
    source = "before-ack-loss-" + ("🙂" * 3_000) + "-after"
    projection = ToolOutputArtifactProcessor(
        object(),
        publisher=publisher,  # type: ignore[arg-type]
    ).prepare(
        workspace_id="workspace",
        result_entry_id="entry",
        public_output=source,
        candidate=None,
        artifact_source_read=False,
        deadline_monotonic=monotonic() + 10,
    )
    assert projection.artifact_disposition is ToolOutputArtifactDisposition.AVAILABLE
    assert publisher.calls == [source.encode("utf-8"), source.encode("utf-8")]


def test_round1_same_body_dedupes_blob_but_not_result_artifact_handle() -> None:
    publisher = _RecordingPublisher()
    processor = _processor(publisher)
    source = "same-body" * 1_001
    first = processor.prepare(
        workspace_id="workspace",
        result_entry_id="entry:one",
        public_output=source,
        candidate=None,
        artifact_source_read=False,
        deadline_monotonic=monotonic() + 10,
    )
    second = processor.prepare(
        workspace_id="workspace",
        result_entry_id="entry:two",
        public_output=source,
        candidate=None,
        artifact_source_read=False,
        deadline_monotonic=monotonic() + 10,
    )
    assert first.artifact_blob == second.artifact_blob
    assert first.artifact_id != second.artifact_id
    assert publisher.calls == [source.encode("utf-8"), source.encode("utf-8")]


def test_round1_oversized_artifact_keeps_known_output_without_publishing() -> None:
    publisher = _RecordingPublisher()
    source = "x" * ((16 << 20) + 1)
    projection = _processor(publisher).prepare(
        workspace_id="workspace",
        result_entry_id="entry",
        public_output=source,
        candidate=None,
        artifact_source_read=False,
        deadline_monotonic=monotonic() + 10,
    )
    assert publisher.calls == []
    assert projection.artifact_disposition is ToolOutputArtifactDisposition.UNAVAILABLE
    assert projection.artifact_unavailability_reason is (
        ToolOutputArtifactUnavailabilityReason.ARTIFACT_CONTENT_TOO_LARGE
    )
    assert projection.display_kind is ToolResultDisplayKind.HEAD_TAIL
    assert projection.canonical_preview.size <= 65_536


def test_round1_retained_snapshot_keeps_both_failure_axes() -> None:
    bounded = _terminal_output(64)
    bounded.append_raw(b"lost-prefix-" * 16)
    bounded.append_raw(b"RETAINED-SENTINEL")
    bounded.finalize(status="success", exit_code=0)
    candidate = bounded.artifact_candidate()
    assert candidate.source_coverage is ToolOutputSourceCoverage.RETAINED_SNAPSHOT
    publisher = _RecordingPublisher(
        failure=KnownArtifactPublicationFailure("publication failed")
    )
    projection = _processor(publisher).prepare(
        workspace_id="workspace",
        result_entry_id="entry",
        public_output=json.dumps(
            {
                "status": "success",
                "terminal_process_action": "log",
                "output": candidate.text,
            },
            ensure_ascii=False,
        ),
        candidate=candidate,
        artifact_source_read=False,
        deadline_monotonic=monotonic() + 10,
    )
    preview = projection.canonical_preview.canonical_bytes.decode()
    assert projection.source_coverage is ToolOutputSourceCoverage.RETAINED_SNAPSHOT
    assert projection.source_coverage_reason is (
        ToolOutputSourceCoverageReason.TERMINAL_RETENTION_GAP
    )
    assert projection.artifact_disposition is ToolOutputArtifactDisposition.UNAVAILABLE
    assert projection.artifact_unavailability_reason is (
        ToolOutputArtifactUnavailabilityReason.BLOB_PUBLICATION_FAILED
    )
    assert "retained snapshot only" in preview
    assert "ARTIFACT UNAVAILABLE" in preview


def test_round2_sanitizer_unavailable_remains_distinct_from_retention_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bounded = _terminal_output(8_192)
    bounded.append_raw(b"safe-prefix")

    def fail_feed(_raw: bytes) -> bytes:
        raise RuntimeError("injected sanitizer failure")

    monkeypatch.setattr(bounded._sanitizer, "feed", fail_feed)  # noqa: SLF001
    assert bounded.append_raw(b"not-public") == b""
    candidate = bounded.artifact_candidate()
    publisher = _RecordingPublisher(
        failure=KnownArtifactPublicationFailure("publication failed")
    )
    projection = _processor(publisher).prepare(
        workspace_id="workspace",
        result_entry_id="entry",
        public_output=json.dumps(
            {"status": "success", "output": candidate.text}, ensure_ascii=False
        ),
        candidate=candidate,
        artifact_source_read=False,
        deadline_monotonic=monotonic() + 10,
    )

    assert projection.source_coverage is ToolOutputSourceCoverage.RETAINED_SNAPSHOT
    assert projection.source_coverage_reason is (
        ToolOutputSourceCoverageReason.TERMINAL_SANITIZER_UNAVAILABLE
    )
    assert projection.artifact_disposition is ToolOutputArtifactDisposition.UNAVAILABLE
    assert projection.artifact_unavailability_reason is (
        ToolOutputArtifactUnavailabilityReason.BLOB_PUBLICATION_FAILED
    )


def test_round1_terminal_candidate_uses_the_public_sanitization_boundary() -> None:
    bounded = _terminal_output(8_192)
    bounded.append_raw(
        b"\x1b[31mvisible\x1b[0m\r\nAPI_TOKEN=private-value\r\n"
        b"Authorization: Bearer abc.def\r\n"
    )
    bounded.finalize(status="success", exit_code=0)
    candidate = bounded.artifact_candidate()
    assert candidate.text == (
        "visible\nAPI_TOKEN=<redacted>\nAuthorization: Bearer <redacted>\n"
    )
    assert "private-value" not in candidate.text
    assert "abc.def" not in candidate.text
    assert "\x1b" not in candidate.text


def test_round1_terminal_preserves_full_sanitized_candidate_and_envelope(
    tmp_path: Path,
) -> None:
    source = "HEAD-" + ("x" * 20_000) + "-MIDDLE-SENTINEL-" + ("尾" * 20_000)
    code = (
        "import sys;"
        "sys.stdout.write('HEAD-' + ('x' * 20000) + "
        "'-MIDDLE-SENTINEL-' + ('尾' * 20000))"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"
    session_id = _name("session")
    port = DirectKernelToolPort(
        workspace_root=tmp_path,
        host_owner_id=_name("host"),
        session_id=session_id,
        live_bus=LiveAgentEventBus(),
        authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
    )

    async def invoke_and_close() -> KernelToolResult:
        borrow, invocation_context = direct_tool_invocation_context(
            port,
            session_id=session_id,
            tool_name="terminal",
            tool_call_id="call:terminal",
            attempt_id="attempt:terminal",
            turn_id="turn:terminal",
            assistant_entry_id="entry:terminal",
        )
        try:
            return await port.invoke(
                tool_name="terminal",
                arguments={
                    "command": command,
                    "yield_time_ms": 10_000,
                    "max_output_chars": 2_000,
                },
                tool_call_id="call:terminal",
                attempt_id="attempt:terminal",
                turn_id="turn:terminal",
                assistant_entry_id="entry:terminal",
                invocation_context=invocation_context,
            )
        finally:
            borrow.close()
            await port.aclose()

    result = asyncio.run(invoke_and_close())
    assert result.output_artifact_candidate is not None
    candidate = result.output_artifact_candidate
    assert candidate.text == source
    assert candidate.source_coverage is ToolOutputSourceCoverage.COMPLETE
    public_envelope = json.loads(result.content)
    assert public_envelope["output"] == source[-2_000:]
    assert public_envelope["status"] == "success"
    assert public_envelope["exit_code"] == 0
    assert public_envelope["cwd"] == str(tmp_path)
    assert public_envelope["process_id"]

    publisher = _RecordingPublisher()
    projection = _processor(publisher).prepare(
        workspace_id="workspace",
        result_entry_id="entry",
        public_output=result.content.decode("utf-8"),
        candidate=candidate,
        artifact_source_read=False,
        deadline_monotonic=monotonic() + 10,
    )
    assert publisher.calls == [source.encode("utf-8")]
    preview_envelope = json.loads(projection.canonical_preview.canonical_bytes)
    assert preview_envelope["status"] == "success"
    assert preview_envelope["exit_code"] == 0
    assert preview_envelope["cwd"] == str(tmp_path)
    assert preview_envelope["process_id"] == public_envelope["process_id"]
    assert "MIDDLE-SENTINEL" not in preview_envelope["output"]
    assert "If the omitted content is necessary" in preview_envelope["output"]


class _ScriptedModel(ScriptedKernelModel):
    pass


def _tool_stream(tool_name: str = "test_tool") -> list[object]:
    arguments = "{}"
    return [
        ToolCallStartPayload("call:1", "call:1", tool_name),
        ToolCallDeltaPayload("call:1", "call:1", arguments),
        ToolCallEndPayload(
            block_identity="call:1",
            tool_call_id="call:1",
            tool_name=tool_name,
            arguments_json=arguments,
            utf8_bytes=len(arguments),
            digest=live_digest(arguments),
        ),
    ]


def _text_stream(text: str) -> list[object]:
    return [
        TextStartPayload("text:1"),
        TextDeltaPayload("text:1", text),
        TextEndPayload("text:1", text, len(text.encode()), live_digest(text)),
    ]


class _LargeKnownOutcomeTool:
    def __init__(self, content: str) -> None:
        self.content = content
        self.invocations = 0

    async def authorize(self, **_kwargs: object) -> KernelToolAuthorization:
        return KernelToolAuthorization(KernelToolAuthorizationKind.ALLOW, "test")

    async def request_confirmation(self, **_kwargs: object) -> KernelToolAuthorization:
        raise AssertionError("test tool does not request confirmation")

    async def invoke(self, **_kwargs: object) -> KernelToolResult:
        self.invocations += 1
        return KernelToolResult(state="SUCCESS", content=self.content.encode())


class _LostToolResultAckRepository(ConversationKernelRepository):
    def __init__(self, provider: object) -> None:
        super().__init__(provider)  # type: ignore[arg-type]
        self.lost_ack_count = 0

    def accept_tool_result(self, *args: object, **kwargs: object):
        accepted = super().accept_tool_result(*args, **kwargs)
        if self.lost_ack_count == 0:
            self.lost_ack_count += 1
            raise OSError("injected lost tool-result commit acknowledgement")
        return accepted


class _RejectToolResultEventRepository(ConversationKernelRepository):
    def _append_events(
        self,
        connection: object,
        guard: object,
        *,
        workspace_id: str,
        drafts: tuple[object, ...],
    ):
        if any(
            getattr(getattr(item, "event_type", None), "value", None)
            == "ToolResultAccepted"
            for item in drafts
        ):
            raise ConversationKernelConflict("injected event append conflict")
        return super()._append_events(  # type: ignore[arg-type]
            connection,
            guard,
            workspace_id=workspace_id,
            drafts=drafts,
        )


@pytest.mark.postgres
def test_round1_runner_accepts_known_outcome_when_publication_fails_and_confirms_lost_ack(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = _LostToolResultAckRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    tool = _LargeKnownOutcomeTool("🙂" * 20_000)
    processor = ToolOutputArtifactProcessor(
        provider,
        publisher=_RecordingPublisher(
            failure=KnownArtifactPublicationFailure("injected blob failure")
        ),
    )
    runner = ConversationKernelRunner(
        repository=repository,
        writer_lease=lease,
        model=_ScriptedModel([_tool_stream(), _text_stream("done")]),
        tools=StructuredToolPort(tool),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        workspace_id=workspace_id,
        tool_output_processor=processor,
    )
    result = asyncio.run(runner.run_turn("run it"))
    assert result.final_text == "done"
    assert tool.invocations == 1
    assert repository.lost_ack_count == 1
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        row = connection.execute(
            """
            SELECT r.output_artifact_disposition, r.output_display_kind,
                   r.output_artifact_unavailability_reason,
                   octet_length(e.inline_content), count(ev.event_id)
            FROM pulsara_v3.tool_results AS r
            JOIN pulsara_v3.transcript_entries AS e
              ON e.session_id = r.session_id AND e.id = r.result_entry_id
            JOIN pulsara_v3.agent_events AS ev
              ON ev.session_id = r.session_id
             AND ev.subject_entry_id = r.result_entry_id
             AND ev.event_type = 'ToolResultAccepted'
            WHERE r.session_id = %s
            GROUP BY r.id, e.id
            """,
            (session_id,),
        ).fetchone()
    disposition, display, reason, preview_bytes, event_count = row
    assert (disposition, display, reason, event_count) == (
        "UNAVAILABLE",
        "HEAD_TAIL",
        "BLOB_PUBLICATION_FAILED",
        1,
    )
    assert 0 < preview_bytes <= 65_536


@pytest.mark.postgres
def test_round1_blob_before_event_conflict_rolls_back_all_canonical_rows(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = _RejectToolResultEventRepository(provider)
    workspace_id = _name("workspace")
    lease, turn_id, assistant_entry_id, tool_call_id, attempt_id = _install_tool_call(
        repository, workspace_id
    )
    result_entry_id = _name("entry")
    projection = ToolOutputArtifactProcessor(provider).prepare(
        workspace_id=workspace_id,
        result_entry_id=result_entry_id,
        public_output="x" * 9_000,
        candidate=None,
        artifact_source_read=False,
        deadline_monotonic=monotonic() + 30,
    )
    candidate = build_prepared_tool_result_acceptance(
        guard=lease.guard,
        workspace_id=workspace_id,
        result_id=_name("result"),
        result_entry_id=result_entry_id,
        turn_id=turn_id,
        assistant_entry_id=assistant_entry_id,
        tool_call_id=tool_call_id,
        attempt_id=attempt_id,
        result_state="SUCCESS",
        canonical_preview_content=projection.canonical_preview,
        artifact_disposition=projection.artifact_disposition,
        artifact_id=projection.artifact_id,
        artifact_blob_descriptor=projection.artifact_blob,
        source_coverage=projection.source_coverage,
        display_kind=projection.display_kind,
        source_coverage_reason=projection.source_coverage_reason,
        artifact_unavailability_reason=projection.artifact_unavailability_reason,
        actor_id="tool",
        observed_at=datetime.now(timezone.utc),
        observation_duration_microseconds=None,
        observation_origin_kind=ToolObservationOrigin.BUILTIN,
        trusted_tool_reported_duration_microseconds=None,
    )
    with pytest.raises(ConversationKernelConflict, match="event append conflict"):
        repository.accept_tool_result(
            lease.guard,
            candidate=candidate,
            deadline_monotonic=monotonic() + 30,
        )
    assert projection.artifact_blob is not None
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.blobs WHERE id = %s",
            (projection.artifact_blob.blob_id,),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.transcript_entries WHERE id = %s",
            (candidate.result_entry_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.tool_results WHERE id = %s",
            (candidate.result_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.agent_events WHERE event_id = %s",
            (candidate.tool_result_occurrence.event_id,),
        ).fetchone() == (0,)


@pytest.mark.postgres
def test_round1_missing_artifact_edge_fails_before_canonical_acceptance(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    workspace_id = _name("workspace")
    lease, turn_id, assistant_entry_id, tool_call_id, attempt_id = _install_tool_call(
        repository, workspace_id
    )
    body = b"missing"
    result_entry_id = _name("entry")
    candidate = build_prepared_tool_result_acceptance(
        guard=lease.guard,
        workspace_id=workspace_id,
        result_id=_name("result"),
        result_entry_id=result_entry_id,
        turn_id=turn_id,
        assistant_entry_id=assistant_entry_id,
        tool_call_id=tool_call_id,
        attempt_id=attempt_id,
        result_state="SUCCESS",
        canonical_preview_content=InlineContent.from_bytes(b"preview"),
        artifact_disposition=ToolOutputArtifactDisposition.AVAILABLE,
        artifact_id=_name("artifact"),
        artifact_blob_descriptor=BlobContent(
            _name("blob"),
            "sha256:" + sha256(body).hexdigest(),
            len(body),
            "text/plain",
            "utf-8",
        ),
        source_coverage=ToolOutputSourceCoverage.COMPLETE,
        display_kind=ToolResultDisplayKind.COMPLETE,
        source_coverage_reason=None,
        artifact_unavailability_reason=None,
        actor_id="tool",
        observed_at=datetime.now(timezone.utc),
        observation_duration_microseconds=None,
        observation_origin_kind=ToolObservationOrigin.BUILTIN,
        trusted_tool_reported_duration_microseconds=None,
    )
    with pytest.raises(ConversationKernelConflict, match="different blob"):
        repository.accept_tool_result(
            lease.guard,
            candidate=candidate,
            deadline_monotonic=monotonic() + 30,
        )
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.transcript_entries WHERE id = %s",
            (result_entry_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.tool_results WHERE id = %s",
            (candidate.result_id,),
        ).fetchone() == (0,)


def _install_tool_call(repository: ConversationKernelRepository, workspace_id: str):
    session_id = _name("session")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    turn_id = _name("turn")
    permission_snapshot_id = _name("permission-snapshot")
    repository.start_root_turn(
        lease.guard,
        command_id=_name("command"),
        turn_id=turn_id,
        entry_id=_name("entry"),
        context_binding_revision_id=_name("binding"),
        permission_snapshot_id=permission_snapshot_id,
        requested_permission_mode=DEFAULT_PERMISSION_MODE,
        content=InlineContent.from_bytes(b"run"),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )
    cut = repository.prepare_provider_input_cut(
        lease.guard,
        turn_id=turn_id,
        deadline_monotonic=monotonic() + 30,
    )
    assistant_entry_id = _name("entry")
    tool_call_id = _name("call")
    repository.commit_assistant_message(
        lease.guard,
        cut=cut,
        entry_id=assistant_entry_id,
        parent_content=InlineContent.from_bytes(b""),
        blocks=(
            AssistantToolCallBlock(
                _name("block"),
                tool_call_id,
                "terminal",
                freeze_json({"command": "true"}),
            ),
        ),
        occurred_at=datetime.now(timezone.utc),
        actor_id="model:test",
        deadline_monotonic=monotonic() + 30,
    )
    attempt = repository.accept_tool_attempt(
        lease.guard,
        attempt_id=_name("attempt"),
        assistant_entry_id=assistant_entry_id,
        tool_call_id=tool_call_id,
        authorization_kind="policy",
        authorization_reference="allow",
        actor_kind="runtime",
        actor_id="tool",
        remote_idempotency_key=None,
        retry_of_attempt_id=None,
        permission_snapshot_fingerprint=build_run_permission_snapshot(
            snapshot_id=permission_snapshot_id,
            requested_mode=DEFAULT_PERMISSION_MODE,
            effective_mode=DEFAULT_PERMISSION_MODE,
            admission_source=RunPermissionAdmissionSource.USER_SUBMISSION,
        ).snapshot_fingerprint,
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )
    return lease, turn_id, assistant_entry_id, tool_call_id, attempt.attempt_id


@pytest.mark.postgres
def test_round1_artifact_body_read_scope_pagination_and_nonrecursive_result(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    workspace_id = _name("workspace")
    lease, turn_id, assistant_entry_id, tool_call_id, attempt_id = _install_tool_call(
        repository, workspace_id
    )
    source = "HEAD-" + ("中🙂" * 20_000) + "-MIDDLE-SENTINEL-" + ("尾" * 20_000)
    result_entry_id = _name("entry")
    # Artifact identity is tied to result_entry_id, so prepare the final
    # candidate once with that exact identity.
    projection = ToolOutputArtifactProcessor(provider).prepare(
        workspace_id=workspace_id,
        result_entry_id=result_entry_id,
        public_output=source,
        candidate=None,
        artifact_source_read=False,
        deadline_monotonic=monotonic() + 30,
    )
    candidate = build_prepared_tool_result_acceptance(
        guard=lease.guard,
        workspace_id=workspace_id,
        result_id=_name("result"),
        result_entry_id=result_entry_id,
        turn_id=turn_id,
        assistant_entry_id=assistant_entry_id,
        tool_call_id=tool_call_id,
        attempt_id=attempt_id,
        result_state="SUCCESS",
        canonical_preview_content=projection.canonical_preview,
        artifact_disposition=projection.artifact_disposition,
        artifact_id=projection.artifact_id,
        artifact_blob_descriptor=projection.artifact_blob,
        source_coverage=projection.source_coverage,
        display_kind=projection.display_kind,
        source_coverage_reason=projection.source_coverage_reason,
        artifact_unavailability_reason=projection.artifact_unavailability_reason,
        actor_id="terminal",
        observed_at=datetime.now(timezone.utc),
        observation_duration_microseconds=None,
        observation_origin_kind=ToolObservationOrigin.TERMINAL_PROCESS,
        trusted_tool_reported_duration_microseconds=None,
    )
    repository.accept_tool_result(
        lease.guard,
        candidate=candidate,
        deadline_monotonic=monotonic() + 30,
    )
    assert (
        repository.confirm_tool_result_winner(
            lease.guard,
            candidate=candidate,
            deadline_monotonic=monotonic() + 30,
        )
        is not None
    )
    drifted = replace(
        candidate,
        canonical_preview_content=InlineContent.from_bytes(b"different preview"),
    )
    with pytest.raises(ConversationKernelConflict, match="different winner"):
        repository.confirm_tool_result_winner(
            lease.guard,
            candidate=drifted,
            deadline_monotonic=monotonic() + 30,
        )
    assert projection.artifact_id is not None
    assert projection.artifact_blob is not None

    # The canonical preview is physically inline even if an exact artifact
    # blob exists.  The database, not only the prepared DTO, owns this guard.
    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as connection:
        with pytest.raises(
            psycopg.errors.CheckViolation,
            match="transcript_entries_tool_result_inline_ck",
        ):
            with connection.transaction():
                connection.execute(
                    """
                    UPDATE pulsara_v3.transcript_entries
                    SET inline_content = NULL, blob_id = %s
                    WHERE session_id = %s AND id = %s
                    """,
                    (
                        projection.artifact_blob.blob_id,
                        lease.guard.session_id,
                        result_entry_id,
                    ),
                )

    read_port = PostgresToolArtifactReadPort(
        provider, session_id=lease.guard.session_id, workspace_id=workspace_id
    )
    info = read_port.info(projection.artifact_id).record
    assert info.size_bytes == len(source.encode())
    assert info.source_coverage is ToolOutputSourceCoverage.COMPLETE
    pieces: list[str] = []
    offset = 0
    while True:
        page = read_port.read_text(
            projection.artifact_id,
            offset_chars=offset,
            max_chars=ARTIFACT_READ_HARD_CHARS,
        )
        pieces.append(page.text)
        if not page.has_more:
            break
        assert page.next_offset_chars is not None
        offset = page.next_offset_chars
    assert "".join(pieces) == source

    other_session = repository.acquire_host_writer(
        session_id=_name("session"),
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    with pytest.raises(KeyError):
        PostgresToolArtifactReadPort(
            provider,
            session_id=other_session.guard.session_id,
            workspace_id=workspace_id,
        ).info(projection.artifact_id)
    with pytest.raises(KeyError):
        PostgresToolArtifactReadPort(
            provider,
            session_id=lease.guard.session_id,
            workspace_id=_name("workspace"),
        ).info(projection.artifact_id)

    read_result = ArtifactReadTool(read_port).execute(
        ToolCall(
            id=_name("call"),
            name="artifact_read",
            arguments={
                "artifact_id": projection.artifact_id,
                "offset_chars": 3,
                "max_chars": 7,
            },
        )
    )
    assert read_result.status is ToolResultState.SUCCESS
    assert read_result.artifact_source_read
    assert read_result.output_artifact_candidate is None
    response = json.loads(read_result.output)
    assert response["text"] == source[3:10]

    info_result = ArtifactReadTool(read_port).execute(
        ToolCall(
            id=_name("call"),
            name="artifact_read",
            arguments={"artifact_id": projection.artifact_id, "mode": "info"},
        )
    )
    info_payload = json.loads(info_result.output)
    assert info_payload["status"] == "success"
    assert info_payload["artifact_id"] == projection.artifact_id
    assert info_payload["source_coverage"] == "COMPLETE"

    multibyte_call_id = _name("call")
    multibyte_result = ArtifactReadTool(read_port).execute(
        ToolCall(
            id=multibyte_call_id,
            name="artifact_read",
            arguments={
                "artifact_id": projection.artifact_id,
                "offset_chars": 5,
                "max_chars": 32_000,
            },
        )
    )
    multibyte_payload = json.loads(multibyte_result.output)
    assert len(multibyte_result.output.encode("utf-8")) <= 65_536
    assert 0 < multibyte_payload["returned_chars"] < 32_000
    assert multibyte_payload["next_offset_chars"] == (
        5 + multibyte_payload["returned_chars"]
    )
    assert (
        source[5 : multibyte_payload["next_offset_chars"]]
        == (multibyte_payload["text"])
    )
    assert (
        conservative_artifact_page_logical_utf8_bytes(
            tool_call_id=multibyte_call_id,
            body=multibyte_result.output,
            model_visible_memory_ids=(),
        )
        <= MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES
    )
    if multibyte_payload["has_more"]:
        next_character = source[multibyte_payload["next_offset_chars"]]
        extended = dict(multibyte_payload)
        extended["text"] += next_character
        extended["returned_chars"] += 1
        extended["next_offset_chars"] += 1
        extended["has_more"] = (
            extended["next_offset_chars"] < extended["total_chars"]
        )
        if not extended["has_more"]:
            extended["next_offset_chars"] = None
        extended_body = json.dumps(
            extended,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        assert (
            conservative_artifact_page_logical_utf8_bytes(
                tool_call_id=multibyte_call_id,
                body=extended_body,
                model_visible_memory_ids=(),
            )
            > MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES
        )

    tool = ArtifactReadTool(read_port)
    invalid_arguments = (
        {"artifact_id": projection.artifact_id, "mode": "binary"},
        {"artifact_id": projection.artifact_id, "offset_chars": -1},
        {"artifact_id": projection.artifact_id, "max_chars": 0},
        {"artifact_id": projection.artifact_id, "max_chars": 32_001},
        {"artifact_id": projection.artifact_id, "unknown": True},
    )
    for arguments in invalid_arguments:
        invalid = tool.execute(
            ToolCall(
                id=_name("call"),
                name="artifact_read",
                arguments=arguments,
            )
        )
        assert invalid.status is ToolResultState.ERROR
        assert json.loads(invalid.output)["status"] == "error"

    unknown = tool.execute(
        ToolCall(
            id=_name("call"),
            name="artifact_read",
            arguments={"artifact_id": _name("artifact")},
        )
    )
    cross_session_tool = ArtifactReadTool(
        PostgresToolArtifactReadPort(
            provider,
            session_id=other_session.guard.session_id,
            workspace_id=workspace_id,
        )
    )
    cross_session = cross_session_tool.execute(
        ToolCall(
            id=_name("call"),
            name="artifact_read",
            arguments={"artifact_id": projection.artifact_id},
        )
    )
    assert json.loads(unknown.output)["status"] == "not_found"
    assert json.loads(cross_session.output)["status"] == "not_found"


@pytest.mark.postgres
def test_round1_retention_gap_and_blob_failure_persist_as_independent_reasons(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    workspace_id = _name("workspace")
    lease, turn_id, assistant_entry_id, tool_call_id, attempt_id = _install_tool_call(
        repository, workspace_id
    )
    bounded = _terminal_output(64)
    bounded.append_raw(b"discarded-prefix" * 16)
    bounded.append_raw(b"retained-tail")
    bounded.finalize(status="success", exit_code=0)
    retained = bounded.artifact_candidate()
    result_entry_id = _name("entry")
    projection = ToolOutputArtifactProcessor(
        provider,
        publisher=_RecordingPublisher(
            failure=KnownArtifactPublicationFailure("injected publication failure")
        ),
    ).prepare(
        workspace_id=workspace_id,
        result_entry_id=result_entry_id,
        public_output=json.dumps(
            {"status": "success", "output": retained.text}, ensure_ascii=False
        ),
        candidate=retained,
        artifact_source_read=False,
        deadline_monotonic=monotonic() + 30,
    )
    candidate = build_prepared_tool_result_acceptance(
        guard=lease.guard,
        workspace_id=workspace_id,
        result_id=_name("result"),
        result_entry_id=result_entry_id,
        turn_id=turn_id,
        assistant_entry_id=assistant_entry_id,
        tool_call_id=tool_call_id,
        attempt_id=attempt_id,
        result_state="SUCCESS",
        canonical_preview_content=projection.canonical_preview,
        artifact_disposition=projection.artifact_disposition,
        artifact_id=projection.artifact_id,
        artifact_blob_descriptor=projection.artifact_blob,
        source_coverage=projection.source_coverage,
        display_kind=projection.display_kind,
        source_coverage_reason=projection.source_coverage_reason,
        artifact_unavailability_reason=projection.artifact_unavailability_reason,
        actor_id="terminal",
        observed_at=datetime.now(timezone.utc),
        observation_duration_microseconds=None,
        observation_origin_kind=ToolObservationOrigin.TERMINAL_PROCESS,
        trusted_tool_reported_duration_microseconds=None,
    )
    repository.accept_tool_result(
        lease.guard, candidate=candidate, deadline_monotonic=monotonic() + 30
    )
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        row = connection.execute(
            """
            SELECT output_source_coverage, output_source_coverage_reason,
                   output_artifact_disposition,
                   output_artifact_unavailability_reason
            FROM pulsara_v3.tool_results
            WHERE session_id = %s AND id = %s
            """,
            (lease.guard.session_id, candidate.result_id),
        ).fetchone()
    assert row == (
        "RETAINED_SNAPSHOT",
        "TERMINAL_RETENTION_GAP",
        "UNAVAILABLE",
        "BLOB_PUBLICATION_FAILED",
    )
    with psycopg.connect(
        stage2_migrated_postgres_database.admin_dsn, autocommit=True
    ) as connection:
        connection.execute(
            """
            UPDATE pulsara_v3.tool_results
            SET output_source_coverage_reason = 'TERMINAL_SANITIZER_UNAVAILABLE'
            WHERE id = %s
            """,
            (candidate.result_id,),
        )
        assert connection.execute(
            """
            SELECT output_source_coverage, output_source_coverage_reason
            FROM pulsara_v3.tool_results WHERE id = %s
            """,
            (candidate.result_id,),
        ).fetchone() == ("RETAINED_SNAPSHOT", "TERMINAL_SANITIZER_UNAVAILABLE")
        connection.execute(
            """
            UPDATE pulsara_v3.tool_results
            SET output_source_coverage_reason = 'TERMINAL_RETENTION_GAP'
            WHERE id = %s
            """,
            (candidate.result_id,),
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                """
                UPDATE pulsara_v3.tool_results
                SET output_source_coverage = 'COMPLETE'
                WHERE id = %s
                """,
                (candidate.result_id,),
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                """
                UPDATE pulsara_v3.tool_results
                SET output_artifact_unavailability_reason = NULL
                WHERE id = %s
                """,
                (candidate.result_id,),
            )
        with pytest.raises(psycopg.errors.NotNullViolation):
            connection.execute(
                """
                UPDATE pulsara_v3.tool_results
                SET output_display_kind = NULL
                WHERE id = %s
                """,
                (candidate.result_id,),
            )


@pytest.mark.postgres
def test_round1_retained_snapshot_artifact_offset_zero_is_retained_body_start(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    workspace_id = _name("workspace")
    lease, turn_id, assistant_entry_id, tool_call_id, attempt_id = _install_tool_call(
        repository, workspace_id
    )
    retained_suffix = b"RETAINED-BODY-START-and-tail"
    bounded = _terminal_output(len(retained_suffix))
    bounded.append_raw(b"original-prefix-that-will-be-evicted" * 8)
    bounded.append_raw(retained_suffix)
    bounded.finalize(status="success", exit_code=0)
    retained = bounded.artifact_candidate()
    result_entry_id = _name("entry")
    projection = ToolOutputArtifactProcessor(provider).prepare(
        workspace_id=workspace_id,
        result_entry_id=result_entry_id,
        public_output=json.dumps(
            {"status": "success", "output": retained.text}, ensure_ascii=False
        ),
        candidate=retained,
        artifact_source_read=False,
        deadline_monotonic=monotonic() + 30,
    )
    assert projection.artifact_disposition is ToolOutputArtifactDisposition.INCOMPLETE
    assert projection.artifact_id is not None
    candidate = build_prepared_tool_result_acceptance(
        guard=lease.guard,
        workspace_id=workspace_id,
        result_id=_name("result"),
        result_entry_id=result_entry_id,
        turn_id=turn_id,
        assistant_entry_id=assistant_entry_id,
        tool_call_id=tool_call_id,
        attempt_id=attempt_id,
        result_state="SUCCESS",
        canonical_preview_content=projection.canonical_preview,
        artifact_disposition=projection.artifact_disposition,
        artifact_id=projection.artifact_id,
        artifact_blob_descriptor=projection.artifact_blob,
        source_coverage=projection.source_coverage,
        display_kind=projection.display_kind,
        source_coverage_reason=projection.source_coverage_reason,
        artifact_unavailability_reason=projection.artifact_unavailability_reason,
        actor_id="terminal",
        observed_at=datetime.now(timezone.utc),
        observation_duration_microseconds=None,
        observation_origin_kind=ToolObservationOrigin.TERMINAL_PROCESS,
        trusted_tool_reported_duration_microseconds=None,
    )
    repository.accept_tool_result(
        lease.guard, candidate=candidate, deadline_monotonic=monotonic() + 30
    )
    page = PostgresToolArtifactReadPort(
        provider,
        session_id=lease.guard.session_id,
        workspace_id=workspace_id,
    ).read_text(projection.artifact_id, offset_chars=0, max_chars=32_000)
    assert page.text == retained.text
    assert page.text.startswith("RETAINED-BODY-START")
    assert page.info.record.source_coverage is (
        ToolOutputSourceCoverage.RETAINED_SNAPSHOT
    )
    assert page.info.record.source_coverage_reason is (
        ToolOutputSourceCoverageReason.TERMINAL_RETENTION_GAP
    )


@pytest.mark.postgres
def test_round1_corrupt_blob_is_one_typed_content_error(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    workspace_id = _name("workspace")
    lease, turn_id, assistant_entry_id, tool_call_id, attempt_id = _install_tool_call(
        repository, workspace_id
    )
    result_entry_id = _name("entry")
    projection = ToolOutputArtifactProcessor(provider).prepare(
        workspace_id=workspace_id,
        result_entry_id=result_entry_id,
        public_output="x" * 9_000,
        candidate=None,
        artifact_source_read=False,
        deadline_monotonic=monotonic() + 30,
    )
    candidate = build_prepared_tool_result_acceptance(
        guard=lease.guard,
        workspace_id=workspace_id,
        result_id=_name("result"),
        result_entry_id=result_entry_id,
        turn_id=turn_id,
        assistant_entry_id=assistant_entry_id,
        tool_call_id=tool_call_id,
        attempt_id=attempt_id,
        result_state="SUCCESS",
        canonical_preview_content=projection.canonical_preview,
        artifact_disposition=projection.artifact_disposition,
        artifact_id=projection.artifact_id,
        artifact_blob_descriptor=projection.artifact_blob,
        source_coverage=projection.source_coverage,
        display_kind=projection.display_kind,
        source_coverage_reason=projection.source_coverage_reason,
        artifact_unavailability_reason=projection.artifact_unavailability_reason,
        actor_id="tool",
        observed_at=datetime.now(timezone.utc),
        observation_duration_microseconds=None,
        observation_origin_kind=ToolObservationOrigin.BUILTIN,
        trusted_tool_reported_duration_microseconds=None,
    )
    repository.accept_tool_result(
        lease.guard, candidate=candidate, deadline_monotonic=monotonic() + 30
    )
    assert projection.artifact_blob is not None
    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as connection:
        connection.execute(
            "UPDATE pulsara_v3.blobs SET body = %s WHERE id = %s",
            (b"y" * 9_000, projection.artifact_blob.blob_id),
        )
        connection.commit()
    port = PostgresToolArtifactReadPort(
        provider, session_id=lease.guard.session_id, workspace_id=workspace_id
    )
    with pytest.raises(ArtifactContentError, match="artifact_content_integrity_failed"):
        port.info(projection.artifact_id or "")
    result = ArtifactReadTool(port).execute(
        ToolCall(
            id=_name("call"),
            name="artifact_read",
            arguments={"artifact_id": projection.artifact_id, "mode": "info"},
        )
    )
    assert json.loads(result.output)["status"] == "content_error"

    invalid_utf8 = b"\xff" * 9_000
    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as connection:
        connection.execute(
            """
            UPDATE pulsara_v3.blobs
            SET body = %s, logical_digest = %s
            WHERE id = %s
            """,
            (
                invalid_utf8,
                "sha256:" + sha256(invalid_utf8).hexdigest(),
                projection.artifact_blob.blob_id,
            ),
        )
        connection.commit()
    with pytest.raises(ArtifactContentError, match="artifact_content_codec_failed"):
        port.info(projection.artifact_id or "")

    # A physically missing blob is canonical corruption, not a not-found
    # scope result.  Fault injection bypasses the FK only for this ephemeral
    # database; the product read must still lower it to one typed content
    # error without changing the accepted tool result.
    with psycopg.connect(
        stage2_migrated_postgres_database.admin_dsn, autocommit=True
    ) as connection:
        connection.execute("SET session_replication_role = replica")
        try:
            connection.execute(
                "DELETE FROM pulsara_v3.blobs WHERE id = %s",
                (projection.artifact_blob.blob_id,),
            )
        finally:
            connection.execute("SET session_replication_role = origin")
    missing = ArtifactReadTool(port).execute(
        ToolCall(
            id=_name("call"),
            name="artifact_read",
            arguments={"artifact_id": projection.artifact_id, "mode": "info"},
        )
    )
    missing_payload = json.loads(missing.output)
    assert missing.status is ToolResultState.ERROR
    assert missing_payload["status"] == "content_error"
    assert missing_payload["error_code"] == "artifact_content_missing"


@pytest.mark.postgres
def test_round1_memory_side_branch_confirmation_is_all_or_none(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    workspace_id = _name("workspace")
    lease, turn_id, assistant_entry_id, tool_call_id, attempt_id = _install_tool_call(
        repository, workspace_id
    )
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        memory_domain_id = str(
            connection.execute(
                "SELECT memory_domain_id FROM pulsara_v3.sessions WHERE id=%s",
                (lease.guard.session_id,),
            ).fetchone()[0]
        )
    memory_candidate = prepare_memory_candidate(
        candidate_id=_name("candidate"),
        memory_domain_id=memory_domain_id,
        origin_workspace_id=workspace_id,
        origin_session_id=lease.guard.session_id,
        producer_kind=MemoryProducerKind.MAIN_AGENT_REMEMBER,
        producer_entry_id=assistant_entry_id,
        producer_tool_call_id=tool_call_id,
        proposal=FrozenMemoryProposal(
            statement="remember",
            scope_kind=MemoryScopeKind.USER,
            scope_id=CTX_USER,
            kind_hint=MemoryKindHint.FACT,
        ),
    )
    candidate = build_prepared_tool_result_acceptance(
        guard=lease.guard,
        workspace_id=workspace_id,
        result_id=_name("result"),
        result_entry_id=_name("entry"),
        turn_id=turn_id,
        assistant_entry_id=assistant_entry_id,
        tool_call_id=tool_call_id,
        attempt_id=attempt_id,
        result_state="SUCCESS",
        canonical_preview_content=InlineContent.from_bytes(b"proposed"),
        artifact_disposition=ToolOutputArtifactDisposition.NOT_REQUIRED,
        artifact_id=None,
        artifact_blob_descriptor=None,
        source_coverage=ToolOutputSourceCoverage.COMPLETE,
        display_kind=ToolResultDisplayKind.COMPLETE,
        source_coverage_reason=None,
        artifact_unavailability_reason=None,
        actor_id="remember_claim",
        observed_at=datetime.now(timezone.utc),
        observation_duration_microseconds=None,
        observation_origin_kind=ToolObservationOrigin.BUILTIN,
        trusted_tool_reported_duration_microseconds=None,
        memory_candidate=memory_candidate,
    )
    assert isinstance(candidate.side_branch, PreparedMemoryProposalSideBranch)
    repository.accept_tool_result(
        lease.guard, candidate=candidate, deadline_monotonic=monotonic() + 30
    )
    assert (
        repository.confirm_tool_result_winner(
            lease.guard, candidate=candidate, deadline_monotonic=monotonic() + 30
        )
        is not None
    )
    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as connection:
        connection.execute(
            "DELETE FROM pulsara_v3.memory_candidates WHERE id = %s",
            (candidate.side_branch.candidate.candidate_id,),
        )
        connection.commit()
    with pytest.raises(
        ConversationKernelConflict, match="memory candidate side branch"
    ):
        repository.confirm_tool_result_winner(
            lease.guard, candidate=candidate, deadline_monotonic=monotonic() + 30
        )


def test_round1_production_descriptor_executor_closure(tmp_path: Path) -> None:
    class _MissingReadPort:
        def info(self, artifact_id: str):
            raise KeyError(artifact_id)

        def lookup(self, artifact_id: str):
            del artifact_id
            return None

        def read_text(self, artifact_id: str, *, offset_chars: int, max_chars: int):
            del offset_chars, max_chars
            raise KeyError(artifact_id)

    port = DirectKernelToolPort(
        workspace_root=tmp_path,
        host_owner_id=_name("host"),
        session_id=_name("session"),
        live_bus=LiveAgentEventBus(),
        authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
        artifact_read_port=_MissingReadPort(),
    )
    specs = {item.name: item for item in port.tool_specs}
    bindings = {item.tool_name: item for item in port.executor_bindings}
    assert "artifact_read" in specs
    assert set(specs) == set(bindings)
    binding = bindings["artifact_read"]
    assert binding.descriptor_id == "builtin:artifact_read"
    assert binding.descriptor_contract_version == "v1"
    assert binding.descriptor_fingerprint.startswith("sha256:")
    assert binding.input_schema_fingerprint.startswith("sha256:")
    assert binding.catalog_entry_fingerprint.startswith("sha256:")
    assert binding.availability_requirement_fingerprint.startswith("sha256:")
    assert binding.permission_contract_fingerprint.startswith("sha256:")
    assert binding.execution_binding_kind == "artifact_read"
    assert binding.is_read_only
    assert binding.is_concurrency_safe
    assert binding.permission_category == "artifact_read"
    assert binding.executor_identity.endswith("ArtifactReadTool#artifact_read")
    schema = thaw_json(specs["artifact_read"].parameters)
    assert isinstance(schema, dict)
    assert schema["additionalProperties"] is False
    assert schema["properties"]["offset_chars"]["minimum"] == 0
    assert schema["properties"]["max_chars"]["maximum"] == 32_000
    asyncio.run(port.aclose())

    hidden = DirectKernelToolPort(
        workspace_root=tmp_path,
        host_owner_id=_name("host"),
        session_id=_name("session"),
        live_bus=LiveAgentEventBus(),
        authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
    )
    assert "artifact_read" not in {item.name for item in hidden.tool_specs}
    asyncio.run(hidden.aclose())
