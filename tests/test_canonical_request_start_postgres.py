"""A canonical request starts before any provider input is prepared."""

from datetime import datetime, timezone
from dataclasses import replace
from time import monotonic

import pytest
from psycopg.rows import dict_row

from pulsara_agent.conversation_kernel.compaction.contracts import (
    COMPACTION_MODEL_CONTRACT,
    COMPACTION_SNAPSHOT_COMPILER_CONTRACT,
    COMPACTION_SUMMARY_PROMPT_CONTRACT,
    CONTEXT_SNAPSHOT_CODEC,
    CONTEXT_SNAPSHOT_MEDIA_TYPE,
    CompactionActiveRequestLocation,
    CompactionCanonicalAdoptionFactoryInput,
    CompactionCanonicalWritePreconditions,
    CompactionContinuationMode,
    CompactionTargetBranch,
    ExpectedCompactionPredecessorRevision,
    FrozenCompactionActiveRequest,
    build_prepared_compaction_canonical_adoption,
    build_prepared_manual_compaction_command,
    canonical_compaction_range_digest,
    freeze_compaction_canonical_range,
)
from pulsara_agent.conversation_kernel.compaction.prompt import (
    build_compaction_snapshot_carrier,
    freeze_compaction_summary_output,
)
from pulsara_agent.conversation_kernel.contracts import InlineContent, PromptDeliveryMode
from pulsara_agent.conversation_kernel.prompt_content import freeze_canonical_prompt
from pulsara_agent.conversation_kernel.reader import CanonicalProviderInputReader
from pulsara_agent.conversation_kernel.repository import (
    ConversationKernelConflict,
    ConversationKernelRepository,
    PlanContinuationDisposition,
    build_prepared_root_turn_intent,
)
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import FrozenPromptContent
from pulsara_agent.llm.model_connections import ModelConnectionId
from pulsara_agent.model_input.contracts import (
    CanonicalInputOriginKind,
    ContextBindingBaseKind,
    FrozenProviderInputItemKind,
    ModelInputScopeKind,
)
from pulsara_agent.primitives.permission import DEFAULT_PERMISSION_MODE
from pulsara_agent.primitives.plan_workflow import PlanHandoffKind
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from tests.support.model_config import (
    enqueue_test_prompt,
    test_model_binding as model_binding,
    test_model_runtime as model_runtime,
)
from tests.support.postgres import verified_postgres_provider
from tests.test_agent_requests_persistence import request, source as source, start
from tests.test_round4_plan_postgres import (
    _commit_plan_batch,
)


pytestmark = pytest.mark.postgres


def deadline():
    return monotonic() + 30


def now():
    return datetime.now(timezone.utc)


@pytest.fixture
def root(stage2_migrated_postgres_database):
    repository = ConversationKernelRepository(
        verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    )
    lease = repository.acquire_host_writer(
        intent="NEW", session_id=random_id("session"),
        workspace_id=random_id("workspace"), writer_owner_id=random_id("host"),
        lease_seconds=300, deadline_monotonic=deadline(),
    )
    runtime = model_runtime()
    binding = model_binding(runtime)
    repository.update_session_model_call_binding(
        lease.guard, binding=binding, deadline_monotonic=deadline(),
    )
    return repository, lease, runtime, binding


def intent_for(lease, text):
    return build_prepared_root_turn_intent(
        session_id=lease.guard.session_id, command_id=random_id("command"),
        turn_id=random_id("turn"), entry_id=random_id("entry"),
        context_binding_revision_id=random_id("binding"),
        permission_snapshot_id=random_id("permission"),
        requested_permission_mode=DEFAULT_PERMISSION_MODE,
        canonical_prompt=freeze_canonical_prompt(FrozenPromptContent.text(text)),
        occurred_at=now(),
    )


def accept(repository, lease, runtime, intent):
    return repository.accept_root_turn_intent(
        lease.guard, intent=intent,
        model_resolution_snapshot=runtime.freeze_resolution_snapshot(),
        deadline_monotonic=deadline(),
    )


def interrupt(repository, lease, turn_id):
    return repository.interrupt_turn(
        lease.guard, turn_id=turn_id, reason="USER_STOP", occurred_at=now(),
        actor_id="test", deadline_monotonic=deadline(),
    )


def enqueue(repository, lease, binding, text, *, target=None):
    identity = random_id("queue")
    enqueue_test_prompt(
        repository, lease.guard, command_id=random_id("command"),
        queue_item_id=identity, client_submission_id=random_id("command"),
        delivery_mode=(PromptDeliveryMode.NEW_TURN if target is None
                       else PromptDeliveryMode.STEER_ACTIVE_TURN),
        target_turn_id=target,
        permission_snapshot_id=random_id("permission") if target is None else None,
        requested_permission_mode=DEFAULT_PERMISSION_MODE if target is None else None,
        model_call_binding=binding if target is None else None,
        content=FrozenPromptContent.text(text), occurred_at=now(), actor_id="test",
        deadline_monotonic=deadline(),
    )
    return identity


def adopt_predecessor(repository, lease, accepted, group):
    read = CanonicalProviderInputReader(repository.connection_provider).read_frozen_compaction_cut(
        repository.prepare_compaction_input_cut(
            lease.guard, turn_id=group.turn_id, allow_terminal=False,
            deadline_monotonic=deadline(),
        ), deadline_monotonic=deadline(),
    )
    canonical = read.dispatch_read.compile_snapshot.canonical_input
    source_range = freeze_compaction_canonical_range(
        scope=read.scope, effective_materialization_lineage_floor=0,
        source_through_sequence=group.predecessor_through_sequence,
        ordered_items=canonical.items, closures=canonical.closures,
        late_outcomes=canonical.late_outcomes,
    )
    carrier = build_compaction_snapshot_carrier(
        summary=freeze_compaction_summary_output("Previous request was stopped.", maximum_utf8_bytes=100),
        recent_human_requests=(), continuation_mode=CompactionContinuationMode.RESUME_ACTIVE_TURN,
        active_request=FrozenCompactionActiveRequest(
            entry_id=group.instruction_entry_id,
            entry_sequence=accepted.entry_sequence,
            location=CompactionActiveRequestLocation.CANONICAL_SUFFIX,
            item_kind=FrozenProviderInputItemKind.USER,
            input_origin=CanonicalInputOriginKind.HUMAN_MESSAGE, content=None,
        ),
    )
    candidate = build_prepared_compaction_canonical_adoption(CompactionCanonicalAdoptionFactoryInput(
        scope=read.scope, target_branch=CompactionTargetBranch.ACTIVE_INSTALLATION,
        expected_turn_status="RUNNING", predecessor=ExpectedCompactionPredecessorRevision(
            binding_revision_id=group.initial_context_binding_revision_id,
            revision_ordinal=0, base_kind=group.initial_base_kind.value,
            context_snapshot_id=group.initial_context_snapshot_id,
            source_through_sequence=group.initial_source_through_sequence,
        ), snapshot_id=random_id("snapshot"), binding_revision_id=random_id("binding"),
        event_id=random_id("event"), source_through_sequence=group.predecessor_through_sequence,
        source_digest=canonical_compaction_range_digest(read.lineage_base, source_range),
        snapshot_content=InlineContent.from_bytes(carrier.body,
            media_type=CONTEXT_SNAPSHOT_MEDIA_TYPE, codec=CONTEXT_SNAPSHOT_CODEC),
        snapshot_carrier=carrier, compiler_contract=COMPACTION_SNAPSHOT_COMPILER_CONTRACT,
        prompt_contract=COMPACTION_SUMMARY_PROMPT_CONTRACT, model_contract=COMPACTION_MODEL_CONTRACT,
        occurred_at=now(), actor_id="test",
    ))
    result = repository.adopt_context_snapshot(
        lease.guard, candidate=candidate, preconditions=CompactionCanonicalWritePreconditions(
            scope=read.scope, expected_turn_status="RUNNING",
            expected_safe_head=group.provider_input_through_sequence, provider_safe=True,
        ), deadline_monotonic=deadline(),
    )
    assert result.kind.value == "FULL"


def test_direct_start_has_real_identity_before_provider_preparation_and_one_active(root):
    repository, lease, runtime, _binding = root
    intent = intent_for(lease, "Original instruction")
    started = accept(repository, lease, runtime, intent)
    group = started.started_input_group
    assert group.instruction_entry_id == intent.entry_id
    assert group.predecessor_through_sequence == 0
    assert group.initial_base_kind is ContextBindingBaseKind.FULL_HISTORY
    assert group.initial_context_binding_revision_id == intent.context_binding_revision_id
    assert group.ordered_items[0].content[0].text == "Original instruction"
    assert group.provider_input_through_sequence == started.accepted.entry_sequence == 1
    with pytest.raises(ConversationKernelConflict, match="RUNNING"):
        accept(repository, lease, runtime, intent_for(lease, "Another request"))
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR, deadline_monotonic=deadline(),
    ) as connection:
        assert connection.execute(
            "SELECT status,current_context_binding_revision_id FROM pulsara_v3.turns WHERE id=%s",
            (intent.turn_id,),
        ).fetchone() == ("RUNNING", intent.context_binding_revision_id)
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.provider_call_usage WHERE session_id=%s",
            (intent.session_id,),
        ).fetchone()[0] == 0
    assert accept(repository, lease, runtime, intent) == started


def test_start_confirmation_survives_adoption_stop_and_later_requests(root):
    repository, lease, runtime, _binding = root
    old = intent_for(lease, "Older request")
    accept(repository, lease, runtime, old)
    interrupt(repository, lease, old.turn_id)
    intent = intent_for(lease, "Current exact request")
    started = accept(repository, lease, runtime, intent)
    adopt_predecessor(repository, lease, started.accepted, started.started_input_group)
    confirmation = repository.confirm_root_turn_intent(
        intent=intent, guard=lease.guard, deadline_monotonic=deadline(),
    )
    assert confirmation.kind.value == "FULL"
    assert confirmation.started_input_group == started.started_input_group
    interrupt(repository, lease, intent.turn_id)
    accept(repository, lease, runtime, intent_for(lease, "Later request"))
    confirmation = repository.confirm_root_turn_intent(
        intent=intent, guard=lease.guard, deadline_monotonic=deadline(),
    )
    assert confirmation.kind.value == "FULL"
    assert confirmation.started_input_group == started.started_input_group
    assert accept(repository, lease, runtime, intent) == started


def test_queued_group_keeps_profile_context_instruction_and_material_identity(source):
    repository, _root, admit = source
    req = request(instruction="Exact instruction", initial_context=("First context", "Second context"))
    admit(req)
    lease, candidate = start(repository, req)
    result = repository.confirm_prepared_prompt_head_consumption(
        candidate=candidate, deadline_monotonic=deadline(),
    )
    group = result.accepted.started_input_group
    assert group.instruction_entry_id == candidate.exact_initial_entry_id
    assert group.predecessor_through_sequence == 0
    assert [item.input_origin for item in group.ordered_items] == [
        CanonicalInputOriginKind.AGENT_PROFILE, CanonicalInputOriginKind.AGENT_MATERIAL,
        CanonicalInputOriginKind.AGENT_MATERIAL, CanonicalInputOriginKind.AGENT_INSTRUCTION,
    ]
    assert [item.content[0].text for item in group.ordered_items[1:]] == [
        "First context", "Second context", "Exact instruction",
    ]
    assert group.ordered_items[-1].source_entry_id == candidate.exact_initial_entry_id
    assert result.accepted.entry_sequence == 4
    assert group.initial_source_through_sequence == 0
    interrupt(repository, lease, candidate.exact_turn_id)
    assert repository.confirm_prepared_prompt_head_consumption(
        candidate=candidate, deadline_monotonic=deadline(),
    ) == result


def test_queued_confirmation_survives_active_adoption_and_terminal_settlement(root):
    repository, lease, runtime, binding = root
    old = intent_for(lease, "Older request")
    accept(repository, lease, runtime, old)
    interrupt(repository, lease, old.turn_id)
    enqueue(repository, lease, binding, "Exact queued request")
    candidate = repository.prepare_prompt_head_consumption(
        session_id=lease.guard.session_id, occurred_at=now(), actor_id="test",
        deadline_monotonic=deadline(),
    )
    result = repository.consume_prepared_prompt_head(
        lease.guard, candidate=candidate, deadline_monotonic=deadline(),
    )
    group = result.accepted.started_input_group
    adopt_predecessor(repository, lease, result.accepted, group)
    assert repository.confirm_prepared_prompt_head_consumption(
        candidate=candidate, deadline_monotonic=deadline(),
    ) == result
    interrupt(repository, lease, candidate.exact_turn_id)
    accept(repository, lease, runtime, intent_for(lease, "Later request"))
    assert repository.confirm_prepared_prompt_head_consumption(
        candidate=candidate, deadline_monotonic=deadline(),
    ) == result


def test_stop_settles_all_target_steers_and_preserves_independent_queue(root):
    repository, lease, runtime, binding = root
    intent = intent_for(lease, "Prepare before first call")
    accept(repository, lease, runtime, intent)
    steers = [enqueue(repository, lease, binding, f"Steer {index}", target=intent.turn_id)
              for index in range(4)]
    queued = enqueue(repository, lease, binding, "Independent next request")
    interrupt(repository, lease, intent.turn_id)
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR, row_factory=dict_row,
        deadline_monotonic=deadline(),
    ) as connection:
        rows = connection.execute(
            "SELECT id,status,target_turn_id FROM pulsara_v3.prompt_queue_items WHERE session_id=%s",
            (intent.session_id,),
        ).fetchall()
    by_id = {row["id"]: row for row in rows}
    assert all(by_id[identity]["status"] == "REJECTED" for identity in steers)
    assert all(by_id[identity]["target_turn_id"] == intent.turn_id for identity in steers)
    assert by_id[queued]["status"] == "PENDING"
    with pytest.raises(ConversationKernelConflict):
        enqueue(repository, lease, binding, "Late steer", target=intent.turn_id)
    candidate = repository.prepare_prompt_head_consumption(
        session_id=intent.session_id, occurred_at=now(), actor_id="test",
        deadline_monotonic=deadline(),
    )
    result = repository.consume_prepared_prompt_head(
        lease.guard, candidate=candidate, deadline_monotonic=deadline(),
    )
    assert result.kind.value == "FULL"
    assert result.accepted.started_input_group.predecessor_through_sequence == 1
    interrupt(repository, lease, candidate.exact_turn_id)
    assert repository.confirm_prepared_prompt_head_consumption(
        candidate=candidate, deadline_monotonic=deadline(),
    ) == result


def test_manual_command_accepts_exact_idle_selection_and_survives_later_queue(root):
    repository, lease, runtime, binding = root
    intent = intent_for(lease, "Previous history")
    accept(repository, lease, runtime, intent)
    interrupt(repository, lease, intent.turn_id)
    source_cut = repository.prepare_compaction_input_cut(
        lease.guard, turn_id=intent.turn_id, allow_terminal=True,
        deadline_monotonic=deadline(),
    )
    candidate = build_prepared_manual_compaction_command(
        session_id=intent.session_id, command_id=random_id("command"),
        scope_kind=ModelInputScopeKind.ROOT, target_turn_id=intent.turn_id,
        model_call_binding=binding, source_cut=source_cut,
    )
    assert repository.accept_manual_compaction_command(
        lease.guard, candidate=candidate, deadline_monotonic=deadline(),
    ).value == "FULL"
    enqueue(repository, lease, binding, "Later request")
    assert repository.confirm_manual_compaction_command(
        candidate=candidate, deadline_monotonic=deadline(),
    ).value == "FULL"
    assert repository.accept_manual_compaction_command(
        lease.guard, candidate=candidate, deadline_monotonic=deadline(),
    ).value == "FULL"
    conflicting = build_prepared_manual_compaction_command(
        session_id=intent.session_id, command_id=random_id("command"),
        scope_kind=ModelInputScopeKind.ROOT, target_turn_id=intent.turn_id,
        model_call_binding=binding, source_cut=source_cut,
    )
    with pytest.raises(ConversationKernelConflict, match="COMPACTION_BUSY"):
        repository.accept_manual_compaction_command(
            lease.guard, candidate=conflicting, deadline_monotonic=deadline(),
        )


def test_manual_command_rejects_changed_selection_or_history_before_acceptance(root):
    repository, lease, runtime, binding = root
    intent = intent_for(lease, "Previous history")
    accept(repository, lease, runtime, intent)
    interrupt(repository, lease, intent.turn_id)
    source_cut = repository.prepare_compaction_input_cut(
        lease.guard, turn_id=intent.turn_id, allow_terminal=True,
        deadline_monotonic=deadline(),
    )
    candidate = build_prepared_manual_compaction_command(
        session_id=intent.session_id, command_id=random_id("command"),
        scope_kind=ModelInputScopeKind.ROOT, target_turn_id=intent.turn_id,
        model_call_binding=binding, source_cut=source_cut,
    )
    changed_source = build_prepared_manual_compaction_command(
        session_id=intent.session_id, command_id=random_id("command"),
        scope_kind=ModelInputScopeKind.ROOT, target_turn_id=intent.turn_id,
        model_call_binding=binding,
        source_cut=replace(source_cut, provider_input_through_sequence=source_cut.provider_input_through_sequence + 1),
    )
    with pytest.raises(ConversationKernelConflict, match="source or idle target changed"):
        repository.accept_manual_compaction_command(
            lease.guard, candidate=changed_source, deadline_monotonic=deadline(),
        )
    repository.update_session_model_call_binding(
        lease.guard,
        binding=model_binding(model_runtime(connection_id=ModelConnectionId(random_id("connection")))),
        deadline_monotonic=deadline(),
    )
    with pytest.raises(ConversationKernelConflict, match="model selection changed"):
        repository.accept_manual_compaction_command(
            lease.guard, candidate=candidate, deadline_monotonic=deadline(),
        )
    assert repository.confirm_manual_compaction_command(
        candidate=candidate, deadline_monotonic=deadline(),
    ).value == "NONE"


def test_atomic_plan_start_confirmation_survives_takeover_before_ack_confirmation(root):
    repository, lease, runtime, _binding = root
    intent = intent_for(lease, "Plan the requested implementation")
    accept(repository, lease, runtime, intent)
    workspace_id = repository.read_session_workspace_id(
        lease.guard, deadline_monotonic=deadline(),
    )
    prepared = _commit_plan_batch(
        repository, lease, workspace_id=workspace_id, turn_id=intent.turn_id,
        selected_tool_name="enter_plan", selected_arguments={"reason": "Need a plan"},
        workflow_id=random_id("plan"), expected_workflow_revision=None,
    )
    accepted = repository.accept_plan_tool_batch(
        lease.guard, candidate=prepared.candidate,
        deadline_monotonic=deadline(),
    )
    group = repository.read_started_turn_input_group(
        lease.guard, turn_id=accepted.continuation_turn_id, deadline_monotonic=deadline(),
    )
    assert group.instruction_entry_id == accepted.continuation_entry_id
    assert group.ordered_items[0].item_kind is FrozenProviderInputItemKind.PLAN_CONTINUATION
    assert group.ordered_items[0].input_origin is CanonicalInputOriginKind.PLAN_CONTINUATION
    assert group.initial_source_through_sequence == group.predecessor_through_sequence
    # The canonical write has committed while its original caller still needs
    # exact acknowledgement. A real writer takeover terminates its successor.
    successor = repository.acquire_host_writer(
        intent="EXISTING", session_id=lease.guard.session_id,
        workspace_id=workspace_id, writer_owner_id=random_id("host"),
        lease_seconds=300, deadline_monotonic=deadline(),
    )
    assert successor.guard.writer_generation > lease.guard.writer_generation
    assert repository.read_turn_status(
        session_id=lease.guard.session_id, turn_id=accepted.continuation_turn_id,
        deadline_monotonic=deadline(),
    ).value == "INTERRUPTED"
    assert repository.confirm_plan_tool_batch_winner(
        candidate=prepared.candidate, deadline_monotonic=deadline(),
    ) == accepted
    inspection = repository.inspect_plan_continuation(
        session_id=lease.guard.session_id, turn_id=accepted.continuation_turn_id,
        initial_entry_id=accepted.continuation_entry_id,
        workflow_id=accepted.workflow_id, interaction_id=None,
        handoff_kind=PlanHandoffKind.ENTERED_PLAN, deadline_monotonic=deadline(),
    )
    assert repository.classify_plan_continuation(
        inspection, lease.guard,
    ) is PlanContinuationDisposition.HISTORICAL_TERMINAL


def test_confirmed_unstarted_queue_rejection_is_terminal_and_has_no_turn(root):
    repository, lease, _runtime, binding = root
    queued = enqueue(repository, lease, binding, "A request rejected by a start gate")
    candidate = repository.prepare_prompt_head_consumption(
        session_id=lease.guard.session_id, occurred_at=now(), actor_id="test",
        deadline_monotonic=deadline(),
    )
    assert repository.confirm_prepared_prompt_head_consumption(
        candidate=candidate, deadline_monotonic=deadline(),
    ).kind.value == "NONE"
    assert repository.reject_prepared_prompt_head_start_rejected(
        lease.guard, candidate=candidate, deadline_monotonic=deadline(),
    )
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR, deadline_monotonic=deadline(),
    ) as connection:
        assert connection.execute(
            "SELECT status,terminal_reason,consumed_entry_id FROM pulsara_v3.prompt_queue_items WHERE id=%s",
            (queued,),
        ).fetchone() == ("REJECTED", "REQUEST_START_REJECTED", None)
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.turns WHERE session_id=%s",
            (lease.guard.session_id,),
        ).fetchone()[0] == 0
