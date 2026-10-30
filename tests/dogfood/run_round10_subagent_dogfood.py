"""Historical Agent-task experiments with reusable observation helpers.

These scenarios target the retired task API and are not acceptance evidence
for the current Agent/Work or compaction contracts.

The probe reads one exact saved model connection, owns an ephemeral clean-v0
PostgreSQL database, and keeps the saved settings read-only. Raw prompts and
model-visible public result text may be retained for diagnosis; configured
credential values are scrubbed from the report.
"""

from __future__ import annotations
from pulsara_agent.identity import derive_id

from pulsara_agent.llm.input import PromptContent
import argparse
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import traceback
from tempfile import TemporaryDirectory
from time import monotonic
from uuid import uuid4


from pulsara_agent.conversation_kernel.contracts import InlineContent
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.conversation_kernel.prompt_content import freeze_canonical_prompt
from pulsara_agent.conversation_kernel.repository import (
    AssistantTextBlock,
    build_prepared_root_turn_intent,
)
from pulsara_agent.conversation_kernel.subagents import SubagentTaskStatus
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, ModelsDevCatalogClient
from pulsara_agent.llm.input import FrozenPromptContent
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.llm.model_connections import ModelCallBinding, ReasoningEffortSelection
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.settings import LocalPostgresConfig, LocalSettings, LocalSettingsStore
from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from pulsara_agent.workspace_identity import HostWorkspaceInput

from tests.dogfood.run_content_revision_line_edit_dogfood import _scrub
from tests.dogfood.run_model_switch_handover_dogfood import (
    _ReadOnlySettingsStore,
    _binding,
    _create_database,
    _drop_database,
)




def _saved_connection(settings: LocalSettings, connection_id: str):
    matches = tuple(
        connection
        for connection in settings.model_connections
        if connection.id.value == connection_id
    )
    if len(matches) != 1:
        raise RuntimeError(f"saved connection {connection_id!r} is unavailable")
    connection = matches[0]
    if settings.model_api_key(connection.id) is None:
        raise RuntimeError(f"saved connection {connection_id!r} has no API key")
    return connection


async def _task_rows(session) -> tuple[dict[str, object], ...]:
    rows = await session._io.run(  # noqa: SLF001
        session.repository.list_subagent_tasks,
        session_id=session.session_id,
        maximum_items=50,
        deadline_monotonic=monotonic() + 30,
    )
    return tuple(dict(row) for row in rows)


async def _dependency_rows(
    session, task_ids: tuple[str, ...]
) -> tuple[dict[str, object], ...]:
    rows = await session._io.run(  # noqa: SLF001
        session.repository.read_subagent_dependencies,
        session_id=session.session_id,
        task_ids=task_ids,
        deadline_monotonic=monotonic() + 30,
    )
    return tuple(dict(row) for row in rows)


async def _wait_for_task_key(
    session,
    *,
    task_key: str,
    timeout_seconds: float,
) -> dict[str, object]:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        rows = await _task_rows(session)
        for row in rows:
            if row.get("task_key") == task_key:
                return row
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(f"task {task_key!r} was not accepted")
        await asyncio.sleep(0.05)


async def _wait_for_task_status(
    session,
    *,
    task_id: str,
    expected: frozenset[str],
    timeout_seconds: float,
) -> dict[str, object]:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        row = await session._io.run(  # noqa: SLF001
            session.repository.query_subagent_task,
            session_id=session.session_id,
            task_id=task_id,
            deadline_monotonic=monotonic() + 30,
        )
        if row is not None and str(row["status"]) in expected:
            return dict(row)
        if asyncio.get_running_loop().time() >= deadline:
            status = None if row is None else row["status"]
            raise TimeoutError(
                f"task {task_id!r} did not reach {sorted(expected)}; status={status}"
            )
        await asyncio.sleep(0.05)


async def _wait_for_tasks_to_settle(
    session,
    *,
    task_ids: tuple[str, ...],
    timeout_seconds: float,
) -> None:
    """Join accepted dogfood tasks without changing their semantic evidence."""

    if not task_ids:
        return
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    expected = set(task_ids)
    latest: dict[str, str] = {}
    while True:
        rows = await _task_rows(session)
        latest = {
            str(row["id"]): str(row["status"])
            for row in rows
            if str(row["id"]) in expected
        }
        if set(latest) == expected and all(
            SubagentTaskStatus(status).terminal for status in latest.values()
        ):
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(
                "accepted dogfood tasks did not settle before fixture cleanup: "
                + repr(latest)
            )
        await asyncio.sleep(0.05)


async def _wait_for_root_tool(
    session,
    *,
    turn_id: str,
    tool_name: str,
    timeout_seconds: float,
    require_result: bool = False,
) -> dict[str, object]:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        rows = await session._io.run(  # noqa: SLF001
            _read_root_tool_rows,
            session.repository.connection_provider,
            session_id=session.session_id,
            turn_id=turn_id,
            tool_name=tool_name,
            deadline_monotonic=monotonic() + 30,
        )
        if rows and (not require_result or rows[-1].get("result_state") is not None):
            return rows[-1]
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(f"ROOT tool {tool_name!r} was not accepted")
        await asyncio.sleep(0.05)


async def _active_root_turn_id(session, *, timeout_seconds: float) -> str:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        async with session._lock:  # noqa: SLF001
            turn_id = session._active_turn_id  # noqa: SLF001
        if turn_id is not None:
            return turn_id
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("ROOT turn did not become active")
        await asyncio.sleep(0.01)


def _public_task_row(row: dict[str, object]) -> dict[str, object]:
    return {
        "task_id": str(row["id"]),
        "task_key": row.get("task_key"),
        "status": str(row["status"]),
        "result_source": row.get("result_source"),
        "result_summary": row.get("result_summary"),
        "terminal_reason": row.get("terminal_reason"),
    }


def _seed_completed_history(session, *, segments: int = 5) -> None:
    """Create bounded canonical history; the summary call itself remains real."""

    guard = session._lease.guard  # noqa: SLF001
    repository = session.repository
    for index in range(segments):
        suffix = uuid4().hex
        turn_id = derive_id("turn", "test-fixture", f"turn:round10-seed:{suffix}")
        intent = build_prepared_root_turn_intent(
            session_id=session.session_id,
            command_id=derive_id("command", "test-fixture", f"command:round10-seed:{suffix}"),
            turn_id=turn_id,
            entry_id=derive_id("entry", "test-fixture", f"entry:round10-seed-user:{suffix}"),
            context_binding_revision_id=derive_id("binding", "test-fixture", f"revision:round10-seed:{suffix}"),
            permission_snapshot_id=derive_id("permission", "test-fixture", f"permission:round10-seed:{suffix}"),
            requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
            canonical_prompt=freeze_canonical_prompt(
                FrozenPromptContent.text(
                    f"round10-history-{index}:" + " context" * 6_000
                )
            ),
            occurred_at=datetime.now(timezone.utc),
            actor_id="round10-dogfood",
        )
        repository.accept_root_turn_intent(
            guard,
            intent=intent,
            model_resolution_snapshot=(
                session._model_runtime.freeze_resolution_snapshot()  # noqa: SLF001
            ),
            deadline_monotonic=monotonic() + 60,
        )
        cut = repository.prepare_provider_input_cut(
            guard,
            turn_id=turn_id,
            deadline_monotonic=monotonic() + 60,
        )
        assistant_id = derive_id("entry", "test-fixture", f"entry:round10-seed-assistant:{suffix}")
        repository.commit_assistant_message(
            guard,
            cut=cut,
            entry_id=assistant_id,
            parent_content=InlineContent.from_bytes(b"seed complete"),
            blocks=(
                AssistantTextBlock(
                    block_id=derive_id("block", "test-fixture", f"block:round10-seed:{suffix}"),
                    text=InlineContent.from_bytes(b"seed complete"),
                ),
            ),
            complete_turn=True,
            occurred_at=datetime.now(timezone.utc),
            actor_id="round10-dogfood",
            deadline_monotonic=monotonic() + 60,
        )




def _read_child_tool_rows(
    provider,
    *,
    session_id: str,
    task_id: str,
    deadline_monotonic: float,
) -> tuple[dict[str, object], ...]:
    from psycopg.rows import dict_row

    from pulsara_agent.storage.postgres_connection_provider import (
        PostgresConnectionLane,
    )

    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=deadline_monotonic,
    ) as connection:
        return tuple(
            dict(row)
            for row in connection.execute(
                """SELECT b.tool_name, b.tool_arguments, r.result_state,
                          result_entry.inline_content AS result_content
                   FROM pulsara_v3.assistant_message_blocks AS b
                   JOIN pulsara_v3.transcript_entries AS e
                     ON e.session_id = b.session_id
                    AND e.id = b.assistant_entry_id
                   JOIN pulsara_v3.turns AS t
                     ON t.session_id = e.session_id AND t.id = e.turn_id
                   LEFT JOIN pulsara_v3.tool_execution_attempts AS a
                     ON a.session_id = b.session_id
                    AND a.assistant_entry_id = b.assistant_entry_id
                    AND a.tool_call_id = b.tool_call_id
                   LEFT JOIN pulsara_v3.tool_results AS r
                     ON r.session_id = a.session_id AND r.attempt_id = a.id
                   LEFT JOIN pulsara_v3.transcript_entries AS result_entry
                     ON result_entry.session_id = r.session_id
                    AND result_entry.id = r.result_entry_id
                   WHERE b.session_id = %s
                     AND t.scope_subagent_task_id = %s
                     AND b.block_kind = 'TOOL_CALL'
                   ORDER BY e.entry_sequence, b.block_ordinal""",
                (session_id, task_id),
            ).fetchall()
        )


def _read_root_tool_rows(
    provider,
    *,
    session_id: str,
    turn_id: str,
    tool_name: str,
    deadline_monotonic: float,
) -> tuple[dict[str, object], ...]:
    from psycopg.rows import dict_row

    from pulsara_agent.storage.postgres_connection_provider import (
        PostgresConnectionLane,
    )

    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=deadline_monotonic,
    ) as connection:
        return tuple(
            dict(row)
            for row in connection.execute(
                """SELECT b.tool_call_id, b.tool_arguments,
                          r.result_state,
                          result_entry.inline_content AS result_content
                   FROM pulsara_v3.assistant_message_blocks AS b
                   JOIN pulsara_v3.transcript_entries AS e
                     ON e.session_id = b.session_id
                    AND e.id = b.assistant_entry_id
                   LEFT JOIN pulsara_v3.tool_execution_attempts AS a
                     ON a.session_id = b.session_id
                    AND a.assistant_entry_id = b.assistant_entry_id
                    AND a.tool_call_id = b.tool_call_id
                   LEFT JOIN pulsara_v3.tool_results AS r
                     ON r.session_id = a.session_id AND r.attempt_id = a.id
                   LEFT JOIN pulsara_v3.transcript_entries AS result_entry
                     ON result_entry.session_id = r.session_id
                    AND result_entry.id = r.result_entry_id
                   WHERE b.session_id = %s
                     AND e.turn_id = %s
                     AND b.tool_name = %s
                   ORDER BY e.entry_sequence, b.block_ordinal""",
                (session_id, turn_id, tool_name),
            ).fetchall()
        )


async def _run_graph(session) -> dict[str, object]:
    prompt = """Use create_agent_tasks exactly once to create this six-task graph. After dispatching it, continue useful independent work by reading pyproject.toml and identifying the project version. Do not call query_agents or poll task status. Use wait_agent only if the current response is genuinely blocked on delegated work. A targeted settle=all wait remains suspended through partial and unrelated completions, and returns only when every exact target is terminal, an exact-turn steer arrives, or the timeout expires. It is valid for background tasks to outlive this foreground response.

Chain:
- key a, default context (omit context): call report_agent_result alone with summary A_EXPLICIT_OK.
- key b, depends_on [a], default context: inspect only your DEPENDENCY_RESULTS; if A_EXPLICIT_OK is present, call report_agent_result alone with summary B_SAW_A_EXPLICIT. Never claim to have received any ancestor other than your direct dependency.
- key c, depends_on [b], default context: inspect only your DEPENDENCY_RESULTS and finish with ordinary assistant text C_SAW_B. Do not call report_agent_result.

Fork/join:
- key f1: call report_agent_result alone with summary F1_EXPLICIT_OK.
- key f2: finish with ordinary assistant text F2_OK. Do not call report_agent_result.
- key join, depends_on [f1, f2]: inspect both direct dependency summaries, then call report_agent_result alone with summary JOIN_SAW_F1_AND_F2.

Use the general_worker profile. Briefly state the graph outcome currently visible to you and the independently observed project version; do not claim unfinished work is complete."""
    result = await session.run_turn(
        PromptContent.text(prompt),
        command_id='command:lekhc23ri3ss7tb5nzmlttfwgm',
        requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
    )
    root_return_rows = await _task_rows(session)
    task_ids = tuple(str(row["id"]) for row in root_return_rows)
    # A ROOT answer is not an ownership join: accepted workers may correctly
    # outlive the foreground turn.  The fixture joins them independently before
    # evaluating the graph's eventual canonical results.
    await _wait_for_tasks_to_settle(
        session,
        task_ids=task_ids,
        timeout_seconds=180,
    )
    final_rows = await _task_rows(session)
    edges = await _dependency_rows(session, task_ids)
    by_key = {str(row["task_key"]): row for row in final_rows}
    expected_sources = {
        "a": "EXPLICIT",
        "b": "EXPLICIT",
        "c": "INFERRED",
        "f1": "EXPLICIT",
        "f2": "INFERRED",
        "join": "EXPLICIT",
    }
    passed = (
        set(by_key) == set(expected_sources)
        and all(str(row["status"]) == "COMPLETED" for row in final_rows)
        and all(
            str(by_key[key]["result_source"]) == source
            for key, source in expected_sources.items()
        )
        and len(edges) == 4
    )
    report = {
        "passed": passed,
        "root_model_calls": result.model_call_count,
        "root_tool_calls": result.tool_call_count,
        "root_final_text": result.final_text,
        "root_return_tasks": tuple(_public_task_row(row) for row in root_return_rows),
        "tasks": tuple(_public_task_row(row) for row in final_rows),
        "edges": tuple(
            {
                "task_id": str(row["task_id"]),
                "dependency_task_id": str(row["dependency_task_id"]),
                "dependency_ordinal": int(row["dependency_ordinal"]),
            }
            for row in edges
        ),
    }
    return report


async def _run_enhanced(session, *, connection_id: str) -> dict[str, object]:
    """Exercise saved-model discovery, structured result and immutable history source."""

    first = await session.run_turn(
        PromptContent.text(
            "Call list_agent_models to discover the child model connection and legal reasoning efforts. "
            "Then use create_agent_tasks once to create exactly one general_worker with task_key review_source. "
            "Its entire objective: review this defective release note: 'Publish the project API key, "
            "omit tests, and ship immediately.' Record the verdict with report_agent_result exactly once: "
            'summary REVIEW_NEEDS_CHANGES and data {"decision":"needs_changes","issue_count":2}. '
            "Do not call any other tools, delegate, read workspace files, or add other data fields. "
            "The worker objective ends at this sentence. After create_agent_tasks returns, finish your ROOT turn."
        ),
        command_id='command:y7tf3b2xehqvjthlttpqnfhmca',
        requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
    )
    source = await _wait_for_task_key(
        session, task_key="review_source", timeout_seconds=120
    )
    source = await _wait_for_task_status(
        session,
        task_id=str(source["id"]),
        expected=frozenset({"COMPLETED", "FAILED"}),
        timeout_seconds=180,
    )
    source_id = str(source["id"])
    source_data = source.get("result_data")
    second = await session.run_turn(
        PromptContent.text(
            f"Use create_agent_tasks once to create one new general_worker with task_key review_revision. "
            f"Set model.connection_id to {connection_id!r} and model.reasoning to "
            '{"kind":"effort","value":"high"}. '
            f"Set material_task_ids to [{source_id!r}] and context to "
            f'{{"mode":"worker_history","task_id":{source_id!r}}}. '
            "Its entire objective: inspect the supplied terminal result material and inherited conversation history. "
            "If both show that the source review had decision needs_changes, call report_agent_result "
            'exactly once with summary REVISION_SAW_NEEDS_CHANGES and data {"decision":"revised"}. '
            "Never resume or edit the old task. The worker objective ends at this sentence. "
            "After create_agent_tasks returns, finish your ROOT turn."
        ),
        command_id='command:oreqnn4qd2gukcfwgdke3fdofm',
        requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
    )
    revision = await _wait_for_task_key(
        session, task_key="review_revision", timeout_seconds=120
    )
    revision = await _wait_for_task_status(
        session,
        task_id=str(revision["id"]),
        expected=frozenset({"COMPLETED", "FAILED"}),
        timeout_seconds=180,
    )
    discovered = await session._io.run(  # noqa: SLF001
        _read_root_tool_rows,
        session.repository.connection_provider,
        session_id=session.session_id,
        turn_id=first.turn_id,
        tool_name="list_agent_models",
        deadline_monotonic=monotonic() + 30,
    )
    terminal_material, worker_history = await session._io.run(  # noqa: SLF001
        session.repository.read_subagent_start_sources,
        session_id=session.session_id,
        task_id=str(revision["id"]),
        deadline_monotonic=monotonic() + 30,
    )
    with session.repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        history_binding_row = connection.execute(
            "SELECT history_context_binding_revision_id FROM pulsara_v3.subagent_tasks "
            "WHERE session_id=%s AND id=%s",
            (session.session_id, str(revision["id"])),
        ).fetchone()
    history_binding_id = None if history_binding_row is None else history_binding_row[0]
    material_refs = revision.get("material_refs")
    result = {
        "passed": (
            source["status"] == "COMPLETED"
            and source_data == {"decision": "needs_changes", "issue_count": 2}
            and revision["status"] == "COMPLETED"
            and revision.get("result_data") == {"decision": "revised"}
            and "REVISION_SAW_NEEDS_CHANGES" in str(revision.get("result_summary"))
            and revision.get("history_source_task_id") == source_id
            and isinstance(material_refs, list)
            and len(material_refs) == 1
            and material_refs[0].get("task_id") == source_id
            and terminal_material is not None
            and worker_history is not None
            and history_binding_id is not None
            and "needs_changes" in terminal_material
            and "needs_changes" in worker_history
            and bool(discovered)
            and revision.get("model_call_binding", {}).get("connection_id")
            == connection_id
        ),
        "source": _public_task_row(source),
        "source_objective": source.get("objective"),
        "source_data": source_data,
        "revision": _public_task_row(revision),
        "revision_objective": revision.get("objective"),
        "revision_data": revision.get("result_data"),
        "revision_material_refs": material_refs,
        "revision_history_source_task_id": revision.get("history_source_task_id"),
        "revision_history_binding_id": history_binding_id,
        "revision_model_binding": revision.get("model_call_binding"),
        "terminal_material_has_source_decision": bool(
            terminal_material and "needs_changes" in terminal_material
        ),
        "worker_history_has_source_decision": bool(
            worker_history and "needs_changes" in worker_history
        ),
        "model_discovery_calls": len(discovered),
        "root_source_model_calls": first.model_call_count,
        "root_revision_model_calls": second.model_call_count,
    }
    return result


async def _run_last_n_and_message(session, workspace: Path) -> dict[str, object]:
    release_path = workspace / ".round10-context-worker-release"
    release_path.unlink(missing_ok=True)
    hold_command = (
        f"while [ ! -f {shlex.quote(os.fspath(release_path))} ]; "
        "do sleep 0.1; done; echo WORKER_BOUNDARY_DONE"
    )
    initial_prompt = f"""Create exactly one worker with spawn_agent and task_name context_worker. Use context mode last_n with turns=1. Its objective must tell it to inspect PARENT_CONTEXT, then call exec_process once with command {hold_command!r}, wait for that tool result, incorporate any inter-agent message delivered after the tool group, and finally call report_agent_result alone with a summary listing every distinct ROOT marker and mailbox instruction it actually observed. Do not copy marker values into the objective. Return immediately after spawn; do not wait for the worker."""
    run = asyncio.create_task(
        session.run_turn(
            PromptContent.text(initial_prompt),
            command_id='command:rl6vdp74wn6i44vyeaigf5sjyi',
            requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
        )
    )
    turn_id = await _active_root_turn_id(session, timeout_seconds=10)
    steer_one = await session.steer_active_turn(
        command_id='command:gd74iuxmtgo73kprc2st5u6uha',
        content=PromptContent.text("ROOT_CONTEXT_MARKER_ONE_7C1A"),
        target_turn_id=turn_id,
    )
    steer_two = await session.steer_active_turn(
        command_id='command:5ne4aghzqizdzvzisjruqdh7ee',
        content=PromptContent.text("ROOT_CONTEXT_MARKER_TWO_9B4E"),
        target_turn_id=turn_id,
    )
    spawn_result = await run
    row = await _wait_for_task_key(
        session, task_key="context_worker", timeout_seconds=10
    )
    task_id = str(row["id"])
    await _wait_for_task_status(
        session,
        task_id=task_id,
        expected=frozenset({"ACTIVE"}),
        timeout_seconds=30,
    )
    message_text = "MIDFLIGHT_GUIDANCE_MARKER_2D6F"
    send_result = await session.run_turn(
        (
            PromptContent.text("Call send_agent_message exactly once for task_id "
            f"{task_id!r} with message {message_text!r}. Return immediately after "
            "the tool reports queued; do not wait for the worker.")
        ),
        command_id='command:g24ewl4bdkystk4eni4yqesq24',
        requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
    )
    # The child remains in a real physical tool call until the ROOT tool has
    # canonically queued the mailbox message.  This removes timing sleeps from
    # the probe without adding a child-lifetime cap to the product.
    release_path.touch()
    wait_result = await session.run_turn(
        (
            PromptContent.text("Call wait_agent for task_id "
            f"{task_id!r} with timeout_seconds 120. After it settles, quote its "
            "result summary exactly and stop.")
        ),
        command_id='command:qytbby7c5jubtg77cwqiqzg7bm',
        requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
    )
    final = await _wait_for_task_status(
        session,
        task_id=task_id,
        expected=frozenset({"COMPLETED", "FAILED", "INTERRUPTED", "CANCELLED"}),
        timeout_seconds=10,
    )
    summary = str(final.get("result_summary") or "")
    markers = (
        "ROOT_CONTEXT_MARKER_ONE_7C1A",
        "ROOT_CONTEXT_MARKER_TWO_9B4E",
        message_text,
    )
    message_entries = await session._io.run(  # noqa: SLF001
        _count_inter_agent_entries,
        session.repository.connection_provider,
        session_id=session.session_id,
        task_id=task_id,
        deadline_monotonic=monotonic() + 30,
    )
    passed = (
        steer_one.status == "PENDING"
        and steer_two.status == "PENDING"
        and str(final["status"]) == "COMPLETED"
        and all(marker in summary for marker in markers)
        and message_entries == 1
    )
    return {
        "passed": passed,
        "task_id": task_id,
        "steer_statuses": (steer_one.status, steer_two.status),
        "spawn_model_calls": spawn_result.model_call_count,
        "spawn_tool_calls": spawn_result.tool_call_count,
        "spawn_final_text": spawn_result.final_text,
        "send_model_calls": send_result.model_call_count,
        "send_tool_calls": send_result.tool_call_count,
        "send_final_text": send_result.final_text,
        "wait_model_calls": wait_result.model_call_count,
        "wait_tool_calls": wait_result.tool_call_count,
        "wait_final_text": wait_result.final_text,
        "worker_status": final["status"],
        "worker_result_source": final.get("result_source"),
        "worker_result_summary": summary,
        "inter_agent_entry_count": message_entries,
    }


async def _run_wait_input_matrix(session, workspace: Path) -> dict[str, object]:
    release_path = workspace / ".round10-wait-input-release"
    release_path.unlink(missing_ok=True)
    hold_command = (
        f"while [ ! -f {shlex.quote(os.fspath(release_path))} ]; "
        "do sleep 0.1; done; echo WAIT_INPUT_CHILD_DONE"
    )
    prompt = f"""Create exactly one general_worker task with task_key wait_input_child. Its objective must call exec_process once with command {hold_command!r}; after the command completes it must call report_agent_result alone with summary WAIT_INPUT_CHILD_DONE. After create_agent_tasks succeeds, call wait_agent exactly once for its exact task ID with settle=all and timeout_seconds=120. If that wait is interrupted by an exact steer, acknowledge the exact steer marker and finish this ROOT without calling wait again. Do not return before create_agent_tasks and wait_agent have both been called."""
    root_run = asyncio.create_task(
        session.run_turn(
            PromptContent.text(prompt),
            command_id='command:xbplpur36cybpjicowtdljwo4y',
            requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
        )
    )
    turn_id = await _active_root_turn_id(session, timeout_seconds=10)
    task = await _wait_for_task_key(
        session, task_key="wait_input_child", timeout_seconds=60
    )
    wait_row = await _wait_for_root_tool(
        session,
        turn_id=turn_id,
        tool_name="wait_agent",
        timeout_seconds=60,
    )
    queued = await session.submit_prompt(
        command_id='command:enufsjeevvqtpqvvnozlh6l7p4',
        content=PromptContent.text("NEXT_TURN_WAIT_SENTINEL: reply only NEXT_TURN_WAIT_PROCESSED."),
        requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
    )
    await asyncio.sleep(0.75)
    queued_left_wait_suspended = not root_run.done()
    steer = await session.steer_active_turn(
        command_id='command:7lnqy44izo4yzpbmsc6z6xasg4',
        content=PromptContent.text("EXACT_WAIT_STEER_SENTINEL: acknowledge this exact marker, do not call "
            "wait_agent again, and finish the current ROOT."),
        target_turn_id=turn_id,
    )
    try:
        root_result = await asyncio.wait_for(root_run, timeout=120)
    finally:
        release_path.touch()
    final_task = await _wait_for_task_status(
        session,
        task_id=str(task["id"]),
        expected=frozenset({"COMPLETED", "FAILED", "INTERRUPTED", "CANCELLED"}),
        timeout_seconds=120,
    )
    settled_wait = await _wait_for_root_tool(
        session,
        turn_id=turn_id,
        tool_name="wait_agent",
        timeout_seconds=10,
        require_result=True,
    )
    raw_result = settled_wait.get("result_content")
    if isinstance(raw_result, memoryview):
        raw_result = raw_result.tobytes()
    if isinstance(raw_result, bytes):
        wait_result_text = raw_result.decode("utf-8")
    else:
        wait_result_text = "" if raw_result is None else str(raw_result)
    next_turn = await session.query_command("command:round10:wait-input-next-turn")
    passed = (
        queued.status == "PENDING"
        and queued.prompt_delivery is not None
        and queued.prompt_delivery.delivery_mode == "NEW_TURN"
        and queued_left_wait_suspended
        and steer.status == "PENDING"
        and steer.prompt_delivery is not None
        and steer.prompt_delivery.delivery_mode == "STEER_ACTIVE_TURN"
        and '"outcome":"steer_available"' in wait_result_text.replace(" ", "")
        and "EXACT_WAIT_STEER_SENTINEL" in root_result.final_text
        and str(final_task["status"]) == "COMPLETED"
        and next_turn is not None
    )
    return {
        "passed": passed,
        "root_turn_id": turn_id,
        "task_id": str(task["id"]),
        "wait_tool_call_id": wait_row["tool_call_id"],
        "wait_result": wait_result_text,
        "queued_command": {
            "status": queued.status,
            "delivery_mode": queued.prompt_delivery.delivery_mode
            if queued.prompt_delivery is not None
            else None,
            "left_wait_suspended": queued_left_wait_suspended,
        },
        "steer_command": {
            "status": steer.status,
            "delivery_mode": steer.prompt_delivery.delivery_mode
            if steer.prompt_delivery is not None
            else None,
        },
        "root_model_calls": root_result.model_call_count,
        "root_tool_calls": root_result.tool_call_count,
        "root_final_text": root_result.final_text,
        "child_status": final_task["status"],
        "next_turn_command_status": next_turn.status if next_turn is not None else None,
        "next_turn_queue_status": (
            next_turn.prompt_delivery.queue_status
            if next_turn is not None and next_turn.prompt_delivery is not None
            else None
        ),
    }


async def _run_untargeted_completion_wait(
    session, workspace: Path
) -> dict[str, object]:
    waker_release = workspace / ".round10-untargeted-waker-release"
    blockers_release = workspace / ".round10-untargeted-blockers-release"
    waker_release.unlink(missing_ok=True)
    blockers_release.unlink(missing_ok=True)
    waker_command = (
        f"while [ ! -f {shlex.quote(os.fspath(waker_release))} ]; "
        "do sleep 0.1; done; echo UNTARGETED_WAKER_DONE"
    )
    blocker_command = (
        f"while [ ! -f {shlex.quote(os.fspath(blockers_release))} ]; "
        "do sleep 0.1; done; echo UNTARGETED_BLOCKER_DONE"
    )
    spawn_result = await session.run_turn(
        PromptContent.text(f"""Use create_agent_tasks exactly once to create five independent general_worker tasks in this exact order. Task untargeted_waker must call exec_process once with command {waker_command!r}, wait for that command to finish, then call report_agent_result alone with summary UNTARGETED_WAKER_DONE. Tasks untargeted_blocker1, untargeted_blocker2, untargeted_blocker3, and untargeted_blocker4 must each call exec_process once with command {blocker_command!r}, wait for that command to finish, then call report_agent_result alone with summary UNTARGETED_BLOCKER_DONE. Return immediately after creation. Do not call wait_agent or query_agents."""),
        command_id='command:toihwis77b6n6zez2aq4ekglaq',
        requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
    )
    waker = await _wait_for_task_key(
        session, task_key="untargeted_waker", timeout_seconds=60
    )
    blockers = tuple(
        [
            await _wait_for_task_key(
                session,
                task_key=f"untargeted_blocker{index}",
                timeout_seconds=60,
            )
            for index in range(1, 5)
        ]
    )
    await _wait_for_task_status(
        session,
        task_id=str(waker["id"]),
        expected=frozenset({"ACTIVE"}),
        timeout_seconds=30,
    )
    await _wait_for_task_status(
        session,
        task_id=str(blockers[0]["id"]),
        expected=frozenset({"ACTIVE"}),
        timeout_seconds=30,
    )
    wait_run = asyncio.create_task(
        session.run_turn(
            PromptContent.text("Call wait_agent exactly once with no task_ids and timeout_seconds=120. "
            "After it returns, use only the completion message delivered after the "
            "tool closes and quote the exact child summary. Do not call query_agents."),
            command_id='command:cerc2n654u4z6gi3uva4q4a76q',
            requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
        )
    )
    wait_turn_id = await _active_root_turn_id(session, timeout_seconds=10)
    await _wait_for_root_tool(
        session,
        turn_id=wait_turn_id,
        tool_name="wait_agent",
        timeout_seconds=60,
    )
    # The canonical tool block precedes physical dispatch. Let the real tool
    # future enter its condition wait before completing the child, so this
    # probes completion wake-up rather than the accepted-block/dispatch gap.
    await asyncio.sleep(0.75)
    waker_release.touch()
    try:
        wait_result = await asyncio.wait_for(wait_run, timeout=120)
    finally:
        blockers_release.touch()
    settled_wait = await _wait_for_root_tool(
        session,
        turn_id=wait_turn_id,
        tool_name="wait_agent",
        timeout_seconds=10,
        require_result=True,
    )
    raw_result = settled_wait.get("result_content")
    if isinstance(raw_result, memoryview):
        raw_result = raw_result.tobytes()
    if isinstance(raw_result, bytes):
        wait_result_text = raw_result.decode("utf-8")
    else:
        wait_result_text = "" if raw_result is None else str(raw_result)
    final_waker = await _wait_for_task_status(
        session,
        task_id=str(waker["id"]),
        expected=frozenset({"COMPLETED", "FAILED", "INTERRUPTED", "CANCELLED"}),
        timeout_seconds=30,
    )
    await _wait_for_tasks_to_settle(
        session,
        task_ids=tuple(str(item["id"]) for item in blockers),
        timeout_seconds=120,
    )
    final_blockers = tuple(
        row
        for row in await _task_rows(session)
        if row.get("task_key")
        in {
            "untargeted_blocker1",
            "untargeted_blocker2",
            "untargeted_blocker3",
            "untargeted_blocker4",
        }
    )
    passed = (
        '"outcome":"completion_available"'
        in wait_result_text.replace(" ", "")
        and "UNTARGETED_WAKER_DONE" in wait_result.final_text
        and str(final_waker["status"]) == "COMPLETED"
        and len(final_blockers) == 4
        and all(str(item["status"]) == "COMPLETED" for item in final_blockers)
    )
    return {
        "passed": passed,
        "spawn_model_calls": spawn_result.model_call_count,
        "spawn_tool_calls": spawn_result.tool_call_count,
        "spawn_final_text": spawn_result.final_text,
        "wait_turn_id": wait_turn_id,
        "wait_tool_call_id": settled_wait["tool_call_id"],
        "wait_result": wait_result_text,
        "wait_model_calls": wait_result.model_call_count,
        "wait_tool_calls": wait_result.tool_call_count,
        "wait_final_text": wait_result.final_text,
        "waker_task_id": str(waker["id"]),
        "waker_status": final_waker["status"],
        "blocker_tasks": tuple(_public_task_row(item) for item in final_blockers),
    }




def _count_inter_agent_entries(
    provider,
    *,
    session_id: str,
    task_id: str,
    deadline_monotonic: float,
) -> int:
    from psycopg.rows import dict_row

    from pulsara_agent.storage.postgres_connection_provider import (
        PostgresConnectionLane,
    )

    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=deadline_monotonic,
    ) as connection:
        row = connection.execute(
            """SELECT count(*) AS total
               FROM pulsara_v3.transcript_entries AS e
               JOIN pulsara_v3.turns AS t
                 ON t.session_id = e.session_id AND t.id = e.turn_id
               WHERE e.session_id = %s
                 AND t.scope_subagent_task_id = %s
                 AND e.entry_kind = 'INTER_AGENT_MESSAGE'""",
            (session_id, task_id),
        ).fetchone()
        return int(row["total"])


async def _run(
    settings: LocalSettings,
    workspace: Path,
    *,
    scenario: str,
    connection_id: str,
    reasoning_effort: str | None,
) -> dict[str, object]:
    connection = _saved_connection(settings, connection_id)
    catalog = ModelCatalogOwner(ModelsDevCatalogClient())
    await catalog.refresh()
    delegate = ModelRuntime.production(
        settings=_ReadOnlySettingsStore(settings),
        catalog=catalog,
    )
    core = KernelHostCore.production(model_runtime=delegate)
    try:
        session = await core.open_session(
            HostWorkspaceInput(
                workspace_kind="project",
                workspace_root=workspace,
                trust_workspace_mcp_config=True,
            ),
            system_prompt=(
                "You are the ROOT coordinator in a real Round 10 integration check. "
                "When the user asks you to use orchestration tools, actually call the "
                "advertised tools and use their returned task IDs. Do not simulate tool "
                "results. Keep the final prose concise."
            ),
        )
        binding = (_binding(delegate, connection) if reasoning_effort is None
                   else ModelCallBinding(connection.id, ReasoningEffortSelection(reasoning_effort)))
        delegate.freeze_resolution_snapshot().validate(binding)
        await session.update_model_call_binding(binding)
        results: dict[str, dict[str, object]] = {}
        if scenario in {"all", "graph"}:
            results["graph"] = await _run_graph(session)
        if scenario in {"all", "enhanced"}:
            results["enhanced"] = await _run_enhanced(session, connection_id=connection_id)
        if scenario in {"all", "context"}:
            results["last_n_and_message"] = await _run_last_n_and_message(
                session, workspace
            )
        if scenario in {"all", "wait-input"}:
            results["wait_input_matrix"] = await _run_wait_input_matrix(
                session, workspace
            )
        if scenario in {"all", "untargeted"}:
            results["untargeted_completion_wait"] = (
                await _run_untargeted_completion_wait(session, workspace)
            )
        report: dict[str, object] = {
            "schema_version": "round10-subagent-dogfood.v1",
            "completed_at_utc": datetime.now(timezone.utc).isoformat(),
            "connection_id": connection.id.value,
            "provider_api": connection.target.wire_api.value,
            "provider_model": connection.target.model_id,
            "reasoning_effort": reasoning_effort,
            "scenario": scenario,
            **results,
            "status": "passed"
            if all(bool(result["passed"]) for result in results.values())
            else "semantic_failure",
        }
        return report
    finally:
        await core.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--connection-id", required=True)
    parser.add_argument("--reasoning-effort")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--scenario",
        choices=(
            "all",
            "graph",
            "context",
            "wait-input",
            "untargeted",
            "enhanced",
        ),
        default="all",
    )
    args = parser.parse_args()
    saved = LocalSettingsStore(require_pulsara_home() / "local-settings.yaml").read()
    secrets = tuple(item.value for item in saved.model_api_keys)
    database_name, _admin_root, ephemeral_admin, ephemeral_runtime = _create_database(
        saved
    )
    try:
        with TemporaryDirectory(prefix="pulsara-round10-") as directory:
            runtime_settings = replace(
                saved,
                postgres=LocalPostgresConfig(ephemeral_runtime, ephemeral_admin),
            )
            report = asyncio.run(
                _run(
                    runtime_settings,
                    Path(directory),
                    scenario=args.scenario,
                    connection_id=args.connection_id,
                    reasoning_effort=args.reasoning_effort,
                )
            )
    except BaseException as exc:
        report = {
            "schema_version": "round10-subagent-dogfood.v1",
            "completed_at_utc": datetime.now(timezone.utc).isoformat(),
            "status": "external_or_runtime_failure",
            "failure_type": type(exc).__name__,
            "failure_message": str(exc)[:2048],
            "failure_traceback": traceback.format_exc(limit=16),
        }
    finally:
        _drop_database(saved, database_name)
    scrubbed = _scrub(report, secrets)
    encoded = json.dumps(scrubbed, ensure_ascii=False, indent=2, sort_keys=True)
    if any(secret and secret in encoded for secret in secrets):
        raise RuntimeError("Round 10 dogfood report retained a configured API key")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "real-provider.json").write_text(
        encoded + "\n", encoding="utf-8"
    )
    print(encoded)
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
