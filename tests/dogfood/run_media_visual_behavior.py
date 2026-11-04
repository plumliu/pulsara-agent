"""Real Host multi-window reads and exact active-turn user steering.

No media UI is running: only explicit model calls can read frames, and the
observable backend read projection has no playback/seek command channel.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
from time import monotonic

from psycopg.conninfo import make_conninfo

from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.desktop_runtime import validate_managed_postgres
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import PromptContent
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, ModelsDevCatalogClient
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.settings import LocalPostgresConfig, LocalSettingsStore
from pulsara_agent.workspace_identity import HostWorkspaceInput
from tests.dogfood.run_media_visual_benchmark import CASES, fixture
from tests.dogfood.run_model_switch_handover_dogfood import (
    _ReadOnlySettingsStore,
    _RecordingModelRuntime,
    _binding,
    _find_connection,
)
from tests.dogfood.run_tool_output_pagination_dogfood import _trace
from tests.support.postgres_database import (
    admin_root_dsn,
    create_migrated_postgres_test_database,
    drop_postgres_test_database,
)


class Records(list):
    on_main = None

    def append(self, value):
        super().append(value)
        if value["purpose"] == "agent_model_loop" and self.on_main is not None:
            self.on_main()


async def run(args):
    home = require_pulsara_home()
    original_home = os.environ.get("PULSARA_HOME")
    socket = str(home / "desktop" / "socket")

    def dsn(role):
        return make_conninfo(host=socket, port="5432", dbname="pulsara", user=role)

    postgres = LocalPostgresConfig(dsn("pulsara_runtime"), dsn("pulsara_admin"))
    validate_managed_postgres(postgres)
    saved = LocalSettingsStore(home / "local-settings.yaml", postgres=postgres).read()
    selected = _find_connection(saved, args.model)
    os.environ.update(
        PULSARA_POSTGRES_DSN=postgres.runtime_dsn,
        PULSARA_POSTGRES_ADMIN_DSN=postgres.admin_dsn,
    )
    database = await asyncio.to_thread(create_migrated_postgres_test_database)
    workspace = (args.output.parent / "visual-behavior-fixtures").resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    isolated = workspace / "home"
    isolated.mkdir(exist_ok=True)
    await asyncio.to_thread(
        fixture, workspace / "lecture.mkv", "lecture", CASES["lecture"]
    )
    records = Records()
    report = {"model": args.model}
    core = session = None
    try:
        os.environ["PULSARA_HOME"] = str(isolated)
        catalog = ModelCatalogOwner(ModelsDevCatalogClient())
        await catalog.refresh()
        runtime = ModelRuntime.production(
            settings=_ReadOnlySettingsStore(
                replace(
                    saved,
                    postgres=LocalPostgresConfig(
                        database.runtime_dsn, database.admin_dsn
                    ),
                )
            ),
            catalog=catalog,
        )
        core = KernelHostCore.production(
            model_runtime=_RecordingModelRuntime(runtime, records)
        )
        session = await core.open_session(
            HostWorkspaceInput(
                workspace_kind="project",
                workspace_root=workspace,
                trust_workspace_mcp_config=False,
            ),
            system_prompt="Follow user instructions with the requested native tools. Be concise. Do not add annotations unless explicitly requested.",
        )
        await session.attach_controller(random_id("attachment"))
        await session.update_model_call_binding(_binding(runtime, selected))
        prompt = (
            "Read lecture.mkv with read_video_frames detail from 1 to 2, then separately from 2 to 3. "
            "Both windows are in the same chapter. Do not annotate and do not send progress commentary between reads. "
            "After both reads, give one final short sentence naming the unchanged main slide label. Only use read_video_frames."
        )
        first = await session.run_turn(
            PromptContent.text(prompt),
            command_id=random_id("command"),
            requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
        )
        trace, _ = await asyncio.to_thread(_trace, session)
        main = [r for r in records if r["purpose"] == "agent_model_loop"]
        report["multi_window"] = {
            "prompt": prompt,
            "answer": first.final_text,
            "tools": [
                {
                    "tool": r["tool_name"],
                    "arguments": r["arguments"],
                    "state": r["result_state"],
                }
                for r in trace
            ],
            "text_before_final_call": [
                b["text"]
                for r in main[:-1]
                for b in r["normalized_blocks"]
                if b["kind"] == "text"
            ],
            "usage": [r["terminal"] for r in main],
        }
        assert len(trace) == 2 and all(
            r["tool_name"] == "read_video_frames" and r["result_state"] == "SUCCESS"
            for r in trace
        )
        assert {(r["arguments"]["from"], r["arguments"]["to"]) for r in trace} == {
            (1, 2),
            (2, 3),
        }
        assert "STEP ONE" in first.final_text.upper()
        # No explicit task remains; current-work spans are gone, and no provider
        # invocation is created by merely retaining the preview-less backend.
        snapshot = asdict(session.current_work_snapshot())
        observed = len(records)
        await asyncio.sleep(0.25)
        assert len(records) == observed
        report["idle_snapshot"] = snapshot
        report["idle_background_model_calls"] = 0
        report["playback_boundary"] = (
            "No renderer/player instantiated; tool reading updates read-range observations only. Browser playback lifecycle is covered separately by frontend tests."
        )
        offset = len(records)
        before = len(trace)
        opened = asyncio.Event()
        records.on_main = opened.set
        task = asyncio.create_task(
            session.run_turn(
                PromptContent.text(
                    "Read lecture.mkv from 1 to 2 using read_video_frames detail, then report the slide label. Do not annotate."
                ),
                command_id=random_id("command"),
                requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
            )
        )
        await opened.wait()
        records.on_main = None
        target = session.active_root_turn_id()
        assert target is not None
        steer_text = (
            "Change location now: use read_video_frames detail on lecture.mkv from 7 to 8 instead. "
            "Report the main slide label at this new location only. Do not annotate; do not play or seek any player."
        )
        begin = monotonic()
        outcome = await session.steer_active_turn(
            command_id=random_id("command"),
            content=PromptContent.text(steer_text),
            target_turn_id=target,
        )
        assert outcome.status == "PENDING", outcome
        result = await task
        all_trace, _ = await asyncio.to_thread(_trace, session)
        calls = [r for r in records[offset:] if r["purpose"] == "agent_model_loop"]
        report["active_steer"] = {
            "text": steer_text,
            "accepted_while_model_active": True,
            "status": outcome.status,
            "target_turn_id": target,
            "answer": result.final_text,
            "seconds_after_steer": monotonic() - begin,
            "tools": [
                {
                    "tool": r["tool_name"],
                    "arguments": r["arguments"],
                    "state": r["result_state"],
                }
                for r in all_trace[before:]
            ],
            "usage": [r["terminal"] for r in calls],
            "steer_observed_in_provider_input": any(
                steer_text in json.dumps(r["provider_input"]) for r in calls
            ),
        }
        assert any(
            r["tool_name"] == "read_video_frames"
            and r["arguments"].get("from") == 7
            and r["arguments"].get("to") == 8
            and r["result_state"] == "SUCCESS"
            for r in all_trace[before:]
        )
        assert not any(r["tool_name"] == "annotate_media" for r in all_trace)
        assert "STEP TWO" in result.final_text.upper()
        assert report["active_steer"]["steer_observed_in_provider_input"]
        report["status"] = "passed"
    except Exception as exc:
        report["error"] = saved.credential_scrub_set().scrub_text(str(exc))
        raise
    finally:
        if core is not None:
            if session is not None:
                await core.close_session(session.host_session_id)
            await core.shutdown()
        if original_home is None:
            os.environ.pop("PULSARA_HOME", None)
        else:
            os.environ["PULSARA_HOME"] = original_home
        await asyncio.to_thread(
            drop_postgres_test_database, admin_root_dsn(), database.database_name
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(saved.credential_scrub_set().scrub_json(report), indent=2)
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="qwen3.8-flash")
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(run(parser.parse_args()))
