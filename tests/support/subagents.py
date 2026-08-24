"""Product-shaped Round 10 subagent fixtures for retained PostgreSQL tests.

The helpers deliberately traverse the stable batch candidate and task-start
settlement APIs.  They do not preserve the removed pre-Round-10 generic task
or child mutation seams.
"""

from __future__ import annotations

from datetime import datetime, timezone
from time import monotonic
from uuid import uuid4

from pulsara_agent.conversation_kernel.repository import (
    AssistantToolCallBlock,
    ConversationKernelRepository,
    build_prepared_tool_result_acceptance,
)
from pulsara_agent.conversation_kernel.cancellation import stable_subagent_turn_id
from pulsara_agent.conversation_kernel.cancellation import (
    ActiveTurnCancellationIntent,
)
from pulsara_agent.conversation_kernel.subagents.contracts import (
    PreparedSubagentLaunch,
    PreparedSubagentTaskBatchAdmission,
    PreparedSubagentTaskDraft,
    SubagentContextMode,
    SubagentProfileKind,
    SubagentTaskStatus,
    _stable_id,
    build_parent_context_call_subject,
    build_parent_context_selection,
    build_subagent_task_start,
)
from pulsara_agent.conversation_kernel.contracts import InlineContent
from pulsara_agent.primitives.context import freeze_json
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.run_permission import (
    RunPermissionAdmissionSource,
    build_run_permission_snapshot,
)
from pulsara_agent.ports.artifact import (
    ToolOutputArtifactDisposition,
    ToolResultDisplayKind,
)
from pulsara_agent.ports.tool_execution import ToolOutputSourceCoverage
from pulsara_agent.primitives.tool_observation import ToolObservationOrigin
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane


def _fixture_id(prefix: str) -> str:
    return f"{prefix}:{uuid4().hex}"


class ActiveSubagentFixtureId(str):
    """String-compatible task identity carrying its exact test launch fact."""

    launch: PreparedSubagentLaunch

    def __new__(
        cls, value: str, launch: PreparedSubagentLaunch
    ) -> "ActiveSubagentFixtureId":
        instance = super().__new__(cls, value)
        instance.launch = launch
        return instance


class StaticSubagentLaunchPreparationPort:
    """Repository-free launch carrier for isolated manager tests."""

    def __init__(
        self,
        *,
        configured_model_identity: str = "test-pro",
        permission_mode: PermissionMode = PermissionMode.BYPASS_PERMISSIONS,
    ) -> None:
        self._model = configured_model_identity
        self._permission_mode = permission_mode

    async def prepare_launch(self, candidate):
        permission = build_run_permission_snapshot(
            snapshot_id=f"parent-permission:{candidate.parent_turn_id}",
            requested_mode=self._permission_mode,
            effective_mode=self._permission_mode,
            admission_source=RunPermissionAdmissionSource.USER_SUBMISSION,
        )
        return PreparedSubagentLaunch(
            task_start=candidate,
            child_turn_id=stable_subagent_turn_id(
                session_id=candidate.session_id,
                task_id=candidate.task_id,
            ),
            configured_model_identity=self._model,
            parent_permission_snapshot=permission,
        )


async def run_admitted_subagent_fixture(
    runner: object,
    task_id: ActiveSubagentFixtureId,
    *,
    cancellation_intent: ActiveTurnCancellationIntent | None = None,
):
    """Exercise the production two-stage child topology in retained tests."""

    intent = await runner.admit_subagent_turn(  # type: ignore[attr-defined]
        launch=task_id.launch,
        cancellation_intent=cancellation_intent,
    )
    return await runner.run_admitted_subagent_turn(  # type: ignore[attr-defined]
        launch=task_id.launch,
        cancellation_intent=intent,
    )


def accept_active_subagent_fixture(
    repository: ConversationKernelRepository,
    lease: object,
    *,
    parent_turn_id: str,
    objective: str,
) -> ActiveSubagentFixtureId:
    """Accept and activate one worker through Round 10's exact product seam."""

    guard = lease.guard
    deadline = monotonic() + 30
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=deadline,
    ) as connection:
        parent = connection.execute(
            """SELECT workspace_id, permission_snapshot_fingerprint
               FROM pulsara_v3.turns
               WHERE session_id = %s AND id = %s""",
            (guard.session_id, parent_turn_id),
        ).fetchone()
    assert parent is not None
    workspace_id = str(parent[0])
    permission_fingerprint = str(parent[1])

    cut = repository.prepare_provider_input_cut(
        guard,
        turn_id=parent_turn_id,
        deadline_monotonic=monotonic() + 30,
    )
    assistant_entry_id = _fixture_id("entry")
    tool_call_id = _fixture_id("call")
    arguments = {"task": objective}
    repository.commit_assistant_message(
        guard,
        cut=cut,
        entry_id=assistant_entry_id,
        parent_content=InlineContent.from_bytes(b"delegate one worker"),
        blocks=(
            AssistantToolCallBlock(
                _fixture_id("block"),
                tool_call_id,
                "spawn_agent",
                freeze_json(arguments),
            ),
        ),
        occurred_at=datetime.now(timezone.utc),
        actor_id="model:test-fixture",
        deadline_monotonic=monotonic() + 30,
    )
    attempt_id = _fixture_id("attempt")
    repository.accept_tool_attempt(
        guard,
        attempt_id=attempt_id,
        assistant_entry_id=assistant_entry_id,
        tool_call_id=tool_call_id,
        authorization_kind="policy",
        authorization_reference="allow",
        actor_kind="runtime",
        actor_id="host:test-fixture",
        remote_idempotency_key=None,
        retry_of_attempt_id=None,
        permission_snapshot_fingerprint=permission_fingerprint,
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )

    subject = build_parent_context_call_subject(
        session_id=guard.session_id,
        caller_turn_id=parent_turn_id,
        provider_input_cut_fingerprint="sha256:test-fixture-cut",
        continuity_epoch_nonce="epoch:test-fixture",
        continuity_epoch_revision=0,
        compiled_semantic_input_fingerprint="sha256:test-fixture-semantic",
        compiled_message_placements_fingerprint="sha256:test-fixture-placements",
        ordered_eligible_units=(),
    )
    selection = build_parent_context_selection(
        subject,
        mode=SubagentContextMode.NONE,
        last_n_turns=None,
    )
    task_id = _stable_id("subagent-task", attempt_id, "0")
    draft = PreparedSubagentTaskDraft(
        task_id=task_id,
        task_key=None,
        label=None,
        profile=SubagentProfileKind.GENERAL_WORKER,
        display_role=None,
        objective=objective,
        context=selection,
        dependency_task_ids=(),
        initial_status=SubagentTaskStatus.PENDING_START,
        pending_reason="CAPACITY",
        terminal_reason=None,
    )
    occurred_at = datetime.now(timezone.utc)
    batch_id = _stable_id("subagent-batch", attempt_id, "batch")
    candidate = PreparedSubagentTaskBatchAdmission(
        session_id=guard.session_id,
        workspace_id=workspace_id,
        writer_generation=guard.writer_generation,
        parent_turn_id=parent_turn_id,
        source_tool_attempt_id=attempt_id,
        permission_snapshot_fingerprint=permission_fingerprint,
        parent_call_subject=subject,
        batch_id=batch_id,
        ordered_tasks=(draft,),
        occurred_at=occurred_at,
        actor_id="host:test-fixture",
    )
    assert repository.accept_subagent_task_batch(
        guard,
        candidate=candidate,
        deadline_monotonic=monotonic() + 30,
    ) == (task_id,)

    start = build_subagent_task_start(
        session_id=guard.session_id,
        workspace_id=workspace_id,
        writer_generation=guard.writer_generation,
        task_id=task_id,
        parent_turn_id=parent_turn_id,
        objective=objective,
        profile=SubagentProfileKind.GENERAL_WORKER,
        parent_context=selection,
        dependency_context=None,
        occurred_at=datetime.now(timezone.utc),
        actor_id="host:test-fixture",
    )
    assert repository.accept_subagent_task_start(
        guard,
        candidate=start,
        deadline_monotonic=monotonic() + 30,
    )
    parent_permission = repository.prepare_subagent_launch_permission(
        guard,
        candidate=start,
        deadline_monotonic=monotonic() + 30,
    )
    launch = PreparedSubagentLaunch(
        task_start=start,
        child_turn_id=stable_subagent_turn_id(
            session_id=guard.session_id,
            task_id=task_id,
        ),
        configured_model_identity="test-pro",
        parent_permission_snapshot=parent_permission,
    )
    observed_at = datetime.now(timezone.utc)
    result = build_prepared_tool_result_acceptance(
        guard=guard,
        workspace_id=workspace_id,
        result_id=_fixture_id("result"),
        result_entry_id=_fixture_id("entry"),
        turn_id=parent_turn_id,
        assistant_entry_id=assistant_entry_id,
        tool_call_id=tool_call_id,
        attempt_id=attempt_id,
        result_state="SUCCESS",
        canonical_preview_content=InlineContent.from_bytes(
            ("{\"status\":\"accepted\",\"task_id\":\"" + task_id + "\"}").encode(
                "utf-8"
            )
        ),
        artifact_disposition=ToolOutputArtifactDisposition.NOT_REQUIRED,
        artifact_id=None,
        artifact_blob_descriptor=None,
        source_coverage=ToolOutputSourceCoverage.COMPLETE,
        display_kind=ToolResultDisplayKind.COMPLETE,
        source_coverage_reason=None,
        artifact_unavailability_reason=None,
        observed_at=observed_at,
        observation_duration_microseconds=None,
        observation_origin_kind=ToolObservationOrigin.BUILTIN,
        trusted_tool_reported_duration_microseconds=None,
        actor_id="host:test-fixture",
    )
    repository.accept_tool_result(
        guard,
        candidate=result,
        deadline_monotonic=monotonic() + 30,
    )
    return ActiveSubagentFixtureId(task_id, launch)


__all__ = [
    "ActiveSubagentFixtureId",
    "StaticSubagentLaunchPreparationPort",
    "accept_active_subagent_fixture",
    "run_admitted_subagent_fixture",
]
