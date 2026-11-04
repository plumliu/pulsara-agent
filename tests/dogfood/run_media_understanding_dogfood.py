"""Exercise actual Host tools, visual provider input, and annotation history.

Uses saved model settings read-only and a disposable database created at the
verified managed desktop endpoint. Reports scrubbed provider usage and answers.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic

import av
from PIL import Image
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


def fixture(path):
    # Caption and visual deliberately disagree. Only a decoded frame can supply
    # the final color; a transcript is evidence of the embedded caption alone.
    cues = b"1\n00:00:00,000 --> 00:00:02,000\nOrange boat.\n"
    with (
        av.open(io.BytesIO(cues), format="srt") as subtitles,
        av.open(str(path), "w") as output,
    ):
        video = output.add_stream("libx264", rate=10)
        video.width, video.height = 320, 180
        stream = output.add_stream_from_template(subtitles.streams.subtitles[0])
        for packet in subtitles.demux():
            if packet.pts is not None:
                packet.stream = stream
                output.mux(packet)
        for i in range(20):
            frame = av.VideoFrame.from_image(
                Image.new("RGB", (320, 180), "red" if i < 10 else "blue")
            )
            for packet in video.encode(frame):
                output.mux(packet)
        for packet in video.encode():
            output.mux(packet)


async def run(args, report):
    home = require_pulsara_home()
    root = os.environ.get("PULSARA_HOME")
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
    records = []
    try:
        with TemporaryDirectory() as directory:
            workspace = Path(directory).resolve()
            media = workspace / "example.mkv"
            fixture(media)
            isolated = workspace / "home"
            isolated.mkdir()
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
            session = None
            try:
                session = await core.open_session(
                    HostWorkspaceInput(
                        workspace_kind="project",
                        workspace_root=workspace,
                        trust_workspace_mcp_config=False,
                    ),
                    system_prompt="Follow the user's instructions using the requested tools. Reply briefly in English.",
                )
                await session.attach_controller(random_id("attachment"))
                await session.update_model_call_binding(_binding(runtime, selected))

                async def turn(prompt):
                    print("Running:", prompt, flush=True)
                    begin = monotonic()
                    result = await session.run_turn(
                        PromptContent.text(prompt),
                        command_id=random_id("command"),
                        requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
                    )
                    report.setdefault("turns", []).append(
                        {
                            "prompt": prompt,
                            "answer": result.final_text,
                            "seconds": monotonic() - begin,
                        }
                    )
                    print(
                        saved.credential_scrub_set().scrub_text(result.final_text),
                        flush=True,
                    )
                    return result.final_text

                await turn(
                    "Use read_video_frames on example.mkv with target only, then read_transcript on the same file. Report the actual first and final colors and quote the exact subtitle. Only use these two native tools."
                )
                await turn(
                    "Call annotate_media on example.mkv from 1 to 2, recording the final actual color. Reply added once committed."
                )
                trace, _ = await asyncio.to_thread(_trace, session)
                report["tools"] = [
                    (r["tool_name"], r["result_state"], r["arguments"]) for r in trace
                ]
                notes = await core.read_media_annotations(
                    memory_domain_id=session._memory_domain_id,
                    path=str(media),
                    modified_ns=str(media.stat().st_mtime_ns),
                    after=None,
                )
                report["notes"] = notes
                main = [r for r in records if r["purpose"] == "agent_model_loop"]
                report["usage"] = [r["terminal"] for r in main]
                projections = [r["provider_input"] for r in main]
                for prev, curr in zip(projections, projections[1:]):
                    assert prev.get("tools") == curr.get("tools")
                    items = "messages" if "messages" in prev else "input"
                    assert prev[items] == curr[items][: len(prev[items])]
                    assert prev.get("instructions") == curr.get("instructions")
                report["prefix_transitions"] = len(main) - 1
                assert any("data:image/" in json.dumps(r) for r in projections)
                assert {
                    r["tool_name"] for r in trace if r["result_state"] == "SUCCESS"
                } >= {"read_video_frames", "read_transcript", "annotate_media"}
                answer = report["turns"][0]["answer"].lower()
                assert all(word in answer for word in ["red", "blue", "orange boat"]), (
                    answer
                )
                assert any(
                    n["from"] == 1 and n["to"] == 2 and "blue" in n["text"].lower()
                    for n in notes["items"]
                )
            finally:
                if session:
                    await core.close_session(session.host_session_id)
                await core.shutdown()
    except Exception as exc:
        report["error"] = saved.credential_scrub_set().scrub_text(str(exc))
        raise
    finally:
        if root is None:
            os.environ.pop("PULSARA_HOME", None)
        else:
            os.environ["PULSARA_HOME"] = root
        await asyncio.to_thread(
            drop_postgres_test_database, admin_root_dsn(), database.database_name
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(
                saved.credential_scrub_set().scrub_json(report),
                indent=2,
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run(args, {"model": args.model}))
