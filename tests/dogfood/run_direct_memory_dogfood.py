"""Real-provider direct-memory probe against a disposable local database.

Saved production settings are read-only. This script does not print configured
credentials and does not alter the user's existing Pulsara database.
"""

from __future__ import annotations

from pulsara_agent.identity import random_id

import argparse
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic

from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.llm.input import PromptContent
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, ModelsDevCatalogClient
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.ports.provider_stream import ProviderModelExecutionFailed
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.settings import LocalPostgresConfig, LocalSettingsStore
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from pulsara_agent.tool_permission import preset_to_policy
from pulsara_agent.workspace_identity import HostWorkspaceInput

from tests.dogfood.run_model_switch_handover_dogfood import (
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
            item
            for item in saved.model_connections
            if item.target.model_id == model_id
            and item.target.wire_api.value == wire_api
            and saved.model_api_key(item.id) is not None
        ),
        None,
    )
    if connection is None:
        raise RuntimeError("requested saved model connection is unavailable")
    database_name, _root_admin, temporary_admin, temporary_runtime = _create_database(
        saved
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
                    postgres=LocalPostgresConfig(temporary_runtime, temporary_admin),
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
            marker = "MEMORY_" + random_id("request").split(":", 1)[1]
            core = KernelHostCore.production(model_runtime=runtime)
            finals: list[str] = []
            try:
                session = await core.open_session(
                    workspace_input,
                    permission_policy=preset_to_policy(
                        PermissionMode.BYPASS_PERMISSIONS
                    ),
                )
                if not await session.attach_controller("direct-memory-dogfood"):
                    raise RuntimeError("dogfood controller could not attach")
                await session.update_model_call_binding(_binding(runtime, connection))

                async def turn(prompt):
                    result = await session.run_turn(
                        PromptContent.text(prompt),
                        command_id=random_id("command"),
                        requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
                    )
                    finals.append(result.final_text)

                def current():
                    with session.repository.connection_provider.connection(
                        lane=PostgresConnectionLane.INSPECTOR,
                        deadline_monotonic=monotonic() + 30,
                    ) as database:
                        return database.execute(
                            "SELECT id,title,statement,context_id,accepted_at,updated_at "
                            "FROM pulsara_v3.memory_facts WHERE title=%s ORDER BY id",
                            (marker,),
                        ).fetchall()

                await turn(
                    f"请调用remember，context_kind=GLOBAL，kind=FACT，title={marker}，"
                    "statement=办公地点是 A 楼。只保存这一条记忆并简短报告结果。"
                )
                created = current()
                if len(created) != 1 or created[0][2] != "办公地点是 A 楼。":
                    raise RuntimeError(
                        "real model did not create the requested complete memory"
                    )
                identifier = str(created[0][0])
                target = json.dumps({"kind": "MEMORY", "memory_id": identifier})
                await turn(
                    f"请用query_memory精确读取target={target}，简短报告完整记忆。"
                )
                await turn(
                    f"请用manage_memory修改target={target}，action=edit，"
                    "statement=办公地点已改为 B 楼。，context_kind=CURRENT_PROJECT。"
                    "保留原标题与类别，不另存新条目。"
                )
                edited = current()
                if (
                    len(edited) != 1
                    or edited[0][0] != created[0][0]
                    or edited[0][1] != marker
                    or edited[0][2] != "办公地点已改为 B 楼。"
                    or edited[0][3] == "ctx:global"
                    or edited[0][4] != created[0][4]
                    or edited[0][5] <= created[0][5]
                ):
                    raise RuntimeError(
                        "real model did not atomically edit the same memory and scope"
                    )
                await turn(
                    f"请用query_memory精确读取target={target}，确认当前正文和保存范围。"
                )
                await turn(
                    f"请用manage_memory删除target={target}，action=delete；不要创建其他条目。"
                )
                remaining = current()
                return {
                    "passed": not remaining,
                    "created": {
                        "target": {"kind": "MEMORY", "memory_id": identifier},
                        "title": marker,
                        "statement": created[0][2],
                    },
                    "edited": {
                        "statement": edited[0][2],
                        "context_kind": "CURRENT_PROJECT",
                        "updated_at": edited[0][5].isoformat(timespec="microseconds"),
                    },
                    "deleted": not remaining,
                    "model_finals": finals,
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
