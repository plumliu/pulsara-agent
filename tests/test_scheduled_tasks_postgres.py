from __future__ import annotations
from datetime import datetime, timedelta, timezone
from time import monotonic
import pytest
from psycopg.rows import dict_row
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from pulsara_agent.conversation_kernel.contracts import PromptDeliveryMode
from pulsara_agent.conversation_kernel.prompt_content import freeze_canonical_prompt
from pulsara_agent.conversation_kernel.repository_errors import SessionWriterConflict
from pulsara_agent.conversation_kernel.steer import (
    build_prompt_ingress_command,
    QueuedRootTurnAdmissionConfirmationKind,
)
from pulsara_agent.llm.input import FrozenPromptContent
from pulsara_agent.model_input.contracts import CanonicalInputOriginKind
from pulsara_agent.primitives.permission import DEFAULT_PERMISSION_MODE
from pulsara_agent.scheduling.contracts import (
    ScheduledAdmission,
    ScheduledTaskError,
)
from tests.test_stage2_conversation_kernel_postgres import (
    _repository,
    _name,
    _two_connection_resolution_cut,
    _root_provider_input_admission,
)

pytestmark = pytest.mark.postgres


@pytest.fixture
def owner(stage2_migrated_postgres_database):
    repo = _repository(stage2_migrated_postgres_database)
    lease = repo.acquire_host_writer(
        intent="NEW",
        session_id=_name("session"),
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=120,
        deadline_monotonic=monotonic() + 30,
    )
    cut, binding, _ = _two_connection_resolution_cut()
    repo.update_session_model_call_binding(
        lease.guard, binding=binding, deadline_monotonic=monotonic() + 30
    )
    return repo, lease, cut


def task(owner, kind="interval"):
    repo, lease, cut = owner
    schedule = {"contract": "scheduled-rule:v1", "kind": kind}
    if kind == "once":
        schedule["run_at_utc"] = (
            (datetime.now(timezone.utc) + timedelta(hours=1))
            .replace(microsecond=0)
            .isoformat()
        )
    else:
        schedule.update(anchor_at_utc="2020-01-01T00:00:00+00:00", seconds=60)
    return repo.create_scheduled_task(
        task_id=_name("scheduled"),
        session_id=lease.guard.session_id,
        memory_domain_id="u_local",
        guard=lease.guard,
        values=dict(
            name="report",
            prompt="original task",
            schedule=schedule,
            timezone="Asia/Shanghai",
            permission_mode=DEFAULT_PERMISSION_MODE.value,
        ),
        model_resolution_snapshot=cut,
        deadline_monotonic=monotonic() + 30,
    )


def admission(task, manual=False, command=None):
    return ScheduledAdmission(
        task["id"],
        task["session_id"],
        task["revision"],
        datetime.now(timezone.utc).replace(microsecond=0)
        if manual
        else task["next_run_at"].astimezone(timezone.utc),
        task["prompt"],
        DEFAULT_PERMISSION_MODE,
        manual=manual,
        client_command_id=command,
    )


def make_due(owner, task):
    repo, lease, _ = owner
    due = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=1)
    with repo._writer_transaction(
        lease.guard, deadline_monotonic=monotonic() + 30
    ) as c:
        c.execute(
            "UPDATE pulsara_v3.scheduled_tasks SET next_run_at=%s WHERE id=%s",
            (due, task["id"]),
        )
    return {**task, "next_run_at": due}


def enqueue(owner, a, command=None):
    repo, lease, cut = owner
    command = command or a.client_command_id or _name("command")
    permissionid = _name("permission")
    permission = repo.prepare_root_permission_snapshot(
        lease.guard,
        snapshot_id=permissionid,
        requested_mode=DEFAULT_PERMISSION_MODE,
        deadline_monotonic=monotonic() + 30,
    )
    candidate = build_prompt_ingress_command(
        session_id=a.session_id,
        command_id=command,
        queue_item_id=_name("queue"),
        client_submission_id=_name("submission"),
        delivery_mode=PromptDeliveryMode.NEW_TURN,
        target_turn_id=None,
        permission_snapshot_id=permissionid,
        requested_permission_mode=DEFAULT_PERMISSION_MODE,
        canonical_prompt=freeze_canonical_prompt(FrozenPromptContent.text(a.prompt)),
        scheduled=a,
    )
    result = repo.enqueue_prompt(
        lease.guard,
        candidate=candidate,
        model_resolution_snapshot=cut,
        occurred_at=datetime.now(timezone.utc),
        actor_id="test",
        deadline_monotonic=monotonic() + 30,
        _expected_permission_snapshot=permission,
    )
    return candidate, result


def read(owner, taskid):
    return owner[0].read_scheduled_task(
        task_id=taskid, memory_domain_id="u_local", deadline_monotonic=monotonic() + 30
    )


def test_final_enqueue_rejects_unchanged_cut_before_database_due(owner):
    # A previously discovered candidate may become future again after a clock
    # rollback. Keep its revision/due identical: only the final database-clock
    # comparison can reject it. A one-hour margin avoids a wall-clock sleep.
    value = task(owner, kind="once")
    candidate = admission(value)
    with pytest.raises(ScheduledTaskError) as rejected:
        enqueue(owner, candidate)
    assert rejected.value.code == "TASK_CUT_CHANGED"
    current = read(owner, value["id"])
    assert current["revision"] == value["revision"]
    assert current["next_run_at"] == candidate.due_at
    assert current["status"] == "ACTIVE"
    repo, lease, _ = owner
    with repo.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM pulsara_v3.prompt_queue_items WHERE session_id=%s",
                (lease.guard.session_id,),
            ).fetchone()[0]
            == 0
        )


def mutate(owner, t, action, values=None):
    repo, lease, _ = owner
    return repo.mutate_scheduled_task(
        task_id=t["id"],
        session_id=t["session_id"],
        memory_domain_id="u_local",
        guard=lease.guard,
        expected_revision=t["revision"],
        action=action,
        values=values,
        deadline_monotonic=monotonic() + 30,
    )


def test_atomic_enqueue_cursor_coalescing_and_old_snapshot(owner):
    t = make_due(owner, task(owner))
    candidate, _ = enqueue(owner, admission(t))
    advanced = read(owner, t["id"])
    assert advanced["next_run_at"] > datetime.now(timezone.utc)
    assert advanced["revision"] == 1
    changed = mutate(
        owner,
        advanced,
        "update",
        dict(
            name="renamed",
            prompt="new task",
            schedule=advanced["schedule"],
            timezone=advanced["timezone"],
            permission_mode=advanced["permission_mode"],
        ),
    )
    assert changed["next_run_at"] == advanced["next_run_at"]
    nextdue = make_due(owner, changed)
    _, merged = enqueue(owner, admission(nextdue))
    assert merged.queue_item_id == candidate.queue_item_id
    with owner[0].connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=monotonic() + 30,
    ) as c:
        rows = c.execute(
            "SELECT * FROM pulsara_v3.prompt_queue_items WHERE session_id=%s",
            (t["session_id"],),
        ).fetchall()
    assert len(rows) == 1 and rows[0]["scheduled_task_revision"] == 1
    assert rows[0]["scheduled_due_at"] == t["next_run_at"]
    paused = mutate(owner, read(owner, t["id"]), "pause")
    assert paused["status"] == "PAUSED" and paused["next_run_at"] is None
    assert (
        owner[0].prepare_prompt_head_consumption(
            session_id=t["session_id"],
            occurred_at=datetime.now(timezone.utc),
            actor_id="test",
            deadline_monotonic=monotonic() + 30,
        )
        is None
    )


def test_enabled_group_includes_dispatched_once_without_making_it_due_again(owner):
    active = make_due(owner, task(owner))
    paused = mutate(owner, task(owner), "pause")
    once = make_due(owner, task(owner, "once"))
    enqueue(owner, admission(once))
    completed = read(owner, once["id"])
    assert completed["status"] == "COMPLETED" and completed["next_run_at"] is None

    def listing(status, **kwargs):
        rows, _ = owner[0].list_scheduled_tasks(
            memory_domain_id="u_local", session_id=active["session_id"],
            status=status, deadline_monotonic=monotonic() + 30, **kwargs,
        )
        return {row["id"] for row in rows}

    enabled_ids = {active["id"], completed["id"]}
    assert listing("ENABLED") == enabled_ids
    assert listing("ACTIVE") == {active["id"]}
    assert listing("COMPLETED") == {completed["id"]}
    assert listing("PAUSED") == {paused["id"]}
    # Group filtering remains inside the cursor query and cannot widen due admission.
    assert listing("ENABLED", cursor=min(enabled_ids)) == {max(enabled_ids)}
    assert listing("ENABLED", due_only=True) == {active["id"]}
    with pytest.raises(ScheduledTaskError) as rejected:
        enqueue(owner, admission(once))
    assert rejected.value.code == "TASK_CUT_CHANGED"


def test_manual_merged_action_survives_consumption_and_task_deletion(owner):
    t = make_due(owner, task(owner, "once"))
    first, _ = enqueue(owner, admission(t))
    assert read(owner, t["id"])["status"] == "COMPLETED"
    assert read(owner, t["id"])["revision"] == 2
    manual = admission(read(owner, t["id"]), True, _name("command"))
    merged = owner[0].merge_scheduled_manual(
        session_id=t["session_id"],
        admission=manual,
        memory_domain_id="u_local",
        guard=owner[1].guard,
        deadline_monotonic=monotonic() + 30,
    )
    assert merged["id"] == first.queue_item_id
    candidate = owner[0].prepare_prompt_head_consumption(
        session_id=t["session_id"],
        occurred_at=datetime.now(timezone.utc),
        actor_id="test",
        deadline_monotonic=monotonic() + 30,
    )
    assert candidate.input_origin is CanonicalInputOriginKind.SCHEDULED_TASK
    assert candidate.scheduled_input.task_id == t["id"]
    result = owner[0].consume_prepared_prompt_head(
        owner[1].guard,
        candidate=candidate,
        provider_input_admission=_root_provider_input_admission(
            candidate.provider_input_candidate
        ),
        deadline_monotonic=monotonic() + 30,
    )
    assert result.kind is QueuedRootTurnAdmissionConfirmationKind.FULL
    mutate(owner, read(owner, t["id"]), "delete")
    confirmed = owner[0].confirm_scheduled_action(
        request=manual.action_value(),
        memory_domain_id="u_local",
        deadline_monotonic=monotonic() + 30,
    )
    assert confirmed["id"] == first.queue_item_id and confirmed["status"] == "CONSUMED"
    assert (
        owner[0].confirm_prepared_prompt_head_consumption(
            candidate=candidate, deadline_monotonic=monotonic() + 30
        )
        == result
    )
    with owner[0].connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR, deadline_monotonic=monotonic() + 30
    ) as c:
        provenance = c.execute(
            "SELECT scheduled_input FROM pulsara_v3.transcript_entries WHERE id=%s",
            (candidate.exact_initial_entry_id,),
        ).fetchone()[0]
    assert provenance == candidate.scheduled_input.to_dict()


def test_edits_revisions_and_old_rejection_do_not_pause_new_cut(owner):
    t = make_due(owner, task(owner))
    old = admission(t)
    values = {
        k: t[k] for k in ("name", "prompt", "schedule", "timezone", "permission_mode")
    }
    values["name"] = "new"
    edited = mutate(owner, t, "update", values)
    assert edited["next_run_at"] == t["next_run_at"] and edited["revision"] == 2
    assert (
        owner[0].observe_scheduled_due(
            session_id=t["session_id"],
            admission=old,
            memory_domain_id="u_local",
            guard=owner[1].guard,
            pause=True,
            deadline_monotonic=monotonic() + 30,
        )
        == "STALE"
    )
    with pytest.raises(ScheduledTaskError, match="重新加载"):
        mutate(owner, t, "pause")
    assert read(owner, t["id"])["status"] == "ACTIVE"


def test_non_preemptive_writer_and_cold_management(owner):
    t = task(owner)
    repo, lease, _ = owner
    with repo.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR, deadline_monotonic=monotonic() + 30
    ) as c:
        workspace_id = c.execute(
            "SELECT workspace_id FROM pulsara_v3.sessions WHERE id=%s",
            (t["session_id"],),
        ).fetchone()[0]
    with pytest.raises(SessionWriterConflict):
        repo.acquire_host_writer(
            intent="SCHEDULED",
            session_id=t["session_id"],
            workspace_id=workspace_id,
            writer_owner_id=_name("host"),
            lease_seconds=60,
            deadline_monotonic=monotonic() + 30,
        )
    repo.release_host_writer(lease.guard, deadline_monotonic=monotonic() + 30)
    paused = repo.mutate_scheduled_task(
        task_id=t["id"],
        session_id=t["session_id"],
        memory_domain_id="u_local",
        guard=None,
        expected_revision=t["revision"],
        action="pause",
        deadline_monotonic=monotonic() + 30,
    )
    assert paused["status"] == "PAUSED"


@pytest.mark.parametrize("action", ["pause", "delete"])
def test_management_vs_consumption_has_one_transaction_winner(owner, action):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from pulsara_agent.conversation_kernel.repository_errors import (
        ConversationKernelConflict,
    )

    t = make_due(owner, task(owner))
    queued, _ = enqueue(owner, admission(t))
    repo, lease, _ = owner
    candidate = repo.prepare_prompt_head_consumption(
        session_id=t["session_id"],
        occurred_at=datetime.now(timezone.utc),
        actor_id="test",
        deadline_monotonic=monotonic() + 30,
    )
    barrier = Barrier(2)

    def consume():
        barrier.wait()
        try:
            return repo.consume_prepared_prompt_head(
                lease.guard,
                candidate=candidate,
                provider_input_admission=_root_provider_input_admission(
                    candidate.provider_input_candidate
                ),
                deadline_monotonic=monotonic() + 30,
            )
        except ConversationKernelConflict:
            return "STALE"

    def manage():
        barrier.wait()
        return mutate(owner, read(owner, t["id"]), action)

    with ThreadPoolExecutor(max_workers=2) as pool:
        consumed = pool.submit(consume)
        managed = pool.submit(manage)
        consumption = consumed.result()
        managed.result()
    with repo.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=monotonic() + 30,
    ) as c:
        q = c.execute(
            "SELECT status FROM pulsara_v3.prompt_queue_items WHERE session_id=%s AND id=%s",
            (t["session_id"], queued.queue_item_id),
        ).fetchone()
        turn = c.execute(
            "SELECT status FROM pulsara_v3.turns WHERE session_id=%s AND id=%s",
            (t["session_id"], candidate.exact_turn_id),
        ).fetchone()
    assert q["status"] in ("CANCELLED", "CONSUMED")
    if q["status"] == "CONSUMED":
        assert consumption.kind is QueuedRootTurnAdmissionConfirmationKind.FULL
        assert turn["status"] == "RUNNING"  # Pause/delete never stops the winner.
    else:
        assert (
            turn is None and consumption != QueuedRootTurnAdmissionConfirmationKind.FULL
        )
    if action == "pause":
        assert read(owner, t["id"])["status"] == "PAUSED"
    else:
        assert read(owner, t["id"]) is None


def test_manual_new_queue_confirmation_rejects_changed_frozen_prompt(owner):
    from dataclasses import replace
    from pulsara_agent.conversation_kernel.steer import PromptIngressConfirmationKind

    t = task(owner)
    a = admission(t, manual=True, command=_name("manual"))
    candidate, _ = enqueue(owner, a)
    assert (
        owner[0]
        .confirm_prompt_ingress(
            candidate=candidate, deadline_monotonic=monotonic() + 30
        )
        .kind
        is PromptIngressConfirmationKind.FULL_COMPATIBLE
    )
    changed = replace(
        candidate,
        scheduled=replace(a, prompt="different frozen prompt"),
        canonical_prompt=freeze_canonical_prompt(
            FrozenPromptContent.text("different frozen prompt")
        ),
    )
    assert (
        owner[0]
        .confirm_prompt_ingress(candidate=changed, deadline_monotonic=monotonic() + 30)
        .kind
        is PromptIngressConfirmationKind.CONFLICT
    )


def test_archive_failure_preserves_plans_success_deletes_every_status(owner):
    from pulsara_agent.conversation_kernel.repository_errors import SessionDeletionBusy

    repo, lease, _ = owner
    active = task(owner)
    paused = mutate(owner, task(owner), "pause")
    once = make_due(owner, task(owner, "once"))
    enqueue(owner, admission(once))
    completed = read(owner, once["id"])
    before = [read(owner, t["id"]) for t in (active, paused, completed)]
    with pytest.raises(SessionDeletionBusy):
        repo.archive_session(
            session_id=active["session_id"],
            memory_domain_id="u_local",
            closed_writer=lease.guard,
            deadline_monotonic=monotonic() + 30,
        )
    assert [read(owner, t["id"]) for t in (active, paused, completed)] == before
    # Completed once can still own pending input: pause cancels that input while
    # leaving the once definition completed, as in the closed status matrix.
    mutate(owner, completed, "pause")
    repo.release_host_writer(lease.guard, deadline_monotonic=monotonic() + 30)
    assert (
        repo.archive_session(
            session_id=active["session_id"],
            memory_domain_id="u_local",
            closed_writer=lease.guard,
            deadline_monotonic=monotonic() + 30,
        )
        == "ARCHIVED"
    )
    assert all(read(owner, t["id"]) is None for t in (active, paused, completed))
    repo.unarchive_session(
        session_id=active["session_id"],
        memory_domain_id="u_local",
        deadline_monotonic=monotonic() + 30,
    )
    assert all(read(owner, t["id"]) is None for t in (active, paused, completed))


def test_large_task_pages_obey_existing_wire_budget_without_total_task_cap(owner):
    repo, lease, cut = owner
    template = task(owner)
    ids = {template["id"]}
    for _ in range(9):
        values = {
            k: template[k]
            for k in ("name", "prompt", "schedule", "timezone", "permission_mode")
        }
        values["prompt"] = "x" * (900 << 10)
        row = repo.create_scheduled_task(
            task_id=_name("large"),
            session_id=template["session_id"],
            memory_domain_id="u_local",
            guard=lease.guard,
            values=values,
            model_resolution_snapshot=cut,
            deadline_monotonic=monotonic() + 30,
        )
        ids.add(row["id"])
    cursor = None
    seen = []
    pages = 0
    while True:
        rows, cursor = repo.list_scheduled_tasks(
            memory_domain_id="u_local",
            session_id=template["session_id"],
            cursor=cursor,
            deadline_monotonic=monotonic() + 30,
        )
        import json

        assert (
            sum(
                len(json.dumps(r, ensure_ascii=False, default=str).encode())
                for r in rows
            )
            <= 7 << 20
        )
        seen.extend(r["id"] for r in rows)
        pages += 1
        if cursor is None:
            break
    assert set(seen) == ids and len(seen) == len(ids) and pages > 1
