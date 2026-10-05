"""Real saved-provider stop, canonical history, continuation and prefix evidence."""

from __future__ import annotations
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.llm.input import PromptContent
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, ModelsDevCatalogClient
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.settings import LocalSettingsStore, LocalPostgresConfig
from pulsara_agent.terminal_protocol.canonical_v3 import CanonicalProtocolReader
from pulsara_agent.scheduling.service import ScheduledTaskService
from pulsara_agent.web_app.session_controller import LocalSessionController
from pulsara_agent.tool_permission import default_permission_policy
from datetime import timedelta
from tests.dogfood.credentials import secret_scrubber
from tests.dogfood.run_model_switch_handover_dogfood import (
    _ReadOnlySettingsStore,
    _RecordingModelRuntime,
    _binding,
    _create_database,
    _drop_database,
)
from time import monotonic


async def run():
    saved = LocalSettingsStore().read()
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
    previous_home = os.environ.get("PULSARA_HOME")
    try:
        with TemporaryDirectory(prefix="pulsara-scheduled-tasks-") as temporary:
            home = Path(temporary) / "home"
            root = Path(temporary) / "workspace"
            home.mkdir()
            root.mkdir()
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
            scheduler = ScheduledTaskService(sessions)
            core.scheduled_tasks = scheduler

            async def scheduled(prompt, permission="bypass-permissions"):
                value = await scheduler.create(
                    session_id=session.session_id,
                    values=dict(
                        name="Dogfood",
                        prompt=prompt,
                        timezone="UTC",
                        permission_mode=permission,
                        schedule={
                            "contract": "scheduled-rule:v1",
                            "kind": "once",
                            "run_at_utc": (
                                datetime.now(timezone.utc) + timedelta(hours=1)
                            )
                            .replace(microsecond=0)
                            .isoformat(),
                        },
                    ),
                )

                def make_due(*, deadline_monotonic):
                    with session.repository._writer_transaction(
                        session._lease.guard, deadline_monotonic=deadline_monotonic
                    ) as c:
                        c.execute(
                            "UPDATE pulsara_v3.scheduled_tasks SET next_run_at=date_trunc('second',clock_timestamp())-interval '1 second' WHERE id=%s",
                            (value["id"],),
                        )

                await scheduler._io.run(make_due, deadline_monotonic=monotonic() + 30)
                scheduler.wake()
                return value

            async def wait_idle():
                async with asyncio.timeout(30):
                    while session._active_task is not None:
                        await asyncio.sleep(0.01)

            block = asyncio.Event()
            try:
                session = (
                    await sessions.create_session(
                        workspace_kind="project", workspace_path=str(root)
                    )
                ).session
                await session.update_model_call_binding(_binding(delegate, connection))
                reached = asyncio.Event()
                tools = session._runner._tools
                original = tools.invoke

                async def stopped_invocation(**kwargs):
                    result = await original(**kwargs)
                    reached.set()
                    await (
                        block.wait()
                    )  # Hold publication until the user stop owner is installed.
                    return result

                tools.invoke = stopped_invocation
                first_task = await scheduled(
                    "Call terminal exactly once with command pwd, yield_time_ms 1000, max_output_chars 1000. Then reply FIRST_DONE."
                )
                await scheduler.start()
                # Bound only this dogfood observation, not production turn lifetime.
                await asyncio.wait_for(reached.wait(), 120)
                print("real tool finished; installing user stop", flush=True)
                target = session._active_turn_id
                outcome = await session.request_stop_turn(
                    command_id=f"command:control:{int(session._control_monotonic() * 1000) + 60000}:dogfood-stop",
                    expected_session_id=session.session_id,
                    expected_host_session_id=session.host_session_id,
                    target_turn_id=target,
                )
                assert (
                    outcome.user_control is not None and outcome.user_control.accepted
                ), outcome.public_code
                block.set()  # Physical settlement must finish; stop does not undo the tool.
                await wait_idle()
                print("stop settled; reading history and continuing", flush=True)
                tools.invoke = original
                reader = CanonicalProtocolReader(session.repository.connection_provider)
                before = await asyncio.to_thread(
                    reader.snapshot,
                    session_id=session.session_id,
                    maximum_entries=256,
                    maximum_control_items=128,
                    deadline_monotonic=monotonic() + 30,
                )
                assert (
                    len(before.interruption_notices) == 1
                    and before.interruption_notices[0].reason == "USER_STOPPED"
                )
                reply = await session.run_turn(
                    PromptContent.text(
                        "Read the runtime previous-turn outcome. Explain briefly why the preceding turn stopped without resuming that task. Independently call terminal once with command pwd, yield_time_ms 1000, max_output_chars 1000, then include NOTICE_OK in your answer."
                    )
                )
                after = await asyncio.to_thread(
                    reader.snapshot,
                    session_id=session.session_id,
                    maximum_entries=256,
                    maximum_control_items=128,
                    deadline_monotonic=monotonic() + 30,
                )
                assert list(after.interruption_notices) == list(
                    before.interruption_notices
                )
                assert "NOTICE_OK" in reply.final_text and len(calls) >= 3
                assert any(
                    "USER_STOPPED"
                    in json.dumps(c["provider_input"], ensure_ascii=False)
                    for c in calls[1:]
                )
                assert any(
                    any(
                        m.get("role") == "tool" for m in c["provider_input"]["messages"]
                    )
                    for c in calls[1:]
                )
                first_projection = await scheduler.get(first_task["id"])
                assert first_projection["status"] == "COMPLETED"
                assert first_projection["last_turn_status"] == "INTERRUPTED"
                assert first_projection["last_entry_id"]
                assert any(
                    "Runtime scheduled trigger"
                    in json.dumps(c["provider_input"], ensure_ascii=False)
                    for c in calls
                )
                # Exercise the ordinary permission fence with a real provider tool
                # request, then the same user stop owner. Do not authorize the write.
                permission_task = await scheduled(
                    "Use terminal exactly once to run touch scheduled_permission_probe.txt with yield_time_ms 1000 and max_output_chars 1000. Wait for its result, then reply PERMISSION_DONE.",
                    permission="ask-permissions",
                )
                async with asyncio.timeout(120):
                    while (
                        session._interactions._pending is None
                        and not session._interactions._dormant
                    ):
                        await asyncio.sleep(0.05)
                pending = (
                    session._interactions._pending or session._interactions._dormant[0]
                )
                assert (
                    pending is not None
                    and not (root / "scheduled_permission_probe.txt").exists()
                )
                print(
                    "scheduled permission request waiting; installing user stop",
                    flush=True,
                )
                stopped = await session.request_stop_turn(
                    command_id=f"command:control:{int(session._control_monotonic() * 1000) + 60000}:scheduled-permission-stop",
                    expected_session_id=session.session_id,
                    expected_host_session_id=session.host_session_id,
                    target_turn_id=session._active_turn_id,
                )
                assert (
                    stopped.user_control is not None and stopped.user_control.accepted
                )
                await wait_idle()
                permission_projection = await scheduler.get(permission_task["id"])
                assert permission_projection["last_turn_status"] == "INTERRUPTED"
                final = await asyncio.to_thread(
                    reader.snapshot,
                    session_id=session.session_id,
                    maximum_entries=256,
                    maximum_control_items=128,
                    deadline_monotonic=monotonic() + 30,
                )
                assert (
                    len(final.interruption_notices) == 2
                    and not (root / "scheduled_permission_probe.txt").exists()
                )
                for previous, current in zip(calls, calls[1:]):
                    left, right = previous["provider_input"], current["provider_input"]
                    assert (
                        right["messages"][: len(left["messages"])] == left["messages"]
                    )
                    assert {k: v for k, v in left.items() if k != "messages"} == {
                        k: v for k, v in right.items() if k != "messages"
                    }
                evidence = {
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "model": connection.target.model_id,
                    "reply": reply.final_text,
                    "notice_count": 2,
                    "scheduled_tasks": [first_projection, permission_projection],
                    "permission_wait_stopped": True,
                    "automatic_timer_without_browser": True,
                    "notice_retained_after_continue": True,
                    "prefix_continuity": True,
                    "tool_closure_present": True,
                    "calls": calls,
                }
                return json.loads(
                    scrubber.scrub_text(json.dumps(evidence, ensure_ascii=False))
                )
            except BaseException:
                state = {
                    "calls": calls,
                    "task": await scheduler.get(first_task["id"]),
                    "active_turn": session._active_turn_id,
                }
                reader = CanonicalProtocolReader(session.repository.connection_provider)
                snap = await asyncio.to_thread(
                    reader.snapshot,
                    session_id=session.session_id,
                    maximum_entries=256,
                    maximum_control_items=128,
                    deadline_monotonic=monotonic() + 30,
                )
                from google.protobuf.json_format import MessageToDict

                state["snapshot"] = MessageToDict(
                    snap, preserving_proto_field_name=True
                )
                Path("/tmp/pulsara-scheduled-failure.json").write_text(
                    scrubber.scrub_text(json.dumps(state, ensure_ascii=False, indent=2))
                )
                raise
            finally:
                block.set()
                await scheduler.aclose()
                await sessions.aclose()
                await core.shutdown()
    finally:
        if previous_home is None:
            os.environ.pop("PULSARA_HOME", None)
        else:
            os.environ["PULSARA_HOME"] = previous_home
        _drop_database(saved, database)


if __name__ == "__main__":
    result = asyncio.run(run())
    destination = Path("output/scheduled-tasks-20261005/dogfood.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "evidence": str(destination.resolve()),
                "model": result["model"],
                "reply": result["reply"],
            },
            ensure_ascii=False,
        )
    )
