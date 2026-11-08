"""Byte-gated local HTTP, official SDKs and the production compaction owners.

The endpoint's 413 decision is a predicate of the received wire bytes, never a
request ordinal. The optional 503 models one transient outage for an identical
condensed request; its subsequent 413 remains a stable byte-limit decision.
"""
from __future__ import annotations

import ast
import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import socket
from threading import Event, Thread
from time import monotonic

from psycopg.rows import dict_row

import pytest

from pulsara_agent.conversation_kernel.compaction.contracts import ResolvedCompactionPolicy
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.conversation_kernel.execution_watchdogs import KernelExecutionWatchdogPolicy
from pulsara_agent.conversation_kernel.ephemeral import SUGGESTION_PROMPT
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import PromptContent, text_part_values
from pulsara_agent.conversation_kernel.prompt_content import decode_canonical_prompt_body
from pulsara_agent.llm.retry import LLMRetryConfig
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.workspace_identity import HostWorkspaceInput
from tests.dogfood.run_compaction_request_start_dogfood import _seed_history
from tests.support.model_config import test_model_binding, test_model_limits, test_model_runtime
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane

APIS = ("openai_chat_completions", "openai_responses")
_SUMMARY = "The historical investigation is complete. Continue the current exact request."
_CURRENT = "HTTP413_CURRENT_INPUT: " + "new evidence for the current request; " * 3600
_SUMMARY_MARKER = "The material designated H is the earlier history to summarize."
_SNAPSHOT_MARKER = "The following is a summary of earlier history."
_AGENT_FIRST = "LOCAL_AGENT_FIRST_WORK"
_AGENT_SECOND = "LOCAL_AGENT_SECOND_WORK " + "current task evidence; " * 2400
_AGENT_HISTORY = "AGENT_VALIDATED_HISTORY " + "complete investigation evidence; " * 8000


@dataclass(frozen=True)
class CapturedRequest:
    path: str
    raw: bytes
    document: dict
    summary: bool
    condensed: bool
    status: int | None


@dataclass
class ByteGate:
    byte_limit: int = 320_000
    error_format: str = "json"
    oversized_status: int = 413
    transient_condensed_503: bool = False
    requests: list[CapturedRequest] = field(default_factory=list)
    evidence_label: str = "case"
    tool_before_failure: bool = False
    delegate_marker: str | None = None
    delegate_code: str | None = None
    agent_history: bool = False
    transport_failure: str | None = None
    release: Event = field(default_factory=Event)
    tool_invocations: list[dict] = field(default_factory=list)
    retained_original_texts: list[str] = field(default_factory=list)
    _unavailable_bodies: set[bytes] = field(default_factory=set)

    def response(self, path: str, body: bytes) -> CapturedRequest:
        document = json.loads(body)
        text = body.decode("utf-8")
        summary = _SUMMARY_MARKER in text
        condensed = _SNAPSHOT_MARKER in text and not summary
        status = self.oversized_status if len(body) > self.byte_limit else 200
        if condensed and self.transient_condensed_503 and body not in self._unavailable_bodies:
            self._unavailable_bodies.add(body)
            status = 503
        if self.transport_failure is not None:
            status = None
        capture = CapturedRequest(path, body, document, summary, condensed, status)
        self.requests.append(capture)
        return capture


def _events(api: str, text: str):
    if api == APIS[0]:
        return [
            {"id": "local-completion", "model": "test-model", "choices": [
                {"index": 0, "delta": {"content": text}, "finish_reason": None}]},
            {"id": "local-completion", "model": "test-model", "choices": [
                {"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]
    return [{"type": "response.completed", "response": {
        "id": "local-response", "model": "test-model", "status": "completed", "output": [
            {"id": "local-message", "type": "message", "role": "assistant", "status": "completed",
             "content": [{"type": "output_text", "text": text, "annotations": []}]}]}}]


def _tool_events(api, code, call_id="local-sideeffect-call"):
    arguments = json.dumps({"code": code})
    if api == APIS[0]:
        return [
            {"id": "local-completion", "model": "test-model", "choices": [{"index": 0,
                "delta": {"tool_calls": [{"index": 0, "id": call_id, "type": "function",
                    "function": {"name": "exec_cell", "arguments": arguments}}]}, "finish_reason": None}]},
            {"id": "local-completion", "model": "test-model", "choices": [{"index": 0,
                "delta": {}, "finish_reason": "tool_calls"}]},
        ]
    return [{"type": "response.completed", "response": {"id": "local-response", "model": "test-model",
        "status": "completed", "output": [{"id": "local-call", "type": "function_call",
            "call_id": call_id, "name": "exec_cell", "arguments": arguments, "status": "completed"}]}}]


@contextmanager
def byte_gated_server(api: str, gate: ByteGate):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            request = gate.response(self.path, body)
            if gate.transport_failure is not None:
                if gate.transport_failure == "timeout":
                    gate.release.wait()
                self.close_connection = True
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass  # The SDK may already have closed its timed-out socket.
                return
            self.send_response(request.status)
            self.send_header("Connection", "close")
            if request.status != 200:
                if gate.error_format == "html":
                    data = b"<html><body>413 Request Entity Too Large</body></html>"
                    content_type = "text/html"
                else:
                    data = json.dumps({"error": {"message": "413 Request Entity Too Large",
                        "code": "request_body_too_large"}}).encode()
                    content_type = "application/json"
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                self.wfile.flush()
                self.close_connection = True
                return
            messages = request.document.get("messages", request.document.get("input"))
            has_call = any(message.get("tool_calls") or message.get("type") == "function_call"
                for message in messages)
            delegate_index = next((index for index, (role, text) in enumerate(_messages(request))
                if role == "user" and gate.delegate_marker is not None and gate.delegate_marker in text), None)
            delegate_has_call = delegate_index is not None and any(
                message.get("tool_calls") or message.get("type") == "function_call"
                for message in messages[delegate_index + 1:])
            if request.summary:
                events = _events(api, _SUMMARY)
            elif delegate_index is not None:
                events = _events(api, "HTTP413_FINISHED") if delegate_has_call else _tool_events(
                    api, gate.delegate_code, call_id="local-delegate-" + gate.delegate_marker)
            elif gate.agent_history and any(role == "user" and text == _AGENT_FIRST
                    for role, text in _messages(request)) and _AGENT_SECOND not in request.raw.decode():
                events = _events(api, _AGENT_HISTORY)
            elif gate.tool_before_failure and not has_call:
                code = 'await tools.write_file({target:{kind:"PATH",path:"http413-sideeffect.txt"},content:"written once"}); console.log("tool evidence ".repeat(4000));'
                events = _tool_events(api, code)
            else:
                events = _events(api, "HTTP413_FINISHED")
            payload = b"".join(("data: " + json.dumps(event) + "\n\n").encode() for event in events)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            self.wfile.flush()
            self.close_connection = True

    instance = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{instance.server_port}/v1"
    finally:
        gate.release.set()
        instance.shutdown()
        instance.server_close()
        thread.join()


def _rows(session, query, args=()):
    with session.repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR, row_factory=dict_row,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        return connection.execute(query, args).fetchall()


async def _run_host(tmp_path, monkeypatch, dsn, api, gate, *, current=_CURRENT, warmup_current=None):
    import pulsara_agent.conversation_kernel.host as kernel_host

    monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
    # macOS system proxies must not route this loopback fixture elsewhere.
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setattr(kernel_host.LocalMcpManagementService, "load_configs", lambda *_a, **_k: ())
    with byte_gated_server(api, gate) as endpoint:
        runtime = test_model_runtime(base_url=endpoint, wire_api=api, postgres_dsn=dsn,
            retry=LLMRetryConfig(enabled=gate.transport_failure is None,
                base_delay_seconds=0.001, max_delay_seconds=0.001, jitter_ratio=0))
        watchdog = KernelExecutionWatchdogPolicy(provider_stream_idle_seconds=0.05) if gate.transport_failure else None
        core = KernelHostCore.production(model_runtime=runtime, watchdog_policy=watchdog)
        root = await core.open_session(HostWorkspaceInput(workspace_kind="project", workspace_root=tmp_path))
        try:
            await root.update_model_call_binding(test_model_binding(runtime))
            # Keep the original request locally executable and below the automatic
            # trigger. Recovery must be caused by the real HTTP status alone.
            root._compaction.policy = ResolvedCompactionPolicy(auto_trigger_ratio=0.99)
            _seed_history(root, segments=8, repetitions=300)
            if warmup_current is not None:
                warmup = await root.run_turn(PromptContent.text(warmup_current),
                    command_id=random_id("command"), requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
                assert warmup.final_text == "HTTP413_FINISHED"
            before = _rows(root, "SELECT id FROM pulsara_v3.transcript_entries WHERE session_id=%s", (root.session_id,))
            command = random_id("command")
            result = None
            failure = None
            try:
                result = await root.run_turn(PromptContent.text(current), command_id=command,
                    requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            except Exception as exc:
                failure = exc
            entries = _rows(root, "SELECT id,entry_kind,inline_content,turn_id FROM pulsara_v3.transcript_entries WHERE session_id=%s ORDER BY entry_sequence", (root.session_id,))
            snapshots = _rows(root, "SELECT * FROM pulsara_v3.context_snapshots WHERE session_id=%s", (root.session_id,))
            if snapshots:
                selected_ids = json.loads(bytes(snapshots[0]["inline_content"]))["retained_entry_ids"]
                selected_rows = _rows(root, "SELECT id,inline_content FROM pulsara_v3.transcript_entries "
                    "WHERE session_id=%s AND id=ANY(%s) ORDER BY entry_sequence", (root.session_id, selected_ids))
                gate.retained_original_texts = ["".join(text_part_values(
                    decode_canonical_prompt_body(bytes(row["inline_content"])).parts)) for row in selected_rows]
            assert {row["id"] for row in before}.issubset({row["id"] for row in entries})
            fresh = [row for row in entries if row["id"] not in {old["id"] for old in before}]
            assert sum(row["entry_kind"] == "USER_MESSAGE" for row in fresh) == 1
            if gate.tool_before_failure:
                gate.tool_invocations = _rows(root,
                    "SELECT i.tool_name,a.id AS attempt_id FROM pulsara_v3.tool_invocations i "
                    "JOIN pulsara_v3.tool_execution_attempts a ON a.invocation_id=i.id AND a.session_id=i.session_id "
                    "WHERE i.session_id=%s AND i.source_kind='CODE_MODE_CALL'", (root.session_id,))
            else:
                assert not any(row["entry_kind"] in {"ASSISTANT_TOOL_REQUEST", "TOOL_RESULT"} for row in fresh)
            evidence_dir = os.environ.get("PULSARA_HTTP413_EVIDENCE_DIR")
            if evidence_dir:
                destination = Path(evidence_dir)
                destination.mkdir(parents=True, exist_ok=True)
                (destination / f"{api}-{gate.evidence_label}.json").write_text(json.dumps({
                    "api": api, "byte_limit": gate.byte_limit,
                    "failure": repr(failure) if failure else None,
                    "final_text": result.final_text if result else None,
                    "requests": [{"path": request.path, "wire_utf8_bytes": len(request.raw),
                        "summary": request.summary, "condensed": request.condensed,
                        "status": request.status, "body": request.document} for request in gate.requests],
                    "fresh_entries": [{"id": row["id"], "kind": row["entry_kind"],
                        "turn_id": row["turn_id"]} for row in fresh],
                    "snapshots": [json.loads(bytes(row["inline_content"])) for row in snapshots],
                    "tool_invocations": gate.tool_invocations,
                }, ensure_ascii=False, indent=2))
            return result, failure, fresh, snapshots
        finally:
            await core.shutdown()


def _messages(request):
    messages = request.document.get("messages", request.document.get("input"))
    assert isinstance(messages, list)
    result = []
    for message in messages:
        content = message.get("content", "")
        text = content if isinstance(content, str) else "".join(
            part.get("text", "") for part in content if isinstance(part, dict))
        result.append((message.get("role"), text))
    return result


def _assert_native_retention(retry, snapshot, fresh, original_texts):
    payload = json.loads(bytes(snapshot["inline_content"]))
    assert set(payload) == {"earlier_context_summary", "retained_entry_ids", "continuation"}
    assert set(payload["continuation"]) == {"mode", "active_request_entry_id"}
    assert payload["earlier_context_summary"] == _SUMMARY
    assert payload["retained_entry_ids"]
    assert len(payload["retained_entry_ids"]) == len(set(payload["retained_entry_ids"]))
    messages = _messages(retry)
    snapshots = [(role, text) for role, text in messages if _SNAPSHOT_MARKER in text]
    assert snapshots == [("user", _SNAPSHOT_MARKER + "\n\n" + _SUMMARY)]
    assert [(role, text) for role, text in messages if _CURRENT in text] == [("user", _CURRENT)]
    retained = [(role, text) for role, text in messages if "Historical source " in text]
    assert retained == [("user", text) for text in original_texts]
    assert all(not text.startswith('"') for _, text in retained)
    assert "Historical source " not in snapshots[0][1]
    assert _CURRENT not in snapshots[0][1]
    assert payload["continuation"]["active_request_entry_id"] == next(
        row["id"] for row in fresh if row["entry_kind"] == "USER_MESSAGE")


def _assert_wire(gate, api):
    assert gate.requests
    assert {request.path for request in gate.requests} == {
        "/v1/chat/completions" if api == APIS[0] else "/v1/responses"}
    assert {request.document["model"] for request in gate.requests} == {"test-model"}
    # No per-attempt route/model change may disguise a new recovery scope.
    originals = [request for request in gate.requests if not request.summary and not request.condensed]
    assert len(originals) == 1
    assert originals[0].status in (400, 413)
    return originals[0]


@pytest.mark.postgres
@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize("error_format", ("json", "html"))
def test_real_http413_compacts_once_then_retries_smaller(tmp_path, monkeypatch, isolated_migrated_postgres_database, api, error_format):
    gate = ByteGate(error_format=error_format, evidence_label="success-" + error_format)
    result, failure, fresh, snapshots = asyncio.run(_run_host(tmp_path, monkeypatch,
        isolated_migrated_postgres_database.runtime_dsn, api, gate))
    original = _assert_wire(gate, api)
    assert failure is None, repr(failure)
    assert result.final_text == "HTTP413_FINISHED"
    summaries = [request for request in gate.requests if request.summary]
    retries = [request for request in gate.requests if request.condensed]
    assert len(summaries) == len(retries) == len(snapshots) == 1
    assert summaries[0].status == retries[0].status == 200
    assert len(retries[0].raw) < len(original.raw)
    assert _CURRENT.encode() not in summaries[0].raw
    assert _CURRENT in retries[0].raw.decode()
    assert sum(row["entry_kind"] == "ASSISTANT_MESSAGE" for row in fresh) == 1
    assert len({row["turn_id"] for row in fresh}) == 1
    _assert_native_retention(retries[0], snapshots[0], fresh, gate.retained_original_texts)


@pytest.mark.postgres
@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize("case", ("summary_413", "retry_413", "retry_503_then_413", "body_413_status_400"))
def test_real_http413_failure_boundaries(tmp_path, monkeypatch, isolated_migrated_postgres_database, api, case):
    gate = ByteGate(
        evidence_label=case,
        byte_limit=250_000 if case == "summary_413" else 320_000,
        transient_condensed_503=case == "retry_503_then_413",
        oversized_status=400 if case == "body_413_status_400" else 413)
    current = _CURRENT + ("mandatory extra current evidence; " * 2200
        if case in {"retry_413", "retry_503_then_413"} else "")
    result, failure, fresh, snapshots = asyncio.run(_run_host(tmp_path, monkeypatch,
        isolated_migrated_postgres_database.runtime_dsn, api, gate, current=current))
    original = _assert_wire(gate, api)
    assert result is None
    assert failure is not None
    summaries = [request for request in gate.requests if request.summary]
    retries = [request for request in gate.requests if request.condensed]
    assert not any(row["entry_kind"] == "ASSISTANT_MESSAGE" for row in fresh)
    if case == "body_413_status_400":
        assert not summaries and not retries and not snapshots
    elif case == "summary_413":
        assert len(summaries) == 1 and summaries[0].status == 413
        assert not retries and not snapshots
        assert b"SOURCE_0" in summaries[0].raw and b"SOURCE_7" in summaries[0].raw
        # H-only would fit this same byte gate, yet recovery must not issue it.
        request = summaries[0]
        instruction = next(text for _, text in _messages(request) if _SUMMARY_MARKER in text)
        match = re.search(r"Message ordinals (\([^)]*\)) are exact retained", instruction)
        assert match, instruction
        reference_ordinals = set(ast.literal_eval(match.group(1)))
        document = dict(request.document)
        key = "messages" if api == APIS[0] else "input"
        ordinal = -1
        history_only = []
        for message in document[key]:
            if message.get("role") != "system":
                ordinal += 1
                if ordinal in reference_ordinals:
                    continue
            history_only.append(message)
        document[key] = history_only
        assert len(json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode()) < gate.byte_limit
    else:
        assert len(summaries) == len(snapshots) == 1
        assert [request.status for request in retries] == ([503, 413] if case == "retry_503_then_413" else [413])
        assert all(len(request.raw) < len(original.raw) for request in retries)
        if case == "retry_503_then_413":
            assert retries[0].raw == retries[1].raw


@pytest.mark.postgres
def test_real_http413_after_completed_tool_does_not_repeat_effect(tmp_path, monkeypatch, isolated_migrated_postgres_database):
    gate = ByteGate(byte_limit=400_000, tool_before_failure=True, evidence_label="completed-tool")
    current = "Write the side effect exactly once, then continue. " + "current evidence " * 1800
    result, failure, fresh, snapshots = asyncio.run(_run_host(tmp_path, monkeypatch,
        isolated_migrated_postgres_database.runtime_dsn, APIS[0], gate, warmup_current=current))
    assert failure is None, repr(failure)
    assert result.final_text == "HTTP413_FINISHED"
    assert (tmp_path / "http413-sideeffect.txt").read_text() == "written once"
    assert [row["tool_name"] for row in gate.tool_invocations] == ["write_file"]
    assert len([request for request in gate.requests if request.summary]) == len(snapshots) == 1
    failed = [request for request in gate.requests if request.status == 413]
    retries = [request for request in gate.requests if request.condensed]
    assert len(failed) == len(retries) == 1
    assert len(retries[0].raw) < len(failed[0].raw)
    assert sum(row["entry_kind"] == "USER_MESSAGE" for row in fresh) == 1
    assert not any(row["entry_kind"] in {"ASSISTANT_TOOL_REQUEST", "TOOL_RESULT"} for row in fresh)
    # The original closed batch is carried as native assistant/tool messages;
    # carrying its call ID through a successor must never dispatch it again.
    # The best-effort suggestion uses an independent auxiliary append. Its
    # temporary instruction is not installed into the business epoch.
    installed = [request for request in gate.requests if not request.summary and not request.condensed
        and _messages(request)[-1][1] != SUGGESTION_PROMPT]
    for previous, current_request in zip(installed, installed[1:]):
        assert previous.document["tools"] == current_request.document["tools"]
        previous_messages = previous.document["messages"]
        assert current_request.document["messages"][:len(previous_messages)] == previous_messages
    messages = retries[0].document["messages"]
    assert any(any(call["id"] == "local-sideeffect-call" for call in message.get("tool_calls", []))
        for message in messages)
    assert any(message.get("role") == "tool" and message.get("tool_call_id") == "local-sideeffect-call"
        for message in messages)


@pytest.mark.postgres
@pytest.mark.parametrize("api", APIS)
def test_subagent_real_http413_uses_same_production_recovery(tmp_path, monkeypatch, isolated_migrated_postgres_database, api):
    async def scenario():
        import pulsara_agent.conversation_kernel.host as kernel_host
        monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setattr(kernel_host.LocalMcpManagementService, "load_configs", lambda *_a, **_k: ())
        gate = ByteGate(byte_limit=340_000, agent_history=True, evidence_label="subagent")
        gate.delegate_marker = "LOCAL_ROOT_CREATE_AGENT"
        gate.delegate_code = "await tools.run_agent(" + json.dumps([{"instruction": _AGENT_FIRST, "context": {"mode": "none"}}]) + ");"
        with byte_gated_server(api, gate) as endpoint:
            runtime = test_model_runtime(base_url=endpoint, wire_api=api,
                postgres_dsn=isolated_migrated_postgres_database.runtime_dsn,
                limits=test_model_limits(total_context_tokens=1_000_000, max_input_tokens=900_000,
                    max_output_tokens=100_000, default_output_tokens=100_000),
                retry=LLMRetryConfig(base_delay_seconds=0.001, max_delay_seconds=0.001, jitter_ratio=0))
            core = KernelHostCore.production(model_runtime=runtime)
            root = await core.open_session(HostWorkspaceInput(workspace_kind="project", workspace_root=tmp_path))
            try:
                await root.update_model_call_binding(test_model_binding(runtime))
                root._compaction.policy = ResolvedCompactionPolicy(auto_trigger_ratio=0.99)
                await root.run_turn(PromptContent.text(gate.delegate_marker), command_id=random_id("command"),
                    requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
                async with asyncio.timeout(30):
                    while True:
                        children = await root._io.run(root.repository.list_root_agent_session_ids,
                            root_session_id=root.session_id, deadline_monotonic=monotonic() + 10)
                        if children:
                            child = core._live_agent_session(children[0])
                            works = await root._io.run(root.repository.list_agent_works,
                                root_session_id=root.session_id, agent_id=children[0], limit=10,
                                deadline_monotonic=monotonic() + 10)
                            if works and works[0]["status"] == "completed" and child.active_root_turn_id() is None:
                                break
                        await asyncio.sleep(0.01)
                first_work_id = works[0]["work_id"]
                child._compaction.policy = ResolvedCompactionPolicy(auto_trigger_ratio=0.99)
                before = _rows(child, "SELECT id FROM pulsara_v3.transcript_entries WHERE session_id=%s", (child.session_id,))
                gate.delegate_marker = "LOCAL_ROOT_REUSE_AGENT"
                gate.delegate_code = "await tools.run_agent(" + json.dumps([{
                    "instruction": _AGENT_SECOND, "target": {"kind": "AGENT", "agent_id": child.session_id}}]) + ");"
                await root.run_turn(PromptContent.text(gate.delegate_marker), command_id=random_id("command"),
                    requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
                async with asyncio.timeout(30):
                    while True:
                        works = await root._io.run(root.repository.list_agent_works,
                            root_session_id=root.session_id, agent_id=child.session_id, limit=10,
                            deadline_monotonic=monotonic() + 10)
                        second_work = next((work for work in works if work["work_id"] != first_work_id), None)
                        if second_work is not None and second_work["status"] in {"completed", "stopped"}:
                            break
                        await asyncio.sleep(0.01)
                assert second_work["status"] == "completed", works
                snapshots = _rows(child, "SELECT * FROM pulsara_v3.context_snapshots WHERE session_id=%s", (child.session_id,))
                entries = _rows(child, "SELECT id,entry_kind,input_origin,inline_content,turn_id FROM pulsara_v3.transcript_entries WHERE session_id=%s ORDER BY entry_sequence", (child.session_id,))
                fresh = [row for row in entries if row["id"] not in {old["id"] for old in before}]
                summaries = [request for request in gate.requests if request.summary]
                failed = [request for request in gate.requests if request.status == 413]
                retries = [request for request in gate.requests if request.condensed]
                evidence_dir = os.environ.get("PULSARA_HTTP413_EVIDENCE_DIR")
                if evidence_dir:
                    destination = Path(evidence_dir)
                    destination.mkdir(parents=True, exist_ok=True)
                    (destination / f"{api}-subagent.json").write_text(json.dumps({
                        "works": works, "fresh_entries": [{"id": row["id"], "kind": row["entry_kind"],
                            "origin": row["input_origin"], "turn_id": row["turn_id"]} for row in fresh],
                        "requests": [{"path": request.path, "wire_utf8_bytes": len(request.raw),
                            "summary": request.summary, "condensed": request.condensed,
                            "status": request.status, "body": request.document} for request in gate.requests],
                        "snapshots": [json.loads(bytes(row["inline_content"])) for row in snapshots],
                    }, ensure_ascii=False, indent=2, default=str))
                assert len(summaries) == len(failed) == len(retries) == len(snapshots) == 1
                assert summaries[0].status == retries[0].status == 200
                assert len(retries[0].raw) < len(failed[0].raw)
                assert _AGENT_HISTORY in summaries[0].raw.decode()
                assert _AGENT_SECOND not in summaries[0].raw.decode()
                assert [(role, text) for role, text in _messages(retries[0]) if _AGENT_SECOND in text] == [("user", _AGENT_SECOND)]
                assert sum(row["input_origin"] == "AGENT_INSTRUCTION" for row in fresh) == 1
                assert sum(row["entry_kind"] == "ASSISTANT_MESSAGE" for row in fresh) == 1
                assert len({row["turn_id"] for row in fresh}) == 1
                assert {row["id"] for row in before}.issubset({row["id"] for row in entries})
            finally:
                await core.shutdown()
    asyncio.run(scenario())


@pytest.mark.postgres
@pytest.mark.parametrize("api", APIS)
def test_summary_larger_than_failed_wire_reaches_real_http_admission(tmp_path, monkeypatch, isolated_migrated_postgres_database, api):
    gate = ByteGate(byte_limit=271_500, evidence_label="larger-summary-http413")
    current = "EXACT_NEW_INPUT " + "new data " * 100
    result, failure, fresh, snapshots = asyncio.run(_run_host(tmp_path, monkeypatch,
        isolated_migrated_postgres_database.runtime_dsn, api, gate, current=current))
    assert result is None and failure is not None
    failed = [request for request in gate.requests if not request.summary and request.status == 413]
    summaries = [request for request in gate.requests if request.summary]
    assert len(failed) == len(summaries) == 1
    # The received auxiliary request proves local preparation admitted it even
    # though it is larger than the business rejection; HTTP then rejects it.
    assert len(summaries[0].raw) > len(failed[0].raw) > gate.byte_limit
    assert summaries[0].status == 413
    assert b"SOURCE_0" in summaries[0].raw and b"SOURCE_7" in summaries[0].raw
    assert not snapshots and not any(request.condensed for request in gate.requests)
    assert sum(row["entry_kind"] == "USER_MESSAGE" for row in fresh) == 1
    assert not any(row["entry_kind"] in {"ASSISTANT_MESSAGE", "ASSISTANT_TOOL_REQUEST", "TOOL_RESULT"} for row in fresh)


@pytest.mark.postgres
@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize("fault", ("timeout", "disconnect"))
def test_real_sdk_transport_failure_is_not_inferred_as_http413(tmp_path, monkeypatch, isolated_migrated_postgres_database, api, fault):
    gate = ByteGate(transport_failure=fault, evidence_label="transport-" + fault)
    result, failure, fresh, snapshots = asyncio.run(_run_host(tmp_path, monkeypatch,
        isolated_migrated_postgres_database.runtime_dsn, api, gate))
    assert result is None and failure is not None
    assert len(gate.requests) == 1
    assert gate.requests[0].status is None
    assert not gate.requests[0].summary and not gate.requests[0].condensed
    assert failure.error.retry_summary.final_status_code is None
    assert not snapshots
    assert sum(row["entry_kind"] == "USER_MESSAGE" for row in fresh) == 1
    assert not any(row["entry_kind"] == "ASSISTANT_MESSAGE" for row in fresh)
