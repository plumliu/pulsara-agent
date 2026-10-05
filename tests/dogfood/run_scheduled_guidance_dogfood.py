"""Observe natural-language scheduling with saved settings and disposable state."""

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
from pulsara_agent.scheduling.service import ScheduledTaskService
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

                await ask(
                    "请从2026年10月7日起，每天早上8点阅读工作区的 README.md，汇报待办项。"
                )
                page = await scheduler.list(session_id=session.session_id)
                assert len(page["tasks"]) == 1, page
                first = page["tasks"][0]
                assert first["schedule"] == {
                    "contract": "scheduled-rule:v1",
                    "kind": "daily",
                    "start_date": "2026-10-07",
                    "time": "08:00",
                }, first
                assert first["timezone"] == "Asia/Shanghai", first
                first_actions = [
                    t["arguments"]["action"]
                    for t in evidence["tools"]
                    if t["turn"] == 1 and t["name"] == "scheduled_tasks"
                ]
                assert first_actions.index("list") < first_actions.index("create"), (
                    first_actions
                )

                await ask(
                    "把刚才每天早上8点的那项任务改为阅读 README.md 后，按优先级汇报待办项。其他配置都保持原样。"
                )
                page = await scheduler.list(session_id=session.session_id)
                assert len(page["tasks"]) == 1, page
                updated = page["tasks"][0]
                assert (
                    updated["id"] == first["id"]
                    and updated["prompt"] != first["prompt"]
                )
                for key in (
                    "name",
                    "schedule",
                    "timezone",
                    "permission_mode",
                    "session_id",
                ):
                    assert updated[key] == first[key], (key, first, updated)
                second_actions = [
                    t["arguments"]["action"]
                    for t in evidence["tools"]
                    if t["turn"] == 2 and t["name"] == "scheduled_tasks"
                ]
                assert second_actions.index("get") < second_actions.index("update"), (
                    second_actions
                )

                await ask(
                    "请运行一段3秒后打印 READY 的 Python 命令，跟进直到它结束，并汇报输出。"
                )
                final = await scheduler.list(session_id=session.session_id)
                assert final["tasks"] == page["tasks"], final
                assert not any(
                    t["turn"] == 3
                    and t["name"] == "scheduled_tasks"
                    and t["arguments"]["action"] == "create"
                    for t in evidence["tools"]
                )
                assert "READY" in evidence["turns"][-1]["reply"]
                assert any(
                    t["turn"] == 3 and t["name"] == "terminal"
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
    path = Path("output/scheduled-tasks-20261006/prompt-guidance-dogfood.json")
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
