"""Real writer/queue boundaries for independent Cell output records."""

from datetime import datetime, timezone
from time import monotonic
from types import SimpleNamespace

import pytest
from psycopg.rows import dict_row

from pulsara_agent.identity import random_id
from pulsara_agent.conversation_kernel.contracts import (
    InlineContent,
    PromptDeliveryMode,
)
from pulsara_agent.conversation_kernel.repository import (
    AssistantTextBlock,
    AssistantToolCallBlock,
    ConversationKernelRepository,
    build_prepared_tool_result_acceptance,
)
from pulsara_agent.conversation_kernel.queued_prompt_actions import QueuedPromptAction
from pulsara_agent.conversation_kernel.steer import (
    PreparedCellOutputIngress,
    PromptIngressConfirmationKind,
    QueuedRootTurnAdmissionConfirmationKind,
)
from pulsara_agent.llm.input import FrozenPromptContent
from pulsara_agent.ports.terminal_observation import CellOutputContent
from pulsara_agent.ports.artifact import (
    ToolOutputArtifactDisposition,
    ToolResultDisplayKind,
)
from pulsara_agent.ports.tool_execution import ToolOutputSourceCoverage
from pulsara_agent.primitives.tool_observation import ToolObservationOrigin
from pulsara_agent.primitives.context import freeze_json
from pulsara_agent.primitives.permission import DEFAULT_PERMISSION_MODE
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from tests.support.model_config import (
    start_test_root_turn,
    test_model_binding,
    test_model_runtime,
)
from tests.support.postgres import verified_postgres_provider

pytestmark = pytest.mark.postgres


def deadline():
    return monotonic() + 30


def now():
    return datetime.now(timezone.utc)


def read(case, sql, args=()):
    with case.repo.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=deadline(),
    ) as connection:
        return connection.execute(sql, args).fetchall()


@pytest.fixture
def source(isolated_migrated_postgres_database):
    repo = ConversationKernelRepository(
        verified_postgres_provider(isolated_migrated_postgres_database.runtime_dsn)
    )
    workspace = random_id("workspace")
    guard = repo.acquire_host_writer(
        intent="NEW",
        session_id=random_id("session"),
        workspace_id=workspace,
        writer_owner_id=random_id("host"),
        lease_seconds=300,
        deadline_monotonic=deadline(),
    ).guard
    runtime = test_model_runtime()
    binding = test_model_binding(runtime)
    turn = random_id("turn")
    start_test_root_turn(
        repo,
        guard,
        command_id=random_id("command"),
        turn_id=turn,
        entry_id=random_id("entry"),
        context_binding_revision_id=random_id("binding"),
        permission_snapshot_id=random_id("permission"),
        requested_permission_mode=DEFAULT_PERMISSION_MODE,
        model_call_binding=binding,
        content=FrozenPromptContent.text("start script"),
        occurred_at=now(),
        deadline_monotonic=deadline(),
    )
    cut = repo.prepare_provider_input_cut(
        guard, turn_id=turn, deadline_monotonic=deadline()
    )
    assistant, call, attempt = (
        random_id("entry"),
        random_id("call"),
        random_id("attempt"),
    )
    repo.commit_assistant_message(
        guard,
        cut=cut,
        entry_id=assistant,
        parent_content=InlineContent.from_bytes(b"exec script"),
        blocks=(
            AssistantToolCallBlock(
                random_id("block"),
                call,
                "exec_cell",
                freeze_json({"code": 'console.log("same");console.log("same")'}),
            ),
        ),
        occurred_at=now(),
        actor_id="model",
        deadline_monotonic=deadline(),
    )
    case = SimpleNamespace(
        repo=repo,
        guard=guard,
        turn=turn,
        workspace=workspace,
        attempt=attempt,
        runtime=runtime,
        binding=binding,
        cell=random_id("cell"),
    )
    permission = read(
        case,
        "SELECT permission_snapshot_fingerprint FROM pulsara_v3.turns WHERE id=%s",
        (turn,),
    )[0]
    repo.accept_tool_attempt(
        guard,
        attempt_id=attempt,
        assistant_entry_id=assistant,
        tool_call_id=call,
        permission_snapshot_fingerprint=permission["permission_snapshot_fingerprint"],
        authorization_kind="policy",
        authorization_reference="trusted",
        actor_kind="runtime",
        actor_id="executor",
        remote_idempotency_key=None,
        retry_of_attempt_id=None,
        occurred_at=now(),
        deadline_monotonic=deadline(),
    )
    repo.freeze_code_mode_source(
        guard,
        attempt_id=attempt,
        content=InlineContent.from_bytes(
            b'console.log("same");console.log("same")', media_type="text/javascript"
        ),
        deadline_monotonic=deadline(),
    )
    result = build_prepared_tool_result_acceptance(
        guard=guard,
        workspace_id=workspace,
        result_id=random_id("result"),
        result_entry_id=random_id("entry"),
        turn_id=turn,
        assistant_entry_id=assistant,
        tool_call_id=call,
        attempt_id=attempt,
        result_state="SUCCESS",
        canonical_preview_content=InlineContent.from_bytes(b'{"accepted":true}'),
        artifact_disposition=ToolOutputArtifactDisposition.NOT_REQUIRED,
        artifact_id=None,
        artifact_blob_descriptor=None,
        source_coverage=ToolOutputSourceCoverage.COMPLETE,
        display_kind=ToolResultDisplayKind.COMPLETE,
        source_coverage_reason=None,
        artifact_unavailability_reason=None,
        observed_at=now(),
        observation_duration_microseconds=1,
        observation_origin_kind=ToolObservationOrigin.BUILTIN,
        trusted_tool_reported_duration_microseconds=None,
        actor_id="exec_cell",
    )
    repo.accept_tool_result(guard, candidate=result, deadline_monotonic=deadline())
    return case


def candidate(case, sequence, text="same"):
    return PreparedCellOutputIngress(
        case.guard.session_id,
        case.workspace,
        CellOutputContent(
            case.cell,
            case.turn,
            case.attempt,
            sequence,
            {"kind": "console", "text": text},
        ),
        now(),
        "runtime",
    )


def enqueue(case, item):
    return case.repo.enqueue_cell_output(
        case.guard, candidate=item, deadline_monotonic=deadline()
    )


def confirm(case, item):
    return case.repo.confirm_cell_output_ingress(
        candidate=item, deadline_monotonic=deadline()
    )


def queue(case):
    return read(
        case,
        "SELECT * FROM pulsara_v3.prompt_queue_items WHERE session_id=%s ORDER BY queue_sequence",
        (case.guard.session_id,),
    )


def finish_source(case):
    cut = case.repo.prepare_provider_input_cut(
        case.guard, turn_id=case.turn, deadline_monotonic=deadline()
    )
    accepted = case.repo.commit_assistant_message(
        case.guard,
        cut=cut,
        entry_id=random_id("entry"),
        parent_content=InlineContent.from_bytes(b"script accepted"),
        blocks=(
            AssistantTextBlock(
                random_id("block"), InlineContent.from_bytes(b"script accepted")
            ),
        ),
        complete_turn=True,
        occurred_at=now(),
        actor_id="model",
        deadline_monotonic=deadline(),
    )
    assert accepted.turn_completed


def test_each_record_has_independent_identity_and_lost_ack_stop_confirms_original(
    source,
):
    a, b = candidate(source, 1), candidate(source, 2)
    assert confirm(source, a).kind is PromptIngressConfirmationKind.NONE
    first, second = enqueue(source, a), enqueue(source, b)
    assert (
        first.delivery_mode
        is second.delivery_mode
        is PromptDeliveryMode.STEER_ACTIVE_TURN
    )
    assert first.target_turn_id == second.target_turn_id == source.turn
    assert first.queue_sequence < second.queue_sequence
    assert a.queue_item_id != b.queue_item_id
    assert enqueue(source, a).queue_sequence == first.queue_sequence
    assert len(queue(source)) == 2
    source.repo.interrupt_turn(
        source.guard,
        turn_id=source.turn,
        reason="USER_STOPPED",
        occurred_at=now(),
        actor_id="user",
        deadline_monotonic=deadline(),
    )
    assert [row["status"] for row in queue(source)] == ["REJECTED", "REJECTED"]
    assert (
        confirm(source, a).kind
        is confirm(source, b).kind
        is PromptIngressConfirmationKind.FULL_COMPATIBLE
    )
    # Confirmation after the target ended cannot redirect or create another row.
    assert enqueue(source, a).target_turn_id == source.turn
    assert len(queue(source)) == 2


def test_idle_records_stay_independent_new_turns_and_cancel_only_one(source):
    finish_source(source)
    a, b, c = candidate(source, 1), candidate(source, 2), candidate(source, 3)
    assert enqueue(source, a).delivery_mode is PromptDeliveryMode.NEW_TURN
    assert enqueue(source, b).delivery_mode is PromptDeliveryMode.NEW_TURN
    assert enqueue(source, c).delivery_mode is PromptDeliveryMode.NEW_TURN
    source.repo.apply_queued_prompt_action(
        source.guard,
        candidate=QueuedPromptAction(
            source.guard.session_id, random_id("command"), a.queue_item_id
        ),
        occurred_at=now(),
        actor_id="user",
        deadline_monotonic=deadline(),
    )
    assert [row["status"] for row in queue(source)] == [
        "CANCELLED",
        "PENDING",
        "PENDING",
    ]
    assert confirm(source, a).kind is PromptIngressConfirmationKind.FULL_COMPATIBLE
    prepared = source.repo.prepare_prompt_head_consumption(
        session_id=source.guard.session_id,
        occurred_at=now(),
        actor_id="runtime",
        deadline_monotonic=deadline(),
    )
    assert prepared.queue_item_id == b.queue_item_id
    accepted = source.repo.consume_prepared_prompt_head(
        source.guard, candidate=prepared, deadline_monotonic=deadline()
    )
    assert accepted.kind is QueuedRootTurnAdmissionConfirmationKind.FULL
    rows = read(
        source,
        "SELECT * FROM pulsara_v3.transcript_entries WHERE id=%s",
        (prepared.exact_initial_entry_id,),
    )
    assert rows[0]["entry_kind"] == "TERMINAL_OBSERVATION"
    assert rows[0]["input_origin"] == "CELL_OUTPUT"
    assert CellOutputContent.from_bytes(bytes(rows[0]["inline_content"])) == b.content
    assert (
        source.repo.confirm_prepared_prompt_head_consumption(
            candidate=prepared, deadline_monotonic=deadline()
        ).kind
        is QueuedRootTurnAdmissionConfirmationKind.FULL
    )
    assert confirm(source, b).kind is PromptIngressConfirmationKind.FULL_COMPATIBLE

    waiting = confirm(source, c)
    assert (
        waiting.delivery_mode is PromptDeliveryMode.NEW_TURN
        and waiting.target_turn_id is None
    )
    later = enqueue(source, candidate(source, 4))
    assert later.delivery_mode is PromptDeliveryMode.STEER_ACTIVE_TURN
    assert later.target_turn_id == prepared.exact_turn_id


def test_two_cell_steers_quote_consume_and_confirm_as_runtime_items(source):
    from pulsara_agent.conversation_kernel.reader import CanonicalProviderInputReader
    from pulsara_agent.conversation_kernel.provider_dispatch import (
        _prospective_steer_compile_snapshot,
    )
    from pulsara_agent.conversation_kernel.steer import (
        build_steer_canonical_base_fence,
        build_steer_consumption_candidate,
        SteerConsumptionConfirmationKind,
    )
    from pulsara_agent.model_input.contracts import (
        FrozenProviderInputItemKind,
        ModelInputScopeKind,
    )
    from pulsara_agent.model_input.continuity import (
        FULL_HISTORY_CONTEXT_BASE_IDENTITY,
        NoNewTriggerAnchor,
        ProcessLocalCanonicalFrontier,
        ProviderInputContinuityScope,
    )
    from tests.support.round3 import new_test_provider_input_continuity_owner

    a, b = candidate(source, 1), candidate(source, 2)
    enqueue(source, a)
    enqueue(source, b)
    facts = source.repo.read_pending_prompt_steer_facts(
        session_id=source.guard.session_id,
        target_turn_id=source.turn,
        deadline_monotonic=deadline(),
    )
    prompts = tuple(
        source.repo.hydrate_pending_prompt_steer(
            fact=fact, deadline_monotonic=deadline()
        )
        for fact in facts
    )
    cut = source.repo.prepare_provider_input_cut(
        source.guard, turn_id=source.turn, deadline_monotonic=deadline()
    )
    reader = CanonicalProviderInputReader(source.repo.connection_provider)
    base = reader.read_frozen_compile_snapshot(cut, deadline_monotonic=deadline())
    quote = _prospective_steer_compile_snapshot(base, facts=facts, prompts=prompts)
    assert [item.item_kind for item in quote.canonical_input.items[-2:]] == [
        FrozenProviderInputItemKind.TERMINAL_OBSERVATION
    ] * 2
    planning = new_test_provider_input_continuity_owner(
        source.guard.session_id
    ).freeze_planning_input(
        scope=ProviderInputContinuityScope(
            source.guard.session_id, ModelInputScopeKind.ROOT
        ),
        canonical_frontier=ProcessLocalCanonicalFrontier(
            latest_context_binding_revision_id=cut.context_binding_revision_id,
            context_base_semantic_identity=FULL_HISTORY_CONTEXT_BASE_IDENTITY,
            through_sequence=cut.provider_input_through_sequence,
            ordered_item_fingerprints=("sha256:" + "1" * 64,),
        ),
        dispatch_anchor=NoNewTriggerAnchor(None),
    )
    fence = build_steer_canonical_base_fence(base)
    for index, (fact, prompt) in enumerate(zip(facts, prompts, strict=True), 1):
        prepared = build_steer_consumption_candidate(
            fact=fact,
            canonical_prompt=prompt,
            expected_entry_sequence=cut.provider_input_through_sequence + index,
            predecessor=planning,
            canonical_base_fence=fence,
            occurred_at=now(),
            actor_id="runtime",
        )
        source.repo.consume_prepared_prompt_steer(
            source.guard, candidate=prepared, deadline_monotonic=deadline()
        )
        assert (
            source.repo.confirm_prepared_prompt_steer(
                candidate=prepared, deadline_monotonic=deadline()
            ).kind
            is SteerConsumptionConfirmationKind.FULL
        )
    final_cut = source.repo.prepare_provider_input_cut(
        source.guard, turn_id=source.turn, deadline_monotonic=deadline()
    )
    actual = reader.read_frozen_compile_snapshot(
        final_cut, deadline_monotonic=deadline()
    )
    assert actual.canonical_input.items[-2:] == quote.canonical_input.items[-2:]


def test_final_fence_and_plan_terminal_drain_keep_each_runtime_record(source):
    a, b = candidate(source, 1), candidate(source, 2)
    enqueue(source, a)
    enqueue(source, b)

    def finish():
        cut = source.repo.prepare_provider_input_cut(
            source.guard, turn_id=source.turn, deadline_monotonic=deadline()
        )
        return source.repo.commit_assistant_message(
            source.guard,
            cut=cut,
            entry_id=random_id("entry"),
            parent_content=InlineContent.from_bytes(b"done"),
            blocks=(
                AssistantTextBlock(
                    random_id("block"), InlineContent.from_bytes(b"done")
                ),
            ),
            complete_turn=True,
            occurred_at=now(),
            actor_id="model",
            deadline_monotonic=deadline(),
        )

    assert not finish().turn_completed
    with source.repo._writer_transaction(
        source.guard, deadline_monotonic=deadline()
    ) as connection:
        source.repo._consume_plan_terminal_inputs(
            connection, source.guard, turn_id=source.turn, workspace_id=source.workspace
        )
    assert [row["status"] for row in queue(source)] == ["CONSUMED", "CONSUMED"]
    entries = read(
        source,
        "SELECT entry_kind,input_origin FROM pulsara_v3.transcript_entries WHERE input_origin='CELL_OUTPUT' AND session_id=%s ORDER BY entry_sequence",
        (source.guard.session_id,),
    )
    assert [(row["entry_kind"], row["input_origin"]) for row in entries] == [
        ("TERMINAL_OBSERVATION", "CELL_OUTPUT")
    ] * 2
    assert finish().turn_completed
    assert (
        confirm(source, a).kind
        is confirm(source, b).kind
        is PromptIngressConfirmationKind.FULL_COMPATIBLE
    )


def test_protocol_future_queue_counts_pages_and_sources_share_fifo(source):
    from tests.support.model_config import enqueue_test_prompt
    from pulsara_agent.terminal_protocol.canonical_v3 import CanonicalProtocolReader

    finish_source(source)

    def human(text):
        command, item = random_id("command"), random_id("queue")
        enqueue_test_prompt(
            source.repo,
            source.guard,
            command_id=command,
            queue_item_id=item,
            client_submission_id=command,
            delivery_mode=PromptDeliveryMode.NEW_TURN,
            target_turn_id=None,
            permission_snapshot_id=random_id("permission"),
            requested_permission_mode=DEFAULT_PERMISSION_MODE,
            model_call_binding=source.binding,
            content=FrozenPromptContent.text(text),
            occurred_at=now(),
            actor_id="user",
            deadline_monotonic=deadline(),
        )
        return item

    first = human("A")
    cell = candidate(source, 1)
    enqueue(source, cell)
    last = human("B")
    reader = CanonicalProtocolReader(source.repo.connection_provider)
    snapshot = reader.snapshot(
        session_id=source.guard.session_id,
        maximum_entries=128,
        maximum_control_items=128,
        deadline_monotonic=deadline(),
    )
    assert snapshot.control.prompt_queue_total_count == 3
    assert [item.queue_item_id for item in snapshot.control.prompt_queue] == [
        first,
        cell.queue_item_id,
        last,
    ]
    assert [item.input_origin for item in snapshot.control.prompt_queue] == [
        "HUMAN_MESSAGE",
        "CELL_OUTPUT",
        "HUMAN_MESSAGE",
    ]
    first_page, cursor = reader.queue_page(
        session_id=source.guard.session_id,
        memory_domain_id="u_local",
        root_session_id=None,
        after_sequence=0,
        limit=2,
        deadline_monotonic=deadline(),
    )
    assert [item.queue_item_id for item in first_page] == [first, cell.queue_item_id]
    assert first_page[1].source_turn_id == source.turn
    assert first_page[1].source_tool_attempt_id == source.attempt
    second_page, following = reader.queue_page(
        session_id=source.guard.session_id,
        memory_domain_id="u_local",
        root_session_id=None,
        after_sequence=cursor,
        limit=2,
        deadline_monotonic=deadline(),
    )
    assert [item.queue_item_id for item in second_page] == [last]
    assert following is None


def test_cell_new_turn_permissions_follow_current_plan_after_preparation(source):
    from pulsara_agent.conversation_kernel.repository import ConversationKernelConflict
    from pulsara_agent.primitives.permission import PermissionMode
    from pulsara_agent.primitives.run_permission import (
        RunPermissionAdmissionSource,
        RunPermissionOverlay,
    )

    finish_source(source)
    item = candidate(source, 1)
    enqueue(source, item)
    stale = source.repo.prepare_prompt_head_consumption(
        session_id=source.guard.session_id,
        occurred_at=now(),
        actor_id="runtime",
        deadline_monotonic=deadline(),
    )
    source.repo.enter_plan_by_user(
        source.guard,
        command_id=random_id("command"),
        workflow_id=random_id("plan"),
        entry_reason="review safely",
        resume_permission_mode=DEFAULT_PERMISSION_MODE,
        occurred_at=now(),
        actor_id="user",
        deadline_monotonic=deadline(),
    )
    with pytest.raises(ConversationKernelConflict, match="permission changed"):
        source.repo.consume_prepared_prompt_head(
            source.guard, candidate=stale, deadline_monotonic=deadline()
        )
    fresh = source.repo.prepare_prompt_head_consumption(
        session_id=source.guard.session_id,
        occurred_at=now(),
        actor_id="runtime",
        deadline_monotonic=deadline(),
    )
    assert fresh.queue_item_id == stale.queue_item_id == item.queue_item_id
    assert fresh.permission_snapshot.effective_mode is PermissionMode.READ_ONLY
    assert fresh.permission_snapshot.overlay is RunPermissionOverlay.PLAN_READ_ONLY
    assert (
        fresh.permission_snapshot.admission_source
        is RunPermissionAdmissionSource.TERMINAL_OBSERVATION
    )
    assert fresh.permission_snapshot.inherited_from_turn_id == source.turn
    accepted = source.repo.consume_prepared_prompt_head(
        source.guard, candidate=fresh, deadline_monotonic=deadline()
    )
    assert accepted.kind is QueuedRootTurnAdmissionConfirmationKind.FULL


def test_cell_codec_preserves_javascript_nul_and_lone_surrogate_values():
    value = CellOutputContent(
        random_id("cell"),
        random_id("turn"),
        random_id("attempt"),
        1,
        {"kind": "console", "text": "zero\x00 lone\ud800 汉字"},
    )
    decoded = CellOutputContent.from_bytes(value.body)
    assert decoded.canonical_mapping()["record"]["text"] == "zero\x00 lone\ud800 汉字"
    assert decoded == value
    with pytest.raises(TypeError, match="immutable"):
        value.record["text"] = "changed"


def test_scheduled_steer_keeps_provenance_through_shared_plan_drain(source):
    from pulsara_agent.conversation_kernel.prompt_content import freeze_canonical_prompt
    from pulsara_agent.conversation_kernel.reader import CanonicalProviderInputReader
    from pulsara_agent.conversation_kernel.steer import build_prompt_ingress_command
    from pulsara_agent.primitives.permission import PermissionMode
    from pulsara_agent.scheduling.contracts import (
        ScheduledAdmission,
        ScheduledTriggerSource,
    )

    task = source.repo.create_scheduled_task(
        status="ACTIVE",
        session_id=source.guard.session_id,
        memory_domain_id="u_local",
        guard=source.guard,
        values={
            "name": "reminder",
            "prompt": "独立提醒",
            "timezone": "UTC",
            "permission_mode": PermissionMode.READ_ONLY.value,
            "schedule": {
                "contract": "scheduled-rule:v1",
                "kind": "interval",
                "anchor_at_utc": "2020-01-01T00:00:00+00:00",
                "seconds": 3600,
            },
        },
        model_resolution_snapshot=source.runtime.freeze_resolution_snapshot(),
        deadline_monotonic=deadline(),
    )
    command = random_id("command")
    admission = ScheduledAdmission(
        task["id"],
        source.guard.session_id,
        task["revision"],
        now().replace(microsecond=0),
        "独立提醒",
        PermissionMode.READ_ONLY,
        ScheduledTriggerSource.RUN_ONCE,
        command,
    )
    item = build_prompt_ingress_command(
        session_id=source.guard.session_id,
        command_id=command,
        queue_item_id=random_id("queue"),
        client_submission_id=command,
        delivery_mode=PromptDeliveryMode.NEW_TURN,
        target_turn_id=None,
        permission_snapshot_id=random_id("permission"),
        requested_permission_mode=PermissionMode.READ_ONLY,
        canonical_prompt=freeze_canonical_prompt(
            FrozenPromptContent.text("独立提醒"), workspace_id=source.workspace
        ),
        scheduled=admission,
    )
    accepted = source.repo.enqueue_prompt(
        source.guard,
        candidate=item,
        model_resolution_snapshot=None,
        occurred_at=now(),
        actor_id="scheduler",
        deadline_monotonic=deadline(),
    )
    assert accepted.delivery_mode is PromptDeliveryMode.STEER_ACTIVE_TURN
    pending = queue(source)[0]
    assert (
        pending["requested_permission_mode"]
        is pending["permission_snapshot_id"]
        is None
    )
    facts = source.repo.read_pending_prompt_steer_facts(
        session_id=source.guard.session_id,
        target_turn_id=source.turn,
        deadline_monotonic=deadline(),
    )
    assert facts[0].scheduled_input.to_dict() == admission.provenance()
    with source.repo._writer_transaction(
        source.guard, deadline_monotonic=deadline()
    ) as connection:
        source.repo._consume_plan_terminal_inputs(
            connection, source.guard, turn_id=source.turn, workspace_id=source.workspace
        )
    row = queue(source)[0]
    entry = read(
        source,
        "SELECT * FROM pulsara_v3.transcript_entries WHERE id=%s",
        (row["consumed_entry_id"],),
    )[0]
    assert (
        entry["entry_kind"] == "USER_STEER"
        and entry["input_origin"] == "SCHEDULED_TASK"
    )
    assert entry["scheduled_input"] == admission.provenance()
    cut = source.repo.prepare_provider_input_cut(
        source.guard, turn_id=source.turn, deadline_monotonic=deadline()
    )
    actual = CanonicalProviderInputReader(
        source.repo.connection_provider
    ).read_frozen_compile_snapshot(cut, deadline_monotonic=deadline())
    assert (
        actual.canonical_input.items[-1].scheduled_input.to_dict()
        == admission.provenance()
    )
    confirmation = source.repo.confirm_prompt_ingress(
        candidate=item, deadline_monotonic=deadline()
    )
    assert confirmation.kind is PromptIngressConfirmationKind.FULL_COMPATIBLE
    assert confirmation.delivery_mode is PromptDeliveryMode.STEER_ACTIVE_TURN
