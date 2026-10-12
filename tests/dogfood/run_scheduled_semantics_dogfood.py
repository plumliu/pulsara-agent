"""Observe natural-language scheduling with saved settings and disposable state."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from uuid import uuid4
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.llm.input import PromptContent
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, ModelsDevCatalogClient
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.scheduling.service import ScheduledTaskService
from tests.test_scheduled_tasks_host import queue
from pulsara_agent.settings import LocalPostgresConfig, LocalSettingsStore
from pulsara_agent.tool_permission import default_permission_policy
from pulsara_agent.web_app.session_controller import LocalSessionController
from tests.dogfood.credentials import secret_scrubber
from tests.dogfood.run_model_switch_handover_dogfood import (
    _ReadOnlySettingsStore,
    _RecordingModelRuntime,
    _binding,
    _create_database,
    _drop_database,
)


async def run(destination: Path):
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
    evidence = {
        "model": connection.target.model_id,
        "calls": [],
        "tools": [],
        "turns": [],
    }
    try:
        with TemporaryDirectory(prefix="pulsara-scheduled-guidance-") as temporary:
            home, root = Path(temporary) / "home", Path(temporary) / "workspace"
            home.mkdir()
            root.mkdir()
            (root / "README.md").write_text(
                "# Example project\n\nTODO: review tests.\n"
            )
            os.environ["PULSARA_HOME"] = str(home)
            settings = _ReadOnlySettingsStore(
                replace(saved, postgres=LocalPostgresConfig(runtime_dsn, admin))
            )
            catalog = ModelCatalogOwner(ModelsDevCatalogClient())
            await catalog.refresh()
            delegate = ModelRuntime.production(settings=settings, catalog=catalog)
            runtime = _RecordingModelRuntime(delegate, evidence["calls"], None)
            core = KernelHostCore.production(model_runtime=runtime)
            sessions = LocalSessionController(
                core=core,
                permission_policy=default_permission_policy(),
                active_skill_names=frozenset(),
            )
            scheduler = ScheduledTaskService(sessions)
            core.scheduled_tasks = scheduler
            try:
                session = (
                    await sessions.create_session(
                        workspace_kind="project",
                        workspace_path=str(root),
                        model_call_binding=_binding(delegate, connection),
                    )
                ).session
                original = session._runner._tools.invoke

                async def observed(**kwargs):
                    result = await original(**kwargs)
                    evidence["tools"].append(
                        {
                            "turn": len(evidence["turns"]),
                            "name": kwargs["tool_name"],
                            "arguments": kwargs["arguments"],
                            "state": result.state,
                            "content": result.content.decode("utf-8"),
                        }
                    )
                    return result

                session._runner._tools.invoke = observed

                async def ask(prompt):
                    item = {"prompt": prompt}
                    evidence["turns"].append(item)
                    # This bounds only one dogfood observation, not production task lifetime.
                    async with asyncio.timeout(180):
                        reply = await session.run_turn(PromptContent.text(prompt))
                    item["reply"] = reply.final_text
                    print(f"turn {len(evidence['turns'])} completed", flush=True)

                task = await scheduler.create(
                    status="ACTIVE",
                    session_id=session.session_id,
                    values={
                        "name": "每10分钟问好",
                        "prompt": "用终端运行 echo hello，确认输出后简短回复结果。",
                        "timezone": "Asia/Shanghai",
                        "permission_mode": "bypass-permissions",
                        "schedule": {
                            "contract": "scheduled-rule:v1",
                            "kind": "interval",
                            "seconds": 600,
                            "anchor_at_utc": (
                                datetime.now(timezone.utc) + timedelta(hours=1)
                            )
                            .replace(microsecond=0)
                            .isoformat(),
                        },
                    },
                )
                await ask("好，帮我停掉当前会话的每10分钟问好计划吧。")
                paused = await scheduler.get(task["id"])
                assert paused["status"] == "PAUSED" and paused["next_run_at"] is None, (
                    paused
                )

                async def wait_accepted(task_id, *, before_count):
                    async with asyncio.timeout(180):
                        while True:
                            rows = await queue(scheduler, session.session_id)
                            relevant = [
                                r for r in rows if r["scheduled_task_id"] == task_id
                            ]
                            if (
                                len(relevant) > before_count
                                and relevant[-1]["status"] == "CONSUMED"
                                and session._active_task is None
                            ):
                                return relevant[-1]
                            await asyncio.sleep(0.1)

                async def run_once(value):
                    rows = await queue(scheduler, session.session_id)
                    before_count = sum(
                        r["scheduled_task_id"] == value["id"] for r in rows
                    )
                    item = {
                        "action": "user clicked run once",
                        "provider_call_start": len(evidence["calls"]),
                    }
                    evidence["turns"].append(item)
                    request = {
                        "task_id": value["id"],
                        "session_id": session.session_id,
                        "expected_revision": value["revision"],
                        "client_command_id": str(uuid4()),
                        "request_at_utc": datetime.now(timezone.utc)
                        .replace(microsecond=0)
                        .isoformat(),
                    }
                    item["ack"] = await scheduler.run_now(request)
                    accepted = await wait_accepted(
                        value["id"], before_count=before_count
                    )
                    assert accepted["scheduled_trigger_source"] == "RUN_ONCE"
                    item["confirmed"] = await scheduler.run_now(request)
                    assert (
                        item["confirmed"]["queue_item_id"]
                        == item["ack"]["queue_item_id"]
                    )
                    item["reply"] = "\n".join(
                        block["text"]
                        for call in evidence["calls"][item["provider_call_start"] :]
                        for block in call["normalized_blocks"]
                        if block["kind"] == "text"
                    )
                    if value["schedule"]["kind"] == "once":
                        try:
                            await scheduler.get(value["id"])
                        except KeyError:
                            item["plan_removed"] = True
                        else:
                            raise AssertionError("accepted once plan still exists")
                    else:
                        after = await scheduler.get(value["id"])
                        for key in (
                            "status",
                            "revision",
                            "next_run_at",
                            "prompt",
                            "schedule",
                        ):
                            assert after[key] == value[key], (key, value, after)
                    print(f"turn {len(evidence['turns'])} completed", flush=True)

                await run_once(paused)
                single_tools = [t for t in evidence["tools"] if t["turn"] == 2]
                assert any(
                    t["name"] == "terminal"
                    and "echo hello" in json.dumps(t["arguments"])
                    for t in single_tools
                ), single_tools
                assert not any(
                    t["name"] == "scheduled_tasks"
                    and t["arguments"].get("action") in {"delete", "resume", "update"}
                    for t in single_tools
                ), single_tools
                assert "hello" in evidence["turns"][-1]["reply"]
                await ask(
                    "今后不要执行终端命令，即使之后收到已保存任务的输入；只回复文字。"
                )
                await run_once(paused)
                assert not any(
                    t["turn"] in (3, 4) and t["name"] == "terminal"
                    for t in evidence["tools"]
                ), evidence["tools"]
                replacement = await scheduler.create(
                    status="ACTIVE",
                    session_id=session.session_id,
                    values={
                        "name": "每周文字检查",
                        "prompt": "只回复 TEXT_CHECK_OK，不使用终端或检索。",
                        "timezone": "Asia/Shanghai",
                        "permission_mode": "bypass-permissions",
                        "schedule": {
                            "contract": "scheduled-rule:v1",
                            "kind": "interval",
                            "seconds": 604800,
                            "anchor_at_utc": (
                                datetime.now(timezone.utc) + timedelta(hours=1)
                            )
                            .replace(microsecond=0)
                            .isoformat(),
                        },
                    },
                )
                replacement = await scheduler.mutate(
                    task_id=replacement["id"],
                    expected_revision=replacement["revision"],
                    action="pause",
                )
                task = await scheduler.mutate(
                    task_id=paused["id"],
                    expected_revision=paused["revision"],
                    action="resume",
                )
                scheduled_before = [r["id"] for r in await queue(scheduler, session.session_id)
                                    if r["input_origin"] == "SCHEDULED_TASK"]
                await ask("现在执行已暂停的‘每周文字检查’，不恢复它，当前自动计划保持原样。")
                assert "TEXT_CHECK_OK" in evidence["turns"][-1]["reply"]
                assert [r["id"] for r in await queue(scheduler, session.session_id)
                        if r["input_origin"] == "SCHEDULED_TASK"] == scheduled_before
                immediate_tools = [t for t in evidence["tools"] if t["turn"] == len(evidence["turns"])]
                assert all(t["arguments"].get("action") in {"list", "get"}
                           for t in immediate_tools if t["name"] == "scheduled_tasks")
                assert (await scheduler.get(task["id"]))["status"] == "ACTIVE"
                assert (await scheduler.get(replacement["id"]))["status"] == "PAUSED"
                await ask("把本会话的自动计划从‘每10分钟问好’换成‘每周文字检查’。")
                assert (await scheduler.get(task["id"]))["status"] == "PAUSED"
                assert (await scheduler.get(replacement["id"]))["status"] == "ACTIVE"
                switches = [
                    t
                    for t in evidence["tools"]
                    if t["name"] == "scheduled_tasks"
                    and t["arguments"].get("action") == "switch"
                ]
                assert (
                    switches
                    and switches[-1]["arguments"]["target"]["kind"] == "existing"
                ), switches
                await ask(
                    "把当前自动计划换成一个新的单次计划，明天同一时间运行；名称叫‘单次文字检查’，提示词只写‘只回复 ONCE_CHECK_OK，不使用终端或检索。’。"
                )
                final = await scheduler.list(session_id=session.session_id)
                once = next(t for t in final["tasks"] if t["status"] == "ACTIVE")
                assert once["schedule"]["kind"] == "once"
                switches = [
                    t
                    for t in evidence["tools"]
                    if t["name"] == "scheduled_tasks"
                    and t["arguments"].get("action") == "switch"
                ]
                assert switches[-1]["arguments"]["target"]["kind"] == "create", switches
                await run_once(once)
                assert "ONCE_CHECK_OK" in evidence["turns"][-1]["reply"]
                once_tools = [
                    t for t in evidence["tools"] if t["turn"] == len(evidence["turns"])
                ]
                assert not any(t["name"] == "scheduled_tasks" for t in once_tools), (
                    once_tools
                )
                timer = await scheduler.create(
                    status="ACTIVE",
                    session_id=session.session_id,
                    values={
                        "name": "自动单次检查",
                        "prompt": "只回复 TIMER_CHECK_OK，不使用终端或检索。",
                        "timezone": "Asia/Shanghai",
                        "permission_mode": "bypass-permissions",
                        "schedule": {
                            "contract": "scheduled-rule:v1",
                            "kind": "once",
                            "run_at_utc": (
                                datetime.now(timezone.utc) + timedelta(seconds=3)
                            )
                            .replace(microsecond=0)
                            .isoformat(),
                        },
                    },
                )
                item = {
                    "action": "timer automatic once",
                    "provider_call_start": len(evidence["calls"]),
                }
                evidence["turns"].append(item)
                await scheduler.start()
                timer_queue = await wait_accepted(timer["id"], before_count=0)
                assert timer_queue["scheduled_trigger_source"] == "TIMER"
                item["reply"] = "\n".join(
                    block["text"]
                    for call in evidence["calls"][item["provider_call_start"] :]
                    for block in call["normalized_blocks"]
                    if block["kind"] == "text"
                )
                assert "TIMER_CHECK_OK" in item["reply"]
                final = await scheduler.list(session_id=session.session_id)
                assert not any(
                    t["id"] in {once["id"], timer["id"]} for t in final["tasks"]
                )
                assert not any(t["status"] == "ACTIVE" for t in final["tasks"])
                assert not any(
                    t["name"].startswith(("memory_search", "session_search"))
                    for t in evidence["tools"]
                )

                for left, right in zip(evidence["calls"], evidence["calls"][1:]):
                    a, b = left["provider_input"], right["provider_input"]
                    assert b["messages"][: len(a["messages"])] == a["messages"]
                    assert {k: v for k, v in a.items() if k != "messages"} == {
                        k: v for k, v in b.items() if k != "messages"
                    }
                evidence.update(
                    {
                        "tasks": final["tasks"],
                        "passed": True,
                        "prefix_continuity": True,
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
            finally:
                await scheduler.aclose()
                await sessions.aclose()
                await core.shutdown()
    finally:
        if previous_home is None:
            os.environ.pop("PULSARA_HOME", None)
        else:
            os.environ["PULSARA_HOME"] = previous_home
        _drop_database(saved, database)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            scrubber.scrub_text(
                json.dumps(evidence, ensure_ascii=False, indent=2, default=str)
            )
            + "\n"
        )
    return evidence


if __name__ == "__main__":
    path = Path("output/scheduled-semantics-20261013/ui-only-run-once-dogfood.json")
    result = asyncio.run(run(path))
    print(
        json.dumps(
            {
                "passed": result["passed"],
                "calls": len(result["calls"]),
                "evidence": str(path.resolve()),
            },
            ensure_ascii=False,
        )
    )
