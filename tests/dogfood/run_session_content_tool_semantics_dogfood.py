"""Real-provider verification of the incremental saved-tool history contract."""

from __future__ import annotations

import asyncio
import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.llm.input import PromptContent
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, ModelsDevCatalogClient
from pulsara_agent.llm.model_connections import (
    ModelCallBinding,
    ReasoningEffortSelection,
)
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.ports.session_content import SESSION_QUERY_TOOL_NAMES
from pulsara_agent.primitives.context import canonical_json_bytes
from pulsara_agent.settings import LocalSettingsStore, LocalPostgresConfig
from pulsara_agent.workspace_identity import HostWorkspaceInput
from tests.dogfood.credentials import secret_scrubber
from tests.dogfood.run_model_switch_handover_dogfood import (
    _ReadOnlySettingsStore,
    _RecordingModelRuntime,
    _create_database,
    _drop_database,
)
from tests.test_conversation_fork import rows


def verify_prefixes(evidence):
    groups = (("reference",), ("execute", "retrieve-reference"), ("recover",))
    checked = []
    for names in groups:
        calls = [
            call
            for phase in evidence["phases"]
            if phase["name"] in names
            for call in evidence["calls"][phase["call_start"] : phase["call_end"]]
            if call["purpose"] == "agent_model_loop"
        ]
        for left, right in zip(calls, calls[1:]):
            a, b = left["provider_input"], right["provider_input"]
            assert json.dumps(a["tools"], ensure_ascii=False) == json.dumps(
                b["tools"], ensure_ascii=False
            )
            assert json.dumps(a["messages"], ensure_ascii=False) == json.dumps(
                b["messages"][: len(a["messages"])], ensure_ascii=False
            )
        checked.append({"phases": list(names), "pairs": max(0, len(calls) - 1)})
    return checked


async def run(evidence, *, model_id="openai/gpt-6-luna"):
    saved = LocalSettingsStore().read()
    connection = next(
        c
        for c in saved.model_connections
        if c.target.model_id == model_id
        and c.target.wire_api.value == "openai_chat_completions"
    )
    if saved.model_api_key(connection.id) is None:
        raise RuntimeError(f"Saved {model_id} Chat credential is required")
    scrubber = secret_scrubber(saved)
    database, _, admin, runtime_dsn = _create_database(saved)
    previous_home = os.environ.get("PULSARA_HOME")
    evidence.update(
        model_id=connection.target.model_id,
        reasoning="high",
        database=database,
        calls=[],
        observations=[],
        phases=[],
    )

    def save():
        path = Path("output/session-content-query-tool-addendum-20261007/dogfood.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            scrubber.scrub_text(json.dumps(evidence, ensure_ascii=False, indent=2))
            + "\n"
        )

    try:
        with TemporaryDirectory(prefix="pulsara-tool-history-") as temporary:
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
            runtime = _RecordingModelRuntime(delegate, evidence["calls"], None)
            core = KernelHostCore.production(model_runtime=runtime)
            try:
                sessions = []
                for name in ("reference", "source"):
                    workspace = root / name
                    workspace.mkdir()
                    session = await core.open_session(
                        HostWorkspaceInput(
                            workspace_kind="project", workspace_root=workspace
                        )
                    )
                    await session.update_model_call_binding(
                        ModelCallBinding(
                            connection.id, ReasoningEffortSelection("high")
                        )
                    )
                    sessions.append(session)
                    original = session._runner._tools.invoke

                    async def recorded(
                        *, _original=original, _session_id=session.session_id, **kwargs
                    ):
                        result = await _original(**kwargs)
                        body = result.content.decode()
                        try:
                            payload = json.loads(body)
                        except ValueError:
                            payload = body
                        evidence["observations"].append(
                            {
                                "session_id": _session_id,
                                "tool": kwargs["tool_name"],
                                "arguments": dict(kwargs["arguments"]),
                                "state": result.state,
                                "payload": payload,
                            }
                        )
                        save()
                        return result

                    session._runner._tools.invoke = recorded
                reference, source = sessions
                repository = source.repository

                async def turn(session, label, prompt):
                    print(f"starting {label}", flush=True)
                    begin = len(evidence["calls"])
                    obs = len(evidence["observations"])
                    # This is the diagnostic operation deadline, not a runtime cap.
                    result = await asyncio.wait_for(
                        session.run_turn(
                            PromptContent.text(prompt),
                            command_id="command:tool-history:" + label,
                        ),
                        600,
                    )
                    calls = [
                        c
                        for c in evidence["calls"][begin:]
                        if c["purpose"] == "agent_model_loop"
                    ]
                    for left, right in zip(calls, calls[1:]):
                        a, b = left["provider_input"], right["provider_input"]
                        assert a["tools"] == b["tools"]
                        assert b["messages"][: len(a["messages"])] == a["messages"]
                    state = rows(
                        repository,
                        "SELECT status FROM pulsara_v3.turns WHERE session_id=%s ORDER BY accepted_at DESC LIMIT 1",
                        (session.session_id,),
                    )[0]["status"]
                    assert state == "COMPLETED", str(result)
                    evidence["phases"].append(
                        {
                            "name": label,
                            "result": str(result),
                            "call_start": begin,
                            "call_end": len(evidence["calls"]),
                            "observation_start": obs,
                            "observation_end": len(evidence["observations"]),
                            "prefix_pairs": max(0, len(calls) - 1),
                        }
                    )
                    save()
                    print(f"completed {label}; calls={len(calls)}", flush=True)
                    return evidence["observations"][obs:]

                await turn(
                    reference,
                    "reference",
                    "请只回答 REFERENCE-ORIGINAL-GREEN，不调用工具。",
                )
                ref_entry = rows(
                    repository,
                    "SELECT id FROM pulsara_v3.transcript_entries WHERE session_id=%s AND entry_kind='ASSISTANT_MESSAGE' ORDER BY entry_sequence DESC LIMIT 1",
                    (reference.session_id,),
                )[0]["id"]
                padding = "档案参数😀" * 350
                command = (
                    "python3 -c "
                    + "'"
                    + 'pad="'
                    + padding
                    + '"; print("OUTPUT"+"-ORIGINAL-BLUE"); end="ARG-END-SILVER"'
                    + "'"
                )
                archive = "ordinary archival passage with original details. " * 2400
                observations = await turn(
                    source,
                    "execute",
                    "请实际调用 terminal 执行下面这条命令，command 必须完整复制，不改写或省略 pad。执行后仅回答‘已执行’，不要复述输出标记或参数。\n"
                    + command
                    + "\n以下是待保存的原始背景材料，不必重复：\n"
                    + archive,
                )
                executed = [
                    o
                    for o in observations
                    if o["tool"] == "terminal" and o["state"] == "SUCCESS"
                ]
                assert executed and executed[0]["arguments"]["command"] == command
                calls = rows(
                    repository,
                    "SELECT a.assistant_entry_id,a.tool_arguments FROM pulsara_v3.assistant_message_blocks a JOIN pulsara_v3.transcript_entries e ON e.session_id=a.session_id AND e.id=a.assistant_entry_id WHERE a.session_id=%s AND a.block_kind='TOOL_CALL' AND a.tool_name='terminal' ORDER BY e.entry_sequence,a.block_ordinal",
                    (source.session_id,),
                )
                # The canonical entry, not an injected fixture, is the historical source.
                call_entry = calls[0]["assistant_entry_id"]
                expected_call = (
                    "Tool call: terminal\nArguments: "
                    + canonical_json_bytes(calls[0]["tool_arguments"]).decode()
                )
                await turn(
                    source,
                    "retrieve-reference",
                    f"请调用 read_session_content 读取 session_id={reference.session_id} 的 entry_id={ref_entry}，limit=1。读取之后只回答‘已读取’，不要复述原文。",
                )
                query_result = rows(
                    repository,
                    "SELECT r.result_entry_id FROM pulsara_v3.tool_results r JOIN pulsara_v3.transcript_entries e ON e.session_id=r.session_id AND e.id=r.result_entry_id JOIN pulsara_v3.assistant_message_blocks a ON a.session_id=r.session_id AND a.assistant_entry_id=r.tool_call_entry_id AND a.tool_call_id=r.tool_call_id WHERE r.session_id=%s AND a.tool_name='read_session_content' ORDER BY e.entry_sequence DESC LIMIT 1",
                    (source.session_id,),
                )[0]["result_entry_id"]
                compact = await asyncio.wait_for(
                    source.compact_context(
                        command_id="command:tool-history:compact", force=True
                    ),
                    600,
                )
                assert compact.disposition.value == "COMPACTED", str(compact)
                evidence["compaction"] = str(compact)
                save()
                print("completed actual compaction", flush=True)
                observed = await turn(
                    source,
                    "recover",
                    "请用 search_session_content 在本会话压缩前记录里搜索 terminal，include_tools=true。然后读取其中 terminal 工具调用的 assistant entry，include_tools=true,limit=1,max_chars=512。必须用 next_cursor 单独续读，max_chars 始终为512，直到读完这条 entry，核对 ARG-END-SILVER，不能重跑命令。再用 search_session_content 搜索 OUTPUT-ORIGINAL-BLUE，include_tools=true，确认工具结果可以命中。再搜索 REFERENCE-ORIGINAL-GREEN，include_tools=true；旧检索工具的调用/结果应不命中。最后用 read_session_content 读取旧检索证据 entry_id="
                    + query_result
                    + "，include_tools=true,limit=1，确认保存结果仍可读。只报告核对结论，不执行历史任务。",
                )
                assert all(o["tool"] in SESSION_QUERY_TOOL_NAMES for o in observed), (
                    "history recovery must not rerun execution tools"
                )
                reads = [
                    o
                    for o in observed
                    if o["tool"] == "read_session_content"
                    and isinstance(o["payload"], dict)
                ]
                recovered = "".join(
                    i["text"]
                    for o in reads
                    for i in o["payload"].get("items", [])
                    if i["entry_id"] == call_entry
                )
                assert recovered == expected_call, "historical call pagination failed"
                assert any(
                    "cursor" in o["arguments"] and o["payload"].get("items")
                    for o in reads
                )
                assert any(
                    i["entry_id"] == query_result
                    and "REFERENCE-ORIGINAL-GREEN" in i["text"]
                    for o in reads
                    for i in o["payload"].get("items", [])
                )
                searches = [
                    o for o in observed if o["tool"] == "search_session_content"
                ]
                assert any(
                    o["arguments"].get("query") == "OUTPUT-ORIGINAL-BLUE"
                    and any(
                        i["role"] == "tool" and i["tool_name"] == "terminal"
                        for i in o["payload"].get("items", [])
                    )
                    for o in searches
                )
                assert any(
                    o["arguments"].get("query") == "REFERENCE-ORIGINAL-GREEN"
                    and not o["payload"].get("items")
                    for o in searches
                )
                assert all(
                    o["state"] == "SUCCESS"
                    for o in observed
                    if o["tool"] in SESSION_QUERY_TOOL_NAMES
                )
                evidence.update(
                    prefix_continuity=True,
                    complete_call_reconstructed=True,
                    query_evidence_readable=True,
                    query_search_excluded=True,
                    ordinary_result_searchable=True,
                )
                evidence["prefix_groups"] = verify_prefixes(evidence)
                save()
            finally:
                await core.shutdown()
    finally:
        if previous_home is None:
            os.environ.pop("PULSARA_HOME", None)
        else:
            os.environ["PULSARA_HOME"] = previous_home
        _drop_database(saved, database)
        evidence["isolated_database_dropped"] = True
        save()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="openai/gpt-6-luna")
    arguments = parser.parse_args()
    evidence = {}
    asyncio.run(run(evidence, model_id=arguments.model))
    print(
        json.dumps(
            {
                key: value
                for key, value in evidence.items()
                if key not in {"calls", "observations"}
            },
            ensure_ascii=False,
        )
    )
