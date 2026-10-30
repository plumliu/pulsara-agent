"""Real saved DeepSeek activation with local barriers and optional stream faults.

Run with ``.venv/bin/python -m scripts.run_unified_input_retry_dogfood``.
No model output is fabricated. Faults are explicitly local SDK-consumer faults;
the production connection, protocol, credentials and proxy remain unchanged.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import asdict, fields, is_dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
import json
import logging
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
import traceback
from unittest.mock import patch

import httpx2
import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.conversation_kernel.host import KernelHostCore, KernelHostSession
from pulsara_agent.conversation_kernel.direct_model import DirectKernelModelPort
from pulsara_agent.conversation_kernel._repository.prompts import hydrate_queued_input_owner
from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
from pulsara_agent.desktop_app.session_controller import LocalSessionController
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import LLMTextPart, PromptAnnotationPart, PromptAnnotationSource, PromptContent
from pulsara_agent.llm.adapters.openai.retrying import expand_provider_diagnostic_scrubber
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, ModelsDevCatalogClient
from pulsara_agent.llm.model_connections import ModelCallBinding, ReasoningEffortSelection
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.model_input.continuity import ProviderInputContinuityScope
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.ports.terminal_observation import CellOutputContent
from pulsara_agent.scheduling.service import ScheduledTaskService
from pulsara_agent.settings import LocalPostgresConfig, LocalSettingsStore
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from pulsara_agent.tool_permission import default_permission_policy
from pulsara_agent.workspace_identity import HostWorkspaceInput
from tests.dogfood.run_compaction_request_start_dogfood import _rows
from tests.dogfood.run_content_revision_line_edit_dogfood import _tool_trace
from tests.dogfood.run_known_agents_handoff_dogfood import eventually
from tests.dogfood.run_model_switch_handover_dogfood import (
    _ReadOnlySettingsStore,
    _RecordingModelRuntime,
    _RecordingTransport,
)
from tests.support.postgres_database import (
    admin_root_dsn,
    create_migrated_postgres_test_database,
    drop_postgres_test_database,
)


def now():
    return datetime.now(timezone.utc).isoformat()


def schedule_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def formal_cell_records(wire_body):
    records = []
    for message in wire_body.get("messages", []):
        content = message.get("content")
        texts = [content] if isinstance(content, str) else [
            part.get("text", "") for part in (content or []) if isinstance(part, dict)
        ]
        for text in texts:
            try:
                value = json.loads(text)
            except (ValueError, TypeError):
                continue
            observation = value.get("pulsara_terminal_observation") if isinstance(value, dict) else None
            if isinstance(observation, dict) and observation.get("schema_version") == "cell_output.v1":
                records.append(observation["record"])
    return records


def public_value(value):
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: getattr(value, item.name) for item in fields(value)
                if item.name != "final_source"}
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return str(value)


class Evidence(logging.Handler):
    """Serialize only after exact saved-secret and encoded-form scrubbing."""

    def __init__(self, destination, saved, endpoint):
        super().__init__()
        self.destination = destination
        # Reuse the production exact-credential owner, including equivalent
        # encoded forms; do not maintain a second diagnostic scrub contract.
        self.scrub = expand_provider_diagnostic_scrubber(
            saved.credential_scrub_set(), endpoint
        )
        self.data = {
            "started_at": now(), "status": "preparing", "calls": [],
            "sdk_attempts": [], "logs": [], "cases": [],
            "fault_contract": "local SDK consumption injection, never an observed upstream fault",
        }

    def emit(self, record):
        self.data["logs"].append({
            "at": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "logger": record.name, "level": record.levelname,
            "message": record.getMessage(),
            "exception": logging.Formatter().formatException(record.exc_info)
            if record.exc_info else None,
        })

    def save(self):
        encoded = json.dumps(self.data, ensure_ascii=False, indent=2, default=public_value)
        safe = self.scrub.scrub_text(encoded)
        assert not self.scrub.contains(safe)
        self.destination.write_text(safe + "\n", encoding="utf-8")


class CapturedSDKStream:
    def __init__(self, delegate, record, fail):
        self.delegate, self.record, self.fail = delegate, record, fail
        self.iterator = delegate.__aiter__()
        self.inject_next = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.inject_next:
            self.inject_next = False
            self.record["local_injected_fault_at"] = now()
            raise httpx2.ReadError("DOGFOOD_LOCAL_CONSUMER_DISCONNECT; upstream fault not claimed")
        try:
            item = await self.iterator.__anext__()
        except StopAsyncIteration:
            self.record["iterator_eof_at"] = now()
            raise
        except BaseException as exc:
            self.record["iterator_error"] = exception_chain(exc)
            raise
        raw = item.model_dump(mode="json")
        self.record["events"].append({"at": now(), "event": raw})
        semantic = any(
            choice.get("delta", {}).get(field)
            for choice in raw.get("choices", [])
            for field in ("content", "reasoning_content", "tool_calls")
        )
        if self.fail and semantic:
            self.inject_next = True
        return item

    async def close(self):
        self.record["close_started_at"] = now()
        try:
            await self.delegate.close()
        except BaseException as exc:
            self.record["close_error"] = exception_chain(exc)
            raise
        finally:
            self.record["close_finished_at"] = now()
        self.record["closed"] = True


def exception_chain(exc):
    pending, seen, rows = [exc], set(), []
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        rows.append({
            "type": type(item).__name__, "message": str(item),
            "body": getattr(item, "body", None),
            "status": getattr(item, "status_code", None),
            "request_id": getattr(item, "request_id", None),
            "traceback": "".join(traceback.format_exception(item)),
        })
        pending.extend(child for child in (item.__cause__, item.__context__) if child)
    return rows


class FirstReadBarrier:
    def __init__(self, delegate, gate):
        self.delegate, self.gate, self.first = delegate, gate, True

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    async def read_next(self):
        if self.first:
            self.first = False
            await self.gate.wait()
        return await self.delegate.read_next()

    async def request_cancel(self, *, reason):
        self.gate.set()
        await self.delegate.request_cancel(reason=reason)

    async def aclose(self):
        self.gate.set()
        await self.delegate.aclose()


def rows(root, table, columns="*", extra="", values=()):
    return _rows(root, f"SELECT {columns} FROM pulsara_v3.{table} WHERE session_id=%s {extra}",
                 (root.session_id, *values))


def queue(root):
    return rows(root, "prompt_queue_items", extra="ORDER BY queue_sequence,id")


def selected_log(root, row, marker):
    with root.repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR, row_factory=dict_row,
        deadline_monotonic=monotonic() + 30,
    ) as conn:
        exact = conn.execute(
            "SELECT q.*,s.workspace_id FROM pulsara_v3.prompt_queue_items q "
            "JOIN pulsara_v3.sessions s ON s.id=q.session_id "
            "WHERE q.session_id=%s AND q.id=%s", (root.session_id, row["id"])
        ).fetchone()
        content = hydrate_queued_input_owner(conn, row=exact)
    assert isinstance(content, CellOutputContent)
    assert content.record["kind"] == "log", content.canonical_mapping()
    assert marker in str(content.record["text"]), content.canonical_mapping()
    return content.canonical_mapping()


def capture_canonical(root, case):
    members = _rows(root, "SELECT id,root_session_id FROM pulsara_v3.sessions WHERE id=%s OR root_session_id=%s ORDER BY id", (root.session_id, root.session_id))
    scope = [item["id"] for item in members]
    case["sessions"] = members
    case["canonical"] = {
        table: _rows(root, f"SELECT * FROM pulsara_v3.{table} WHERE session_id=ANY(%s)", (scope,))
        for table in ("turns", "transcript_entries", "assistant_message_blocks", "tool_invocations", "tool_execution_attempts", "tool_results", "prompt_queue_items")
    }


def verify_work_material_delivery(report, case):
    proofs = []
    for item in case["canonical"]["prompt_queue_items"]:
        materials = item.get("work_inputs") or []
        if not materials:
            continue
        turns = {turn["id"] for turn in case["canonical"]["turns"]
                 if turn["session_id"] == item["session_id"]
                 and turn["initial_entry_id"] == item["consumed_entry_id"]}
        calls = [(i, call) for i, call in enumerate(report["calls"])
                 if call.get("session_id") == item["session_id"] and call.get("turn_id") in turns]
        assert calls, "accepted Work materials need a matching provider call"
        call_index, call = calls[0]
        messages = call["provider_input"]["messages"]
        for body in {value["body"] for value in materials}:
            expected = sum(value["body"] == body for value in materials)
            assert sum(message.get("role") == "user" and message.get("content") == body
                       for message in messages) >= expected
        actual_sdk = [i for i, attempt in enumerate(report["sdk_attempts"])
                      if attempt.get("wire_body", {}).get("messages") == messages]
        assert actual_sdk, "Work material projection must match actual SDK wire messages"
        proofs.append({"work_id": item["id"], "session_id": item["session_id"],
                       "call_index": call_index, "actual_sdk_attempt_indices": actual_sdk,
                       "expected_materials": materials,
                       "actual_user_messages": [{"index": i, "content": message["content"]}
                                                for i, message in enumerate(messages)
                                                if message.get("role") == "user" and message.get("content") in {value["body"] for value in materials}],
                       "conclusion": "Canonical Work materials were delivered as exact user messages in the actual SDK request; model claims of not seeing them do not establish delivery failure or successful substantive synthesis."})
    return proofs


async def run(destination: Path, selected):
    home = require_pulsara_home()
    saved = LocalSettingsStore(home / "local-settings.yaml").read()
    connection = next((item for item in saved.model_connections
        if item.target.route_id == "deepseek" and item.target.model_id == "deepseek-flash"
        and item.target.wire_api.value == "openai_chat_completions"), None)
    if connection is None or not saved.model_api_key(connection.id):
        raise RuntimeError("missing saved deepseek/deepseek-flash Chat Completions connection or credential")
    if destination.exists():
        raise RuntimeError(f"evidence destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    evidence = Evidence(destination, saved, connection.base_url)
    report = evidence.data
    report.update(saved_home=str(home), connection_id=connection.id.value,
                  model="deepseek-flash", wire_api="openai_chat_completions", reasoning="high")
    # The desktop owner supplies these same socket settings in its startup frame.
    socket = home / "desktop/socket"
    admin = os.environ.get("PULSARA_POSTGRES_ADMIN_DSN") or f"host='{socket}' port=5432 dbname=pulsara user=pulsara_admin"
    runtime_dsn = os.environ.get("PULSARA_POSTGRES_DSN") or f"host='{socket}' port=5432 dbname=pulsara user=pulsara_runtime"
    try:
        for label, dsn in (("admin", admin), ("runtime", runtime_dsn)):
            parts = conninfo_to_dict(dsn)
            expected_user = "pulsara_admin" if label == "admin" else "pulsara_runtime"
            if (parts.get("host") != str(socket) or parts.get("dbname") != "pulsara"
                    or parts.get("port", "5432") != "5432" or parts.get("user") != expected_user):
                raise RuntimeError(f"unverified {label} root: expected exact saved-home desktop Unix socket")
            with psycopg.connect(dsn, connect_timeout=5) as conn:
                actual = conn.execute("SELECT current_database(),current_user,inet_server_addr()::text").fetchone()
                # Only the existing admin role may inspect this server setting;
                # runtime proves its exact DSN/database/role and Unix transport.
                server_socket = conn.execute("SELECT current_setting('unix_socket_directories')").fetchone()[0] if label == "admin" else None
            assert actual[0] == "pulsara" and actual[1] == expected_user and actual[2] is None, actual
            assert label != "admin" or server_socket == str(socket), server_socket
            report.setdefault("database_preflight", []).append({"label": label, "dsn": dsn, "actual": actual, "server_socket": server_socket})
    except BaseException as exc:
        report.update(status="preflight_failed", finished_at=now(), failure=exception_chain(exc))
        evidence.save()
        raise
    prior_env = {key: os.environ.get(key) for key in ("PULSARA_HOME", "PULSARA_POSTGRES_ADMIN_DSN", "PULSARA_POSTGRES_DSN")}
    os.environ["PULSARA_POSTGRES_ADMIN_DSN"] = admin
    os.environ["PULSARA_POSTGRES_DSN"] = runtime_dsn
    database = None
    temporary_root = None
    core = scheduler = None
    root = None
    state = {}
    context_owners = {}
    original_invoke = DirectKernelToolPort.invoke
    original_open = _RecordingTransport.open_stream
    original_admit = KernelHostSession._run_queued_root_admission
    original_preflight = DirectKernelModelPort.preflight_execution
    from openai.resources.chat.completions import AsyncCompletions
    original_create = AsyncCompletions.create

    def captured_preflight(owner, request, **kwargs):
        identity = request.compiled_input.canonical_input_identity
        context_owners[request.compiled_input.context_id] = {
            "session_id": identity.session_id, "turn_id": identity.turn_id,
            "scope": identity.conversation_scope_kind.value,
        }
        return original_preflight(owner, request, **kwargs)

    async def captured_create(owner, **kwargs):
        record = {"started_at": now(), "request": kwargs, "events": []}
        report["sdk_attempts"].append(record)
        text = json.dumps(kwargs, ensure_ascii=False, default=public_value)
        root_tools = any(tool.get("function", {}).get("name") == "run_agent"
                         for tool in kwargs.get("tools", []))
        fail = ("DOGFOOD_FAIL_MIDDLE" in text and not root_tools) or (
            state.get("inject_once") and not state.get("once_injected")
            and "DOGFOOD_RETRY_TARGET" in text
        )
        if fail:
            state["once_injected"] = True
            record["local_fault_plan"] = "raise typed ReadError after real semantic SDK delta"
        try:
            stream = await original_create(owner, **kwargs)
        except BaseException as exc:
            record["opening_error"] = exception_chain(exc)
            record["finished_at"] = now()
            raise
        record["http_status"] = stream.response.status_code
        record["response_headers"] = dict(stream.response.headers)
        record["request_url"] = str(stream.response.request.url)
        record["wire_body"] = json.loads(stream.response.request.content)
        return CapturedSDKStream(stream, record, fail)

    def captured_open(owner, *, call, context):
        if state.get("retry_release") and "DOGFOOD_RETRY_TARGET" in str(context.messages):
            adapter = owner._delegate._adapter
            original_sleep, barrier = adapter.retry_sleep, state["retry_release"]
            if not state.get("sleep_barrier_installed"):
                state["sleep_barrier_installed"] = True
                async def held_sleep(delay):
                    await barrier.wait()
                    await original_sleep(delay)
                adapter.retry_sleep = held_sleep
        result = original_open(owner, call=call, context=context)
        report["calls"][-1].update(context_owners[context.context_id])
        if state.get("child_gate") and "DOGFOOD_CHILD" in json.dumps(
                report["calls"][-1]["provider_input"], ensure_ascii=False):
            if not any(tool.name == "run_agent" for tool in context.tools):
                opened = {"at": now(), **context_owners[context.context_id]}
                report.setdefault("child_opens", []).append(opened)
                state.setdefault("child_open_session_ids", set()).add(opened["session_id"])
                state["child_started"].set()
                return FirstReadBarrier(result, state["child_gate"])
        return result

    async def captured_invoke(owner, **kwargs):
        path = str(kwargs["arguments"].get("target", {}).get("path", ""))
        if kwargs["tool_name"] == "read_file" and path.endswith("cell-release.txt") and state.get("cell_gate"):
            state["cell_started"].set()
            await state["cell_gate"].wait()
        if kwargs["tool_name"] == "write_file" and path.endswith("second.txt") and state.get("batch_gate"):
            state["batch_metadata"] = kwargs
            state["batch_started"].set()
            await state["batch_gate"].wait()
        result = await original_invoke(owner, **kwargs)
        report.setdefault("invocations", []).append({
            "at": now(), "session_id": kwargs["invocation_context"].session_id,
            "name": kwargs["tool_name"], "arguments": kwargs["arguments"],
            "tool_call_id": kwargs["tool_call_id"], "attempt_id": kwargs["attempt_id"],
            "result": result,
        })
        return result

    async def captured_admit(owner, candidate, intent):
        if state.get("queue_gate") and owner.session_id == state.get("root_id"):
            state["queue_started"].set()
            await state["queue_gate"].wait()
        return await original_admit(owner, candidate, intent)

    logger = logging.getLogger("pulsara_agent")
    logger.addHandler(evidence)
    logger.setLevel(logging.DEBUG)
    try:
        database = create_migrated_postgres_test_database()
        report["isolated_database"] = asdict(database)
        temporary_root = TemporaryDirectory(prefix="pulsara-input-retry-")
        with ExitStack() as stack:
            temp = Path(temporary_root.name)
            (temp / "home").mkdir()
            os.environ["PULSARA_HOME"] = str(temp / "home")
            settings = _ReadOnlySettingsStore(replace(saved, postgres=LocalPostgresConfig(database.runtime_dsn, database.admin_dsn)))
            catalog = ModelCatalogOwner(ModelsDevCatalogClient())
            await catalog.refresh()
            native = ModelRuntime.production(settings=settings, catalog=catalog)
            binding = ModelCallBinding(connection.id, ReasoningEffortSelection("high"))
            native.freeze_resolution_snapshot().validate(binding)
            core = KernelHostCore.production(model_runtime=_RecordingModelRuntime(native, report["calls"]))
            sessions = LocalSessionController(core=core, permission_policy=default_permission_policy(), active_skill_names=frozenset())
            scheduler = ScheduledTaskService(sessions)
            core.scheduled_tasks = scheduler
            stack.enter_context(patch.object(AsyncCompletions, "create", captured_create))
            stack.enter_context(patch.object(DirectKernelModelPort, "preflight_execution", captured_preflight))
            stack.enter_context(patch.object(_RecordingTransport, "open_stream", captured_open))
            stack.enter_context(patch.object(DirectKernelToolPort, "invoke", captured_invoke))
            stack.enter_context(patch.object(KernelHostSession, "_run_queued_root_admission", captured_admit))

            async def new_root(name, setup=None):
                path = temp / name
                path.mkdir()
                if setup is not None:
                    setup(path)
                handle = await sessions.create_session(workspace_kind="project", workspace_path=str(path), model_call_binding=binding)
                await handle.session.attach_controller("input-retry-dogfood")
                return handle.session, handle.session.workspace.workspace_root

            async def ask(root, prompt):
                item = {"started_at": now(), "prompt": prompt}
                report.setdefault("turns", []).append(item)
                evidence.save()
                # A harness observation deadline, never a production lifetime cap.
                async with asyncio.timeout(600):
                    result = await root.run_turn(prompt if isinstance(prompt, PromptContent) else PromptContent.text(prompt), requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
                item.update(finished_at=now(), result=result)
                evidence.save()
                return result

            async def execute_code(root, code):
                before = {x["id"] for x in rows(root, "tool_execution_attempts", extra="AND code_source_content_digest IS NOT NULL")}
                start = len(report.get("invocations", []))
                result = await ask(root, "Call exec_cell exactly once with this exact code, without changing it or calling wait. After the accepted receipt, reply IDLE_ARMED and finish immediately.\n" + code)
                calls = [x for x in report.get("invocations", [])[start:] if x["session_id"] == root.session_id and x["name"] == "exec_cell"]
                assert len(calls) == 1 and calls[0]["arguments"].get("code") == code
                attempts = [x for x in rows(root, "tool_execution_attempts", extra="AND code_source_content_digest IS NOT NULL") if x["id"] not in before]
                assert len(attempts) == 1
                actual = await sessions.cell_source(root.session_id, attempts[0]["id"], {})
                assert actual["source"]["text"] == code
                report.setdefault("executed_sources", []).append({
                    "session_id": root.session_id, "attempt_id": attempts[0]["id"],
                    "requested_code": code, "actual_frozen_source": actual,
                })
                return result

            for name in selected:
                state.clear()
                expectations = {
                    "native_work": "native run_agent then wait_agent exposes only status/identity; explicit query_agents returns the complete real Work final without automatic ROOT input",
                    "idle_selected": "parallel facts/risks feed review internally; one selected console record wakes idle ROOT as NEW_TURN; no automatic Work input",
                    "active_output": "two independent console records wait as exact steer while the real two-write batch settles once",
                    "failed_middle": "six locally injected SDK faults stop only middle Work; script queries failure and emits only selected review",
                    "mixed_cancel": "human A, two Cell records, human B, two RUN_ONCE and TIMER share pending NEW_TURN order and cancel independently",
                    "retry": "one local partial-stream fault; close finishes before the identical real HTTP request is repeated successfully",
                    "retry_inputs": "Cell and scheduled steer accepted during backoff do not alter retry wire; next safe point consumes both; one physical write",
                    "stop_retry": "explicit Stop while waiting prevents second HTTP attempt and clears current retry observation",
                    "long_horizon": "one ROOT keeps its epoch and exact prefix through parallel Work/Cell phases before and after eight successful logical requests, then receives a fresh per-request retry allowance",
                    "source_history": "silent code/path Cells retire without new input; frozen source remains readable after file deletion and Host replacement, with cross-ROOT denial",
                    "references": "real human body and annotation comment select known Skill bodies and explicitly read file references; quoted Skill/file references remain inert across an approved close/resume cold boundary",
                }
                case = {"name": name, "expected": expectations[name], "started_at": now(), "status": "running", "call_start": len(report["calls"]), "sdk_start": len(report["sdk_attempts"])}
                report["cases"].append(case)
                def reference_setup(path):
                    for skill, marker in (("ref-body", "REF_BODY_PAYLOAD"), ("ref-comment", "REF_COMMENT_PAYLOAD"), ("ref-quote", "REF_QUOTE_FORBIDDEN_BODY")):
                        directory = path / ".pulsara/skills" / skill
                        directory.mkdir(parents=True)
                        (directory / "SKILL.md").write_text(f"---\nname: {skill}\ndescription: Isolated reference fixture.\n---\n\n{marker}. Read only the files explicitly requested in active human text or comments.\n")
                    for filename, marker in (("body.txt", "REF_BODY_FILE_VALUE"), ("comment.txt", "REF_COMMENT_FILE_VALUE"), ("quote.txt", "REF_QUOTE_FILE_MUST_NOT_READ")):
                        (path / filename).write_text(marker)
                root, workspace = await new_root(name, reference_setup if name == "references" else None)
                state["root_id"] = root.session_id
                case["root_session_id"] = root.session_id
                if name == "native_work":
                    marker = "NATIVE_WORK_REAL_FINAL_7C2"
                    case["result"] = await ask(root,
                        'Use only native tools for this simple delegation. Call run_agent exactly once with '
                        '{"instruction":"Reply exactly ' + marker + '. Do not use tools.","context":{"mode":"none"}}. '
                        'Retain both returned pointers. Call wait_agent with targets:[receipt.agent], settle:"all", '
                        'timeout_seconds:300. Wait again if not settled. Then query_agents with target:receipt.work '
                        'to read the actual complete final. Only after the query returns completed, report that final. '
                        'Do not call exec_cell, terminal, or create another Work.')
                    trace = await asyncio.to_thread(_tool_trace, root)
                    case["tool_trace"] = trace
                    runs = [x for x in trace if x["tool_name"] == "run_agent"]
                    waits = [x for x in trace if x["tool_name"] == "wait_agent"]
                    queries = [x for x in trace if x["tool_name"] == "query_agents"]
                    assert len(runs) == 1 and waits and queries
                    assert not any(x["tool_name"] == "exec_cell" for x in trace)
                    receipt = runs[0]["result"]
                    assert all(x["arguments"]["targets"] == [receipt["agent"]] for x in waits)
                    for wait in waits:
                        assert marker not in json.dumps(wait["result"], ensure_ascii=False)
                        assert all(not ({"instruction", "final", "diagnostic"} & work.keys())
                                   for work in wait["result"].get("works", []))
                    selected = [x for x in queries if x["arguments"].get("target") == receipt["work"]]
                    assert selected and marker in str(selected[-1]["result"]["final"])
                    assert selected[-1]["result"]["status"] == "completed"
                    assert runs[0]["request_sequence"] < waits[0]["request_sequence"] < selected[-1]["request_sequence"]
                    case["queue"] = queue(root)
                    assert not any(x["input_origin"] == "AGENT_MATERIAL" for x in case["queue"])
                    case["wait_without_body_then_explicit_final"] = True
                elif name == "idle_selected":
                    state.update(child_gate=asyncio.Event(), child_started=asyncio.Event())
                    code = '''const work = await tools.run_agent.batch({
facts:{name:"Facts",instruction:"DOGFOOD_CHILD_FACTS. Reply exactly FACTS_INTERNAL_ONLY. No tools."},
risks:{name:"Risks",instruction:"DOGFOOD_CHILD_RISKS. Reply exactly RISKS_INTERNAL_ONLY. No tools."},
review:{name:"Review",depends_on:({facts,risks})=>[facts.work,risks.work],instruction:"DOGFOOD_CHILD_REVIEW. Read both prerequisite results and reply SELECTED_REVIEW_OK. Do not use tools."}
});
await tools.wait_agent.works({targets:[work.review.work]});
const selected=await tools.query_agents({target:work.review.work});
if(selected.status!=="completed"||!String(selected.final).includes("SELECTED_REVIEW_OK"))throw Error("Selected review did not complete with its expected final");
console.log({selected:selected.final,review:work.review});'''
                    case["script"] = code
                    first = await execute_code(root, code)
                    await state["child_started"].wait()
                    assert root.active_root_turn_id() is None, "ROOT must be idle before child release"
                    case["before_release_queue"] = queue(root)
                    case["first_result"] = first
                    state["child_gate"].set()
                    async def selected_consumed():
                        items = queue(root)
                        return items if any(x["input_origin"] == "CELL_OUTPUT" and x["status"] == "CONSUMED" for x in items) and root.active_root_turn_id() is None else None
                    case["queue"] = await eventually(selected_consumed, timeout=600)
                    output = [x for x in case["queue"] if x["input_origin"] == "CELL_OUTPUT"]
                    assert len(output) == 1 and output[0]["delivery_mode"] == "NEW_TURN"
                    case["selected_output"] = selected_log(root, output[0], "SELECTED_REVIEW_OK")
                    assert not any(x["input_origin"] == "AGENT_MATERIAL" for x in case["queue"])
                    case["tool_trace"] = await asyncio.to_thread(_tool_trace, root)
                elif name == "active_output":
                    (workspace / "cell-release.txt").write_text("release barrier")
                    state.update(cell_gate=asyncio.Event(), cell_started=asyncio.Event(), batch_gate=asyncio.Event(), batch_started=asyncio.Event())
                    await execute_code(root, 'await tools.read_file({target:{kind:"PATH",path:"cell-release.txt"}});console.log("CELL_ACTIVE_ONE");console.log("CELL_ACTIVE_TWO");')
                    await state["cell_started"].wait()
                    running = asyncio.create_task(ask(root, 'Call native write_file exactly twice in one single parallel tool-call batch, with these exact argument objects: '
                        '{"target":{"kind":"PATH","path":"first.txt"},"content":"FIRST_REAL_EFFECT\\n"} and '
                        '{"target":{"kind":"PATH","path":"second.txt"},"content":"SECOND_REAL_EFFECT\\n"}. '
                        'Each content ends with exactly one LF newline as encoded in JSON. After both results briefly report; do not repeat the earlier Cell or either write.'))
                    await asyncio.wait_for(state["batch_started"].wait(), 180)
                    turn = root.active_root_turn_id()
                    assert (workspace / "first.txt").read_text() == "FIRST_REAL_EFFECT\n"
                    assert not (workspace / "second.txt").exists()
                    state["cell_gate"].set()
                    async def two_pending():
                        matches = [x for x in queue(root) if x["input_origin"] == "CELL_OUTPUT"]
                        return matches if len(matches) == 2 else None
                    outputs = await eventually(two_pending)
                    assert len({x["id"] for x in outputs}) == 2
                    assert all(x["delivery_mode"] == "STEER_ACTIVE_TURN" and x["target_turn_id"] == turn and x["status"] == "PENDING" for x in outputs)
                    case["while_batch_blocked"] = outputs
                    state["batch_gate"].set()
                    case["result"] = await running
                    case["queue"] = queue(root)
                    assert all(x["status"] == "CONSUMED" for x in case["queue"] if x["id"] in {o["id"] for o in outputs})
                    assert (workspace / "second.txt").read_text() == "SECOND_REAL_EFFECT\n"
                    case["tool_trace"] = await asyncio.to_thread(_tool_trace, root)
                elif name == "retry":
                    state["inject_once"] = True
                    case["result"] = await ask(root, "DOGFOOD_RETRY_TARGET. Reply with exactly RECOVERED_REAL_PROVIDER and no tools.")
                    attempts = report["sdk_attempts"][case["sdk_start"]:]
                    injected = [x for x in attempts if x.get("local_injected_fault_at")]
                    assert len(injected) == 1 and len(attempts) == 2
                    assert attempts[0]["request"] == attempts[1]["request"]
                    assert all(x.get("closed") for x in attempts)
                    assert attempts[0]["close_finished_at"] <= attempts[1]["started_at"]
                    case["identical_wire_requests"] = True
                elif name == "stop_retry":
                    state["inject_once"] = True
                    running = asyncio.create_task(ask(root, "DOGFOOD_RETRY_TARGET. Reply with STOP_CONTROL_TEXT and no tools."))
                    async def waiting():
                        value = root._runner.provider_retry_observation()
                        return value if value and value["phase"] == "waiting" else None
                    case["waiting_observation"] = await eventually(waiting, timeout=180)
                    target = root.active_root_turn_id()
                    case["stop_receipt"] = await root.request_stop_turn(
                        command_id=random_id("command"),
                        admission_deadline_ms=root.control_admission_deadline_ms(),
                        expected_session_id=root.session_id,
                        expected_host_session_id=root.host_session_id,
                        target_turn_id=target,
                    )
                    outcome = (await asyncio.gather(running, return_exceptions=True))[0]
                    case["result"] = exception_chain(outcome) if isinstance(outcome, BaseException) else outcome
                    stopped = rows(root, "turns", extra="AND id=%s", values=(target,))
                    assert len(stopped) == 1 and stopped[0]["status"] == "INTERRUPTED" and stopped[0]["terminal_reason"] == "USER_STOPPED"
                    case["stopped_turn"] = stopped
                    attempts = report["sdk_attempts"][case["sdk_start"]:]
                    assert len(attempts) == 1 and attempts[0].get("closed")
                    case["after_stop_retry_observation"] = root._runner.provider_retry_observation()
                    assert case["after_stop_retry_observation"] is None
                    case["no_retry_after_stop"] = True
                elif name == "retry_inputs":
                    (workspace / "cell-release.txt").write_text("release barrier")
                    state.update(cell_gate=asyncio.Event(), cell_started=asyncio.Event())
                    await execute_code(root, 'await tools.read_file({target:{kind:"PATH",path:"cell-release.txt"}});console.log("CELL_DURING_RETRY");')
                    await state["cell_started"].wait()
                    case["sdk_start"] = len(report["sdk_attempts"])
                    state.update(inject_once=True, retry_release=asyncio.Event())
                    running = asyncio.create_task(ask(root, 'DOGFOOD_RETRY_TARGET. Call write_file exactly once with arguments '
                        '{"target":{"kind":"PATH","path":"retry-effect.txt"},"content":"ONCE_REAL_EFFECT\\n"}. '
                        'The content ends with exactly one LF newline as encoded in JSON. Then report the result. Do not create any other files.'))
                    async def waiting():
                        value = root._runner.provider_retry_observation()
                        return value if value and value["phase"] == "waiting" else None
                    case["waiting_observation"] = await eventually(waiting, timeout=180)
                    target = root.active_root_turn_id()
                    state["cell_gate"].set()
                    task = await scheduler.create(session_id=root.session_id, status="ACTIVE", values={
                        "name": "Retry supplement", "prompt": "SCHEDULE_DURING_RETRY: preserve the one-file operation and acknowledge this supplementary input.",
                        "timezone": "UTC", "permission_mode": "bypass-permissions",
                        "schedule": {"contract": "scheduled-rule:v1", "kind": "interval", "seconds": 3600, "anchor_at_utc": (datetime.now(timezone.utc)+timedelta(hours=1)).replace(microsecond=0).isoformat()},
                    })
                    receipt = await scheduler.run_now({"client_command_id": random_id("command"), "session_id": root.session_id,
                        "task_id": task["id"], "expected_revision": task["revision"], "request_at_utc": schedule_now()})
                    assert receipt["delivery_mode"] == "STEER_ACTIVE_TURN" and receipt["target_turn_id"] == target
                    async def both_pending():
                        values = [x for x in queue(root) if x["target_turn_id"] == target]
                        return values if len(values) == 2 and all(x["status"] == "PENDING" for x in values) else None
                    case["pending_during_backoff"] = await eventually(both_pending)
                    assert root._runner.provider_retry_observation()["phase"] == "waiting"
                    assert not (workspace / "retry-effect.txt").exists()
                    state["retry_release"].set()
                    case["result"] = await running
                    attempts = report["sdk_attempts"][case["sdk_start"]:]
                    assert len(attempts) >= 3 and attempts[0]["wire_body"] == attempts[1]["wire_body"]
                    assert not any(x.get("text") == "CELL_DURING_RETRY" for x in formal_cell_records(attempts[0]["wire_body"]))
                    assert "SCHEDULE_DURING_RETRY" not in json.dumps(attempts[0]["wire_body"])
                    assert any(any(record.get("kind") == "log" and record.get("text") == "CELL_DURING_RETRY" for record in formal_cell_records(x["wire_body"])) and "SCHEDULE_DURING_RETRY" in json.dumps(x["wire_body"]) for x in attempts[2:])
                    case["formal_cell_records_by_attempt"] = [formal_cell_records(x["wire_body"]) for x in attempts]
                    assert (workspace / "retry-effect.txt").read_text() == "ONCE_REAL_EFFECT\n"
                    case["tool_trace"] = await asyncio.to_thread(_tool_trace, root)
                    writes = [x for x in case["tool_trace"] if x["tool_name"] == "write_file" and x["arguments"].get("target", {}).get("path", "").endswith("retry-effect.txt")]
                    assert len(writes) == 1 and writes[0]["result_state"] == "SUCCESS"
                    case["queue"] = queue(root)
                    assert all(x["status"] == "CONSUMED" for x in case["queue"] if x["target_turn_id"] == target)
                    case["frozen_retry_then_safe_point_consumption"] = True
                elif name == "failed_middle":
                    state.update(child_gate=asyncio.Event(), child_started=asyncio.Event())
                    code = '''const work=await tools.run_agent.batch({
facts:{name:"Facts",instruction:"DOGFOOD_CHILD_FACTS. Reply FACTS_FAILED_CASE_INTERNAL. No tools."},
broken:{name:"Middle",instruction:"DOGFOOD_CHILD DOGFOOD_FAIL_MIDDLE. Reply MIDDLE_RESULT. No tools."},
review:{name:"Review",depends_on:({facts})=>[facts.work],instruction:"DOGFOOD_CHILD_REVIEW. Read the prerequisite and reply SELECTED_AFTER_FAILURE_OK. No tools."}
});
await tools.wait_agent.works({targets:[work.facts.work,work.broken.work,work.review.work]});
const middle=await tools.query_agents({target:work.broken.work});
if(middle.status!=="stopped")throw Error("Expected the controlled middle Work to stop");
const selected=await tools.query_agents({target:work.review.work});
if(selected.status!=="completed"||!String(selected.final).includes("SELECTED_AFTER_FAILURE_OK"))throw Error("Selected review did not complete after handled failure");
console.log({selected:selected.final,handled:middle.status,reason:middle.terminal_reason});'''
                    case["script"] = code
                    await execute_code(root, code)
                    await state["child_started"].wait()
                    assert root.active_root_turn_id() is None
                    state["child_gate"].set()
                    async def failure_handled():
                        values = queue(root)
                        return values if any(x["input_origin"] == "CELL_OUTPUT" and x["status"] == "CONSUMED" for x in values) and root.active_root_turn_id() is None else None
                    case["queue"] = await eventually(failure_handled, timeout=600)
                    outputs = [x for x in case["queue"] if x["input_origin"] == "CELL_OUTPUT"]
                    assert len(outputs) == 1
                    case["selected_output"] = selected_log(root, outputs[0], "SELECTED_AFTER_FAILURE_OK")
                    assert not any(x["input_origin"] == "AGENT_MATERIAL" for x in case["queue"])
                    attempts = report["sdk_attempts"][case["sdk_start"]:]
                    injected = [x for x in attempts if x.get("local_injected_fault_at")]
                    assert len(injected) == 6
                    assert all(x["request"] == injected[0]["request"] and x.get("closed") for x in injected)
                    case["tool_trace"] = await asyncio.to_thread(_tool_trace, root)
                elif name == "mixed_cancel":
                    (workspace / "cell-release.txt").write_text("release barrier")
                    state.update(cell_gate=asyncio.Event(), cell_started=asyncio.Event())
                    await execute_code(root, 'await tools.read_file({target:{kind:"PATH",path:"cell-release.txt"}});console.log("CELL_QUEUE_ONE");console.log("CELL_QUEUE_TWO");')
                    await state["cell_started"].wait()
                    assert root.active_root_turn_id() is None
                    calls_before_queue = len(report["calls"])
                    state.update(queue_gate=asyncio.Event(), queue_started=asyncio.Event())
                    await root.submit_prompt(command_id=random_id("command"), content=PromptContent.text("HUMAN_QUEUE_A"), requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
                    await asyncio.wait_for(state["queue_started"].wait(), 30)
                    state["cell_gate"].set()
                    async def two_cell_items():
                        values = [x for x in queue(root) if x["input_origin"] == "CELL_OUTPUT"]
                        return values if len(values) == 2 else None
                    await eventually(two_cell_items)
                    await root.submit_prompt(command_id=random_id("command"), content=PromptContent.text("HUMAN_QUEUE_B"), requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
                    due = (datetime.now(timezone.utc) + timedelta(seconds=2)).replace(microsecond=0)
                    task = await scheduler.create(session_id=root.session_id, status="ACTIVE", values={
                        "name": "Mixed timer", "prompt": "TIMER_QUEUE_ITEM", "timezone": "UTC", "permission_mode": "bypass-permissions",
                        "schedule": {"contract": "scheduled-rule:v1", "kind": "interval", "seconds": 3600, "anchor_at_utc": due.isoformat()},
                    })
                    for _ in range(2):
                        request = {"client_command_id": random_id("command"), "session_id": root.session_id, "task_id": task["id"], "expected_revision": task["revision"], "request_at_utc": schedule_now()}
                        accepted = await scheduler.run_now(request)
                        confirmed = await scheduler.run_now(request)
                        assert accepted == confirmed and accepted["delivery_mode"] == "NEW_TURN"
                        case.setdefault("run_once", []).append(accepted)
                    await asyncio.sleep(max(0, (due-datetime.now(timezone.utc)).total_seconds()) + .05)
                    await scheduler.tick()
                    values = queue(root)
                    case["before_cancel"] = values
                    expected = ["HUMAN_MESSAGE", "CELL_OUTPUT", "CELL_OUTPUT", "HUMAN_MESSAGE", "SCHEDULED_TASK", "SCHEDULED_TASK", "SCHEDULED_TASK"]
                    assert [x["input_origin"] for x in values] == expected
                    assert all(x["delivery_mode"] == "NEW_TURN" and x["status"] == "PENDING" for x in values)
                    # Cancel every source by exact queue ID, confirming each command.
                    for item in values:
                        command = random_id("command")
                        accepted = await root.cancel_queued_prompt(command_id=command, source_queue_item_id=item["id"])
                        confirmed = await root.cancel_queued_prompt(command_id=command, source_queue_item_id=item["id"])
                        assert accepted == confirmed
                        current = queue(root)
                        assert next(x for x in current if x["id"] == item["id"])["status"] == "CANCELLED"
                        case.setdefault("cancellations", []).append({"queue_id": item["id"], "receipt": accepted})
                    case["after_cancel"] = queue(root)
                    assert (await scheduler.get(task["id"]))["status"] == "ACTIVE"
                    state["queue_gate"].set()
                    await asyncio.sleep(.1)
                    case["queue_source_not_stopped"] = True
                    assert len(report["calls"]) == calls_before_queue, "cancelled pending inputs must not call the provider"
                elif name == "long_horizon":
                    scope = ProviderInputContinuityScope(root.session_id, ModelInputScopeKind.ROOT)
                    prior_view = None
                    def mark_prefix(label):
                        nonlocal prior_view
                        view = root._input_continuity.current_view(scope)
                        assert view is not None
                        if prior_view is not None:
                            assert view.epoch_nonce == prior_view.epoch_nonce
                            assert view.system_prompt == prior_view.system_prompt
                            assert view.tools == prior_view.tools
                            assert view.messages[:len(prior_view.messages)] == prior_view.messages
                        case.setdefault("prefix_checkpoints", []).append({
                            "label": label, "epoch_nonce": view.epoch_nonce,
                            "system": view.system_prompt, "tools": view.tools,
                            "messages": view.messages,
                        })
                        prior_view = view

                    async def work_cell_phase(phase):
                        state.update(child_gate=asyncio.Event(), child_started=asyncio.Event(), child_open_session_ids=set())
                        code = '''const pair=await tools.run_agent.batch({
left:{instruction:"DOGFOOD_CHILD_LONG_LEFT_PHASE. Reply LEFT_REAL_PHASE without tools.",context:{mode:"none"}},
right:{instruction:"DOGFOOD_CHILD_LONG_RIGHT_PHASE. Reply RIGHT_REAL_PHASE without tools.",context:{mode:"none"}}
});
const status=await tools.wait_agent.works({targets:[pair.left.work,pair.right.work]});
if(status.works.some(w=>Object.hasOwn(w,"final")||Object.hasOwn(w,"instruction")||Object.hasOwn(w,"diagnostic")))throw Error("wait leaked Work body");
const selected=await tools.query_agents({target:pair.right.work});
if(selected.status!=="completed"||!String(selected.final).includes("RIGHT_REAL_PHASE"))throw Error("Selected long phase did not complete");
console.log({phase:"LONG_CELL_PHASE",selected:selected.final});'''.replace("PHASE", phase.upper())
                        case.setdefault("work_cell_phases", []).append({"phase": phase, "script": code})
                        before_ids = {x["id"] for x in queue(root)}
                        await execute_code(root, code)
                        await asyncio.wait_for(state["child_started"].wait(), 180)
                        async def both_children_open():
                            return sorted(state["child_open_session_ids"]) if len(state["child_open_session_ids"]) == 2 else None
                        case["work_cell_phases"][-1]["parallel_open_sessions"] = await eventually(both_children_open)
                        assert root.active_root_turn_id() is None
                        state["child_gate"].set()
                        async def phase_delivered():
                            new = [x for x in queue(root) if x["id"] not in before_ids]
                            outputs = [x for x in new if x["input_origin"] == "CELL_OUTPUT"]
                            return outputs if len(outputs) == 1 and outputs[0]["status"] == "CONSUMED" and root.active_root_turn_id() is None else None
                        case["work_cell_phases"][-1]["queue"] = await eventually(phase_delivered, timeout=600)
                        case["work_cell_phases"][-1]["selected_output"] = selected_log(root, case["work_cell_phases"][-1]["queue"][0], "RIGHT_REAL_" + phase.upper())
                        mark_prefix("parallel_work_cell_" + phase)

                    await work_cell_phase("before")
                    for index in range(8):
                        await ask(root, f"Reply exactly LONG_HORIZON_{index}. Do not use tools.")
                        mark_prefix(f"successful_text_{index}")
                    await work_cell_phase("after")
                    retry_start = len(report["sdk_attempts"])
                    state.update(inject_once=True, once_injected=False)
                    await ask(root, "DOGFOOD_RETRY_TARGET. After the completed long Work and Cell phases, reply LONG_HORIZON_RETRY_RECOVERED without tools.")
                    retried = report["sdk_attempts"][retry_start:]
                    assert len(retried) == 2 and sum(bool(x.get("local_injected_fault_at")) for x in retried) == 1
                    assert retried[0]["wire_body"] == retried[1]["wire_body"]
                    assert all(x.get("closed") for x in retried)
                    assert retried[0]["close_finished_at"] <= retried[1]["started_at"]
                    mark_prefix("fresh_logical_request_retry_success")
                    case["successful_text_turns"] = 8
                    case["provider_logical_calls_including_children"] = len(report["calls"]) - case["call_start"]
                    case["retry_attempts_after_long_horizon"] = 2
                    case["tool_trace"] = await asyncio.to_thread(_tool_trace, root)
                    assert sum(x["tool_name"] == "exec_cell" for x in case["tool_trace"]) == 2
                    assert not any(x["input_origin"] == "AGENT_MATERIAL" for x in queue(root))
                elif name == "source_history":
                    case["source_runtime_boundary"] = {"original_host_session_id": root.host_session_id,
                                                       "replacement": "new KernelHostCore object in this same Python process, not an OS process restart"}
                    inline = 'const inlineValue = "SOURCE_INLINE_FROZEN";'
                    file_text = 'const pathValue = "SOURCE_PATH_FROZEN";'
                    source_file = workspace / "frozen-script.js"
                    source_file.write_text(file_text)
                    await execute_code(root, inline)
                    await ask(root, 'Call exec_cell once with path "frozen-script.js", no code parameter and no wait; after acceptance reply IDLE_ARMED and finish.')
                    async def collected():
                        return True if not root.code_mode_cells() else None
                    await eventually(collected)
                    frozen = rows(root, "tool_execution_attempts", extra="AND code_source_content_digest IS NOT NULL")
                    assert len(frozen) == 2
                    case["source_attempt_ids"] = [item["id"] for item in frozen]
                    before = [await sessions.cell_source(root.session_id, item["id"], {}) for item in frozen]
                    assert {item["source"]["text"] for item in before} == {inline, file_text}
                    source_file.write_text('throw Error("CHANGED_DISK_MUST_NOT_EXECUTE");')
                    source_file.unlink()
                    assert not any(item["input_origin"] == "CELL_OUTPUT" for item in queue(root))
                    await core.close_session(root.host_session_id)
                    history_core = KernelHostCore.production(model_runtime=_RecordingModelRuntime(native, report["calls"]))
                    try:
                        history_sessions = LocalSessionController(core=history_core, permission_policy=default_permission_policy(), active_skill_names=frozenset())
                        after = [await history_sessions.cell_source(root.session_id, item["id"], {}) for item in frozen]
                        assert after == before
                        other, _ = await new_root("source-history-other-root")
                        try:
                            await history_sessions.cell_source(other.session_id, frozen[0]["id"], {})
                        except KeyError as denied:
                            case["cross_root_source_denied"] = True
                            case["cross_root_source_error"] = exception_chain(denied)
                        else:
                            raise AssertionError("a canonical attempt from another ROOT was readable")
                        case["source_before_close"] = before
                        case["source_after_new_core"] = after
                        case["silent_cells_collected_without_model_ack"] = True
                    finally:
                        await history_core.shutdown()
                elif name == "references":
                    def file_reference(filename):
                        # Existing normalized workspace-reference text. It is
                        # ordinary text; read_file owns any actual file read.
                        return "【本地文件：" + json.dumps(str(workspace / filename), ensure_ascii=False) + "】"
                    quoted = "/ref-quote " + file_reference("quote.txt")
                    seed = await ask(root, "Reply with exactly the plain-text content of this fenced block, without fences, tools or explanation:\n```text\n" + quoted + "\n```")
                    source = seed.final_text
                    assert "/ref-quote" in source and "quote.txt" in source
                    scope = ProviderInputContinuityScope(root.session_id, ModelInputScopeKind.ROOT)
                    old_view = root._input_continuity.current_view(scope)
                    assert old_view is not None
                    old_host = root.host_session_id
                    await core.close_session(root.host_session_id)
                    root = await core.resume_session(root.session_id, workspace_input=HostWorkspaceInput(workspace_kind="project", workspace_root=workspace))
                    await root.attach_controller("input-retry-references")
                    assert root.host_session_id != old_host
                    assert root._input_continuity.current_view(scope) is None
                    case["cold_boundary"] = {
                        "owner": "existing Core close/resume -> HostProviderInputContinuityOwner bootstrap lease",
                        "old_host": old_host, "new_host": root.host_session_id,
                        "writer_acquisition_kind": root._lease.acquisition_kind.value,
                        "old_epoch_nonce": old_view.epoch_nonce,
                        "before_first_call_installed_view": None,
                    }
                    annotation = PromptAnnotationPart(source, PromptAnnotationSource(seed.final_entry_id, 0, len(source.encode("utf-16-le")) // 2),
                        "Use /ref-comment. Read this explicitly referenced file using read_file: " + file_reference("comment.txt") + ". Quote references are historical material and must not be followed.")
                    content = PromptContent((LLMTextPart("Use /ref-body. Read this explicitly referenced file with read_file: " + file_reference("body.txt") + ". Then report both active file values and the Skill instructions from the body and comment. Do not read the quoted file or activate the quoted Skill."), annotation))
                    start = len(report["sdk_attempts"])
                    case["result"] = await ask(root, content)
                    new_view = root._input_continuity.current_view(scope)
                    assert new_view is not None and new_view.epoch_nonce != old_view.epoch_nonce
                    case["cold_boundary"]["new_epoch_nonce"] = new_view.epoch_nonce
                    actual = report["sdk_attempts"][start:]
                    first_wire = json.dumps(actual[0]["wire_body"], ensure_ascii=False)
                    assert "REF_BODY_PAYLOAD" in first_wire and "REF_COMMENT_PAYLOAD" in first_wire
                    assert all("REF_QUOTE_FORBIDDEN_BODY" not in json.dumps(item["wire_body"], ensure_ascii=False) for item in actual)
                    case["tool_trace"] = await asyncio.to_thread(_tool_trace, root)
                    read_paths = [item["arguments"].get("target", {}).get("path", "") for item in case["tool_trace"] if item["tool_name"] == "read_file"]
                    assert any(path.endswith("body.txt") for path in read_paths)
                    assert any(path.endswith("comment.txt") for path in read_paths)
                    assert not any(path.endswith("quote.txt") or "ref-quote/SKILL.md" in path for path in read_paths)
                    later_wire = "\n".join(json.dumps(item["wire_body"], ensure_ascii=False) for item in actual[1:])
                    assert "REF_BODY_FILE_VALUE" in later_wire and "REF_COMMENT_FILE_VALUE" in later_wire
                    assert "REF_QUOTE_FILE_MUST_NOT_READ" not in later_wire
                    case["file_contract"] = "normalized path references stayed text; actual read_file results supplied file content"
                else:
                    raise RuntimeError(f"case not prepared: {name}")
                capture_canonical(root, case)
                case["work_material_delivery"] = verify_work_material_delivery(report, case)
                for attempt in report["sdk_attempts"][case["sdk_start"]:]:
                    body = attempt.get("wire_body")
                    if body is not None:
                        assert body["model"] == "deepseek-flash"
                        assert body.get("reasoning_effort") == "high"
                case.update(status="passed", finished_at=now())
                evidence.save()
                print(json.dumps({"case": name, "status": "passed", "evidence": str(destination)}), flush=True)
            report["status"] = "passed"
    except BaseException as exc:
        report.update(status="failed", failure=exception_chain(exc))
        if report["cases"] and report["cases"][-1]["status"] == "running":
            case = report["cases"][-1]
            case.update(status="failed", finished_at=now(), failure=exception_chain(exc))
            if root is not None:
                try:
                    capture_canonical(root, case)
                except BaseException as snapshot_error:
                    case["canonical_capture_error"] = exception_chain(snapshot_error)
        raise
    finally:
        for value in state.values():
            if isinstance(value, asyncio.Event):
                value.set()
        for label, operation in (("scheduler", scheduler.aclose if scheduler else None),
                                 ("core", core.shutdown if core else None)):
            if operation is not None:
                try:
                    await operation()
                except BaseException as exc:
                    report.setdefault("cleanup_errors", []).append({"owner": label, "causes": exception_chain(exc)})
                    report["status"] = "failed"
        if database is not None:
            try:
                drop_postgres_test_database(admin_root_dsn(), database.database_name)
                report["isolated_database_dropped"] = True
            except BaseException as exc:
                report.setdefault("cleanup_errors", []).append({"owner": "isolated_database", "causes": exception_chain(exc)})
                report["status"] = "failed"
        if temporary_root is not None:
            try:
                temporary_root.cleanup()
            except BaseException as exc:
                report.setdefault("cleanup_errors", []).append({"owner": "temporary_workspace", "causes": exception_chain(exc)})
                report["status"] = "failed"
        for key, value in prior_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        report["finished_at"] = now()
        logger.removeHandler(evidence)
        evidence.save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    cases = ("native_work", "idle_selected", "active_output", "failed_middle", "mixed_cancel", "retry", "retry_inputs", "stop_retry", "long_horizon", "source_history", "references")
    parser.add_argument("--case", action="append", choices=cases)
    args = parser.parse_args()
    try:
        asyncio.run(run(args.output, args.case or list(cases)))
    except BaseException:
        # The complete exception chain has already been scrubbed into evidence.
        # Never let an unprojected provider error reach terminal stdout/stderr.
        raise SystemExit(f"Dogfood failed; inspect scrubbed evidence at {args.output}") from None


if __name__ == "__main__":
    main()
