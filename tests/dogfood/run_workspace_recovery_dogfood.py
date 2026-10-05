"""Real saved-provider cold reads, explicit empty recovery and ordinary suffix."""

from __future__ import annotations
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory

from pulsara_agent.conversation_kernel.host import (
    KernelHostCore,
    default_permission_policy,
)
from pulsara_agent.llm.input import PromptContent
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, ModelsDevCatalogClient
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.settings import LocalSettingsStore, LocalPostgresConfig
from pulsara_agent.workspace_identity import HostWorkspaceInput, WorkspaceUnavailable
from pulsara_agent.web_app.session_controller import LocalSessionController
from pulsara_agent.web_app.browser_bridge import LocalBrowserBridge
from tests.dogfood.credentials import secret_scrubber
from tests.dogfood.run_model_switch_handover_dogfood import (
    _ReadOnlySettingsStore,
    _RecordingModelRuntime,
    _binding,
    _create_database,
    _drop_database,
)


async def run():
    saved = LocalSettingsStore().read()  # Ordinary resolver, before changing home.
    connection = next(
        (
            c
            for c in saved.model_connections
            if c.target.model_id == "deepseek-flash"
            and c.target.wire_api.value == "openai_chat_completions"
        ),
        None,
    )
    if connection is None or saved.model_api_key(connection.id) is None:
        raise RuntimeError(
            "Saved deepseek-flash Chat connection/credential is required"
        )
    scrubber = secret_scrubber(saved)
    database, _, admin, runtime_dsn = _create_database(saved)
    original_home = os.environ.get("PULSARA_HOME")
    try:
        with TemporaryDirectory(prefix="pulsara-workspace-recovery-") as temporary:
            base = Path(temporary)
            home, root = base / "home", base / "workspace"
            home.mkdir()
            root.mkdir()
            (root / "original.txt").write_text("This file must not be recovered.")
            os.environ["PULSARA_HOME"] = str(home)
            settings = _ReadOnlySettingsStore(
                replace(saved, postgres=LocalPostgresConfig(runtime_dsn, admin))
            )
            catalog = ModelCatalogOwner(ModelsDevCatalogClient())
            await catalog.refresh()
            delegate = ModelRuntime.production(settings=settings, catalog=catalog)
            calls = []
            runtime = _RecordingModelRuntime(delegate, calls, None)
            core = KernelHostCore.production(model_runtime=runtime)
            sessions = LocalSessionController(
                core=core,
                permission_policy=default_permission_policy(),
                active_skill_names=frozenset(),
            )
            bridge = LocalBrowserBridge(sessions=sessions, protocol_server=object())
            source = HostWorkspaceInput(workspace_kind="project", workspace_root=root)
            try:
                first = await core.open_session(source)
                await first.update_model_call_binding(_binding(delegate, connection))
                original = await first.run_turn(
                    PromptContent.text("Reply exactly HISTORY_OK. Do not call tools.")
                )
                session_id, workspace_id = (
                    first.session_id,
                    first.workspace.workspace_key,
                )
                await core.close_session(first.host_session_id)
                shutil.rmtree(root)
                with_error = False
                try:
                    await sessions.resume_session(session_id)
                except WorkspaceUnavailable:
                    with_error = True
                assert with_error and not root.exists()
                before = len(calls)
                cold = await bridge.connect(
                    session_id, browser_instance_id="dogfood:cold"
                )
                assert (
                    cold["view"] == "history"
                    and "role" not in cold
                    and "connection_id" not in cold
                )
                snapshot = await sessions.history_snapshot(session_id)
                assert (
                    snapshot.snapshot.snapshot.entries
                    and not sessions._by_session
                    and len(calls) == before
                )
                restored = await sessions.restore_missing_workspace(session_id)
                assert (
                    restored.session.session_id == session_id
                    and restored.session.workspace.workspace_key == workspace_id
                )
                assert (
                    list(root.iterdir()) == []
                    and restored.session._workspace_recreated_at is not None
                )
                followup = await restored.session.run_turn(
                    PromptContent.text(
                        "Call terminal exactly once with command pwd, yield_time_ms 1000 and max_output_chars 1000. "
                        "After the result, read any trusted runtime feedback added to this conversation. "
                        "Explain briefly whether original files were recovered and include RESTORE_OK."
                    )
                )
                resumed_calls = calls[before:]
                assert len(resumed_calls) >= 2, (
                    "Expected the requested tool to reach an ordinary ROOT barrier"
                )
                first_input = resumed_calls[0]["provider_input"]
                for call in resumed_calls[1:]:
                    current = call["provider_input"]
                    assert (
                        current["messages"][: len(first_input["messages"])]
                        == first_input["messages"]
                    )
                    assert {k: v for k, v in current.items() if k != "messages"} == {
                        k: v for k, v in first_input.items() if k != "messages"
                    }
                assert any(
                    "workspace_recreated"
                    in json.dumps(call["provider_input"], ensure_ascii=False)
                    for call in resumed_calls[1:]
                )
                assert (
                    restored.session._workspace_recreated_at is None
                    and not (root / "original.txt").exists()
                )
                assert "RESTORE_OK" in followup.final_text
                evidence = dict(
                    completed_at=datetime.now(timezone.utc).isoformat(),
                    model=connection.target.model_id,
                    session_id=session_id,
                    workspace_id=workspace_id,
                    original_reply=original.final_text,
                    restored_reply=followup.final_text,
                    cold_entries=len(snapshot.snapshot.snapshot.entries),
                    cold_allocated_host=False,
                    original_file_recovered=False,
                    calls=calls,
                    prefix_continuity=True,
                )
                return json.loads(
                    scrubber.scrub_text(json.dumps(evidence, ensure_ascii=False))
                )
            finally:
                await bridge.aclose()
                await sessions.aclose()
                await core.shutdown()
    finally:
        if original_home is None:
            os.environ.pop("PULSARA_HOME", None)
        else:
            os.environ["PULSARA_HOME"] = original_home
        _drop_database(saved, database)


if __name__ == "__main__":
    result = asyncio.run(run())
    destination = Path("output/session-workspace-recovery-20261005/dogfood.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "evidence": str(destination.resolve()),
                "model": result["model"],
                "reply": result["restored_reply"],
            },
            ensure_ascii=False,
        )
    )
