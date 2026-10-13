from __future__ import annotations
from uuid import uuid4
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
    ScheduledTriggerSource,
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
    schedule = {"contract": 'scheduled-rule:v1', "kind": kind}
    if kind == "once":
        schedule["run_at_utc"] = (
            (datetime.now(timezone.utc) + timedelta(hours=1))
            .replace(microsecond=0)
            .isoformat()
        )
    else:
        schedule.update(anchor_at_utc="2020-01-01T00:00:00+00:00", seconds=60)
    return repo.create_scheduled_task(

        status="ACTIVE",
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


@pytest.mark.parametrize("status_fields", [{}, {"status": None}, {"status": "PAUSED"}])
@pytest.mark.parametrize("kind", ["interval", "once"])
def test_create_paused_alongside_active_does_not_change_existing_plan(owner, status_fields, kind):
    repo, lease, cut = owner
    active = task(owner, kind)
    row = repo.create_scheduled_task(

        session_id=active["session_id"], memory_domain_id="u_local", guard=lease.guard,
        values={k: active[k] for k in ("name", "prompt", "schedule", "timezone", "permission_mode")},
        model_resolution_snapshot=cut, deadline_monotonic=monotonic() + 30,
        **status_fields,
    )
    assert row["status"] == "PAUSED"
    assert row["next_run_at"] is None
    unchanged = read(owner, active["id"])
    assert (unchanged["status"], unchanged["revision"], unchanged["next_run_at"]) == (
        "ACTIVE", active["revision"], active["next_run_at"],
    )
    with pytest.raises(ScheduledTaskError) as error:
        task(owner, kind)
    assert error.value.code == "ACTIVE_TASK_CONFLICT"


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
        trigger_source=ScheduledTriggerSource.RUN_ONCE
        if manual
        else ScheduledTriggerSource.TIMER,
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
        client_submission_id=_name("command"),
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


def test_tool_null_scope_preserves_filtered_pages_and_service_all_sessions(
    owner, monkeypatch
):
    import asyncio
    from dataclasses import replace
    from types import SimpleNamespace

    from pulsara_agent.conversation_kernel._repository import scheduling
    from pulsara_agent.scheduling.service import ScheduledTaskService

    # Small page fixture exercises the existing keyset boundary without adding
    # a product inventory/lifetime limit or creating 129 unrelated task rows.
    monkeypatch.setattr(
        scheduling,
        "STAGE2_LIMITS",
        replace(scheduling.STAGE2_LIMITS, history_page_default_entries=1),
    )
    repo, lease, cut = owner
    current = [mutate(owner, task(owner), "pause") for _ in range(3)]
    active = task(owner)
    other_lease = repo.acquire_host_writer(
        intent="NEW",
        session_id=_name("session"),
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=120,
        deadline_monotonic=monotonic() + 30,
    )
    _, binding, _ = _two_connection_resolution_cut()
    repo.update_session_model_call_binding(
        other_lease.guard, binding=binding, deadline_monotonic=monotonic() + 30
    )
    other = task((repo, other_lease, cut))

    async def run():
        service = ScheduledTaskService(SimpleNamespace())

        async def read_repository(method, **kwargs):
            return getattr(repo, method)(
                memory_domain_id="u_local",
                deadline_monotonic=monotonic() + 30,
                **kwargs,
            )

        service._read = read_repository
        arguments = {"action": "list", "session_id": None, "status": "PAUSED"}
        ids = []
        while True:
            page = await service.invoke(
                arguments,
                default_session_id=lease.guard.session_id,
                permission_mode=DEFAULT_PERMISSION_MODE,
            )
            ids.extend(row["id"] for row in page["tasks"])
            assert all(
                row["session_id"] == lease.guard.session_id for row in page["tasks"]
            )
            if page["next_cursor"] is None:
                break
            arguments = {**arguments, "cursor": page["next_cursor"]}
        assert ids == sorted(row["id"] for row in current)
        assert active["id"] not in ids and other["id"] not in ids
        selected = await service.invoke(
            {"action": "list", "session_id": other_lease.guard.session_id},
            default_session_id=lease.guard.session_id,
            permission_mode=DEFAULT_PERMISSION_MODE,
        )
        assert [row["id"] for row in selected["tasks"]] == [other["id"]]
        all_ids = []
        cursor = None
        while True:
            page = await service.list(cursor=cursor)
            all_ids.extend(row["id"] for row in page["tasks"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        assert set(all_ids) >= {
            *(row["id"] for row in current),
            active["id"],
            other["id"],
        }

    asyncio.run(run())


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


def test_once_retirement_leaves_only_active_and_paused_filters(owner):
    paused = mutate(owner, task(owner), "pause")
    once = make_due(owner, task(owner, "once"))
    first, _ = enqueue(owner, admission(once))
    assert read(owner, once["id"]) is None
    active = make_due(owner, task(owner))

    def listing(status, **kwargs):
        rows, _ = owner[0].list_scheduled_tasks(
            memory_domain_id="u_local",
            session_id=active["session_id"],
            status=status,
            deadline_monotonic=monotonic() + 30,
            **kwargs,
        )
        return {row["id"] for row in rows}

    assert listing(None) == {active["id"], paused["id"]}
    assert listing("ACTIVE") == {active["id"]}
    assert listing("PAUSED") == {paused["id"]}
    assert listing("ACTIVE", due_only=True) == {active["id"]}
    for removed_status in ("ENABLED", "COMPLETED"):
        with pytest.raises(ScheduledTaskError, match="任务状态无效"):
            listing(removed_status)
    with pytest.raises(ScheduledTaskError) as rejected:
        enqueue(owner, admission(once))
    assert rejected.value.code == "TASK_NOT_FOUND"


def test_manual_merged_action_survives_consumption_and_task_deletion(owner):
    t = make_due(owner, task(owner))
    first, _ = enqueue(owner, admission(t))
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
    a = admission(t, manual=True, command=_name("command"))
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
    paused = mutate(owner, task(owner), "pause")
    once = make_due(owner, task(owner, "once"))
    pending, _ = enqueue(owner, admission(once))
    assert read(owner, once["id"]) is None
    active = task(owner)
    before = [read(owner, t["id"]) for t in (active, paused)]
    with pytest.raises(SessionDeletionBusy):
        repo.archive_session(
            session_id=active["session_id"],
            memory_domain_id="u_local",
            closed_writer=lease.guard,
            deadline_monotonic=monotonic() + 30,
        )
    assert [read(owner, t["id"]) for t in (active, paused)] == before
    from pulsara_agent.conversation_kernel.queued_prompt_actions import (
        QueuedPromptAction,
    )

    repo.apply_queued_prompt_action(
        lease.guard,
        candidate=QueuedPromptAction(
            active["session_id"], _name("command"), pending.queue_item_id
        ),
        occurred_at=datetime.now(timezone.utc),
        actor_id="test",
        deadline_monotonic=monotonic() + 30,
    )
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
    assert all(read(owner, t["id"]) is None for t in (active, paused))
    repo.unarchive_session(
        session_id=active["session_id"],
        memory_domain_id="u_local",
        deadline_monotonic=monotonic() + 30,
    )
    assert all(read(owner, t["id"]) is None for t in (active, paused))


def test_large_task_pages_obey_existing_wire_budget_without_total_task_cap(owner):
    repo, lease, cut = owner
    template = mutate(owner, task(owner), "pause")
    ids = {template["id"]}
    for _ in range(9):
        values = {
            k: template[k]
            for k in ("name", "prompt", "schedule", "timezone", "permission_mode")
        }
        values["prompt"] = "x" * (900 << 10)
        row = repo.create_scheduled_task(

            session_id=template["session_id"],
            memory_domain_id="u_local",
            guard=lease.guard,
            values=values,
            model_resolution_snapshot=cut,
            deadline_monotonic=monotonic() + 30,
        )
        assert row["status"] == "PAUSED"
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


@pytest.mark.parametrize("operation", ["create", "resume"])
def test_one_active_plan_rejects_conflict_without_changing_existing_rows(
    owner, operation
):
    inactive = mutate(owner, task(owner), "pause")
    active = task(owner)
    before = [read(owner, row["id"]) for row in (inactive, active)]
    with pytest.raises(ScheduledTaskError) as error:
        if operation == "create":
            task(owner)
        elif operation == "resume":
            mutate(owner, inactive, "resume")

    assert error.value.code == "ACTIVE_TASK_CONFLICT"
    assert [read(owner, row["id"]) for row in (inactive, active)] == before
    # Editing and idempotently resuming the same ACTIVE row remain allowed.
    assert mutate(owner, active, "resume")["revision"] == active["revision"]
    mutate(owner, active, "pause")
    if operation == "create":
        assert task(owner)["status"] == "ACTIVE"
    elif operation == "resume":
        assert mutate(owner, inactive, "resume")["status"] == "ACTIVE"


@pytest.mark.parametrize("operation", ["create", "resume"])
def test_concurrent_cold_management_admits_only_one_active_plan(owner, operation):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    repo, lease, cut = owner
    paused = [mutate(owner, task(owner), "pause") for _ in range(2)]
    repo.release_host_writer(lease.guard, deadline_monotonic=monotonic() + 30)
    barrier = Barrier(2)

    def activate(row):
        barrier.wait(timeout=10)
        try:
            kwargs = dict(
                session_id=row["session_id"],
                memory_domain_id="u_local",
                guard=None,
                deadline_monotonic=monotonic() + 30,
            )
            if operation == "create":
                values = {
                    k: row[k]
                    for k in (
                        "name",
                        "prompt",
                        "schedule",
                        "timezone",
                        "permission_mode",
                    )
                }
                return repo.create_scheduled_task(

                    status="ACTIVE",
                    values=values,
                    model_resolution_snapshot=cut,
                    **kwargs,
                )["status"]
            return repo.mutate_scheduled_task(
                task_id=row["id"],
                expected_revision=row["revision"],
                action="resume",
                **kwargs,
            )["status"]
        except ScheduledTaskError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(activate, paused))
    assert sorted(results) == ["ACTIVE", "ACTIVE_TASK_CONFLICT"]
    rows, cursor = repo.list_scheduled_tasks(
        memory_domain_id="u_local",
        session_id=lease.guard.session_id,
        status="ACTIVE",
        deadline_monotonic=monotonic() + 30,
    )
    assert len(rows) == 1 and cursor is None


def test_database_constraint_rejects_second_active_row_outside_management_owner(owner):
    from psycopg.errors import UniqueViolation

    paused = mutate(owner, task(owner), "pause")
    task(owner)
    repo, lease, _ = owner
    with pytest.raises(UniqueViolation) as error:
        with repo._writer_transaction(
            lease.guard, deadline_monotonic=monotonic() + 30
        ) as connection:
            connection.execute(
                "UPDATE pulsara_v3.scheduled_tasks SET status='ACTIVE', next_run_at=clock_timestamp() WHERE id=%s",
                (paused["id"],),
            )
    assert error.value.diag.constraint_name == "scheduled_tasks_one_active_session"
    assert read(owner, paused["id"])["status"] == "PAUSED"


def switch(owner, source, target, **kwargs):
    repo, lease, cut = owner
    return repo.switch_scheduled_task(
        task_id=source["id"],
        session_id=source["session_id"],
        memory_domain_id="u_local",
        guard=lease.guard,
        expected_revision=source["revision"],
        target=target,

        model_resolution_snapshot=cut,
        deadline_monotonic=monotonic() + 30,
        **kwargs,
    )


@pytest.mark.parametrize("new", [False, True])
def test_atomic_switch_cancels_only_old_pending_and_enables_target(owner, new):
    destination = mutate(owner, task(owner), "pause")
    source = task(owner)
    original, _ = enqueue(owner, admission(source, True, _name("command")))
    target = (
        {
            "kind": "create",
            "values": {
                k: destination[k]
                for k in ("name", "prompt", "schedule", "timezone", "permission_mode")
            },
        }
        if new
        else {
            "kind": "existing",
            "task_id": destination["id"],
            "expected_revision": destination["revision"],
        }
    )
    paused, active = switch(owner, source, target)
    assert paused["status"] == "PAUSED" and paused["next_run_at"] is None
    assert paused["revision"] == source["revision"] + 1
    assert active["status"] == "ACTIVE" and active["next_run_at"] is not None
    assert active["session_id"] == source["session_id"]
    with owner[0].connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR, deadline_monotonic=monotonic() + 30
    ) as c:
        assert (
            c.execute(
                "SELECT status FROM pulsara_v3.prompt_queue_items WHERE id=%s",
                (original.queue_item_id,),
            ).fetchone()[0]
            == "CANCELLED"
        )
        assert (
            c.execute(
                "SELECT count(*) FROM pulsara_v3.scheduled_tasks WHERE session_id=%s AND status='ACTIVE'",
                (source["session_id"],),
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize(
    "failure", ["stale_target", "invalid_time", "write_after_cancel"]
)
def test_switch_failure_rolls_back_both_plans_and_pending(owner, monkeypatch, failure):
    destination = mutate(owner, task(owner), "pause")
    source = task(owner)
    original, _ = enqueue(owner, admission(source, True, _name("command")))
    before = [read(owner, t["id"]) for t in (source, destination)]
    target = {
        "kind": "existing",
        "task_id": destination["id"],
        "expected_revision": destination["revision"],
    }
    if failure == "stale_target":
        target["expected_revision"] += 1
    elif failure == "invalid_time":
        target = {
            "kind": "create",
            "values": {
                k: destination[k]
                for k in ("name", "prompt", "schedule", "timezone", "permission_mode")
            },
        }
        target["values"]["schedule"] = {
            "contract": 'scheduled-rule:v1',
            "kind": "once",
            "run_at_utc": "2020-01-01T00:00:00+00:00",
        }
    else:
        cancel = owner[0]._cancel_scheduled_pending_in_connection

        def failed(*args, **kwargs):
            cancel(*args, **kwargs)
            raise RuntimeError("injected failure after queue cancellation")

        monkeypatch.setattr(owner[0], "_cancel_scheduled_pending_in_connection", failed)
    with pytest.raises((ScheduledTaskError, RuntimeError)):
        switch(owner, source, target)
    assert [read(owner, t["id"]) for t in (source, destination)] == before
    with owner[0].connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR, deadline_monotonic=monotonic() + 30
    ) as c:
        assert (
            c.execute(
                "SELECT status FROM pulsara_v3.prompt_queue_items WHERE id=%s",
                (original.queue_item_id,),
            ).fetchone()[0]
            == "PENDING"
        )
        assert (
            c.execute(
                "SELECT count(*) FROM pulsara_v3.session_commands WHERE session_id=%s AND command_kind='CANCEL_PROMPT'",
                (source["session_id"],),
            ).fetchone()[0]
            == 0
        )


def test_manual_once_retirement_confirmation_and_source_conflict(owner):
    from dataclasses import replace

    t = mutate(owner, task(owner, "once"), "pause")
    a = admission(t, True, _name("command"))
    first, _ = enqueue(owner, a)
    assert read(owner, t["id"]) is None
    request = a.action_value()
    confirmed = owner[0].confirm_scheduled_action(
        request=request, memory_domain_id="u_local", deadline_monotonic=monotonic() + 30
    )
    assert confirmed["id"] == first.queue_item_id and confirmed["status"] == "PENDING"
    with pytest.raises(ScheduledTaskError) as conflict:
        owner[0].confirm_scheduled_action(
            request={**request, "trigger_source": "TIMER"},
            memory_domain_id="u_local",
            deadline_monotonic=monotonic() + 30,
        )
    assert conflict.value.code == "COMMAND_CONFLICT"
    with pytest.raises(ScheduledTaskError) as removed:
        enqueue(owner, replace(a, client_command_id=_name("command")))
    assert removed.value.code == "TASK_NOT_FOUND"


@pytest.mark.parametrize("original_source", list(ScheduledTriggerSource))
def test_coalescing_never_upgrades_original_trigger_source(owner, original_source):
    from dataclasses import replace

    t = make_due(owner, task(owner))
    initial = replace(
        admission(
            t,
            original_source is not ScheduledTriggerSource.TIMER,
            _name("command")
            if original_source is not ScheduledTriggerSource.TIMER
            else None,
        ),
        trigger_source=original_source,
    )
    first, _ = enqueue(owner, initial)
    merged = owner[0].merge_scheduled_manual(
        session_id=t["session_id"],
        admission=admission(t, True, _name("command")),
        memory_domain_id="u_local",
        guard=owner[1].guard,
        deadline_monotonic=monotonic() + 30,
    )
    assert merged["id"] == first.queue_item_id
    assert merged["scheduled_trigger_source"] == original_source.value


def test_scheduled_session_search_includes_paused_deduplicates_and_pages(owner, monkeypatch):
    from dataclasses import replace
    from pulsara_agent.conversation_kernel._repository import scheduling

    repo, first, cut = owner
    keyword = "分类-" + uuid4().hex
    first_task = mutate(owner, task(owner), "pause")
    mutate(owner, task(owner), "pause")
    repo.rename_session(session_id=first.guard.session_id, memory_domain_id="u_local",
                        title=keyword + " 暂停_%", deadline_monotonic=monotonic() + 30)
    second = repo.acquire_host_writer(
        intent="NEW", session_id=_name("session"), workspace_id=_name("workspace"),
        writer_owner_id=_name("host"), lease_seconds=120, deadline_monotonic=monotonic() + 30,
    )
    _, binding, _ = _two_connection_resolution_cut()
    repo.update_session_model_call_binding(second.guard, binding=binding, deadline_monotonic=monotonic() + 30)
    task((repo, second, cut))
    repo.rename_session(session_id=second.guard.session_id, memory_domain_id="u_local",
                        title=keyword + " ACTIVE", deadline_monotonic=monotonic() + 30)
    empty = repo.acquire_host_writer(
        intent="NEW", session_id=_name("session"), workspace_id=_name("workspace"),
        writer_owner_id=_name("host"), lease_seconds=120, deadline_monotonic=monotonic() + 30,
    )
    repo.rename_session(session_id=empty.guard.session_id, memory_domain_id="u_local",
                        title=keyword + " 无任务", deadline_monotonic=monotonic() + 30)
    monkeypatch.setattr(scheduling, "STAGE2_LIMITS", replace(scheduling.STAGE2_LIMITS, history_page_default_entries=1))
    kwargs = dict(memory_domain_id="u_local", query=keyword, deadline_monotonic=monotonic() + 30)
    rows, cursor = repo.list_scheduled_task_sessions(**kwargs)
    assert len(rows) == 1 and cursor == rows[0]["id"]
    tail, cursor = repo.list_scheduled_task_sessions(**kwargs, cursor=cursor)
    assert len(tail) == 1 and cursor is None
    assert {row["id"] for row in rows + tail} == {first.guard.session_id, second.guard.session_id}
    rows, _ = repo.list_scheduled_task_sessions(**{**kwargs, "query": keyword + " _%"})
    assert [row["id"] for row in rows] == [first_task["session_id"]]
    rows, _ = repo.list_scheduled_task_sessions(**{**kwargs, "memory_domain_id": "another_domain"})
    assert rows == []


@pytest.mark.parametrize('final_admission', [False, True])
@pytest.mark.parametrize('manual_source', [False, True])
def test_run_once_reuses_running_turn_and_reopens_after_settlement(owner, final_admission, manual_source):
    from pulsara_agent.scheduling.service import ScheduledTaskService

    repo, lease, _ = owner
    t = make_due(owner, task(owner))
    initial, _ = enqueue(owner, admission(t, manual_source, _name("command") if manual_source else None))
    candidate = repo.prepare_prompt_head_consumption(
        session_id=t['session_id'], occurred_at=datetime.now(timezone.utc),
        actor_id='test', deadline_monotonic=monotonic() + 30,
    )
    repo.consume_prepared_prompt_head(
        lease.guard, candidate=candidate,
        provider_input_admission=_root_provider_input_admission(candidate.provider_input_candidate),
        deadline_monotonic=monotonic() + 30,
    )
    current = read(owner, t['id'])
    assert current['last_turn_status'] == 'RUNNING'
    request = admission(current, True, _name("command"))
    if final_admission:
        # Simulates preparation finishing after the first input was consumed.
        _, accepted = enqueue(owner, request)
        assert accepted.queue_item_id == initial.queue_item_id
    else:
        merged = repo.merge_scheduled_manual(
            session_id=t['session_id'], admission=request, memory_domain_id='u_local',
            guard=lease.guard, deadline_monotonic=monotonic() + 30,
        )
        assert merged['id'] == initial.queue_item_id
    confirmed = repo.confirm_scheduled_action(
        request=request.action_value(), memory_domain_id='u_local', deadline_monotonic=monotonic() + 30,
    )
    assert ScheduledTaskService._run_result(confirmed, request.action_value()) == {
        'queue_item_id': initial.queue_item_id, 'status': 'RUNNING', 'coalesced': True,
    }
    assert read(owner, t['id'])['pending_queue_item_id'] is None
    assert read(owner, t['id'])['revision'] == current['revision']
    repo.interrupt_turn(
        lease.guard, turn_id=current['last_turn_id'], reason='TEST_BOUNDARY',
        occurred_at=datetime.now(timezone.utc), actor_id='test', deadline_monotonic=monotonic() + 30,
    )
    # Retrying the old command still confirms the old input after completion.
    assert repo.confirm_scheduled_action(
        request=request.action_value(), memory_domain_id='u_local', deadline_monotonic=monotonic() + 30,
    )['id'] == initial.queue_item_id
    next_request = admission(read(owner, t['id']), True, _name("command"))
    assert repo.merge_scheduled_manual(
        session_id=t['session_id'], admission=next_request, memory_domain_id='u_local',
        guard=lease.guard, deadline_monotonic=monotonic() + 30,
    ) is None
    next_candidate, accepted = enqueue(owner, next_request)
    next_id = accepted.queue_item_id or next_candidate.queue_item_id
    assert next_id != initial.queue_item_id
    assert read(owner, t['id'])['pending_queue_item_id'] == next_id


def test_concurrent_manual_submissions_share_one_pending_input(owner):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    t = mutate(owner, task(owner), 'pause')
    gate = Barrier(2)

    def submit(_):
        gate.wait()
        candidate, accepted = enqueue(owner, admission(t, True, _name("command")))
        return accepted.queue_item_id or candidate.queue_item_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(submit, range(2)))
    assert ids[0] == ids[1] == read(owner, t['id'])['pending_queue_item_id']
