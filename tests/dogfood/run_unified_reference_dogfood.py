"""Real-provider typed-reference round trips in an isolated production Host."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from pulsara_agent.identity import is_valid_id, random_id
from pulsara_agent.llm.input import PromptContent, PromptImagePart
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, ModelsDevCatalogClient
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.settings import LocalPostgresConfig
from pulsara_agent.workspace_identity import HostWorkspaceInput
from tests.dogfood import run_image_history_recovery_dogfood as common
from tests.dogfood.run_model_switch_handover_dogfood import (
    _ReadOnlySettingsStore,
    _binding,
    _create_database,
    _drop_database,
    _find_connection,
)


async def run(args, report, saved):
    selected = _find_connection(saved, args.model)
    database, _, admin, dsn = await asyncio.to_thread(_create_database, saved)
    report["database"] = database
    previous = os.environ.get("PULSARA_HOME")
    core = None
    try:
        with TemporaryDirectory(prefix="pulsara-reference-dogfood-") as directory:
            home = Path(directory) / "home"
            home.mkdir()
            workspace = Path(directory).resolve() / "workspace"
            workspace.mkdir()
            os.environ["PULSARA_HOME"] = str(home)
            catalog = ModelCatalogOwner(ModelsDevCatalogClient())
            await catalog.refresh()
            delegate = ModelRuntime.production(
                settings=_ReadOnlySettingsStore(
                    replace(saved, postgres=LocalPostgresConfig(dsn, admin))
                ),
                catalog=catalog,
            )
            runtime = common.Runtime(delegate, report)
            core = KernelHostCore.production(model_runtime=runtime)
            session = await core.open_session(
                HostWorkspaceInput(
                    workspace_kind="project",
                    workspace_root=workspace,
                    trust_workspace_mcp_config=False,
                ),
                system_prompt="Perform the user's requested local validation accurately. Use the available tools, copy returned references exactly, and report observed results briefly.",
            )
            await session.update_model_call_binding(_binding(delegate, selected))

            async def turn(phase, prompt):
                report["phase"] = phase
                print(phase, flush=True)
                report.setdefault("inputs", []).append(
                    {"phase": phase, "text": str(prompt)}
                )
                result = await session.run_turn(
                    prompt,
                    command_id=random_id("command"),
                    requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
                )
                report["outcomes"].append({"phase": phase, "text": result.final_text})
                return result.final_text

            if args.scenario == "history":
                await turn(
                    "text_image_upload",
                    PromptContent(
                        (
                            PromptImagePart(
                                Path(report["fixtures"][0]["path"]).read_bytes(),
                                "image/png",
                            ),
                        )
                    ),
                )
                calls = common.phase_calls(report, "text_image_upload")
                assert calls and all(
                    tuple(c["input_modalities"]) == ("text",) for c in calls
                ), calls[0]["input_modalities"]
                assert all(c["image_occurrences"] == 0 for c in calls)
                assert not common.tool_calls(calls), common.tool_calls(calls)
                await turn(
                    "text_history_recovery",
                    PromptContent.text(
                        "Use read_session_content to locate the image I uploaded earlier in this same conversation. Then copy its returned IMAGE_REF target into view_image. Explain honestly whether your current model can see it; do not infer pixels from the reference."
                    ),
                )
                tools = common.tool_calls(
                    common.phase_calls(report, "text_history_recovery")
                )
                assert any(t["tool_name"] == "read_session_content" for t in tools)
                refs = [
                    json.loads(t["arguments"])["image_ref"]
                    for t in tools
                    if t["tool_name"] == "view_image"
                ]
                assert refs and all(is_valid_id(ref, "image") for ref in refs)
                assert all(c["image_occurrences"] == 0 for c in report["calls"])
                report["checks"][
                    "text_model_neutral_image_and_explicit_history_roundtrip"
                ] = True
            else:
                await turn(
                    "memory_roundtrip",
                    PromptContent.text(
                        "For this disposable test project, remember the factual fixture 'The reference validation canary is ORCHID-731.' in CURRENT_PROJECT, then use memory_get with the exact returned MEMORY target and report the retrieved statement."
                    ),
                )
                tools = common.tool_calls(
                    common.phase_calls(report, "memory_roundtrip")
                )
                assert any(t["tool_name"] == "remember" for t in tools)
                ids = [
                    json.loads(t["arguments"])["target"]["memory_id"]
                    for t in tools
                    if t["tool_name"] == "memory_get"
                ]
                assert ids and all(is_valid_id(value, "memory") for value in ids)
                report["checks"]["memory_roundtrip"] = True
                await turn(
                    "artifact_roundtrip",
                    PromptContent.text(
                        "Use terminal to run python3 -c \"print('0123456789' * 16000); print('ARTIFACT_CANARY_529')\". The full stdout exceeds the inline preview. Use artifact_read with the returned artifact_id to read the saved output; do not fabricate an ID. Report the artifact ID and the canary at the end."
                    ),
                )
                tools = common.tool_calls(
                    common.phase_calls(report, "artifact_roundtrip")
                )
                ids = [
                    json.loads(t["arguments"])["artifact_id"]
                    for t in tools
                    if t["tool_name"] == "artifact_read"
                ]
                assert ids and all(is_valid_id(value, "artifact") for value in ids)
                report["checks"]["artifact_roundtrip"] = True
                await turn(
                    "connection_task_roundtrip",
                    PromptContent.text(
                        f"List available agent models with list_agent_models. Select the saved connection for {args.model}, then spawn exactly one agent with that explicit connection. Its task is to answer 19+23 without tools. Wait for that exact returned task ID with wait_agent and report its result."
                    ),
                )
                tools = common.tool_calls(
                    common.phase_calls(report, "connection_task_roundtrip")
                )
                assert any(t["tool_name"] == "list_agent_models" for t in tools)
                spawns = [
                    json.loads(t["arguments"])
                    for t in tools
                    if t["tool_name"] == "spawn_agent"
                ]
                assert (
                    len(spawns) == 1
                    and spawns[0]["model"]["connection_id"] == selected.id.value
                )
                waits = [
                    json.loads(t["arguments"])
                    for t in tools
                    if t["tool_name"] == "wait_agent"
                ]
                assert waits and any(
                    v.get("task_ids")
                    and all(is_valid_id(i, "task") for i in v["task_ids"])
                    for v in waits
                )
                assert "42" in report["outcomes"][-1]["text"]
                report["checks"]["connection_and_task_roundtrip"] = True
            # Each scene consists of repeated appends in its live root epoch.
            root_calls = [
                c for c in report["calls"] if c["purpose"] == "agent_model_loop"
            ]
            # Child model loops have their own independent prefixes.
            if args.scenario == "history":
                common.base.check_prefix(root_calls)
            common.verify_real_requests(report)
            report["checks"]["actual_http_matches_frozen_wire"] = True
            report["passed"] = True
    finally:
        if core is not None:
            await core.shutdown()
        if previous is None:
            os.environ.pop("PULSARA_HOME", None)
        else:
            os.environ["PULSARA_HOME"] = previous
        await asyncio.to_thread(_drop_database, saved, database)


if __name__ == "__main__":
    common.run = run
    raise SystemExit(common.main())
