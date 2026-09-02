"""Run the Round 5B real-provider compaction activation dogfood.

The probe owns an ephemeral clean-v0 database and trusted temporary
workspaces.  Its diagnostic report retains exact prompts, normalized provider
blocks, model text, tool arguments/results and compaction outcomes.  Only the
exact PULSARA_API_KEY value is scrubbed before the report leaves the process.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from enum import Enum
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
import traceback
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import yaml

from pulsara_agent.conversation_kernel.compaction.contracts import (
    CompactionTrigger,
    ResolvedCompactionPolicy,
)
from pulsara_agent.conversation_kernel.contracts import InlineContent
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.conversation_kernel.repository import AssistantTextBlock
from pulsara_agent.llm.input import MessageRole
from pulsara_agent.llm.result import TransportUsageReport
from pulsara_agent.ports.live_agent_event import (
    DataEndPayload,
    DataStartPayload,
    TextEndPayload,
    TextStartPayload,
    ThinkingEndPayload,
    ThinkingStartPayload,
    ToolCallEndPayload,
    ToolCallStartPayload,
)
from pulsara_agent.ports.provider_stream import ProviderStreamTerminal
from pulsara_agent.model_input.continuity import (
    ProviderInputContinuityScope,
    decode_runtime_observation,
)
from pulsara_agent.model_input.contracts import (
    ContextSourceKind,
    ModelInputScopeKind,
)
from pulsara_agent.primitives.model_call import ModelCallPurpose
from pulsara_agent.primitives.permission import DEFAULT_PERMISSION_MODE
from pulsara_agent.settings import PulsaraSettings, StorageConfig, load_env_file
from pulsara_agent.storage.migrations.runner import PostgresMigrationRunner
from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane,
)
from pulsara_agent.workspace_identity import HostWorkspaceInput


_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "round9_mcp_server.py"
_SKILL_NAME = "round5b-retained-check"
_SENTINEL = "ROUND5B_SKILL_SUCCESS"
_MCP_SENTINEL = "round5b-sentinel"
_API_KEY_REDACTION = "<PULSARA_API_KEY>"
_SCHEMA_VERSION = "round5b-compaction-dogfood.v5-final-wire-estimation"


def _scrub_exact(value: object, secret: str) -> object:
    if isinstance(value, str):
        return value.replace(secret, _API_KEY_REDACTION) if secret else value
    if isinstance(value, bytes):
        return _scrub_exact(value.decode("utf-8"), secret)
    if isinstance(value, dict):
        return {
            str(_scrub_exact(key, secret)): _scrub_exact(item, secret)
            for key, item in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [_scrub_exact(item, secret) for item in value]
    if isinstance(value, Enum):
        return _scrub_exact(value.value, secret)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _scrub_exact(str(value), secret)


def _message_trace(message) -> dict[str, object]:
    return {
        "role": message.role.value,
        "content": tuple(message.content),
        "tool_calls": tuple(
            {
                "id": item.id,
                "name": item.name,
                "arguments": item.arguments,
            }
            for item in message.tool_calls
        ),
        "tool_call_id": message.tool_call_id,
    }


def _provider_usage_trace(report) -> dict[str, object]:
    usage = report.usage
    return {
        "usage_status": report.usage_status,
        "input_tokens": None if usage is None else usage.input_tokens,
        "cached_input_tokens": (None if usage is None else usage.cached_input_tokens),
        "output_tokens": None if usage is None else usage.output_tokens,
        "reasoning_output_tokens": (
            None if usage is None else usage.reasoning_output_tokens
        ),
        "total_tokens": None if usage is None else usage.total_tokens,
        "reported_model_id": report.reported_model_id,
        "diagnostics": tuple(
            item.model_dump(mode="json") for item in report.provider_diagnostics
        ),
    }


def _provider_wire_quote_trace(quote) -> dict[str, object]:
    return {
        "wire_api": quote.wire_api,
        "estimator_fingerprint": quote.estimator_fingerprint,
        "semantic_estimated_input_tokens": (quote.semantic_estimated_input_tokens),
        "generic_wire_estimated_input_tokens": (
            quote.generic_wire_estimated_input_tokens
        ),
        "replaced_generic_wire_estimated_tokens": (
            quote.replaced_generic_wire_estimated_tokens
        ),
        "replay_wire_estimated_tokens": quote.replay_wire_estimated_tokens,
        "final_wire_estimated_input_tokens": (quote.final_wire_estimated_input_tokens),
        "effective_input_budget_tokens": quote.effective_input_budget_tokens,
        "final_wire_utf8_bytes": quote.final_wire_utf8_bytes,
    }


def _cache_usage_totals(
    records: tuple[dict[str, object], ...],
) -> dict[str, object]:
    reports = tuple(
        item["provider_usage"]
        for item in records
        if isinstance(item.get("provider_usage"), dict)
    )
    reported = tuple(item for item in reports if item.get("usage_status") == "reported")
    cache_observable = tuple(
        item
        for item in reported
        if isinstance(item.get("input_tokens"), int)
        and isinstance(item.get("cached_input_tokens"), int)
    )
    reported_input_tokens = sum(
        int(item["input_tokens"])
        for item in reported
        if isinstance(item.get("input_tokens"), int)
    )
    cache_observable_input_tokens = sum(
        int(item["input_tokens"]) for item in cache_observable
    )
    cached_input_tokens = sum(
        int(item["cached_input_tokens"]) for item in cache_observable
    )
    observable_rate = (
        None
        if cache_observable_input_tokens == 0
        else cached_input_tokens / cache_observable_input_tokens
    )
    complete = len(cache_observable) == len(records)
    return {
        "total_model_calls": len(records),
        "terminal_usage_observed_calls": len(reports),
        "usage_reported_calls": len(reported),
        "usage_missing_calls": len(records) - len(reported),
        "cache_field_reported_calls": len(cache_observable),
        "cache_field_missing_calls": len(records) - len(cache_observable),
        "reported_input_tokens": reported_input_tokens,
        "cache_observable_input_tokens": cache_observable_input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "observable_cache_hit_fraction": (
            f"{cached_input_tokens}/{cache_observable_input_tokens}"
        ),
        "observable_cache_hit_rate": observable_rate,
        "end_to_end_cache_hit_rate": observable_rate if complete else None,
    }


def _cache_usage_summary(
    records: tuple[dict[str, object], ...],
) -> dict[str, object]:
    result = _cache_usage_totals(records)
    purposes = sorted({str(item.get("purpose")) for item in records})
    result["by_purpose"] = {
        purpose: _cache_usage_totals(
            tuple(item for item in records if item.get("purpose") == purpose)
        )
        for purpose in purposes
    }
    return result


class _DogfoodTrace:
    def __init__(
        self,
        *,
        scenario: str,
        api_key: str,
        suppress_one_summary_usage: bool = False,
    ) -> None:
        self._secret = api_key
        self._active_turn_sequence: int | None = None
        self._turns: list[dict[str, object]] = []
        self._model_calls: list[dict[str, object]] = []
        self._tool_invocations: list[dict[str, object]] = []
        self._compactions: list[dict[str, object]] = []
        self._scenario = scenario
        self._suppress_one_summary_usage = suppress_one_summary_usage
        self._usage_suppression_count = 0

    def scrub(self, value: object) -> object:
        return _scrub_exact(value, self._secret)

    def begin_turn(self, *, command_id: str, prompt: str) -> dict[str, object]:
        if self._active_turn_sequence is not None:
            raise RuntimeError("dogfood trace turn overlap")
        record = {
            "sequence": len(self._turns) + 1,
            "command_id": command_id,
            "user_prompt": self.scrub(prompt),
            "status": "RUNNING",
        }
        self._turns.append(record)
        self._active_turn_sequence = int(record["sequence"])
        return record

    def finish_turn(self, record: dict[str, object], result: object) -> None:
        record.update(
            {
                "status": "COMPLETED",
                "turn_id": result.turn_id,  # type: ignore[attr-defined]
                "final_model_text": self.scrub(result.final_text),  # type: ignore[attr-defined]
                "model_call_count": result.model_call_count,  # type: ignore[attr-defined]
            }
        )
        self._active_turn_sequence = None

    def fail_turn(self, record: dict[str, object], error: BaseException) -> None:
        record.update(
            {
                "status": "RAISED",
                "failure_type": type(error).__name__,
                "failure_message": self.scrub(str(error)),
            }
        )
        self._active_turn_sequence = None

    def start_model_call(self, *, call, context) -> dict[str, object]:
        purpose = call.fact.purpose.value
        if call.fact.purpose is ModelCallPurpose.CONTEXT_COMPACTION_SUMMARY:
            call_kind = (
                "REPAIR"
                if context.messages[-1].role is MessageRole.TOOL_RESULT
                else "INITIAL"
            )
            summary_start = next(
                (
                    index
                    for index, message in enumerate(context.messages)
                    if message.role is MessageRole.USER
                    and any(
                        "CONTEXT CHECKPOINT COMPACTION" in item
                        for item in message.content
                    )
                ),
                len(context.messages) - 1,
            )
            ephemeral_input_suffix = tuple(
                _message_trace(message) for message in context.messages[summary_start:]
            )
        else:
            call_kind = "FOREGROUND"
            ephemeral_input_suffix = ()
        plan = context.provider_wire_input_plan
        if plan is None:
            raise RuntimeError("provider open lacks its executable final-wire plan")
        record = {
            "sequence": len(self._model_calls) + 1,
            "turn_sequence": self._active_turn_sequence,
            "resolved_model_call_id": call.resolved_model_call_id,
            "purpose": purpose,
            "call_kind": call_kind,
            "model_call_index": context.model_call_index,
            "tool_choice": context.tool_choice,
            "compiler_estimated_input_tokens": context.compiler_estimated_input_tokens,
            "provider_wire_quote": _provider_wire_quote_trace(plan.quote),
            "ephemeral_input_suffix": self.scrub(ephemeral_input_suffix),
            "normalized_blocks": [],
            "terminal": None,
        }
        self._model_calls.append(record)
        return record

    def claim_summary_usage_suppression(self, *, call) -> bool:
        if (
            not self._suppress_one_summary_usage
            or self._usage_suppression_count != 0
            or call.fact.purpose is not ModelCallPurpose.CONTEXT_COMPACTION_SUMMARY
        ):
            return False
        self._usage_suppression_count = 1
        return True

    def record_tool_invocation(self, record: dict[str, object]) -> None:
        record = {**record, "sequence": len(self._tool_invocations) + 1}
        scrubbed = self.scrub(record)
        assert isinstance(scrubbed, dict)
        self._tool_invocations.append(scrubbed)

    def record_compaction(self, record: dict[str, object]) -> None:
        scrubbed = self.scrub(record)
        assert isinstance(scrubbed, dict)
        self._compactions.append(scrubbed)

    def report(self) -> dict[str, object]:
        return {
            "scenario": self._scenario,
            "turns": self._turns,
            "model_calls": self._model_calls,
            "tool_invocations": self._tool_invocations,
            "compactions": self._compactions,
            "provider_cache_usage": _cache_usage_summary(tuple(self._model_calls)),
            "usage_suppression_count": self._usage_suppression_count,
        }


class _DogfoodScenarioFailure(RuntimeError):
    def __init__(
        self,
        *,
        scenario: str,
        cause: BaseException,
        diagnostic: dict[str, object],
    ) -> None:
        super().__init__(f"{scenario} failed: {type(cause).__name__}: {cause}")
        self.cause = cause
        self.diagnostic = diagnostic


class _TracingExecution:
    def __init__(
        self,
        delegate,
        *,
        trace: _DogfoodTrace,
        record,
        suppress_usage: bool,
    ) -> None:
        self._delegate = delegate
        self._trace = trace
        self._record = record
        self._suppress_usage = suppress_usage
        self._blocks: dict[str, dict[str, object]] = {}

    async def read_next(self):
        item = await self._delegate.read_next()
        if isinstance(
            item,
            (
                TextStartPayload,
                ThinkingStartPayload,
                DataStartPayload,
                ToolCallStartPayload,
            ),
        ):
            if isinstance(item, TextStartPayload):
                block = {"type": "TEXT", "block_identity": item.block_identity}
            elif isinstance(item, ThinkingStartPayload):
                block = {"type": "THINKING", "block_identity": item.block_identity}
            elif isinstance(item, DataStartPayload):
                block = {
                    "type": "DATA",
                    "block_identity": item.block_identity,
                    "media_type": item.media_type,
                }
            else:
                block = {
                    "type": "TOOL_CALL",
                    "block_identity": item.block_identity,
                    "tool_call_id": item.tool_call_id,
                    "tool_name": item.tool_name,
                }
            self._blocks[item.block_identity] = block
            self._record["normalized_blocks"].append(block)
        elif isinstance(
            item,
            (TextEndPayload, ThinkingEndPayload, DataEndPayload, ToolCallEndPayload),
        ):
            block = self._blocks[item.block_identity]
            if isinstance(item, (TextEndPayload, ThinkingEndPayload)):
                block["text"] = self._trace.scrub(item.final_text)
            elif isinstance(item, DataEndPayload):
                block["data"] = self._trace.scrub(item.final_data)
            else:
                block["tool_call_id"] = item.tool_call_id
                block["tool_name"] = item.tool_name
                block["arguments"] = self._trace.scrub(item.arguments_json)
        elif isinstance(item, ProviderStreamTerminal):
            reported_usage = item.usage
            if self._suppress_usage:
                item = replace(
                    item,
                    usage=TransportUsageReport(
                        usage_status="missing",
                        usage=None,
                        provider_diagnostics=reported_usage.provider_diagnostics,
                        reported_model_id=reported_usage.reported_model_id,
                    ),
                )
            self._record["provider_reported_usage"] = _provider_usage_trace(
                reported_usage
            )
            self._record["provider_usage"] = _provider_usage_trace(item.usage)
            self._record["terminal"] = {
                "kind": item.terminal_kind.value,
                "incomplete_reason": (
                    None
                    if item.incomplete_reason is None
                    else item.incomplete_reason.value
                ),
                "error": (
                    None
                    if item.error is None
                    else self._trace.scrub(item.error.model_dump(mode="json"))
                ),
            }
        return item

    async def request_cancel(self, *, reason: str) -> None:
        await self._delegate.request_cancel(reason=reason)

    async def aclose(self) -> None:
        await self._delegate.aclose()

    async def wait_physical_completion(self):
        return await self._delegate.wait_physical_completion()


class _TracingTransport:
    def __init__(self, delegate, trace: _DogfoodTrace) -> None:
        self._delegate = delegate
        self._trace = trace
        for name in (
            "api",
            "binding_id",
            "contract_version",
            "sanitizer_contract_fingerprint",
            "boundary_contract_fingerprint",
        ):
            setattr(self, name, getattr(delegate, name))

    def open_stream(self, *, call, context):
        record = self._trace.start_model_call(call=call, context=context)
        execution = self._delegate.open_stream(call=call, context=context)
        return _TracingExecution(
            execution,
            trace=self._trace,
            record=record,
            suppress_usage=self._trace.claim_summary_usage_suppression(call=call),
        )


def _dsn_with_database(dsn: str, database_name: str) -> str:
    values = conninfo_to_dict(dsn)
    values["dbname"] = database_name
    return make_conninfo(**values)


def _require_loopback_pulsara(dsn: str, *, label: str) -> None:
    values = conninfo_to_dict(dsn)
    host = str(values.get("host", "")).strip().lower()
    database = str(values.get("dbname", "")).strip()
    if host not in {"localhost", "127.0.0.1", "::1"} or database != "pulsara":
        raise RuntimeError(f"{label} must target exact loopback database pulsara")


def _drop_database(admin_root_dsn: str, database_name: str) -> None:
    with psycopg.connect(admin_root_dsn, autocommit=True) as connection:
        connection.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(database_name)
            )
        )


def _create_database(settings: PulsaraSettings) -> tuple[str, str, str]:
    admin_root_dsn = os.getenv("PULSARA_POSTGRES_ADMIN_DSN", "").strip()
    if not admin_root_dsn:
        raise RuntimeError("PULSARA_POSTGRES_ADMIN_DSN is required")
    _require_loopback_pulsara(admin_root_dsn, label="admin DSN")
    _require_loopback_pulsara(settings.storage.postgres_dsn, label="runtime DSN")
    database_name = f"pulsara_round5b_{os.getpid()}_{uuid4().hex[:10]}"
    admin_dsn = _dsn_with_database(admin_root_dsn, database_name)
    runtime_dsn = _dsn_with_database(settings.storage.postgres_dsn, database_name)
    with psycopg.connect(admin_root_dsn, autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
        )
    try:
        runner = PostgresMigrationRunner(
            admin_dsn=admin_dsn,
            runtime_dsn=runtime_dsn,
        )
        first = runner.migrate(deadline_monotonic=monotonic() + 240)
        second = runner.migrate(deadline_monotonic=monotonic() + 240)
        if first.applied_versions != (0,) or second.applied_versions != ():
            raise RuntimeError("ephemeral clean-v0 migration was not repeatable")
    except BaseException:
        _drop_database(admin_root_dsn, database_name)
        raise
    return admin_root_dsn, database_name, runtime_dsn


def _runtime_settings(env_file: str, runtime_dsn: str) -> PulsaraSettings:
    load_env_file(env_file, override=False)
    os.environ["PULSARA_MEMORY_AUTO_DENSE"] = "false"
    os.environ["PULSARA_MEMORY_EXPLICIT_RERANK"] = "false"
    settings = PulsaraSettings.from_env()
    return replace(
        settings,
        storage=StorageConfig(postgres_dsn=runtime_dsn),
    )


def _write_mcp_config(
    workspace: Path,
    servers: tuple[tuple[str, tuple[str, ...]], ...],
) -> None:
    values: dict[str, object] = {"servers": {}}
    entries = values["servers"]
    assert isinstance(entries, dict)
    for server_id, names in servers:
        entries[server_id] = {
            "enabled": True,
            "required": True,
            "scope_policy": "ROOT_ONLY",
            "supports_parallel_tool_calls": False,
            "catalog_refresh_interval_ms": "DISABLED",
            "exposure_policy": {
                "include_tool_names": list(names),
                "invalid_tool_policy": "FAIL_SERVER",
            },
            "transport": {
                "type": "stdio",
                "command": os.sys.executable,
                "args": [os.fspath(_FIXTURE)],
            },
        }
    target = workspace / ".pulsara" / "mcp.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        yaml.safe_dump(values, sort_keys=True, allow_unicode=True),
        encoding="utf-8",
    )


def _write_skill(workspace: Path, *, changed: bool = False) -> Path:
    target = workspace / ".pulsara" / "skills" / _SKILL_NAME / "SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    instruction = (
        "After this file body is available, call search_files on this SKILL.md "
        "once for the exact phrase 'Retention checkpoint sentinel'. Wait for its "
        "result. Then call the "
        "native MCP tool mcp__late__direct_echo exactly once with the text value "
        f"{_MCP_SENTINEL!r}. After its result, answer with {_SENTINEL}."
        if not changed
        else "This manifest was intentionally changed after the completed run."
    )
    target.write_text(
        "---\n"
        f"name: {_SKILL_NAME}\n"
        "description: A bounded workflow used only for the Round 5B retained "
        "Skill integration check.\n"
        "---\n\n"
        "# Retained context integration check\n\n"
        "Retention checkpoint sentinel.\n\n"
        f"{instruction}\n",
        encoding="utf-8",
    )
    return target


def _isolated_mcp_configs(workspace: Path):
    from pulsara_agent.mcp_config import load_mcp_server_configs

    return load_mcp_server_configs(
        workspace_root=workspace,
        user_config_path=workspace / ".missing-user-mcp.yaml",
        trust_workspace_config=True,
    )


def _install_provider_trace(session, trace: _DogfoodTrace) -> None:
    transports = session._model._registry._transports  # noqa: SLF001
    for api, transport in tuple(transports.items()):
        transports[api] = _TracingTransport(transport, trace)


def _install_tool_trace(
    session,
    trace: _DogfoodTrace,
    *,
    observed_tools: list[str] | None = None,
) -> None:
    tools = session._tools  # noqa: SLF001
    original = tools.invoke

    async def invoke(**kwargs):
        record = {
            "turn_id": kwargs["turn_id"],
            "assistant_entry_id": kwargs["assistant_entry_id"],
            "tool_call_id": kwargs["tool_call_id"],
            "attempt_id": kwargs["attempt_id"],
            "tool_name": kwargs["tool_name"],
            "arguments": dict(kwargs["arguments"]),
        }
        try:
            result = await original(**kwargs)
        except BaseException as exc:
            record.update(
                {
                    "status": "RAISED",
                    "failure_type": type(exc).__name__,
                    "failure_message": str(exc),
                }
            )
            trace.record_tool_invocation(record)
            raise
        record.update(
            {
                "status": "COMPLETED",
                "result_state": result.state,
                "result_content": result.content.decode("utf-8"),
                "remote_identity": result.remote_identity,
                "physical_timing": result.physical_timing,
                "effect_class": result.effect_class,
            }
        )
        trace.record_tool_invocation(record)
        if observed_tools is not None:
            observed_tools.append(str(kwargs["tool_name"]))
        return result

    tools.invoke = invoke


async def _run_traced_turn(
    session,
    trace: _DogfoodTrace,
    prompt: str,
    *,
    command_id: str,
):
    record = trace.begin_turn(command_id=command_id, prompt=prompt)
    try:
        result = await session.run_turn(prompt, command_id=command_id)
    except BaseException as exc:
        trace.fail_turn(record, exc)
        raise
    trace.finish_turn(record, result)
    return result


def _scope(session) -> ProviderInputContinuityScope:
    return ProviderInputContinuityScope(
        session_id=session.session_id,
        scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
    )


def _current_epoch(session):
    view = session._input_continuity.current_view(_scope(session))  # noqa: SLF001
    if view is None:
        raise RuntimeError("real-provider dogfood has no installed epoch")
    return view


def _route_counts(view) -> dict[str, int]:
    result = {"DIRECT": 0, "NEW_MCP_META_ONLY": 0, "UNAVAILABLE": 0}
    for route in view.mcp_route_projection.routes:
        result[route.route.value] += 1
    return result


def _route_details(view) -> tuple[tuple[str, str, str, str], ...]:
    return tuple(
        (
            item.target.server_id,
            item.target.remote_tool_name,
            item.route.value,
            item.public_reason_code.value,
        )
        for item in view.mcp_route_projection.routes
    )


def _runtime_source_presence(view, kind: ContextSourceKind) -> str | None:
    matches = [item for item in view.source_heads if item.source_kind is kind]
    if len(matches) != 1:
        return None
    return matches[0].presence.value


def _runtime_source_body_contains(view, kind: ContextSourceKind, text: str) -> bool:
    for message in view.messages:
        try:
            observation = decode_runtime_observation(message)
        except ValueError:
            continue
        if observation.source_kind is kind and text in observation.body:
            return True
    return False


def _seed_completed_history(session, *, segments: int = 2) -> None:
    guard = session._lease.guard  # noqa: SLF001
    repository = session.repository
    for index in range(segments):
        suffix = uuid4().hex
        turn_id = f"turn:round5b-seed:{suffix}"
        repository.start_root_turn(
            guard,
            command_id=f"command:round5b-seed:{suffix}",
            turn_id=turn_id,
            entry_id=f"entry:round5b-seed-user:{suffix}",
            context_binding_revision_id=f"revision:round5b-seed:{suffix}",
            permission_snapshot_id=f"permission:round5b-seed:{suffix}",
            requested_permission_mode=DEFAULT_PERMISSION_MODE,
            content=InlineContent.from_bytes(
                (f"historical-segment-{index}:" + " context" * 6_000).encode()
            ),
            occurred_at=datetime.now(timezone.utc),
            actor_id="round5b-dogfood",
            deadline_monotonic=monotonic() + 60,
        )
        cut = repository.prepare_provider_input_cut(
            guard,
            turn_id=turn_id,
            deadline_monotonic=monotonic() + 60,
        )
        assistant_id = f"entry:round5b-seed-assistant:{suffix}"
        repository.commit_assistant_message(
            guard,
            cut=cut,
            entry_id=assistant_id,
            parent_content=InlineContent.from_bytes(b"seed complete"),
            blocks=(
                AssistantTextBlock(
                    block_id=f"block:round5b-seed:{suffix}",
                    text=InlineContent.from_bytes(b"seed complete"),
                ),
            ),
            provider_wire_api="openai_chat_completions",
            complete_turn=True,
            occurred_at=datetime.now(timezone.utc),
            actor_id="round5b-dogfood",
            deadline_monotonic=monotonic() + 60,
        )


class _RecordingTransport:
    def __init__(self, delegate, on_open) -> None:
        self._delegate = delegate
        self._on_open = on_open
        self.binding_id = delegate.binding_id
        self.contract_version = delegate.contract_version

    def open_stream(self, *, call, context):
        self._on_open(context)
        return self._delegate.open_stream(call=call, context=context)


def _install_summary_recorder(session, records: list[dict[str, object]]) -> None:
    model = session._model  # noqa: SLF001
    original = model.resolve_compaction_summary_call
    previous_initial_wire: tuple[object, ...] | None = None

    def resolve(**kwargs):
        call = original(**kwargs)

        def opened(context) -> None:
            nonlocal previous_initial_wire
            old = _current_epoch(session)
            materialization = context.provider_wire_input_plan.materialization
            old_wire = old.wire_input_plan.materialization
            is_repair = context.messages[-1].role is MessageRole.TOOL_RESULT
            if is_repair:
                wire_prefix_exact = bool(
                    previous_initial_wire is not None
                    and materialization.ordered_input_items[
                        : len(previous_initial_wire)
                    ]
                    == previous_initial_wire
                )
            else:
                overlap = min(
                    len(materialization.ordered_input_items) - 1,
                    len(old_wire.ordered_input_items),
                )
                wire_prefix_exact = (
                    materialization.ordered_input_items[:overlap]
                    == old_wire.ordered_input_items[:overlap]
                )
                previous_initial_wire = materialization.ordered_input_items
            records.append(
                {
                    "call_kind": "REPAIR" if is_repair else "INITIAL",
                    "old_epoch_nonce": old.epoch_nonce,
                    "old_epoch_revision": old.epoch_revision,
                    "old_semantic_prefix": old.semantic_prefix_fingerprint,
                    "old_route_counts": _route_counts(old),
                    "tool_choice": context.tool_choice,
                    "tools_exact": materialization.tool_items == old_wire.tool_items,
                    "wire_prefix_exact": wire_prefix_exact,
                    "input_tokens": context.compiler_estimated_input_tokens,
                    "provider_wire_quote": _provider_wire_quote_trace(
                        context.provider_wire_input_plan.quote
                    ),
                }
            )

        transport = _RecordingTransport(call.target.transport, opened)
        return replace(call, target=replace(call.target, transport=transport))

    model.resolve_compaction_summary_call = resolve


def _summary_call_shape(records: list[dict[str, object]]) -> dict[str, object]:
    initial_calls = 0
    repair_calls = 0
    sequence_valid = True
    previous_kind: str | None = None
    for item in records:
        kind = str(item["call_kind"])
        if kind == "INITIAL":
            initial_calls += 1
        elif kind == "REPAIR":
            repair_calls += 1
            if previous_kind != "INITIAL":
                sequence_valid = False
        else:
            sequence_valid = False
        previous_kind = kind
    return {
        "physical_calls": len(records),
        "initial_calls": initial_calls,
        "repair_calls": repair_calls,
        "at_most_one_repair_per_initial": (
            sequence_valid and repair_calls <= initial_calls
        ),
    }


def _install_read_activation(session, observed_tools: list[str]) -> None:
    tools = session._tools  # noqa: SLF001
    original = tools.invoke

    async def invoke(**kwargs):
        result = await original(**kwargs)
        name = str(kwargs["tool_name"])
        observed_tools.append(name)
        if name == "search_files" and observed_tools.count("search_files") == 1:
            session._compaction.policy = ResolvedCompactionPolicy(  # noqa: SLF001
                automatic_enabled=True,
                auto_trigger_ratio=0.35,
                post_compaction_target_ratio=0.30,
                minimum_reclaim_tokens=1,
                maximum_retained_tool_groups=1,
            )
        return result

    tools.invoke = invoke


def _install_compaction_trigger_recorder(
    session,
    records: list[dict[str, object]],
    observed_tools: list[str],
    trace: _DogfoodTrace,
) -> None:
    """Record every exact safe-point trigger and terminal outcome."""

    coordinator = session._runner.compaction  # noqa: SLF001
    original = coordinator.execute_active

    async def execute(**kwargs):
        from pulsara_agent.conversation_kernel.compaction import (
            coordinator as compaction_module,
        )

        before = _snapshot_fingerprints(session)
        observed_before = tuple(observed_tools)
        base = {
            "sequence": len(records) + 1,
            "trigger": kwargs["trigger"].value,
            "turn_id": kwargs["turn_id"],
            "model_call_index": kwargs["model_call_index"],
            "tool_calls_before_compaction": len(observed_before),
            "tool_names_before_compaction": observed_before,
            "snapshot_count_before": len(before),
        }
        wire_transitions: list[dict[str, object]] = []
        validate_transition = compaction_module.validate_compaction_wire_transition

        def record_wire_transition(**values):
            try:
                transition = validate_transition(**values)
            except BaseException as exc:
                source_candidate = values["source_candidate"]
                successor_wire = values["successor_wire"]
                successor_candidate = successor_wire.candidate
                source_call = source_candidate.call
                successor_call = successor_candidate.call
                source_identity = (
                    source_candidate.semantic_input.canonical_input_identity
                )
                successor_identity = (
                    successor_candidate.semantic_input.canonical_input_identity
                )
                wire_transitions.append(
                    {
                        "phase": values["phase"],
                        "validation_failure_type": type(exc).__name__,
                        "validation_failure_message": str(exc),
                        "source": _provider_wire_quote_trace(
                            values["source_view"].provider_wire_quote
                        ),
                        "successor": _provider_wire_quote_trace(successor_wire.quote),
                        "target_fact_equal": (
                            source_call.target.fact == successor_call.target.fact
                        ),
                        "provider_profile_equal": (
                            source_call.target.model_profile.provider_profile
                            == successor_call.target.model_profile.provider_profile
                        ),
                        "native_projection_equal": (
                            source_candidate.native_projection_set
                            == successor_candidate.native_projection_set
                        ),
                        "scope_equal": (
                            source_identity.session_id == successor_identity.session_id
                            and source_identity.turn_id == successor_identity.turn_id
                            and source_identity.conversation_scope_kind
                            is successor_identity.conversation_scope_kind
                            and source_identity.scope_subagent_task_id
                            == successor_identity.scope_subagent_task_id
                        ),
                        "lineage_exact": (
                            compaction_module._compaction_cut_lineage_exactly_joins(
                                source_candidate=source_candidate,
                                successor_candidate=successor_candidate,
                                phase=values["phase"],
                            )
                        ),
                        "source_binding": repr(
                            source_candidate.canonical_read.compile_snapshot.context_binding_fact
                        ),
                        "successor_binding": repr(
                            successor_candidate.canonical_read.compile_snapshot.context_binding_fact
                        ),
                        "source_identity": repr(source_identity),
                        "successor_identity": repr(successor_identity),
                    }
                )
                raise
            source = transition.source_quote
            successor = transition.successor_quote
            wire_transitions.append(
                {
                    "phase": transition.phase,
                    "source": _provider_wire_quote_trace(source),
                    "successor": _provider_wire_quote_trace(successor),
                    "estimated_reclaim_tokens": transition.reclaim_tokens,
                    "exact_wire_byte_delta": (
                        source.final_wire_utf8_bytes - successor.final_wire_utf8_bytes
                    ),
                }
            )
            return transition

        compaction_module.validate_compaction_wire_transition = record_wire_transition
        try:
            result = await original(**kwargs)
        except BaseException as exc:
            failed = {
                **base,
                "snapshot_count_after": len(_snapshot_fingerprints(session)),
                "disposition": "RAISED",
                "failure_type": type(exc).__name__,
                "failure_message": str(exc),
                "wire_transitions": tuple(wire_transitions),
            }
            records.append(failed)
            trace.record_compaction(failed)
            raise
        finally:
            compaction_module.validate_compaction_wire_transition = validate_transition
        outcome = result.outcome
        after = _snapshot_fingerprints(session)
        completed = {
            **base,
            "snapshot_count_after": len(after),
            "disposition": outcome.disposition.value,
            "snapshot_id_present": outcome.snapshot_id is not None,
            "revision_ordinal": outcome.revision_ordinal,
            "public_code": outcome.public_code,
            "wire_transitions": tuple(wire_transitions),
        }
        records.append(completed)
        trace.record_compaction(completed)
        return result

    coordinator.execute_active = execute


def _snapshot_fingerprints(session) -> tuple[str, ...]:
    with session.repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        rows = connection.execute(
            "SELECT content_digest FROM pulsara_v3.context_snapshots "
            "WHERE session_id = %s ORDER BY created_at, id",
            (session.session_id,),
        ).fetchall()
    return tuple(str(row[0]) for row in rows)


def _tool_trajectory(session) -> tuple[str, ...]:
    with session.repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        rows = connection.execute(
            "SELECT b.tool_name FROM pulsara_v3.assistant_message_blocks b "
            "JOIN pulsara_v3.transcript_entries e "
            "ON e.session_id=b.session_id AND e.id=b.assistant_entry_id "
            "WHERE b.session_id=%s AND b.block_kind='TOOL_CALL' "
            "ORDER BY e.entry_sequence,b.block_ordinal",
            (session.session_id,),
        ).fetchall()
    return tuple(str(row[0]) for row in rows)


def _tool_trajectory_for_turn(session, turn_id: str) -> tuple[str, ...]:
    with session.repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        rows = connection.execute(
            "SELECT b.tool_name FROM pulsara_v3.assistant_message_blocks b "
            "JOIN pulsara_v3.transcript_entries e "
            "ON e.session_id=b.session_id AND e.id=b.assistant_entry_id "
            "WHERE b.session_id=%s AND e.turn_id=%s "
            "AND b.block_kind='TOOL_CALL' "
            "ORDER BY e.entry_sequence,b.block_ordinal",
            (session.session_id, turn_id),
        ).fetchall()
    return tuple(str(row[0]) for row in rows)


async def _run_retained_and_repeated(
    *, settings: PulsaraSettings, workspace: Path
) -> dict[str, object]:
    import pulsara_agent.conversation_kernel.host as host_module

    _write_mcp_config(workspace, (("direct", ("direct_echo",)),))
    _write_skill(workspace)
    original_loader = host_module.load_mcp_server_configs
    host_module.load_mcp_server_configs = lambda **_: _isolated_mcp_configs(workspace)
    core = KernelHostCore.production(settings=settings)
    trace = _DogfoodTrace(
        scenario="retained_and_repeated",
        api_key=settings.llm.api_key,
        suppress_one_summary_usage=True,
    )
    summary_records: list[dict[str, object]] = []
    compaction_records: list[dict[str, object]] = []
    observed_tools: list[str] = []
    try:
        session = await core.open_session(
            HostWorkspaceInput(
                workspace_kind="project",
                workspace_root=workspace,
                trust_workspace_mcp_config=True,
            ),
            system_prompt=(
                "You are performing a bounded local product integration task. "
                "Use catalog and tool observations accurately, and keep final "
                "answers brief."
            ),
        )
        _install_provider_trace(session, trace)
        _install_tool_trace(session, trace)
        session._compaction.policy = ResolvedCompactionPolicy(  # noqa: SLF001
            automatic_enabled=False,
            minimum_reclaim_tokens=1,
        )
        ready = await session._mcp_supervisor.wait_for_server_settlement(  # noqa: SLF001
            "direct", timeout_seconds=20
        )
        if ready.value != "READY":
            raise RuntimeError("initial MCP fixture did not become READY")
        await _run_traced_turn(
            session,
            trace,
            "Acknowledge this integration preflight briefly without using a tool.",
            command_id="command:round5b:preflight",
        )
        initial_epoch = _current_epoch(session)

        _write_mcp_config(
            workspace,
            (("direct", ("direct_echo",)), ("late", ("direct_echo",))),
        )
        await session.reload_mcp_configs(
            _isolated_mcp_configs(workspace),
            deadline_monotonic=monotonic() + 30,
        )
        ready = await session._mcp_supervisor.wait_for_server_settlement(  # noqa: SLF001
            "late", timeout_seconds=20
        )
        if ready.value != "READY":
            raise RuntimeError("late MCP fixture did not become READY")
        inspected = await _run_traced_turn(
            session,
            trace,
            "Inspect the newly listed late-server direct_echo tool so its exact "
            "argument shape is available for a later task. Do not execute it yet.",
            command_id="command:round5b:inspect",
        )
        del inspected
        before_compaction = _current_epoch(session)
        old_ref_tokens = tuple(session._tools._mcp_meta_refs._refs)  # noqa: SLF001
        if not old_ref_tokens:
            raise RuntimeError("provider did not install an exact MCP inspect ref")

        _seed_completed_history(session)
        _install_summary_recorder(session, summary_records)
        _install_read_activation(session, observed_tools)
        _install_compaction_trigger_recorder(
            session,
            compaction_records,
            observed_tools,
            trace,
        )
        skill_result = await _run_traced_turn(
            session,
            trace,
            f"Use the cataloged {_SKILL_NAME} workflow for this bounded check. "
            "If its exact body is not already present in current model input, "
            f"call read_file on .pulsara/skills/{_SKILL_NAME}/SKILL.md with "
            "offset 1 and limit 2000 exactly once. If the body is already "
            "present, do not read it again. Then follow that workflow.",
            command_id="command:round5b:skill",
        )
        first_successor = _current_epoch(session)
        if not summary_records:
            raise RuntimeError(
                "skill run did not trigger compaction: installed_estimate="
                f"{first_successor.final_estimate.total_input_tokens}; tools="
                f"{','.join(observed_tools)}"
            )
        first_snapshots = _snapshot_fingerprints(session)
        try:
            session._tools._mcp_meta_refs.resolve_callable(  # noqa: SLF001
                old_ref_tokens[0],
                conversation_scope_kind=ModelInputScopeKind.ROOT,
                scope_subagent_task_id=None,
                continuity_epoch_nonce=first_successor.epoch_nonce,
            )
        except LookupError:
            old_ref_stale = True
        else:
            old_ref_stale = False

        # A later true ROOT run establishes a second active compaction and a
        # second durable snapshot.  The current correction remains in the
        # protected tail and must override any prior summary.
        _write_skill(workspace, changed=True)
        _seed_completed_history(session, segments=12)
        session._compaction.policy = ResolvedCompactionPolicy(  # noqa: SLF001
            automatic_enabled=True,
            auto_trigger_ratio=0.85,
            post_compaction_target_ratio=0.80,
            minimum_reclaim_tokens=1,
        )
        corrected = await _run_traced_turn(
            session,
            trace,
            "Correction: the current answer sentinel is BLUE. Reply with BLUE "
            "and do not use any tool.",
            command_id="command:round5b:correction",
        )
        second_successor = _current_epoch(session)
        all_snapshots = _snapshot_fingerprints(session)
        trajectory = _tool_trajectory(session)
        skill_turn_trajectory = _tool_trajectory_for_turn(
            session,
            skill_result.turn_id,
        )
        mid_turn_records = tuple(
            item
            for item in compaction_records
            if item["trigger"] == CompactionTrigger.MID_TURN_FOLLOWUP.value
        )
        mid_turn = mid_turn_records[0] if len(mid_turn_records) == 1 else None
        try:
            skill_mcp_index = skill_turn_trajectory.index("mcp__late__direct_echo")
        except ValueError:
            skill_mcp_index = -1
        required_skill_sequence_seen = bool(
            skill_turn_trajectory[:1] == ("read_file",)
            and skill_turn_trajectory.count("read_file") == 1
            and skill_turn_trajectory.count("mcp__late__direct_echo") == 1
            and skill_mcp_index > 0
            and skill_turn_trajectory[1:skill_mcp_index].count("search_files") >= 1
        )
        result = {
            "initial_epoch_revision": initial_epoch.epoch_revision,
            "late_meta_before_compaction": (
                _route_counts(before_compaction)["NEW_MCP_META_ONLY"] == 1
            ),
            "summary_calls": len(summary_records),
            "summary_call_shape": _summary_call_shape(summary_records),
            "summary_proofs": tuple(summary_records),
            "first_snapshot_count": len(first_snapshots),
            "final_snapshot_count": len(all_snapshots),
            "snapshot_fingerprints": all_snapshots,
            "successor_epoch_changed": (
                first_successor.epoch_nonce != summary_records[0]["old_epoch_nonce"]
            ),
            "successor_routes": _route_counts(first_successor),
            "successor_route_details": _route_details(first_successor),
            "old_meta_ref_stale": old_ref_stale,
            "retained_skill_presence": _runtime_source_presence(
                first_successor, ContextSourceKind.RETAINED_SKILL_CONTEXT
            ),
            "retained_skill_exact_body_seen": _runtime_source_body_contains(
                first_successor,
                ContextSourceKind.RETAINED_SKILL_CONTEXT,
                _MCP_SENTINEL,
            ),
            "skill_final_sentinel": _SENTINEL in skill_result.final_text,
            "read_file_calls": observed_tools.count("read_file"),
            "search_file_calls": observed_tools.count("search_files"),
            "late_direct_calls": observed_tools.count("mcp__late__direct_echo"),
            "second_epoch_changed": (
                second_successor.epoch_nonce != first_successor.epoch_nonce
            ),
            "second_binding_uses_latest_snapshot": (
                len(all_snapshots) == 2
                and second_successor.canonical_frontier.context_base_semantic_identity
                != first_successor.canonical_frontier.context_base_semantic_identity
            ),
            "new_root_retained_skill_presence": _runtime_source_presence(
                second_successor, ContextSourceKind.RETAINED_SKILL_CONTEXT
            ),
            "correction_won": "BLUE" in corrected.final_text,
            "canonical_tool_trajectory": trajectory,
            "tool_attempt_count": len(trajectory),
            "diagnostic_trace": trace.report(),
            "agentic_mid_turn": {
                "exactly_one_mid_turn_compaction": len(mid_turn_records) == 1,
                "same_turn": (
                    mid_turn is not None and mid_turn["turn_id"] == skill_result.turn_id
                ),
                "trigger": None if mid_turn is None else mid_turn["trigger"],
                "disposition": (None if mid_turn is None else mid_turn["disposition"]),
                "snapshot_count_before": (
                    None if mid_turn is None else mid_turn["snapshot_count_before"]
                ),
                "snapshot_count_after": (
                    None if mid_turn is None else mid_turn["snapshot_count_after"]
                ),
                "tool_calls_before_compaction": (
                    None
                    if mid_turn is None
                    else mid_turn["tool_calls_before_compaction"]
                ),
                "tool_names_before_compaction": (
                    () if mid_turn is None else mid_turn["tool_names_before_compaction"]
                ),
                "same_turn_tool_trajectory": skill_turn_trajectory,
                "post_compaction_tool_suffix": skill_turn_trajectory[2:],
                "required_skill_sequence_seen": required_skill_sequence_seen,
                "model_call_count": skill_result.model_call_count,
                "final_answer_nonempty": bool(skill_result.final_text.strip()),
            },
        }
        result["passed"] = bool(
            result["late_meta_before_compaction"]
            and result["summary_call_shape"]["initial_calls"] >= 2
            and result["summary_call_shape"]["at_most_one_repair_per_initial"]
            and len(compaction_records) == 2
            and all(item["disposition"] == "COMPACTED" for item in compaction_records)
            and all(
                item["tool_choice"] == "auto"
                and item["tools_exact"]
                and item["wire_prefix_exact"]
                for item in summary_records
            )
            and result["first_snapshot_count"] == 1
            and result["final_snapshot_count"] == 2
            and result["successor_epoch_changed"]
            and result["successor_routes"]["DIRECT"] == 2
            and result["old_meta_ref_stale"]
            and result["retained_skill_presence"] == "VALUE"
            and result["retained_skill_exact_body_seen"]
            and result["skill_final_sentinel"]
            and result["read_file_calls"] == 1
            and result["search_file_calls"] >= 1
            and result["late_direct_calls"] == 1
            and result["second_epoch_changed"]
            and result["second_binding_uses_latest_snapshot"]
            and result["new_root_retained_skill_presence"] in {None, "CLEARED"}
            and result["correction_won"]
            and result["agentic_mid_turn"]["exactly_one_mid_turn_compaction"]
            and result["agentic_mid_turn"]["same_turn"]
            and result["agentic_mid_turn"]["trigger"]
            == CompactionTrigger.MID_TURN_FOLLOWUP.value
            and result["agentic_mid_turn"]["disposition"] == "COMPACTED"
            and result["agentic_mid_turn"]["snapshot_count_before"] == 0
            and result["agentic_mid_turn"]["snapshot_count_after"] == 1
            and result["agentic_mid_turn"]["tool_calls_before_compaction"] == 2
            and result["agentic_mid_turn"]["same_turn_tool_trajectory"]
            and result["agentic_mid_turn"]["required_skill_sequence_seen"]
            and result["agentic_mid_turn"]["final_answer_nonempty"]
        )
        return result
    except BaseException as exc:
        raise _DogfoodScenarioFailure(
            scenario="retained_and_repeated",
            cause=exc,
            diagnostic={
                "trace": trace.report(),
                "summary_records": tuple(summary_records),
                "compaction_records": tuple(compaction_records),
                "observed_tools": tuple(observed_tools),
            },
        ) from exc
    finally:
        try:
            await core.shutdown()
        finally:
            host_module.load_mcp_server_configs = original_loader


async def _run_overbound(
    *, settings: PulsaraSettings, workspace: Path
) -> dict[str, object]:
    import pulsara_agent.conversation_kernel.host as host_module

    _write_mcp_config(workspace, (("direct", ("direct_echo",)),))
    original_loader = host_module.load_mcp_server_configs
    host_module.load_mcp_server_configs = lambda **_: _isolated_mcp_configs(workspace)
    core = KernelHostCore.production(settings=settings)
    trace = _DogfoodTrace(
        scenario="overbound_mcp",
        api_key=settings.llm.api_key,
    )
    summary_records: list[dict[str, object]] = []
    compaction_records: list[dict[str, object]] = []
    observed_tools: list[str] = []
    try:
        session = await core.open_session(
            HostWorkspaceInput(
                workspace_kind="project",
                workspace_root=workspace,
                trust_workspace_mcp_config=True,
            ),
            system_prompt=(
                "You are performing a bounded MCP integration check. Use the "
                "advertised catalog route and keep the final answer brief."
            ),
        )
        _install_provider_trace(session, trace)
        _install_tool_trace(session, trace, observed_tools=observed_tools)
        session._compaction.policy = ResolvedCompactionPolicy(  # noqa: SLF001
            automatic_enabled=False,
            minimum_reclaim_tokens=1,
        )
        ready = await session._mcp_supervisor.wait_for_server_settlement(  # noqa: SLF001
            "direct", timeout_seconds=20
        )
        if ready.value != "READY":
            raise RuntimeError("overbound initial MCP fixture did not become READY")
        await _run_traced_turn(
            session,
            trace,
            "Acknowledge this preflight briefly without a tool.",
            command_id="command:round5b:overbound-preflight",
        )
        _seed_completed_history(session, segments=15)
        _write_mcp_config(
            workspace,
            (
                ("direct", ("direct_echo",)),
                ("bulk", tuple(f"bulk_{index:02d}" for index in range(48))),
            ),
        )
        await session.reload_mcp_configs(
            _isolated_mcp_configs(workspace),
            deadline_monotonic=monotonic() + 30,
        )
        ready = await session._mcp_supervisor.wait_for_server_settlement(  # noqa: SLF001
            "bulk", timeout_seconds=20
        )
        if ready.value != "READY":
            raise RuntimeError("overbound MCP fixture did not become READY")
        _install_summary_recorder(session, summary_records)
        _install_compaction_trigger_recorder(
            session,
            compaction_records,
            observed_tools,
            trace,
        )
        session._compaction.policy = ResolvedCompactionPolicy(  # noqa: SLF001
            automatic_enabled=True,
            auto_trigger_ratio=0.99,
            post_compaction_target_ratio=0.98,
            minimum_reclaim_tokens=1,
        )
        final = await _run_traced_turn(
            session,
            trace,
            "Use bulk_00 from MCP server bulk once with the harmless text value "
            "round5b, following the current catalog route, then report its result.",
            command_id="command:round5b:overbound",
        )
        successor = _current_epoch(session)
        routes = _route_counts(successor)
        trajectory = _tool_trajectory(session)
        result = {
            "summary_calls": len(summary_records),
            "summary_call_shape": _summary_call_shape(summary_records),
            "summary_proofs": tuple(summary_records),
            "snapshot_count": len(_snapshot_fingerprints(session)),
            "route_counts": routes,
            "route_details": _route_details(successor),
            "builtin_direct_count": sum(
                not item.provider_name.startswith("mcp__")
                for item in successor.direct_native_projection_set.tool_versions
            ),
            "partial_mcp_direct_count": sum(
                item.provider_name.startswith("mcp__")
                for item in successor.direct_native_projection_set.tool_versions
            ),
            "inspect_calls": trajectory.count("inspect_new_mcp_tool"),
            "meta_use_calls": trajectory.count("use_new_mcp_tool"),
            "canonical_tool_trajectory": trajectory,
            "final_nonempty": bool(final.final_text.strip()),
            "diagnostic_trace": trace.report(),
        }
        result["passed"] = bool(
            result["summary_call_shape"]["initial_calls"] == 1
            and result["summary_call_shape"]["at_most_one_repair_per_initial"]
            and result["snapshot_count"] == 1
            and routes["DIRECT"] == 0
            and routes["NEW_MCP_META_ONLY"] == 49
            and result["builtin_direct_count"] > 0
            and result["partial_mcp_direct_count"] == 0
            and result["inspect_calls"] == 1
            and result["meta_use_calls"] == 1
            and result["final_nonempty"]
        )
        return result
    except BaseException as exc:
        raise _DogfoodScenarioFailure(
            scenario="overbound_mcp",
            cause=exc,
            diagnostic={
                "trace": trace.report(),
                "summary_records": tuple(summary_records),
                "compaction_records": tuple(compaction_records),
                "observed_tools": tuple(observed_tools),
            },
        ) from exc
    finally:
        try:
            await core.shutdown()
        finally:
            host_module.load_mcp_server_configs = original_loader


async def _run(settings: PulsaraSettings) -> dict[str, object]:
    with TemporaryDirectory(prefix="pulsara-round5b-retained-") as first_dir:
        retained = await _run_retained_and_repeated(
            settings=settings,
            workspace=Path(first_dir),
        )
    with TemporaryDirectory(prefix="pulsara-round5b-overbound-") as second_dir:
        overbound = await _run_overbound(
            settings=settings,
            workspace=Path(second_dir),
        )
    retained_calls = tuple(
        retained["diagnostic_trace"]["model_calls"]  # type: ignore[index]
    )
    overbound_calls = tuple(
        overbound["diagnostic_trace"]["model_calls"]  # type: ignore[index]
    )
    usage = _cache_usage_summary(retained_calls + overbound_calls)
    return {
        "schema_version": _SCHEMA_VERSION,
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        "provider_api": settings.llm.api,
        "provider_model": settings.llm.pro.model_id,
        "retained_and_repeated": retained,
        "overbound_mcp": overbound,
        "diagnostic_trace_recorded": True,
        "pulsara_api_key_recorded": False,
        "provider_cache_usage": usage,
        "status": (
            "passed"
            if retained["passed"]
            and overbound["passed"]
            and usage["usage_reported_calls"] > 0
            and usage["usage_missing_calls"] > 0
            else "failed"
        ),
    }


@contextmanager
def _isolated_user_definition_environment():
    """Keep this fixed-fixture dogfood independent of operator-owned roots."""

    original_home = os.environ.get("HOME")
    original_pulsara_home = os.environ.get("PULSARA_HOME")
    try:
        with TemporaryDirectory(prefix="pulsara-round5b-home-") as directory:
            root = Path(directory).resolve(strict=True)
            home = root / "home"
            product_home = root / "pulsara-home"
            home.mkdir()
            product_home.mkdir()
            os.environ["HOME"] = os.fspath(home)
            os.environ["PULSARA_HOME"] = os.fspath(product_home)
            yield
    finally:
        if original_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = original_home
        if original_pulsara_home is None:
            os.environ.pop("PULSARA_HOME", None)
        else:
            os.environ["PULSARA_HOME"] = original_pulsara_home


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=".env")
    parser.add_argument(
        "--trace-output",
        help="write the exact API-key-scrubbed observable report as JSON",
    )
    args = parser.parse_args()
    load_env_file(args.env_file, override=False)
    initial = PulsaraSettings.from_env()
    admin_root_dsn, database_name, runtime_dsn = _create_database(initial)
    try:
        with _isolated_user_definition_environment():
            report = asyncio.run(_run(_runtime_settings(args.env_file, runtime_dsn)))
    except BaseException as exc:
        root_failure = exc.cause if isinstance(exc, _DogfoodScenarioFailure) else exc
        frame = traceback.extract_tb(root_failure.__traceback__)[-1]
        report = {
            "schema_version": _SCHEMA_VERSION,
            "completed_at_utc": datetime.now(timezone.utc).isoformat(),
            "status": "external_or_runtime_failure",
            "provider_api": initial.llm.api,
            "provider_model": initial.llm.pro.model_id,
            "failure_type": type(root_failure).__name__,
            "failure_message": _scrub_exact(
                str(root_failure)[:512], initial.llm.api_key
            ),
            "failure_site": f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}",
            "failure_traceback": _scrub_exact(
                traceback.format_exc(), initial.llm.api_key
            ),
            "failure_diagnostic": (
                None
                if not isinstance(exc, _DogfoodScenarioFailure)
                else _scrub_exact(exc.diagnostic, initial.llm.api_key)
            ),
            "diagnostic_trace_recorded": isinstance(exc, _DogfoodScenarioFailure),
            "pulsara_api_key_recorded": False,
        }
    finally:
        _drop_database(admin_root_dsn, database_name)
    scrubbed_report = _scrub_exact(report, initial.llm.api_key)
    encoded_report = json.dumps(scrubbed_report, sort_keys=True)
    if initial.llm.api_key and initial.llm.api_key in encoded_report:
        raise RuntimeError("dogfood report retained PULSARA_API_KEY")
    if args.trace_output:
        Path(args.trace_output).expanduser().resolve().write_text(
            json.dumps(
                scrubbed_report,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    print(encoded_report)
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
