"""Exercise both saved DeepSeek wires across canonical ROOT reprojection.

The saved settings are read-only.  All canonical writes use a verified,
disposable loopback PostgreSQL database and a temporary Pulsara home.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from uuid import uuid4

from pulsara_agent.conversation_kernel.compaction.contracts import (
    CompactionDisposition,
    ResolvedCompactionPolicy,
)
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.llm.input import PromptContent
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, ModelsDevCatalogClient
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.settings import LocalPostgresConfig, LocalSettingsStore
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from pulsara_agent.workspace_identity import HostWorkspaceInput

from run_model_switch_handover_dogfood import (
    _ReadOnlySettingsStore,
    _RecordingModelRuntime,
    _binding,
    _create_database,
    _drop_database,
    _scrub,
    _seed_completed_history,
)


async def _run() -> dict[str, object]:
    saved = LocalSettingsStore().read()
    deepseek = {
        connection.target.wire_api.value: connection
        for connection in saved.model_connections
        if connection.target.model_id == "deepseek-flash"
        and connection.target.route_id == "deepseek"
    }
    expected_wires = {"openai_chat_completions", "openai_responses"}
    if set(deepseek) != expected_wires or any(
        saved.model_api_key(connection.id) is None
        for connection in deepseek.values()
    ):
        raise RuntimeError("both saved DeepSeek Chat and Responses connections are required")
    database_name, _root_admin, ephemeral_admin, ephemeral_runtime = (
        _create_database(saved)
    )
    previous_home = os.environ.get("PULSARA_HOME")
    try:
        with TemporaryDirectory(prefix="pulsara-deepseek-epoch-") as temporary:
            root = Path(temporary).resolve()
            product_home = root / "pulsara-home"
            workspace = root / "workspace"
            product_home.mkdir()
            workspace.mkdir()
            os.environ["PULSARA_HOME"] = os.fspath(product_home)
            injected = _ReadOnlySettingsStore(
                replace(
                    saved,
                    postgres=LocalPostgresConfig(
                        ephemeral_runtime, ephemeral_admin
                    ),
                )
            )
            catalog = ModelCatalogOwner(ModelsDevCatalogClient())
            await catalog.refresh()
            delegate = ModelRuntime.production(settings=injected, catalog=catalog)  # type: ignore[arg-type]
            calls: list[dict[str, object]] = []
            runtime = _RecordingModelRuntime(delegate, calls, None)
            core = KernelHostCore.production(model_runtime=runtime)  # type: ignore[arg-type]
            try:
                session = await core.open_session(
                    HostWorkspaceInput(
                        workspace_kind="project",
                        workspace_root=workspace,
                        trust_workspace_mcp_config=False,
                    ),
                    system_prompt=(
                        "This is a bounded continuity test. Answer directly and "
                        "briefly; do not call tools."
                    ),
                )
                if not await session.attach_controller("deepseek-epoch-dogfood"):
                    raise RuntimeError("dogfood controller could not attach")
                chat = deepseek["openai_chat_completions"]
                responses = deepseek["openai_responses"]
                marker_chat = f"DS_CHAT_CONTEXT_{uuid4().hex[:12].upper()}"
                marker_responses = f"DS_RESP_CONTEXT_{uuid4().hex[:12].upper()}"
                sequence = (
                    (
                        chat,
                        f"The marker for this conversation is {marker_chat}. "
                        "Reply CHAT_OK.",
                    ),
                    (
                        responses,
                        "What exact marker did my preceding request contain? "
                        "Reply with just that marker.",
                    ),
                    (
                        responses,
                        f"The new marker for this conversation is {marker_responses}. "
                        "Reply RESP_OK.",
                    ),
                    (
                        chat,
                        "What new exact marker did my immediately preceding request "
                        "contain? Reply with just that marker.",
                    ),
                )
                finals: list[str] = []
                turn_calls: list[tuple[dict[str, object], ...]] = []
                for index, (connection, prompt) in enumerate(sequence):
                    await session.update_model_call_binding(_binding(delegate, connection))
                    first_call = len(calls)
                    result = await session.run_turn(
                        PromptContent.text(prompt),
                        command_id=f"command:deepseek-epoch:{uuid4().hex}:{index}",
                    )
                    finals.append(result.final_text)
                    turn_calls.append(tuple(calls[first_call:]))
                session._compaction.policy = ResolvedCompactionPolicy(  # noqa: SLF001
                    automatic_enabled=False,
                    minimum_reclaim_tokens=1,
                    maximum_recent_human_messages=1,
                    maximum_recent_human_text_utf8_bytes=8 << 10,
                )
                seeded_bytes = await _seed_completed_history(
                    session, segments=6, repetitions=300
                )
                compaction = await session.compact_context(
                    command_id=f"command:deepseek-epoch:compact:{uuid4().hex}",
                    force=True,
                )
                await session.update_model_call_binding(_binding(delegate, responses))
                first_call = len(calls)
                after_compaction = await session.run_turn(
                    PromptContent.text("Reply exactly POST_COMPACTION_OK."),
                    command_id=f"command:deepseek-epoch:resume:{uuid4().hex}",
                )
                turn_calls.append(tuple(calls[first_call:]))
                with session.repository.connection_provider.connection(
                    lane=PostgresConnectionLane.INSPECTOR,
                    deadline_monotonic=monotonic() + 30,
                ) as connection:
                    assistant_count = connection.execute(
                        "SELECT count(*) FROM pulsara_v3.transcript_entries "
                        "WHERE session_id=%s AND entry_kind='ASSISTANT_MESSAGE'",
                        (session.session_id,),
                    ).fetchone()[0]
                    snapshot_count = connection.execute(
                        "SELECT count(*) FROM pulsara_v3.context_snapshots "
                        "WHERE session_id=%s",
                        (session.session_id,),
                    ).fetchone()[0]
                    replay_wires = connection.execute(
                        "SELECT wire_api, count(*) FROM "
                        "pulsara_v3.provider_assistant_replay_fragments "
                        "WHERE session_id=%s GROUP BY wire_api ORDER BY wire_api",
                        (session.session_id,),
                    ).fetchall()
                    adopted_count = connection.execute(
                        "SELECT count(*) FROM pulsara_v3.agent_events "
                        "WHERE session_id=%s AND event_type='CompactionAdopted'",
                        (session.session_id,),
                    ).fetchone()[0]
                opens = tuple(
                    item for item in calls if item["purpose"] == "agent_model_loop"
                )
                expected = (
                    "openai_chat_completions",
                    "openai_responses",
                    "openai_responses",
                    "openai_chat_completions",
                    "openai_responses",
                )
                wires_by_turn = tuple(
                    tuple(str(item["wire_api"]) for item in group)
                    for group in turn_calls
                )
                return {
                    "completed_at_utc": datetime.now(timezone.utc).isoformat(),
                    "database": database_name,
                    "passed": (
                        marker_chat in finals[1]
                        and marker_responses in finals[3]
                        and "POST_COMPACTION_OK" in after_compaction.final_text
                        and compaction.disposition is CompactionDisposition.COMPACTED
                        and all(
                            wires and set(wires) == {expected[index]}
                            for index, wires in enumerate(wires_by_turn)
                        )
                        and int(assistant_count) >= 11
                        and int(snapshot_count) == 1
                        and int(adopted_count) == 1
                        and seeded_bytes > 90_000
                        and any(
                            item["purpose"] == "context_compaction_summary"
                            for item in calls
                        )
                        and all(
                            isinstance(item["terminal"], dict)
                            and item["terminal"].get("kind") == "COMPLETED"
                            for item in opens
                        )
                    ),
                    "final_texts": finals,
                    "post_compaction_final_text": after_compaction.final_text,
                    "compaction_disposition": compaction.disposition.value,
                    "seeded_canonical_history_utf8_bytes": seeded_bytes,
                    "wires_by_turn": wires_by_turn,
                    "assistant_rows": int(assistant_count),
                    "compaction_snapshots": int(snapshot_count),
                    "compaction_adopted_events": int(adopted_count),
                    "replay_wires": [(str(wire), int(count)) for wire, count in replay_wires],
                    "provider_calls": calls,
                }
            finally:
                await core.shutdown()
    finally:
        if previous_home is None:
            os.environ.pop("PULSARA_HOME", None)
        else:
            os.environ["PULSARA_HOME"] = previous_home
        _drop_database(saved, database_name)


def main() -> int:
    secrets = tuple(item.value for item in LocalSettingsStore().read().model_api_keys)
    try:
        report = asyncio.run(_run())
    except BaseException as exc:
        report = {
            "passed": False,
            "failure_type": type(exc).__name__,
            "failure_message": str(exc),
        }
    scrubbed = _scrub(report, secrets)
    encoded = json.dumps(scrubbed, ensure_ascii=False)
    if any(secret and secret in encoded for secret in secrets):
        raise RuntimeError("dogfood report retained a configured model API key")
    print(json.dumps(scrubbed, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if bool(report.get("passed")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
