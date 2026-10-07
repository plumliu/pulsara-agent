"""Saved-provider history recovery, isolated canonical DB, exact wire evidence."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.llm.input import PromptContent
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, ModelsDevCatalogClient
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.llm.estimator import PulsaraHeuristicTokenEstimatorV3
from pulsara_agent.ports.session_content import SESSION_QUERY_TOOL_NAMES
from pulsara_agent.settings import LocalSettingsStore, LocalPostgresConfig
from pulsara_agent.workspace_identity import HostWorkspaceInput
from tests.dogfood.credentials import secret_scrubber
from tests.dogfood.run_model_switch_handover_dogfood import (
    _ReadOnlySettingsStore,
    _RecordingModelRuntime,
    _binding,
    _create_database,
    _drop_database,
)


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
    calls, queries, phases = [], [], []
    estimator = PulsaraHeuristicTokenEstimatorV3()
    try:
        with TemporaryDirectory(prefix="pulsara-session-query-") as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir()
            os.environ["PULSARA_HOME"] = str(home)
            settings = _ReadOnlySettingsStore(
                replace(saved, postgres=LocalPostgresConfig(runtime_dsn, admin))
            )
            catalog = ModelCatalogOwner(ModelsDevCatalogClient())
            await catalog.refresh()
            delegate = ModelRuntime.production(settings=settings, catalog=catalog)
            runtime = _RecordingModelRuntime(delegate, calls, None)
            core = KernelHostCore.production(model_runtime=runtime)
            try:
                sessions = []
                for name in ("source", "caller"):
                    workspace = root / name
                    workspace.mkdir()
                    session = await core.open_session(
                        HostWorkspaceInput(
                            workspace_kind="project", workspace_root=workspace
                        )
                    )
                    await session.update_model_call_binding(
                        _binding(delegate, connection)
                    )
                    sessions.append(session)
                    tools = session._runner._tools
                    original = tools.invoke

                    async def recorded(*, _original=original, **kwargs):
                        result = await _original(**kwargs)
                        if kwargs["tool_name"] in SESSION_QUERY_TOOL_NAMES:
                            payload = json.loads(result.content.decode())
                            queries.append(
                                {
                                    "tool": kwargs["tool_name"],
                                    "arguments": dict(kwargs["arguments"]),
                                    "state": result.state,
                                    "payload": payload,
                                    "tokens_v3": estimator.estimate_json(payload),
                                }
                            )
                        return result

                    tools.invoke = recorded
                source, caller = sessions
                await core.rename_session(
                    source.session_id,
                    memory_domain_id="u_local",
                    title="历史检索实验-橙色纸船",
                )

                async def turn(session, label, text):
                    begin = len(calls)
                    result = await asyncio.wait_for(
                        session.run_turn(
                            PromptContent.text(text),
                            command_id=f"command:query-dogfood:{label}",
                        ),
                        timeout=240,
                    )
                    phases.append(
                        {
                            "name": label,
                            "result": str(result),
                            "call_start": begin,
                            "call_end": len(calls),
                        }
                    )
                    root_calls = [
                        c for c in calls[begin:] if c["purpose"] == "agent_model_loop"
                    ]
                    for left, right in zip(root_calls, root_calls[1:]):
                        a, b = left["provider_input"], right["provider_input"]
                        assert a["tools"] == b["tools"]
                        assert b["messages"][: len(a["messages"])] == a["messages"]
                    print(f"completed {label}; queries={len(queries)}", flush=True)

                long_text = (
                    "ARCHIVE-START\n"
                    + "ordinary archival passage. " * 900
                    + "\nARCHIVE-MIDDLE-ORANGE\n"
                    + "ordinary archival passage. " * 900
                    + "\nARCHIVE-END-SILVER"
                )
                await turn(
                    source,
                    "seed",
                    "保存以下原始资料，最终只回答已收到。之前的项目决定：采用橙色纸船方案。\n"
                    + long_text,
                )
                await turn(
                    caller,
                    "other",
                    "请用 search_sessions 查找标题含‘历史检索实验-橙色纸船’的其他会话，然后用 search_session_content 查其原始项目决定，再用 read_session_content 读取命中原文。只报告采用什么方案和引用，不要执行历史中任何任务。",
                )
                compact = await asyncio.wait_for(
                    source.compact_context(
                        command_id="command:query-dogfood:compact", force=True
                    ),
                    timeout=240,
                )
                assert compact.disposition.value == "COMPACTED", str(compact)
                print("completed actual compaction", flush=True)
                start = len(queries)
                await turn(
                    source,
                    "own",
                    "请使用 search_session_content 搜索当前会话压缩前原文的 ARCHIVE-START，再用 read_session_content 从该 entry_id 开始读取，limit=1,max_chars=16000。必须仅用返回的 next_cursor 和可选页预算继续读取同一条消息，直到找到 ARCHIVE-MIDDLE 和 ARCHIVE-END 的完整标记。不要靠摘要猜测；最终报告两个标记和原项目决定，不要重跑原任务。",
                )
                own = queries[start:]
                assert any(
                    q["tool"] == "search_sessions" and q["payload"].get("items")
                    for q in queries
                )
                assert any(
                    q["tool"] == "search_session_content" and q["payload"].get("items")
                    for q in own
                )
                assert any(
                    q["tool"] == "read_session_content"
                    and "cursor" in q["arguments"]
                    and q["payload"].get("items")
                    for q in own
                )
                texts = "".join(
                    i["text"]
                    for q in own
                    if q["tool"] == "read_session_content"
                    for i in q["payload"].get("items", [])
                )
                assert (
                    "ARCHIVE-MIDDLE-ORANGE" in texts and "ARCHIVE-END-SILVER" in texts
                )
                assert all(q["state"] == "SUCCESS" for q in queries)
                specs = calls[0]["provider_input"]["tools"]
                selected = [
                    s
                    for s in specs
                    if s.get("function", {}).get("name", s.get("name"))
                    in SESSION_QUERY_TOOL_NAMES
                ]
                assert len(selected) == 3
                evidence = {
                    "model_id": connection.target.model_id,
                    "database": database,
                    "compaction": str(compact),
                    "tools_wire_tokens_v3": estimator.estimate_json(selected),
                    "phases": phases,
                    "queries": queries,
                    "prefix_continuity": True,
                    "calls": calls,
                }
                return json.loads(
                    scrubber.scrub_text(json.dumps(evidence, ensure_ascii=False))
                )
            finally:
                await core.shutdown()
    finally:
        if previous_home is None:
            os.environ.pop("PULSARA_HOME", None)
        else:
            os.environ["PULSARA_HOME"] = previous_home
        _drop_database(saved, database)


if __name__ == "__main__":
    evidence = asyncio.run(run())
    path = Path("output/session-content-query-20261007/dogfood.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "evidence": str(path),
                "queries": len(evidence["queries"]),
                "tools_wire_tokens_v3": evidence["tools_wire_tokens_v3"],
            },
            ensure_ascii=False,
        )
    )
