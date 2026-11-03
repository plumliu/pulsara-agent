"""Canonical tool-result fixtures shared by source, history and output tests."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import monotonic
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import FrozenPromptContent
from pulsara_agent.conversation_kernel.contracts import InlineContent
from pulsara_agent.conversation_kernel.blob import CanonicalContentPublisher
from pulsara_agent.conversation_kernel.repository import ConversationKernelRepository, AssistantToolCallBlock
from pulsara_agent.conversation_kernel.tool_outputs import ToolOutputProcessor
from pulsara_agent.primitives.context import freeze_json
from pulsara_agent.primitives.permission import DEFAULT_PERMISSION_MODE
from pulsara_agent.primitives.run_permission import RunPermissionAdmissionSource, build_run_permission_snapshot
from tests.support.model_config import acquire_bound_test_writer, start_test_root_turn, test_model_binding, test_model_runtime


def _name(kind):
    return random_id(kind)


@dataclass
class RecordingOutputPublisher:
    failure: Exception | None = None
    calls: list[bytes] = field(default_factory=list)

    def materialize(self, *, session_id, content, media_type, codec, deadline_monotonic):
        self.calls.append(bytes(content))
        if self.failure is not None:
            raise self.failure
        return CanonicalContentPublisher(object()).describe(
            workspace_id="workspace:" + "a" * 26, content=bytes(content), media_type=media_type, codec=codec)


def output_processor(publisher=None):
    return ToolOutputProcessor(object(), publisher=publisher or RecordingOutputPublisher())


def prepare_output(processor, *, native_output, output_omitted, **body_arguments):
    body = processor.materialize_body(**body_arguments)
    return processor.prepare(
        body=body, native_output=native_output, output_omitted=output_omitted
    )


def _install_tool_call(
    repository: ConversationKernelRepository,
    workspace_id: str,
    *,
    tool_name="exec_process",
    arguments=None,
    attempted=True,
    memory_domain_id="u_local",
):
    session_id = _name("session")
    lease = acquire_bound_test_writer(
        repository,
        session_id=session_id,
        memory_domain_id=memory_domain_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    turn_id = _name("turn")
    permission_snapshot_id = _name("permission")
    start_test_root_turn(
        repository,
        lease.guard,
        command_id=_name("command"),
        turn_id=turn_id,
        entry_id=_name("entry"),
        context_binding_revision_id=_name("binding"),
        permission_snapshot_id=permission_snapshot_id,
        requested_permission_mode=DEFAULT_PERMISSION_MODE,
        model_call_binding=test_model_binding(test_model_runtime()),
        content=FrozenPromptContent.text("run"),
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
                tool_name,
                freeze_json({"command": "true"} if arguments is None else arguments),
            ),
        ),
        occurred_at=datetime.now(timezone.utc),
        actor_id="model:test",
        deadline_monotonic=monotonic() + 30,
    )
    if not attempted:
        return lease, turn_id, assistant_entry_id, tool_call_id, None
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
