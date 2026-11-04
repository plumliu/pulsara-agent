"""Host/runner/canonical PostgreSQL/real SDK HTTP boundary for pure-audio reads."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from io import BytesIO
import json
import wave

from aiohttp import web
import pytest

from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.conversation_kernel.tool_outputs import PostgresToolOutputReadPort
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import PromptContent, text_part_values
from pulsara_agent.media.asr_config import AsrConnection
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.tool_permission import preset_to_policy
from pulsara_agent.workspace_identity import HostWorkspaceInput
from tests.support.model_config import (
    test_model_binding as model_binding,
    test_model_runtime as model_runtime,
)
from tests.support.postgres import guarded_postgres_test_connection
from tests.test_round4_plan_host import _DetachedDraftModel, _text, _tool_call

pytestmark = pytest.mark.postgres


class TranscriptHostModel(_DetachedDraftModel):
    def __init__(self):
        super().__init__()
        self.action = None
        self.turns = set()

    async def stream(self, request):
        self.requests.append(request)
        if request.turn_id not in self.turns:
            self.turns.add(request.turn_id)
            name, arguments = self.action
            events = _tool_call(name, random_id("call"), arguments)
        else:
            events = _text("TRANSCRIPT_HOST_DONE")
        for event in events:
            yield event


def test_pure_audio_host_sdk_overlap_model_change_cleanup_and_exact_history(
    tmp_path, monkeypatch, isolated_migrated_postgres_database
):
    import pulsara_agent.conversation_kernel.host as host_module

    database = isolated_migrated_postgres_database
    monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
    model = TranscriptHostModel()
    monkeypatch.setattr(host_module, "DirectKernelModelPort", lambda **_: model)
    monkeypatch.setattr(
        host_module.LocalMcpManagementService, "load_configs", lambda *_a, **_k: ()
    )
    path = tmp_path / "pure-audio.wav"
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\0\0" * 64000)

    async def scenario():
        requests = []

        async def transcribe(request):
            assert request.path == "/custom/transcriptions"
            assert (
                request.headers["Authorization"] == "Bearer loopback-fixture-asr-secret"
            )
            reader = await request.multipart()
            fields = []
            uploaded = None
            while part := await reader.next():
                value = bytes(await part.read())
                fields.append((part.name, value))
                if part.name == "file":
                    uploaded = value
            assert uploaded is not None and uploaded[:4] == b"RIFF"
            with wave.open(BytesIO(uploaded)) as audio:
                duration = len(audio.readframes(audio.getnframes())) / (
                    audio.getframerate() * audio.getnchannels() * audio.getsampwidth()
                )
            assert ("response_format", b"verbose_json") in fields
            assert ("timestamp_granularities[]", b"word") in fields
            chosen_model = next(
                value.decode() for name, value in fields if name == "model"
            )
            requests.append((chosen_model, duration))
            return web.json_response(
                {
                    "text": f"spoken-{chosen_model}",
                    "words": [
                        {"word": f"spoken-{chosen_model}", "start": 0, "end": duration}
                    ],
                    "language": "en",
                    "model": chosen_model,
                }
            )

        app = web.Application()
        app.router.add_post("/custom/transcriptions", transcribe)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        endpoint = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/custom/transcriptions"
        runtime = model_runtime(
            postgres_dsn=database.runtime_dsn,
            model_id="test-pro",
            wire_api="openai_chat_completions",
        )
        runtime.settings.value = replace(
            runtime.settings.read(),
            asr=AsrConnection(endpoint, "model-A", "loopback-fixture-asr-secret"),
        )
        core = KernelHostCore.production(
            model_runtime=runtime,
            permission_policy=preset_to_policy(PermissionMode.BYPASS_PERMISSIONS),
        )
        session = None
        try:
            session = await core.open_session(
                HostWorkspaceInput(workspace_kind="project", workspace_root=tmp_path)
            )
            await session.update_model_call_binding(model_binding(runtime))
            reader = PostgresToolOutputReadPort(
                session.repository.connection_provider,
                session_id=session.session_id,
                workspace_id=session.workspace.workspace_key,
            )

            async def invoke(name, arguments):
                model.action = name, arguments
                result = await session.run_turn(PromptContent.text(f"Invoke {name}"))
                assert result.final_text == "TRANSCRIPT_HOST_DONE"
                with guarded_postgres_test_connection(
                    database.runtime_dsn
                ) as connection:
                    row = connection.execute(
                        "SELECT r.result_entry_id,r.result_state FROM pulsara_v3.tool_results r JOIN pulsara_v3.transcript_entries e ON e.id=r.result_entry_id WHERE r.session_id=%s ORDER BY e.entry_sequence DESC LIMIT 1",
                        (session.session_id,),
                    ).fetchone()
                assert row[1] == "SUCCESS"
                body = reader.read_body(
                    {"kind": "SESSION_ENTRY", "entry_id": row[0]}
                ).content
                return row[0], body, json.loads(body)

            target = {"kind": "PATH", "path": str(path)}
            first_entry, first_bytes, first = await invoke(
                "read_transcript", {"target": target, "from": 0, "to": 2}
            )
            assert first["source"] == "asr" and first["complete"] is True
            assert first["entries"][0]["text"] == "spoken-model-A"
            assert requests == [("model-A", 2)]
            runtime.settings.value = replace(
                runtime.settings.read(),
                asr=AsrConnection(endpoint, "model-B", "loopback-fixture-asr-secret"),
            )
            _, _, second = await invoke(
                "read_transcript", {"target": target, "from": 1, "to": 3}
            )
            assert requests == [("model-A", 2), ("model-B", 1)]
            assert [entry["text"] for entry in second["entries"]] == [
                "spoken-model-A",
                "spoken-model-B",
            ]
            await invoke("read_transcript", {"target": target, "from": 0, "to": 3})
            assert len(requests) == 2
            # Cache disposal is independent of already accepted source pages.
            with guarded_postgres_test_connection(database.runtime_dsn) as connection:
                connection.execute(
                    "UPDATE pulsara_v3.asr_cache SET last_used_at=statement_timestamp()-interval '3 days'"
                )
            assert (await core.clear_old_asr_cache())["deleted_blocks"] == 2
            _, _, reread = await invoke(
                "read_file",
                {"target": {"kind": "SESSION_ENTRY", "entry_id": first_entry}},
            )
            assert reread["content"].encode() == first_bytes
            copied = tmp_path / "saved-first-page.json"
            await invoke(
                "write_file",
                {
                    "target": {"kind": "PATH", "path": str(copied)},
                    "source": {"kind": "SESSION_ENTRY", "entry_id": first_entry},
                },
            )
            assert copied.read_bytes() == first_bytes
            assert (
                reader.read_body(
                    {"kind": "SESSION_ENTRY", "entry_id": first_entry}
                ).content
                == first_bytes
            )
            assert len(requests) == 2
            # The normal next model call receives the exact original page.
            rendered = "\n".join(
                text
                for request in model.requests
                for message in request.compiled_input.messages
                for text in text_part_values(message.content)
            )
            assert "spoken-model-A" in rendered and "spoken-model-B" in rendered
            assert core._sessions

            # A second session joins the same accepted gap before the initiating
            # Host closes. Its cloned descriptor and Core decoder/cache IO must
            # stay alive, and the frozen first model must be charged only once.
            from pulsara_agent.media.audio import extract_audio

            shared_path = tmp_path / "shared-audio.wav"
            shared_path.write_bytes(path.read_bytes())
            entered = asyncio.Event()
            release = asyncio.Event()
            physical = core._media_asr_runtime
            original_run = physical._worker.run

            async def gated_decode(operation, *args, **kwargs):
                if operation is extract_audio:
                    entered.set()
                    await release.wait()
                return await original_run(operation, *args, **kwargs)

            monkeypatch.setattr(physical._worker, "run", gated_decode)
            runtime.settings.value = replace(
                runtime.settings.read(),
                asr=AsrConnection(endpoint, "model-C", "loopback-fixture-asr-secret"),
            )
            model.action = (
                "read_transcript",
                {
                    "target": {"kind": "PATH", "path": str(shared_path)},
                    "from": 0,
                    "to": 2,
                },
            )
            initiating = asyncio.create_task(
                session.run_turn(PromptContent.text("Read shared audio A"))
            )
            await asyncio.wait_for(entered.wait(), 20)
            survivor = await core.open_session(
                HostWorkspaceInput(workspace_kind="project", workspace_root=tmp_path)
            )
            await survivor.update_model_call_binding(model_binding(runtime))
            runtime.settings.value = replace(
                runtime.settings.read(),
                asr=AsrConnection(endpoint, "model-D", "loopback-fixture-asr-secret"),
            )
            surviving = asyncio.create_task(
                survivor.run_turn(PromptContent.text("Read shared audio B"))
            )
            try:
                async with asyncio.timeout(20):
                    while not any(work.waiters == 2 for work in physical.work._active):
                        await asyncio.sleep(0.01)
                await core.close_session(session.host_session_id)
                session = None
                await asyncio.gather(initiating, return_exceptions=True)
                release.set()
                assert (await surviving).final_text == "TRANSCRIPT_HOST_DONE"
                assert requests[-1] == ("model-C", 2)
                assert len(requests) == 3
                with guarded_postgres_test_connection(
                    database.runtime_dsn
                ) as connection:
                    cached = connection.execute(
                        "SELECT provenance->>'model' FROM pulsara_v3.asr_cache WHERE source_path=%s",
                        (str(shared_path),),
                    ).fetchall()
                assert cached == [("model-C",)]
            finally:
                release.set()
                await core.close_session(survivor.host_session_id)
        finally:
            if session is not None:
                await core.close_session(session.host_session_id)
            await core.shutdown()
            await runner.cleanup()

    asyncio.run(scenario())
