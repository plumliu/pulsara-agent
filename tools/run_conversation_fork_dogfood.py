"""Real-provider Fork acceptance using a disposable clean-v0 database.

Retains exact final-wire materialization and model replies; only configured
API key values are scrubbed. Never mutates the user's configured database.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from uuid import uuid4

from psycopg.rows import dict_row
from pulsara_agent.conversation_kernel.compaction.contracts import (
    ResolvedCompactionPolicy,
)
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, ModelsDevCatalogClient
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.primitives.context import thaw_json
from pulsara_agent.settings import LocalSettingsStore, LocalPostgresConfig
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from pulsara_agent.workspace_identity import HostWorkspaceInput
from run_content_revision_line_edit_dogfood import _scrub
from run_model_switch_handover_dogfood import (
    _ReadOnlySettingsStore,
    _binding,
    _create_database,
    _drop_database,
    _find_connection,
    _RecordingModelRuntime,
    _RecordingTransport,
)


class _ForkTransport(_RecordingTransport):
    def open_stream(self, *, call, context):
        execution = super().open_stream(call=call, context=context)
        self._records[-1]["wire_input"] = thaw_json(
            context.provider_wire_input_plan.materialization.context_bearing_projection
        )
        return execution


class _ForkRuntime(_RecordingModelRuntime):
    def resolve_target(self, binding, *, timeout_policy):
        target = self._delegate.resolve_target(binding, timeout_policy=timeout_policy)
        return replace(
            target, transport=_ForkTransport(target.transport, self._records, None)
        )


def _anchor(session):
    with session.repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        return str(
            connection.execute(
                "SELECT final_entry_id FROM pulsara_v3.turns WHERE session_id=%s ORDER BY accepted_at DESC LIMIT 1",
                (session.session_id,),
            ).fetchone()["final_entry_id"]
        )


async def run(model_id, report):
    saved = LocalSettingsStore().read()
    selected = _find_connection(saved, model_id)
    database, _, admin, dsn = _create_database(saved)
    try:
        settings = _ReadOnlySettingsStore(
            replace(saved, postgres=LocalPostgresConfig(dsn, admin))
        )
        catalog = ModelCatalogOwner(ModelsDevCatalogClient())
        await catalog.refresh()
        delegate = ModelRuntime.production(settings=settings, catalog=catalog)
        records = report["provider_calls"]
        runtime = _ForkRuntime(delegate, records, None)
        core = KernelHostCore.production(model_runtime=runtime)
        with TemporaryDirectory(prefix="pulsara-fork-dogfood-") as directory:
            workspace = HostWorkspaceInput(
                workspace_kind="project",
                workspace_root=Path(directory),
                trust_workspace_mcp_config=False,
            )
            try:
                parent = await core.open_session(
                    workspace,
                    system_prompt="Follow the user's context checks precisely. Do not call tools. Keep replies short.",
                )
                await parent.update_model_call_binding(_binding(runtime, selected))
                # Existing per-compaction policy, chosen to make this finite
                # acceptance fixture reclaim its intentionally irrelevant padding.
                parent._compaction.policy = ResolvedCompactionPolicy(
                    automatic_enabled=False,
                    minimum_reclaim_tokens=1,
                    maximum_recent_human_utf8_bytes=1024,
                )
                prompt = (
                    "The project token is FORK_ALPHA_812. Remember it. Reply with that token only. The following is irrelevant historical filler, never reproduce it:\n"
                    + (
                        "Disposable filler row: no new task, no new facts; discard this row during summarization.\n"
                        * 400
                    )
                )
                first = await parent.run_turn(
                    prompt, command_id=f"command:{uuid4().hex}"
                )
                report["source_replies"].append(first.final_text)
                anchor_a = await asyncio.to_thread(_anchor, parent)
                later = await parent.run_turn(
                    "A later branch note is FUTURE_B_913. Acknowledge in one short sentence.",
                    command_id=f"command:{uuid4().hex}",
                )
                report["source_replies"].append(later.final_text)

                async def branch(
                    label,
                    anchor,
                    required,
                    forbidden,
                    *,
                    expected_reply="FORK_ALPHA_812",
                ):
                    child_id = f"session:{uuid4().hex}"
                    creation = await core.fork_conversation(
                        source_session_id=parent.session_id,
                        anchor_entry_id=anchor,
                        child_session_id=child_id,
                        memory_domain_id="u_local",
                    )
                    if not creation.created:
                        raise RuntimeError(creation.public_code)
                    child = await core.resume_session(
                        child_id, workspace_input=workspace
                    )
                    with child.repository.connection_provider.connection(
                        lane=PostgresConnectionLane.INSPECTOR,
                        row_factory=dict_row,
                        deadline_monotonic=monotonic() + 30,
                    ) as connection:
                        for table in (
                            "turns",
                            "agent_events",
                            "session_commands",
                            "tool_execution_attempts",
                            "subagent_tasks",
                            "plan_workflows",
                        ):
                            from psycopg import sql

                            assert (
                                connection.execute(
                                    sql.SQL(
                                        "SELECT count(*) AS n FROM pulsara_v3.{} WHERE session_id=%s"
                                    ).format(sql.Identifier(table)),
                                    (child_id,),
                                ).fetchone()["n"]
                                == 0
                            )
                    begin = len(records)
                    question = (
                        "Which acknowledgment marker starting COMPACTED_ appears in the inherited conversation? Reply with that exact marker only; do not use tools."
                        if label == "compacted"
                        else "What project token is present in the inherited conversation? Answer with the exact token only; do not use tools."
                    )
                    outcome = await child.run_turn(
                        question, command_id=f"command:{uuid4().hex}"
                    )
                    actual = json.dumps(
                        [item["wire_input"] for item in records[begin:]],
                        ensure_ascii=False,
                    )
                    assert records[begin:] and required in actual
                    for marker in forbidden:
                        assert marker not in actual, (label, marker)
                    assert expected_reply in outcome.final_text, outcome
                    if label == "compacted":
                        with child.repository.connection_provider.connection(
                            lane=PostgresConnectionLane.INSPECTOR,
                            row_factory=dict_row,
                            deadline_monotonic=monotonic() + 30,
                        ) as connection:
                            snapshot_rows = connection.execute(
                                "SELECT c.inline_content FROM pulsara_v3.context_snapshots c WHERE c.session_id IN (%s,%s)",
                                (parent.session_id, child_id),
                            ).fetchall()
                        assert len(snapshot_rows) == 2
                        summaries = [
                            json.loads(bytes(row["inline_content"]))[
                                "earlier_context_summary"
                            ]
                            for row in snapshot_rows
                        ]
                        assert summaries[0] == summaries[1]
                    report["branches"].append(
                        {
                            "scenario": label,
                            "child_session_id": child_id,
                            "source_anchor": anchor,
                            "reply": outcome.final_text,
                            "provider_call_sequences": [
                                r["sequence"] for r in records[begin:]
                            ],
                            "forbidden_markers": forbidden,
                        }
                    )
                    await core.close_session(
                        child.host_session_id, close_conversation=False
                    )

                await branch(
                    "uncompacted", anchor_a, "FORK_ALPHA_812", ["FUTURE_B_913"]
                )
                compacted = await parent.compact_context(
                    command_id=f"command:{uuid4().hex}", force=True
                )
                report["compaction"] = {
                    "disposition": compacted.disposition.value,
                    "detail": str(compacted),
                }
                assert compacted.disposition.value == "COMPACTED", compacted
                current = await parent.run_turn(
                    "Now acknowledge COMPACTED_ANCHOR_714; retain the original project token.",
                    command_id=f"command:{uuid4().hex}",
                )
                report["source_replies"].append(current.final_text)
                anchor_c = await asyncio.to_thread(_anchor, parent)
                newest = await parent.run_turn(
                    "Future-only note FUTURE_D_615. Acknowledge briefly.",
                    command_id=f"command:{uuid4().hex}",
                )
                report["source_replies"].append(newest.final_text)
                await branch(
                    "compacted",
                    anchor_c,
                    "COMPACTED_ANCHOR_714",
                    [
                        "FUTURE_D_615",
                        "Disposable filler row: no new task, no new facts; discard this row during summarization.\n"
                        * 10,
                    ],
                    expected_reply="COMPACTED_ANCHOR_714",
                )
                await branch(
                    "historical_before_compaction",
                    anchor_a,
                    "FORK_ALPHA_812",
                    ["FUTURE_B_913", "COMPACTED_ANCHOR_714", "FUTURE_D_615"],
                )
            finally:
                await core.shutdown()
    finally:
        _drop_database(saved, database)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="openai/gpt-5.6-luna")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = {
        "schema_version": "conversation-fork-dogfood.v1",
        "model_id": args.model,
        "provider_calls": [],
        "source_replies": [],
        "branches": [],
    }
    try:
        asyncio.run(run(args.model, report))
        report["status"] = "passed"
    except BaseException as error:
        import traceback

        report.update(
            status="failed", error=str(error), traceback=traceback.format_exc()
        )
    report["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
    secrets = tuple(item.value for item in LocalSettingsStore().read().model_api_keys)
    encoded = json.dumps(_scrub(report, secrets), ensure_ascii=False, indent=2)
    assert not any(secret and secret in encoded for secret in secrets)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(encoded + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "status": report["status"],
                "calls": len(report["provider_calls"]),
                "branches": report["branches"],
                "error": report.get("error"),
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    return int(report["status"] != "passed")


if __name__ == "__main__":
    raise SystemExit(main())
