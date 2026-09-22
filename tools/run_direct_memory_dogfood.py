"""Real-provider direct-memory probe against a disposable local database.

Saved production settings are read-only. This script does not print configured
credentials and does not alter the user's existing Pulsara database.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from uuid import uuid4

from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.conversation_kernel.memory.management import MemoryManagementSelection
from pulsara_agent.llm.input import PromptContent
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, ModelsDevCatalogClient
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.ports.provider_stream import ProviderModelExecutionFailed
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.settings import LocalPostgresConfig, LocalSettingsStore
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from pulsara_agent.tool_permission import preset_to_policy
from pulsara_agent.workspace_identity import HostWorkspaceInput

from run_model_switch_handover_dogfood import (
    _ReadOnlySettingsStore,
    _binding,
    _create_database,
    _drop_database,
    _scrub,
)


async def _run(*, model_id: str, wire_api: str) -> dict[str, object]:
    saved = LocalSettingsStore().read()
    connection = next(
        (
            item for item in saved.model_connections
            if item.target.model_id == model_id
            and item.target.wire_api.value == wire_api
            and saved.model_api_key(item.id) is not None
        ),
        None,
    )
    if connection is None:
        raise RuntimeError("requested saved model connection is unavailable")
    database_name, _root_admin, temporary_admin, temporary_runtime = (
        _create_database(saved)
    )
    previous_home = os.environ.get("PULSARA_HOME")
    try:
        with TemporaryDirectory(prefix="pulsara-direct-memory-") as temporary:
            root = Path(temporary).resolve()
            home = root / "home"
            workspace = root / "workspace"
            home.mkdir()
            workspace.mkdir()
            os.environ["PULSARA_HOME"] = os.fspath(home)
            settings = _ReadOnlySettingsStore(
                replace(
                    saved,
                    postgres=LocalPostgresConfig(
                        temporary_runtime, temporary_admin
                    ),
                )
            )
            catalog = ModelCatalogOwner(ModelsDevCatalogClient())
            await catalog.refresh()
            runtime = ModelRuntime.production(settings=settings, catalog=catalog)  # type: ignore[arg-type]
            workspace_input = HostWorkspaceInput(
                workspace_kind="project",
                workspace_root=workspace,
                trust_workspace_mcp_config=False,
            )
            marker = "ORION_" + uuid4().hex[:10].upper()
            observation_file = workspace / "arrival_notes.txt"
            observation_file.write_text(
                f"项目编号：{marker}\n实地记录日期：2026-09-21\n"
                "办公地点：A 楼。\n访客指引草稿：暂未决定。\n",
                encoding="utf-8",
            )
            prompts = (
                f"请调用 remember 工具，把这条测试事实存成 GLOBAL 范围的 FACT："
                f"‘项目 {marker} 的办公地点是 A 楼。’ 保存后简短报告工具结果。",
                f"地点已改为 B 楼。请先调用 remember 把‘项目 {marker} 的办公地点是 B 楼。’"
                "存成 GLOBAL FACT，再用 mark_memory_relation 将新事实标为取代旧事实。"
                "如工具返回相关记忆，请使用精确 memory_id。最后简短报告结果。",
                *(
                    f"请调用 remember，将‘项目 {marker} 的第 Q{index} 季营收为 "
                    f"{index * 10}。’存成 GLOBAL FACT；只需报告工具状态。"
                    for index in range(1, 5)
                ),
            )
            core = KernelHostCore.production(model_runtime=runtime)
            finals: list[str] = []
            session_id: str
            project_source_id: str
            project_dependent_id: str
            project_workspace_id: str
            try:
                session = await core.open_session(
                    workspace_input,
                    permission_policy=preset_to_policy(
                        PermissionMode.BYPASS_PERMISSIONS
                    ),
                )
                session_id = session.session_id
                if not await session.attach_controller("direct-memory-dogfood"):
                    raise RuntimeError("dogfood controller could not attach")
                await session.update_model_call_binding(_binding(runtime, connection))
                observed = await session.run_turn(
                    PromptContent.text(
                        f"请先用 read_file 工具读取工作目录中的 arrival_notes.txt。"
                        f"根据读到的内容，调用 remember 保存一条 CURRENT_PROJECT 的 FACT，"
                        f"用你自己的自然语言概括项目 {marker} 的办公地点和记录日期；"
                        "不要把整个文件原样存储，也不要另建同义记忆。最后简短报告状态。"
                    ),
                    command_id=f"command:direct-memory:observed:{uuid4().hex}",
                    requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
                )
                finals.append(observed.final_text)
                with session.repository.connection_provider.connection(
                    lane=PostgresConnectionLane.INSPECTOR,
                    deadline_monotonic=monotonic() + 30,
                ) as database:
                    observed_facts = database.execute(
                        "SELECT f.id, f.statement, s.workspace_id, b.tool_name "
                        "FROM pulsara_v3.memory_facts f "
                        "JOIN pulsara_v3.tool_results tr "
                        "ON tr.id=f.created_by_tool_result_id "
                        "JOIN pulsara_v3.sessions s "
                        "ON s.id=tr.session_id "
                        "AND s.memory_domain_id=f.memory_domain_id "
                        "JOIN pulsara_v3.assistant_message_blocks b "
                        "ON b.session_id=tr.session_id "
                        "AND b.assistant_entry_id=tr.tool_call_entry_id "
                        "AND b.tool_call_id=tr.tool_call_id "
                        "WHERE tr.session_id=%s AND f.fact_kind='FACT' "
                        "AND f.context_id<>'ctx:global' ORDER BY f.accepted_at",
                        (session_id,),
                    ).fetchall()
                    observed_tool_calls = database.execute(
                        "SELECT b.tool_name FROM pulsara_v3.assistant_message_blocks b "
                        "WHERE b.session_id=%s AND b.block_kind='TOOL_CALL'",
                        (session_id,),
                    ).fetchall()
                if len(observed_facts) != 1 or not any(
                    item[0] == "read_file" for item in observed_tool_calls
                ):
                    raise RuntimeError("real model did not read the file and save one project FACT")
                if str(observed_facts[0][3]) != "remember":
                    raise RuntimeError(
                        "project FACT was not owned by its canonical remember ToolResult"
                    )
                project_source_id = str(observed_facts[0][0])
                project_workspace_id = str(observed_facts[0][2])
                if marker not in str(observed_facts[0][1]) or "A 楼" not in str(observed_facts[0][1]):
                    raise RuntimeError("saved project FACT did not retain the observed subject")
                derived = await session.run_turn(
                    PromptContent.text(
                        f"请另存一条与办公地点事实语义不同的 CURRENT_PROJECT DECISION："
                        f"项目 {marker} 的访客指引应标注 A 楼入口。"
                        f"这项决定基于已保存的地点记忆 {project_source_id}，"
                        "调用 remember 时将该精确 ID 放进 based_on_memory_ids。"
                        "不要再保存一条同义的地点 FACT。最后简短报告状态。"
                    ),
                    command_id=f"command:direct-memory:derived:{uuid4().hex}",
                    requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
                )
                finals.append(derived.final_text)
                with session.repository.connection_provider.connection(
                    lane=PostgresConnectionLane.INSPECTOR,
                    deadline_monotonic=monotonic() + 30,
                ) as database:
                    dependency = database.execute(
                        "SELECT r.source_fact_id FROM pulsara_v3.memory_relations r "
                        "WHERE r.relation_kind='BASED_ON' AND r.target_fact_id=%s",
                        (project_source_id,),
                    ).fetchone()
                if dependency is None:
                    raise RuntimeError("real model did not establish the distinct memory basis")
                project_dependent_id = str(dependency[0])
                for prompt in prompts:
                    result = await session.run_turn(
                        PromptContent.text(prompt),
                        command_id=f"command:direct-memory:{uuid4().hex}",
                        requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
                    )
                    finals.append(result.final_text)
                with session.repository.connection_provider.connection(
                    lane=PostgresConnectionLane.INSPECTOR,
                    deadline_monotonic=monotonic() + 30,
                ) as database:
                    facts = database.execute(
                        "SELECT f.id, f.lifecycle, f.fact_kind, f.statement "
                        "FROM pulsara_v3.memory_facts f "
                        "JOIN pulsara_v3.tool_results tr "
                        "ON tr.id=f.created_by_tool_result_id "
                        "WHERE tr.session_id=%s ORDER BY f.accepted_at, f.id",
                        (session_id,),
                    ).fetchall()
                    relations = database.execute(
                        "SELECT mr.relation_kind, mr.source_fact_id, "
                        "mr.target_fact_id FROM pulsara_v3.memory_relations mr "
                        "JOIN pulsara_v3.tool_results tr "
                        "ON tr.id=mr.created_by_tool_result_id "
                        "WHERE tr.session_id=%s",
                        (session_id,),
                    ).fetchall()
                    q4_remember = database.execute(
                        """
                        SELECT r.model_visible_memory_fact_ids
                        FROM pulsara_v3.tool_results AS r
                        JOIN pulsara_v3.assistant_message_blocks AS b
                          ON b.session_id=r.session_id
                         AND b.assistant_entry_id=r.tool_call_entry_id
                         AND b.tool_call_id=r.tool_call_id
                        WHERE r.session_id=%s AND b.tool_name='remember'
                          AND b.tool_arguments->>'statement' LIKE %s
                          AND r.result_state='SUCCESS'
                        ORDER BY r.accepted_at DESC LIMIT 1
                        """,
                        (session_id, f"%{marker}%Q4%"),
                    ).fetchone()
                if len(facts) < 2:
                    raise RuntimeError("real model did not save two direct memory facts")
                newer = next(
                    (row for row in facts if "B 楼" in str(row[3])), None
                )
                if newer is None:
                    raise RuntimeError("real model did not save the replacement fact")
                if q4_remember is None or len(q4_remember[0]) != 4:
                    raise RuntimeError(
                        "real model did not receive three related memories "
                        "with its Q4 remember result"
                    )
                replacement_id = str(newer[0])
            finally:
                await core.shutdown()

            # A fresh Host must read the same canonical memory without a
            # replayed governor decision or process-local candidate queue.
            resumed_core = KernelHostCore.production(model_runtime=runtime)
            try:
                resumed = await resumed_core.resume_session(
                    session_id,
                    workspace_input=workspace_input,
                    permission_policy=preset_to_policy(
                        PermissionMode.BYPASS_PERMISSIONS
                    ),
                )
                if not await resumed.attach_controller("direct-memory-dogfood-resume"):
                    raise RuntimeError("resumed controller could not attach")
                await resumed.update_model_call_binding(_binding(runtime, connection))
                result = await resumed.run_turn(
                    PromptContent.text(
                        f"请用 memory_get 读取 memory_id={replacement_id}，"
                        "并简短告诉我保存的地点。"
                    ),
                    command_id=f"command:direct-memory:resume:{uuid4().hex}",
                    requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
                )
                finals.append(result.final_text)
                selection = MemoryManagementSelection()
                preview = resumed.repository.memory_deletion_preview(
                    memory_domain_id="u_local",
                    selection=selection,
                    fact_id=replacement_id,
                    deadline_monotonic=monotonic() + 30,
                )
                resumed.repository.execute_memory_deletion(
                    memory_domain_id="u_local",
                    selection=selection,
                    fact_id=replacement_id,
                    additional=(),
                    expected_records=lambda: iter(preview),
                    deadline_monotonic=monotonic() + 30,
                )
                project_selection = MemoryManagementSelection(
                    "project", project_workspace_id
                )
                project_preview = resumed.repository.memory_deletion_preview(
                    memory_domain_id="u_local",
                    selection=project_selection,
                    fact_id=project_source_id,
                    deadline_monotonic=monotonic() + 30,
                )
                resumed.repository.execute_memory_deletion(
                    memory_domain_id="u_local",
                    selection=project_selection,
                    fact_id=project_source_id,
                    additional=(),
                    expected_records=lambda: iter(project_preview),
                    deadline_monotonic=monotonic() + 30,
                )
                with resumed.repository.connection_provider.connection(
                    lane=PostgresConnectionLane.INSPECTOR,
                    deadline_monotonic=monotonic() + 30,
                ) as database:
                    remaining = database.execute(
                        "SELECT count(*) FROM pulsara_v3.memory_facts WHERE id=%s",
                        (replacement_id,),
                    ).fetchone()[0]
                    project_remaining = database.execute(
                        "SELECT count(*) FROM pulsara_v3.memory_facts "
                        "WHERE id=ANY(%s::text[])",
                        ([project_source_id, project_dependent_id],),
                    ).fetchone()[0]
                return {
                    "passed": (
                        len(facts) >= 2
                        and any(row[0] == "SUPERSEDES" for row in relations)
                        and q4_remember is not None
                        and len(q4_remember[0]) == 4
                        and "B 楼" in finals[-1]
                        and remaining == 0
                        and project_remaining == 0
                    ),
                    "facts": [
                        {"id": str(row[0]), "lifecycle": str(row[1]),
                         "kind": str(row[2]), "statement": str(row[3])}
                        for row in facts
                    ],
                    "relations": [tuple(map(str, row)) for row in relations],
                    "q4_visible_memory_count": len(q4_remember[0]),
                    "model_finals": finals,
                    "deleted_replacement": remaining == 0,
                    "project_basis_cascade_deleted": project_remaining == 0,
                    "project_deletion_preview_types": [
                        json.loads(item)["type"] for item in project_preview
                    ],
                    "deletion_preview_types": [
                        json.loads(item)["type"] for item in preview
                    ],
                }
            finally:
                await resumed_core.shutdown()
    finally:
        if previous_home is None:
            os.environ.pop("PULSARA_HOME", None)
        else:
            os.environ["PULSARA_HOME"] = previous_home
        _drop_database(saved, database_name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-id", default="gpt-5.5")
    parser.add_argument("--wire-api", default="openai_responses")
    arguments = parser.parse_args()
    secrets = tuple(item.value for item in LocalSettingsStore().read().model_api_keys)
    try:
        report = asyncio.run(
            _run(model_id=arguments.model_id, wire_api=arguments.wire_api)
        )
    except BaseException as exc:
        report = {
            "passed": False,
            "failure_type": type(exc).__name__,
            "failure_message": str(exc),
        }
        if isinstance(exc, ProviderModelExecutionFailed):
            report["provider_message"] = exc.error.message
            report["provider_diagnostics"] = [
                item.model_dump(mode="json") for item in exc.error.diagnostics
            ]
    scrubbed = _scrub(report, secrets)
    encoded = json.dumps(scrubbed, ensure_ascii=False)
    if any(secret and secret in encoded for secret in secrets):
        raise RuntimeError("dogfood report retained a configured model API key")
    print(json.dumps(scrubbed, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if bool(report.get("passed")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
