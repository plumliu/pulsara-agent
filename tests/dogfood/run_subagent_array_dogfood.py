"""Observe real saved DeepSeek using array dispatch, waits and Agent reuse.

Preparation is offline by default. Run with --execute only after implementation
and independent review are ready. The model receives ordinary task requests, not
an API script. Cognitive observations are evidence, not exact-call assertions.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
from collections.abc import Mapping
from contextlib import ExitStack
from copy import copy, deepcopy
from dataclasses import asdict, fields, is_dataclass, replace
import json
import logging
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
import traceback
from unittest.mock import patch

from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.conversation_kernel.direct_model import DirectKernelModelPort
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.identity import require_id, random_id
from pulsara_agent.llm.input import PromptContent
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, ModelsDevCatalogClient
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.settings import LocalPostgresConfig, LocalSettingsStore
from pulsara_agent.workspace_identity import HostWorkspaceInput
from tests.dogfood.run_compaction_request_start_dogfood import _rows
from tests.dogfood.run_model_switch_handover_dogfood import (
    _ReadOnlySettingsStore,
    _RecordingModelRuntime,
    _RecordingTransport,
    _binding,
)
from tests.dogfood.run_stop_chain_dogfood import Evidence, _database_preflight, _now
from tests.support.postgres_database import (
    create_migrated_postgres_test_database,
    drop_postgres_test_database,
)
from tests.test_agent_code_mode_execution import _formal_cell_rows


FIXTURES = {
    "README.md": "# 分页导出审查\n需求在 requirements.md，实现是 paging.py。请只审查，不修改实现。\n",
    "requirements.md": (
        "导出输入是按创建顺序排列的记录，id 是唯一整数，允许为 0；不要重新排序。\n"
        "游标 after_id 为空表示从第一条开始，否则从该 ID 后面继续。\n"
        "游标 ID 不存在时应报错。size 必须为正整数；bool 不是有效 size。\n"
        "返回最多 size 条；只有后面还有记录时才返回下一页游标，否则 next_cursor 为 None。\n"
        "输入记录不能被修改。\n"
    ),
    "paging.py": (
        "def page(records, size, after_id=None):\n"
        "    if size < 1:\n        raise ValueError('size must be positive')\n"
        "    start = 0\n"
        "    if after_id:\n"
        "        start = next((i + 1 for i, row in enumerate(records) if row['id'] == after_id), 0)\n"
        "    selected = records[start:start + size]\n"
        "    cursor = selected[-1]['id'] if len(selected) == size else None\n"
        "    return {'items': selected, 'next_cursor': cursor}\n"
    ),
}

PHASES = (
    (
        "initial_review",
        "请帮我审查工作区这个小型分页导出实现。让两个子代理分工：一位检查实现与需求是否一致，"
        "另一位独立检查容易遗漏的边界情况。给每项工作一个简短清楚的任务标题。"
        "如果两人发现值得核对的分歧，让他们直接交流。等两人的实际结果出来后，"
        "你汇总问题、具体例子和不确定点。这里只做审查，不改实现，也不用联网查资料。",
    ),
    (
        "reuse_followup",
        "继续刚才的审查，不要另建代理。复用刚才负责边界检查的那个子代理，"
        "给它一项新工作：针对 id=0、找不到游标、恰好满一页这三种情况，"
        "补充最小输入、预期结果和修复后应保留的行为。"
        "再让刚才负责需求核对的代理在前一项工作完成后复核这些补充，"
        "指出有没有与需求矛盾的地方。这两项新工作各用自己的简短任务标题。"
        "将这两项新工作一起派发，并声明前一项完成后才能开始后一项的依赖。"
        "你等实际复核结果后，将最终建议写入 review.md，并告诉我哪些建议来自后续复核。仍不要修改实现。",
    ),
)


def _evidence_value(value):
    """Copy diagnostic containers, including frozen production values, without pickle."""
    if isinstance(value, Mapping):
        return {key: _evidence_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_evidence_value(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _evidence_value(getattr(value, field.name)) for field in fields(value)}
    # Scalar/immutable leaves retain Evidence's existing JSON encoding.
    return value


async def _save(evidence):
    # Capture on the event loop, then serialize, scrub and write off-loop. SDK
    # and logging observers may keep adding evidence while the write is running.
    snapshot = copy(evidence)
    snapshot.data = _evidence_value(evidence.data)
    snapshot.destination = evidence.destination.with_suffix(".writing")

    def write():
        snapshot.save()
        snapshot.destination.replace(evidence.destination)

    await asyncio.to_thread(write)


def _observe_sessions(stack, core):
    """Retain actual owners even after renewal failure removes them from Core."""
    observed = []
    original = core._register_prepared_session

    async def registered(session, capability_baseline):
        observed.append(session)
        return await original(session, capability_baseline)

    stack.enter_context(patch.object(core, "_register_prepared_session", registered))
    return observed


def _decoded(value):
    if value is None:
        return None
    text = bytes(value).decode("utf-8") if not isinstance(value, str) else value
    try:
        return json.loads(text)
    except ValueError:
        return text


def _works(root):
    agents = root.repository.list_root_agent_session_ids(
        root_session_id=root.session_id, deadline_monotonic=monotonic() + 30
    )
    rows = []
    for agent in agents:
        cursor = None
        while True:
            page = root.repository.list_agent_works(
                root_session_id=root.session_id,
                agent_id=agent,
                cursor=cursor,
                limit=50,
                deadline_monotonic=monotonic() + 30,
            )
            rows.extend(page)
            if len(page) < 50:
                break
            cursor = page[-1]["cursor"]
    return list(agents), rows


def _snapshot(root):
    agents, works = _works(root)
    sessions = [root.session_id, *agents]
    tools = _rows(
        root,
        """
        SELECT i.session_id,i.id AS invocation_id,i.turn_id,i.source_kind,
               i.source_exec_attempt_id,i.call_sequence,
               COALESCE(i.tool_name,b.tool_name) AS tool_name,
               COALESCE(i.original_arguments,b.tool_arguments) AS arguments,
               r.result_state,r.attempt_id,r.result_facts,r.output_source_coverage,
               r.output_unavailability_reason,
               COALESCE(r.output_body_inline_content,body.body) AS result_body
        FROM pulsara_v3.tool_invocations i
        LEFT JOIN pulsara_v3.assistant_message_blocks b ON b.session_id=i.session_id
          AND b.assistant_entry_id=i.assistant_entry_id AND b.tool_call_id=i.tool_call_id
        LEFT JOIN pulsara_v3.tool_results r ON r.session_id=i.session_id AND r.invocation_id=i.id
        LEFT JOIN pulsara_v3.blobs body ON body.id=r.output_body_blob_id AND body.workspace_id=r.workspace_id
        WHERE i.session_id=ANY(%s)
        ORDER BY r.accepted_at NULLS LAST,i.session_id,i.call_sequence NULLS FIRST
    """,
        (sessions,),
    )
    for tool in tools:
        tool["result"] = _decoded(tool.pop("result_body"))
    sources = []
    for tool in tools:
        if tool["tool_name"] == "exec_cell" and tool["attempt_id"]:
            sources.append(
                {
                    "session_id": tool["session_id"],
                    "attempt_id": tool["attempt_id"],
                    "source": root.repository.read_code_mode_source(
                        session_id=tool["session_id"],
                        attempt_id=tool["attempt_id"],
                        deadline_monotonic=monotonic() + 30,
                    ),
                }
            )
    entries = _rows(
        root,
        """
        SELECT e.session_id,e.id,e.turn_id,e.entry_sequence,e.entry_kind,
               COALESCE(e.inline_content,b.body) AS content
        FROM pulsara_v3.transcript_entries e
        LEFT JOIN pulsara_v3.blobs b ON b.id=e.blob_id AND b.workspace_id=e.workspace_id
        WHERE e.session_id=ANY(%s) ORDER BY e.session_id,e.entry_sequence
    """,
        (sessions,),
    )
    for entry in entries:
        entry["content"] = _decoded(entry["content"])
    outputs = [
        {"queue_item_id": row["id"], "status": row["status"], "record": record}
        for row, record in _formal_cell_rows(root)
    ]
    return {
        "agent_ids": agents,
        "works": works,
        "tools": tools,
        "cell_sources": sources,
        "formal_cell_outputs": outputs,
        "entries": entries,
    }


async def _settle(root, core):
    """Observe real owners without dictating logs, scripts, retries or call counts."""

    def idle():
        return all(
            session._active_task is None and not session.code_mode_cells()
            for session in tuple(core._sessions.values())
        )

    while True:
        if not idle():
            await asyncio.sleep(0.1)
            continue
        _, works = await asyncio.to_thread(_works, root)
        outputs = await asyncio.to_thread(_formal_cell_rows, root)
        if (
            all(row["status"] in {"completed", "stopped"} for row in works)
            and idle()
            and all(row["status"] != "PENDING" for row, _ in outputs)
        ):
            return
        await asyncio.sleep(0.1)


class _ObservedExecution:
    def __init__(self, delegate, record):
        self.delegate, self.record = delegate, record

    async def read_next(self):
        try:
            item = await self.delegate.read_next()
            self.record.setdefault("normalized_events", []).append(
                {"at": _now(), "value": item}
            )
            return item
        except BaseException as error:
            self.record["read_error"] = {
                "type": type(error).__name__,
                "message": str(error),
            }
            raise

    async def request_cancel(self, *, reason):
        self.record.setdefault("cancellations", []).append(
            {"at": _now(), "reason": reason}
        )
        return await self.delegate.request_cancel(reason=reason)

    async def aclose(self):
        try:
            return await self.delegate.aclose()
        finally:
            self.record["close_returned_or_raised_at"] = _now()

    async def wait_physical_completion(self):
        result = await self.delegate.wait_physical_completion()
        self.record["physical_completion"] = result
        self.record["physical_completion_at"] = _now()
        return result


def _install_observers(stack, evidence, active):
    """Delegate SDK and production owners unchanged; retain their actual values."""
    import openai
    from openai.resources.chat.completions import AsyncCompletions
    from openai.resources.responses import AsyncResponses

    preflights, sdk_clients = {}, {}
    original_preflight = DirectKernelModelPort.preflight_execution
    original_open = _RecordingTransport.open_stream
    original_process = openai.AsyncOpenAI._process_response_data

    def preflight(owner, request, **kwargs):
        execution = original_preflight(owner, request, **kwargs)
        preflights[request.prepared_call.call.resolved_model_call_id] = {
            "session_id": request.session_id,
            "turn_id": request.turn_id,
            "epoch_nonce": execution.append_candidate.epoch_nonce,
            "scope": request.compiled_input.canonical_input_identity.conversation_scope_kind.value,
            "system": request.wire_input_plan.materialization.root_policy_value,
            "tools": request.wire_input_plan.materialization.tool_items,
            "messages": request.wire_input_plan.materialization.ordered_input_items,
            "native_tools": [tool.name for tool in request.compiled_input.tools],
        }
        return execution

    def opened(owner, *, call, context):
        execution = original_open(owner, call=call, context=context)
        record = evidence.data["calls"][-1]
        record.update(phase=active["phase"], opened_at=_now())
        if call.resolved_model_call_id in preflights:
            record["installed_input"] = preflights.pop(call.resolved_model_call_id)
        return _ObservedExecution(execution, record)

    def process(owner, *, data, **kwargs):
        record = sdk_clients.get(owner)
        if record is not None:
            record["raw_response_items"].append(deepcopy(data))
        return original_process(owner, data=data, **kwargs)

    def create_observer(original, wire):
        async def create(owner, **kwargs):
            record = {
                "wire": wire,
                "phase": active["phase"],
                "started_at": _now(),
                "request": kwargs,
                "raw_response_items": [],
            }
            evidence.data["sdk_requests"].append(record)
            sdk_clients[owner._client] = record
            try:
                result = await original(owner, **kwargs)
                record["sdk_open_returned_at"] = _now()
                return result
            except BaseException as error:
                record["error"] = {"type": type(error).__name__, "message": str(error)}
                raise

        return create

    stack.enter_context(
        patch.object(DirectKernelModelPort, "preflight_execution", preflight)
    )
    stack.enter_context(patch.object(_RecordingTransport, "open_stream", opened))
    stack.enter_context(
        patch.object(openai.AsyncOpenAI, "_process_response_data", process)
    )
    stack.enter_context(
        patch.object(
            AsyncCompletions, "create", create_observer(AsyncCompletions.create, "chat")
        )
    )
    stack.enter_context(
        patch.object(
            AsyncResponses,
            "create",
            create_observer(AsyncResponses.create, "responses"),
        )
    )


def _invariants(evidence, root_id):
    previous, checks = {}, []
    agent_calls = [
        call for call in evidence.data["calls"] if call["purpose"] == "agent_model_loop"
    ]
    assert agent_calls, "Real agent model calls were not observed"
    assert all(call.get("installed_input") is not None for call in agent_calls), (
        "An agent model call was not matched to its installed preflight input"
    )
    assert any(call["installed_input"]["session_id"] == root_id for call in agent_calls)
    for call in evidence.data["calls"]:
        value = call.get("installed_input")
        if value is None:
            continue  # Auxiliary owners have their own input contracts.
        names = set(value["native_tools"])
        forbidden = {"run_agent", "decide", "wait_agent", "wait_work"} | (
            {"wait_message"}
            if value["session_id"] == root_id
            else {"exec_cell", "stop_agent"}
        )
        assert not names & forbidden, (value["session_id"], names & forbidden)
        key = value["session_id"], value["epoch_nonce"]
        old = previous.get(key)
        if old is not None:
            assert (
                value["system"] == old["system"] and value["tools"] == old["tools"]
            ), key
            assert value["messages"][: len(old["messages"])] == old["messages"], key
        previous[key] = value
        checks.append(
            {
                "session_id": key[0],
                "epoch_nonce": key[1],
                "call": call["sequence"],
                "prefix_extends_previous": old is not None,
                "native_matrix_valid": True,
            }
        )
    for phase in evidence.data["phases"]:
        snapshot = phase.get("snapshot", {})
        work_by_id = {work["work_id"]: work for work in snapshot.get("works", [])}
        for work in work_by_id.values():
            require_id(work["agent_id"], "session")
            require_id(work["work_id"], "queue")
        for tool in snapshot.get("tools", []):
            if tool["result_state"] != "SUCCESS":
                continue
            if tool["tool_name"] in {"run_agent", "exec_cell", "stop_agent", "decide"}:
                assert tool["session_id"] == root_id
            if tool["tool_name"] == "wait_agent":
                assert tool["session_id"] == root_id
                assert tool["source_kind"] == "CODE_MODE_CALL"
                assert "agents" in tool["arguments"] and "works" not in tool["arguments"]
                assert tool["arguments"].get("until", "idle") in {"idle", "work_end"}
            if tool["tool_name"] == "wait_message":
                assert tool["session_id"] != root_id
                assert set(tool["arguments"]) <= {"timeout_seconds"}
            if tool["tool_name"] == "run_agent":
                assert (
                    tool["source_kind"] == "CODE_MODE_CALL"
                    and tool["session_id"] == root_id
                )
                result = tool["result"]
                assert (
                    len(result["agents"])
                    == len(result["works"])
                    == len(result["details"])
                )
                for agent, work, detail in zip(
                    result["agents"], result["works"], result["details"], strict=True
                ):
                    canonical = work_by_id[work["work_id"]]
                    assert canonical["agent_id"] == agent["agent_id"]
                    assert canonical["work_title"] == detail["work_title"]
                    assert detail["inspect"]["arguments"]["target"] == work
    evidence.data["hard_checks"] = checks
    evidence.data["prefix_coverage"] = {
        "agent_model_calls": len(agent_calls),
        "matched_installed_inputs": len(checks),
        "same_epoch_prefix_pairs": sum(
            item["prefix_extends_previous"] for item in checks
        ),
        "session_epoch_groups": [
            {"session_id": session, "epoch_nonce": epoch} for session, epoch in previous
        ],
        "boundary_scope": "Exact within-epoch continuity only; a new epoch is not itself evidence of authorized compaction adoption.",
    }


def _observations(phases):
    """Report what occurred; do not convert natural model choices into a pass rate."""
    observations = []
    previous_works, previous_agents, previous_cells = set(), set(), set()
    agent_dispatch_cells = {}
    for phase in phases:
        snapshot = phase.get("snapshot", {})
        works = snapshot.get("works", [])
        new = [work for work in works if work["work_id"] not in previous_works]
        sources = snapshot.get("cell_sources", [])
        new_sources = [
            item for item in sources if item["attempt_id"] not in previous_cells
        ]
        tools = snapshot.get("tools", [])
        dispatches = []
        for item in tools:
            cell = item["source_exec_attempt_id"]
            if item["tool_name"] != "run_agent" or cell in previous_cells:
                continue
            for member in item["arguments"].get("members", []):
                parameters = member["parameters"]
                earlier = sorted(agent_dispatch_cells.get(member["agent_id"], set()))
                dispatches.append(
                    {
                        "source_exec_attempt_id": cell,
                        "agent_id": member["agent_id"],
                        "work_id": member["work_id"],
                        "target": parameters.get("target"),
                        "depends_on": parameters.get("depends_on", []),
                        "prior_dispatch_cells": earlier,
                        "explicit_cross_cell_reuse": bool(parameters.get("target"))
                        and any(prior != cell for prior in earlier),
                        "result_state": item["result_state"],
                    }
                )
                if item["result_state"] == "SUCCESS":
                    agent_dispatch_cells.setdefault(member["agent_id"], set()).add(cell)
        observations.append(
            {
                "phase": phase["name"],
                "new_work_ids": [work["work_id"] for work in new],
                "reused_agent_ids": sorted(
                    {work["agent_id"] for work in new} & previous_agents
                ),
                "new_agent_ids": sorted(
                    {work["agent_id"] for work in new} - previous_agents
                ),
                "new_cell_attempt_ids": [item["attempt_id"] for item in new_sources],
                "dispatch_observations": dispatches,
                "legacy_helper_text_in_new_sources": [
                    item["attempt_id"]
                    for item in new_sources
                    if any(
                        token in (item["source"] or "")
                        for token in ("run_agent.batch", "wait_agent.works")
                    )
                ],
                "work_titles": [
                    {"work_id": work["work_id"], "title": work.get("work_title")}
                    for work in new
                ],
                "wait_calls": [
                    {
                        key: item[key]
                        for key in (
                            "session_id",
                            "source_kind",
                            "arguments",
                            "result_state",
                            "result",
                        )
                    }
                    for item in tools
                    if item["tool_name"] in {"wait_agent", "wait_message"}
                ],
                "tool_errors": [
                    item
                    for item in tools
                    if item["result_state"] not in {None, "SUCCESS"}
                ],
                "assessment": "Requires reading actual sources/results; no exact-call or wording assertions.",
            }
        )
        previous_works.update(work["work_id"] for work in works)
        previous_agents.update(snapshot.get("agent_ids", []))
        previous_cells.update(item["attempt_id"] for item in sources)
    return observations


def _workspace_files(workspace):
    files = {}
    for path in workspace.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        body = path.read_bytes()
        try:
            value = {"encoding": "utf-8", "content": body.decode("utf-8")}
        except UnicodeDecodeError:
            value = {
                "encoding": "base64",
                "content": base64.b64encode(body).decode("ascii"),
            }
        files[str(path.relative_to(workspace))] = value
    return files


async def run(args):
    home = require_pulsara_home()
    saved = LocalSettingsStore(home / "local-settings.yaml").read()
    selected = [
        connection
        for connection in saved.model_connections
        if connection.target.route_id == "deepseek"
        and connection.target.wire_api.value == args.wire
        and (args.model is None or connection.target.model_id == args.model)
    ]
    if len(selected) != 1 or saved.model_api_key(selected[0].id) is None:
        raise RuntimeError(
            "Select one saved DeepSeek connection with a saved credential using --model/--wire."
        )
    connection = selected[0]
    destination = args.output.resolve()
    if destination.exists():
        raise RuntimeError(f"Evidence already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    evidence = Evidence(destination, saved, "https://api.deepseek.com")
    evidence.data.update(
        phases=[],
        saved_home=str(home),
        model=connection.target.model_id,
        wire=args.wire,
        prompts=[{"name": name, "text": text} for name, text in PHASES],
        fixtures=FIXTURES,
        scope="Real saved DeepSeek ROOT and subagents; ordinary production tools and Skill; no scripted model.",
        isolation="Temporary workspace/home and disposable database; saved settings otherwise unchanged.",
        observation_budget_seconds=args.timeout,
    )
    if not args.execute:
        evidence.data.update(status="prepared_offline", finished_at=_now())
        await _save(evidence)
        return evidence

    prior_env = {
        key: os.environ.get(key)
        for key in (
            "PULSARA_HOME",
            "PULSARA_POSTGRES_ADMIN_DSN",
            "PULSARA_POSTGRES_DSN",
        )
    }
    logger, database, admin = logging.getLogger(), None, None
    handlers = logger.handlers[:]
    logger.handlers = [evidence]
    try:
        admin, runtime_dsn, facts = await asyncio.to_thread(
            _database_preflight, home, args.postgres_config
        )
        evidence.data["database_preflight"] = facts
        os.environ.update(
            PULSARA_POSTGRES_ADMIN_DSN=admin, PULSARA_POSTGRES_DSN=runtime_dsn
        )
        database = await asyncio.to_thread(create_migrated_postgres_test_database)
        evidence.data["isolated_database"] = asdict(database)
        with (
            TemporaryDirectory(prefix="pulsara-subagent-array-") as temporary,
            ExitStack() as stack,
        ):
            directory = Path(temporary)
            workspace, temporary_home = directory / "workspace", directory / "home"
            workspace.mkdir()
            temporary_home.mkdir()
            for name, text in FIXTURES.items():
                (workspace / name).write_text(text, encoding="utf-8")
            os.environ["PULSARA_HOME"] = str(temporary_home)
            settings = _ReadOnlySettingsStore(
                replace(
                    saved,
                    postgres=LocalPostgresConfig(
                        database.runtime_dsn, database.admin_dsn
                    ),
                )
            )
            catalog = ModelCatalogOwner(ModelsDevCatalogClient())
            await catalog.refresh()
            native = ModelRuntime.production(settings=settings, catalog=catalog)
            runtime = _RecordingModelRuntime(native, evidence.data["calls"])
            active = {"phase": "opening"}
            _install_observers(stack, evidence, active)
            core = KernelHostCore.production(model_runtime=runtime)
            observed_sessions = _observe_sessions(stack, core)
            root = None
            try:
                root = await core.open_session(
                    HostWorkspaceInput(
                        workspace_kind="project",
                        workspace_root=workspace,
                        trust_workspace_mcp_config=False,
                    )
                )
                await root.update_model_call_binding(_binding(native, connection))
                evidence.data["root_session_id"] = root.session_id
                for name, text in PHASES:
                    active["phase"] = name
                    phase = {"name": name, "prompt": text, "started_at": _now()}
                    evidence.data["phases"].append(phase)
                    await _save(evidence)
                    try:
                        async with asyncio.timeout(args.timeout):
                            result = await root.run_turn(
                                PromptContent.text(text),
                                command_id=random_id("command"),
                                requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
                            )
                            phase["initial_turn_result"] = result
                            await _settle(root, core)
                    finally:
                        phase["snapshot"] = await asyncio.to_thread(_snapshot, root)
                        phase["finished_at"] = _now()
                        await _save(evidence)
                evidence.data["observations"] = _observations(evidence.data["phases"])
                _invariants(evidence, root.session_id)
                evidence.data["status"] = "completed_for_review"
            finally:
                try:
                    if root is not None:
                        evidence.data["before_shutdown"] = await asyncio.to_thread(
                            _snapshot, root
                        )
                finally:
                    try:
                        await core.shutdown()
                    finally:
                        evidence.data["physical_close"] = [
                            {
                                "session_id": session.session_id,
                                "host_session_id": session.host_session_id,
                                "complete": session.physical_close_complete,
                                "children_stopped": session.local_children_stopped,
                            }
                            for session in observed_sessions
                        ]
                        evidence.data["workspace_files"] = _workspace_files(workspace)
                assert root is None or root in observed_sessions
                assert all(
                    item["complete"] and item["children_stopped"]
                    for item in evidence.data["physical_close"]
                )
    except BaseException:
        evidence.data.update(status="failed", failure=traceback.format_exc())
    finally:
        try:
            if database is not None:
                await asyncio.to_thread(
                    drop_postgres_test_database, admin, database.database_name
                )
                evidence.data["isolated_database_dropped"] = True
        except BaseException:
            evidence.data.update(
                status="failed", database_cleanup_failure=traceback.format_exc()
            )
        finally:
            for key, value in prior_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            evidence.data["finished_at"] = _now()
            try:
                await _save(evidence)
            finally:
                logger.handlers = handlers
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Perform real provider requests after review.",
    )
    parser.add_argument(
        "--output", type=Path, default=Path("output/subagent-array-dogfood/report.json")
    )
    parser.add_argument(
        "--postgres-config",
        type=Path,
        default=Path("output/subagent-array-implementation/postgres.json"),
    )
    parser.add_argument(
        "--wire",
        choices=("openai_chat_completions", "openai_responses"),
        default="openai_chat_completions",
    )
    parser.add_argument(
        "--model", help="Exact saved DeepSeek model if multiple connections match."
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=600,
        help="Per-phase observation budget, not a product lifetime cap.",
    )
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    evidence = asyncio.run(run(args))
    print(
        json.dumps(
            {"status": evidence.data["status"], "report": str(evidence.destination)},
            ensure_ascii=False,
        )
    )
    return (
        0
        if evidence.data["status"] in {"prepared_offline", "completed_for_review"}
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
