"""Compaction reads accepted file observations, including private JS results."""

from datetime import datetime, timezone
from time import monotonic
import pytest

from pulsara_agent.conversation_kernel.repository import (
    ConversationKernelRepository,
    AssistantToolCallBlock,
    build_prepared_tool_result_acceptance,
)
from pulsara_agent.conversation_kernel.content_parts import ResultPart
from pulsara_agent.conversation_kernel.reader import CanonicalProviderInputReader
from pulsara_agent.conversation_kernel.compaction.retained_skill import (
    freeze_retained_skill_context,
)
from pulsara_agent.conversation_kernel.tool_invocations import (
    ToolInvocationSource,
    ToolInvocationSourceKind,
    current_tool_invocation,
)
from pulsara_agent.conversation_kernel.contracts import InlineContent
from pulsara_agent.llm.estimator import PulsaraHeuristicTokenEstimatorV3
from pulsara_agent.ports.tool_execution import ToolCall
from pulsara_agent.tools.builtins.filesystem import ReadFileTool
from pulsara_agent.primitives.context import freeze_json
from pulsara_agent.primitives.tool_observation import ToolObservationOrigin
from pulsara_agent.identity import random_id
from tests.support.postgres import verified_postgres_provider
from tests.test_hardcut_entry_parts import open_turn
from tests.test_native_tool_delivery_postgres import save
from tests.test_round5b_long_horizon_context_compaction import _current_skill_inspection
from tests.test_stage2_conversation_kernel_postgres import (
    _assistant_permission_fingerprint,
)


@pytest.mark.postgres
@pytest.mark.parametrize("source_kind", ["native", "js"])
def test_successful_partial_native_and_unpublished_js_reads_reach_skill_handoff(
    stage2_migrated_postgres_database, tmp_path, source_kind
):
    repo = ConversationKernelRepository(
        verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    )
    inspection = _current_skill_inspection(tmp_path, 1)
    path = inspection.readable_skills[0].path.relative_to(tmp_path).as_posix()
    fields = {"target": {"kind": "PATH", "path": path}}
    actual_read = ReadFileTool(tmp_path).execute(
        ToolCall(random_id("call"), "read_file", {**fields, "limit": 1})
    )
    assert actual_read.status.value == "success"
    value = actual_read.business_value
    lease, intent, _, cut = open_turn(repo)
    if source_kind == "native":
        accepted = save(
            repo, lease, intent.turn_id, value=value, arguments={**fields, "limit": 1}
        )
        result_entry_id = accepted.result_entry_id
    else:
        assistant, call, exec_attempt = (
            random_id(k) for k in ("entry", "call", "attempt")
        )
        repo.commit_assistant_message(
            lease.guard,
            cut=cut,
            entry_id=assistant,
            blocks=(
                AssistantToolCallBlock(
                    random_id("block"),
                    call,
                    "exec_cell",
                    freeze_json(
                        {
                            "code": 'await tools.read_file({target:{kind:"PATH",path:"'
                            + path
                            + '"}})'
                        }
                    ),
                ),
            ),
            complete_turn=False,
            occurred_at=datetime.now(timezone.utc),
            actor_id="model:test",
            deadline_monotonic=monotonic() + 30,
        )
        permission = _assistant_permission_fingerprint(
            repo, lease.guard, assistant, monotonic() + 30
        )
        repo.accept_tool_attempt(
            lease.guard,
            attempt_id=exec_attempt,
            assistant_entry_id=assistant,
            tool_call_id=call,
            authorization_kind="policy",
            authorization_reference="test",
            actor_kind="runtime",
            actor_id="test",
            remote_idempotency_key=None,
            retry_of_attempt_id=None,
            permission_snapshot_fingerprint=permission,
            occurred_at=datetime.now(timezone.utc),
            deadline_monotonic=monotonic() + 30,
        )
        repo.freeze_code_mode_source(
            lease.guard,
            attempt_id=exec_attempt,
            content=InlineContent.from_bytes(
                b"await tools.read_file(...)", media_type="text/javascript"
            ),
            deadline_monotonic=monotonic() + 30,
        )
        invocation = ToolInvocationSource(
            random_id("call"),
            ToolInvocationSourceKind.CODE_MODE_CALL,
            intent.turn_id,
            source_exec_attempt_id=exec_attempt,
            call_sequence=1,
            tool_name="read_file",
            original_arguments=freeze_json({**fields, "limit": 1}),
        )
        token = current_tool_invocation.set(invocation)
        try:
            attempt, result_entry_id = random_id("attempt"), random_id("entry")
            repo.accept_tool_capability_decision(
                lease.guard,
                decision_id=random_id("decision"),
                assistant_entry_id=None,
                tool_call_id=invocation.invocation_id,
                decision="ALLOW",
                authorization_reference="test",
                redacted_subject="tool:read_file",
                attempt_id=attempt,
                result_id=None,
                result_entry_id=None,
                denial_content=None,
                denial_result_state=None,
                occurred_at=datetime.now(timezone.utc),
                actor_id="test",
                permission_snapshot_fingerprint=permission,
                deadline_monotonic=monotonic() + 30,
            )
            accepted = build_prepared_tool_result_acceptance(
                guard=lease.guard,
                workspace_id=stage_workspace(repo, intent.turn_id),
                result_id=random_id("result"),
                result_entry_id=result_entry_id,
                turn_id=intent.turn_id,
                assistant_entry_id=None,
                tool_call_id=invocation.invocation_id,
                attempt_id=attempt,
                result_state="SUCCESS",
                public_fields=value.public_fields,
                content_parts=value.content,
                observed_at=datetime.now(timezone.utc),
                observation_duration_microseconds=1,
                observation_origin_kind=ToolObservationOrigin.BUILTIN,
                trusted_tool_reported_duration_microseconds=None,
                actor_id="read_file",
            )
            repo.accept_tool_result(
                lease.guard, candidate=accepted, deadline_monotonic=monotonic() + 30
            )
        finally:
            current_tool_invocation.reset(token)
    cut = repo.prepare_provider_input_cut(
        lease.guard, turn_id=intent.turn_id, deadline_monotonic=monotonic() + 30
    )
    canonical = CanonicalProviderInputReader(
        repo.connection_provider
    ).read_frozen_compaction_cut(cut, deadline_monotonic=monotonic() + 30)
    items = canonical.dispatch_read.compile_snapshot.canonical_input.items
    result = [item for item in items if item.source_entry_id == result_entry_id]
    selection = freeze_retained_skill_context(
        canonical_read=canonical,
        predecessor_epoch=None,
        inspection=inspection,
        estimator=PulsaraHeuristicTokenEstimatorV3(),
    )
    assert len(result) == (1 if source_kind == "native" else 0)
    assert canonical.successful_file_read_paths == (path,)
    assert len(selection.ordered_items) == 1
    assert selection.ordered_items[0].skill_path == str(
        inspection.readable_skills[0].path
    )
    assert "read_file" in selection.rendered_body
    assert inspection.readable_skills[0].body not in selection.rendered_body

    # Repeated compaction carries the same identity even when its transcript
    # contains no result body, and a disabled/deleted source is filtered out.
    from dataclasses import replace
    from types import SimpleNamespace
    from pulsara_agent.model_input.continuity import (
        InstalledObservationState,
        ProviderInputContinuityScope,
    )

    identity = canonical.dispatch_read.compile_snapshot.canonical_input.identity
    predecessor = SimpleNamespace(
        scope=ProviderInputContinuityScope(
            identity.session_id, identity.conversation_scope_kind
        ),
        observation_state=InstalledObservationState(
            reread_skills=selection.ordered_items
        ),
    )
    inherited = replace(canonical, successful_file_read_paths=())
    assert (
        freeze_retained_skill_context(
            canonical_read=inherited,
            predecessor_epoch=predecessor,
            inspection=inspection,
            estimator=PulsaraHeuristicTokenEstimatorV3(),
        ).ordered_items
        == selection.ordered_items
    )
    assert (
        freeze_retained_skill_context(
            canonical_read=canonical,
            predecessor_epoch=predecessor,
            inspection=replace(inspection, readable_skills=()),
            estimator=PulsaraHeuristicTokenEstimatorV3(),
        ).ordered_items
        == ()
    )


def stage_workspace(repo, turn):
    from pulsara_agent.storage.postgres_connection_provider import (
        PostgresConnectionLane,
    )

    with repo.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR, deadline_monotonic=monotonic() + 30
    ) as c:
        return c.execute(
            "SELECT workspace_id FROM pulsara_v3.turns WHERE id=%s", (turn,)
        ).fetchone()[0]


@pytest.mark.postgres
def test_skill_read_facts_obey_result_state_session_and_exact_cut(
    stage2_migrated_postgres_database, tmp_path
):
    from pulsara_agent.ports.tool_execution import ToolBusinessValue

    repo = ConversationKernelRepository(
        verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    )
    inspection = _current_skill_inspection(tmp_path, 3)
    paths = tuple(
        item.path.relative_to(tmp_path).as_posix()
        for item in inspection.readable_skills
    )
    lease, intent, _, _ = open_turn(repo)

    def fields(path):
        return {"target": {"kind": "PATH", "path": path}}

    first = ReadFileTool(tmp_path).execute(
        ToolCall(
            random_id("call"),
            "read_file",
            {**fields(paths[0]), "offset": 2, "limit": 1},
        )
    )
    save(
        repo,
        lease,
        intent.turn_id,
        value=first.business_value,
        arguments={**fields(paths[0]), "offset": 2, "limit": 1},
    )
    cut = repo.prepare_provider_input_cut(
        lease.guard, turn_id=intent.turn_id, deadline_monotonic=monotonic() + 30
    )
    save(
        repo,
        lease,
        intent.turn_id,
        value=ToolBusinessValue(fields(paths[1]), (ResultPart("text", "read failed"),)),
        state="APPLICATION_ERROR",
    )
    last = ReadFileTool(tmp_path).execute(
        ToolCall(random_id("call"), "read_file", fields(paths[2]))
    )
    save(
        repo,
        lease,
        intent.turn_id,
        value=last.business_value,
        arguments=fields(paths[2]),
    )
    # A real read belonging to another session cannot join this source cut.
    other_lease, other_intent, _, _ = open_turn(repo)
    save(
        repo,
        other_lease,
        other_intent.turn_id,
        value=ToolBusinessValue(
            fields(paths[1]), (ResultPart("text", "other session"),)
        ),
    )
    reader = CanonicalProviderInputReader(repo.connection_provider)
    earlier = reader.read_frozen_compaction_cut(
        cut, deadline_monotonic=monotonic() + 30
    )
    assert earlier.successful_file_read_paths == (paths[0],)
    current_cut = repo.prepare_provider_input_cut(
        lease.guard, turn_id=intent.turn_id, deadline_monotonic=monotonic() + 30
    )
    current = reader.read_frozen_compaction_cut(
        current_cut, deadline_monotonic=monotonic() + 30
    )
    assert current.successful_file_read_paths == (paths[0], paths[2])
