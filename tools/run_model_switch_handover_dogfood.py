"""Run the real-provider model-switch handover dogfood.

The probe reads two existing local model connections, but never mutates or
copies their secrets.  It owns an ephemeral verified loopback PostgreSQL
database and a temporary workspace, installs a real source-model epoch with a
tool call, appends deterministic canonical history, then switches to the real
destination model.  The emitted report contains exact normalized model output
and local final-wire measurements; configured API-key values are scrubbed.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from typing import Any
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from pulsara_agent.conversation_kernel.compaction.contracts import (
    ResolvedCompactionPolicy,
)
from pulsara_agent.conversation_kernel.contracts import InlineContent
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.conversation_kernel.repository import AssistantTextBlock
from pulsara_agent.conversation_kernel.repository import (
    build_prepared_root_turn_intent,
)
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, ModelsDevCatalogClient
from pulsara_agent.llm.model_connections import (
    ModelCallBinding,
    reasoning_selection_to_dict,
)
from pulsara_agent.llm.model_target import default_reasoning_selection
from pulsara_agent.llm.normalized_transport import NormalizedLLMTransport
from pulsara_agent.llm.provider_sanitization import sanitize_provider_failure
from pulsara_agent.llm.resolution import ResolvedModelTarget
from pulsara_agent.llm.result import TransportUsageReport
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.ports.live_agent_event import (
    DataEndPayload,
    TextEndPayload,
    ThinkingEndPayload,
    ToolCallEndPayload,
)
from pulsara_agent.ports.provider_stream import (
    ProviderNormalizedTerminalKind,
    ProviderPhysicalCompletion,
    ProviderPhysicalCompletionStatus,
    ProviderStreamTerminal,
)
from pulsara_agent.primitives.permission import DEFAULT_PERMISSION_MODE
from pulsara_agent.settings import (
    LocalPostgresConfig,
    LocalSettings,
    LocalSettingsStore,
)
from pulsara_agent.storage.migrations.runner import PostgresMigrationRunner
from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane,
)
from pulsara_agent.workspace_identity import HostWorkspaceInput


_SCHEMA_VERSION = "model-switch-handover-dogfood.v2"
_SOURCE_MODEL = "openai/gpt-5.6-luna"
_DESTINATION_MODEL = "gpt-5.5"


@dataclass(frozen=True, slots=True)
class _ReadOnlySettingsStore:
    value: LocalSettings

    def read(self) -> LocalSettings:
        return self.value


@dataclass(slots=True)
class _ForcedSourceSummaryProviderError:
    connection_id: str
    used: bool = False

    def applies(self, *, purpose: str, connection_id: str) -> bool:
        if (
            self.used
            or purpose != "context_compaction_summary"
            or connection_id != self.connection_id
        ):
            return False
        self.used = True
        return True


class _ForcedProviderErrorExecution:
    def __init__(self) -> None:
        self._terminal_pending = True

    async def read_next(self):
        if not self._terminal_pending:
            return None
        self._terminal_pending = False
        return ProviderStreamTerminal(
            terminal_kind=ProviderNormalizedTerminalKind.PROVIDER_ERROR,
            usage=TransportUsageReport(usage_status="missing", usage=None),
            error=sanitize_provider_failure(
                message="dogfood-injected source provider quota exhaustion",
                code_hint="429",
            ),
        )

    async def request_cancel(self, *, reason: str) -> None:
        del reason
        self._terminal_pending = False

    async def aclose(self) -> None:
        self._terminal_pending = False

    async def wait_physical_completion(self) -> ProviderPhysicalCompletion:
        return ProviderPhysicalCompletion(
            ProviderPhysicalCompletionStatus.COMPLETED,
            None,
        )


class _RecordingExecution:
    def __init__(self, delegate: Any, record: dict[str, object]) -> None:
        self._delegate = delegate
        self._record = record

    async def read_next(self):
        item = await self._delegate.read_next()
        blocks = self._record["normalized_blocks"]
        assert isinstance(blocks, list)
        if isinstance(item, TextEndPayload):
            blocks.append({"kind": "text", "text": item.final_text})
        elif isinstance(item, ThinkingEndPayload):
            blocks.append({"kind": "thinking", "text": item.final_text})
        elif isinstance(item, ToolCallEndPayload):
            blocks.append(
                {
                    "kind": "tool_call",
                    "tool_call_id": item.tool_call_id,
                    "tool_name": item.tool_name,
                    "arguments": item.arguments_json,
                }
            )
        elif isinstance(item, DataEndPayload):
            blocks.append(
                {
                    "kind": "data",
                    "media_type": item.media_type,
                    "data": item.final_data,
                }
            )
        elif isinstance(item, ProviderStreamTerminal):
            usage = item.usage.usage
            self._record["terminal"] = {
                "kind": item.terminal_kind.value,
                "incomplete_reason": (
                    None
                    if item.incomplete_reason is None
                    else item.incomplete_reason.value
                ),
                "error_code": None if item.error is None else item.error.code.value,
                "error_message": None if item.error is None else item.error.message,
                "usage_status": item.usage.usage_status,
                "input_tokens": None if usage is None else usage.input_tokens,
                "output_tokens": None if usage is None else usage.output_tokens,
                "reasoning_output_tokens": (
                    None if usage is None else usage.reasoning_output_tokens
                ),
            }
        return item

    async def request_cancel(self, *, reason: str) -> None:
        await self._delegate.request_cancel(reason=reason)

    async def aclose(self) -> None:
        await self._delegate.aclose()

    async def wait_physical_completion(self):
        return await self._delegate.wait_physical_completion()


class _RecordingTransport:
    def __init__(
        self,
        delegate: NormalizedLLMTransport,
        records: list[dict[str, object]],
        forced_source_summary_error: _ForcedSourceSummaryProviderError | None,
    ) -> None:
        self._delegate = delegate
        self._records = records
        self._forced_source_summary_error = forced_source_summary_error
        for name in (
            "api",
            "binding_id",
            "contract_version",
            "sanitizer_contract_fingerprint",
            "boundary_contract_fingerprint",
        ):
            setattr(self, name, getattr(delegate, name))

    def open_stream(self, *, call, context):
        plan = context.provider_wire_input_plan
        if plan is None:
            raise RuntimeError("real-provider open lacks an executable wire plan")
        quote = plan.quote
        record: dict[str, object] = {
            "sequence": len(self._records) + 1,
            "purpose": call.fact.purpose.value,
            "connection_id": call.binding.connection_id.value,
            "reasoning": reasoning_selection_to_dict(call.binding.reasoning),
            "route_id": call.target.fact.route_id,
            "model_id": call.target.fact.model_id,
            "wire_api": call.target.fact.wire_api,
            "message_count": len(context.messages),
            "tool_count": len(context.tools),
            "final_wire_estimated_input_tokens": (
                quote.final_wire_estimated_input_tokens
            ),
            "effective_input_budget_tokens": quote.effective_input_budget_tokens,
            "final_wire_utf8_bytes": quote.final_wire_utf8_bytes,
            "normalized_blocks": [],
            "terminal": None,
        }
        self._records.append(record)
        if (
            self._forced_source_summary_error is not None
            and self._forced_source_summary_error.applies(
                purpose=call.fact.purpose.value,
                connection_id=call.binding.connection_id.value,
            )
        ):
            record["dogfood_injected_source_provider_error"] = True
            return _RecordingExecution(_ForcedProviderErrorExecution(), record)
        return _RecordingExecution(
            self._delegate.open_stream(call=call, context=context), record
        )


class _RecordingModelRuntime:
    def __init__(
        self,
        delegate: ModelRuntime,
        records: list[dict[str, object]],
        forced_source_summary_error: _ForcedSourceSummaryProviderError | None,
    ) -> None:
        self._delegate = delegate
        self._records = records
        self._forced_source_summary_error = forced_source_summary_error
        self.settings = delegate.settings
        self.catalog = delegate.catalog
        self.route_wires = delegate.route_wires

    def connection(self, binding):
        return self._delegate.connection(binding)

    def selectable_catalog(self):
        return self._delegate.selectable_catalog()

    def freeze_resolution_snapshot(self):
        return self._delegate.freeze_resolution_snapshot()

    def resolve_target(self, binding, *, timeout_policy) -> ResolvedModelTarget:
        target = self._delegate.resolve_target(binding, timeout_policy=timeout_policy)
        return replace(
            target,
            transport=_RecordingTransport(
                target.transport,
                self._records,
                self._forced_source_summary_error,
            ),
        )


def _require_disposable_local_root(dsn: str, *, label: str) -> None:
    values = conninfo_to_dict(dsn)
    host = str(values.get("host", "")).strip().casefold()
    database = str(values.get("dbname", "")).strip()
    if host not in {"localhost", "127.0.0.1", "::1"} or database != "pulsara":
        raise RuntimeError(f"{label} must name the exact loopback pulsara database")


def _dsn_with_database(dsn: str, database: str) -> str:
    values = conninfo_to_dict(dsn)
    values["dbname"] = database
    return make_conninfo(**values)


def _create_database(settings: LocalSettings) -> tuple[str, str, str, str]:
    postgres = settings.postgres
    if postgres is None or postgres.admin_dsn is None:
        raise RuntimeError("saved local PostgreSQL admin/runtime DSNs are required")
    _require_disposable_local_root(postgres.admin_dsn, label="admin DSN")
    _require_disposable_local_root(postgres.runtime_dsn, label="runtime DSN")
    name = f"pulsara_model_switch_{os.getpid()}_{uuid4().hex[:10]}"
    admin = _dsn_with_database(postgres.admin_dsn, name)
    runtime = _dsn_with_database(postgres.runtime_dsn, name)
    with psycopg.connect(postgres.admin_dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        runner = PostgresMigrationRunner(admin_dsn=admin, runtime_dsn=runtime)
        first = runner.migrate(deadline_monotonic=monotonic() + 240)
        second = runner.migrate(deadline_monotonic=monotonic() + 240)
        if first.applied_versions != (0,) or second.applied_versions != ():
            raise RuntimeError("ephemeral clean-v0 migration was not repeatable")
    except BaseException:
        _drop_database(settings, name)
        raise
    return name, postgres.admin_dsn, admin, runtime


def _drop_database(settings: LocalSettings, name: str) -> None:
    postgres = settings.postgres
    if postgres is None or postgres.admin_dsn is None:
        return
    with psycopg.connect(postgres.admin_dsn, autocommit=True) as connection:
        connection.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(name)
            )
        )


def _seed_completed_history(
    session,
    *,
    segments: int,
    repetitions: int,
) -> int:
    guard = session._lease.guard  # noqa: SLF001
    repository = session.repository
    total_bytes = 0
    for index in range(segments):
        suffix = uuid4().hex
        turn_id = f"turn:model-switch-dogfood:{suffix}"
        body = (
            f"Historical user request {index}: preserve this exact advisory "
            + (
                "context for a later cross-model handover; "
                f"segment={index:02d}. "
            )
            * repetitions
        )
        total_bytes += len(body.encode("utf-8"))
        intent = build_prepared_root_turn_intent(
            session_id=session.session_id,
            command_id=f"command:model-switch-dogfood:{suffix}",
            turn_id=turn_id,
            entry_id=f"entry:model-switch-dogfood:user:{suffix}",
            context_binding_revision_id=f"revision:model-switch-dogfood:{suffix}",
            permission_snapshot_id=f"permission:model-switch-dogfood:{suffix}",
            requested_permission_mode=DEFAULT_PERMISSION_MODE,
            content=InlineContent.from_bytes(body.encode("utf-8")),
            occurred_at=datetime.now(timezone.utc),
            actor_id="model-switch-dogfood",
        )
        repository.accept_root_turn_intent(
            guard,
            intent=intent,
            model_resolution_snapshot=(
                session._model_runtime.freeze_resolution_snapshot()  # noqa: SLF001
            ),
            deadline_monotonic=monotonic() + 60,
        )
        cut = repository.prepare_provider_input_cut(
            guard,
            turn_id=turn_id,
            deadline_monotonic=monotonic() + 60,
        )
        assistant_text = f"Acknowledged historical segment {index}."
        repository.commit_assistant_message(
            guard,
            cut=cut,
            entry_id=f"entry:model-switch-dogfood:assistant:{suffix}",
            parent_content=InlineContent.from_bytes(assistant_text.encode("utf-8")),
            blocks=(
                AssistantTextBlock(
                    block_id=f"block:model-switch-dogfood:{suffix}",
                    text=InlineContent.from_bytes(assistant_text.encode("utf-8")),
                ),
            ),
            # These rows are deterministic public-semantic fixtures, not
            # fabricated provider-native Responses replay.
            provider_wire_api="openai_chat_completions",
            complete_turn=True,
            occurred_at=datetime.now(timezone.utc),
            actor_id="model-switch-dogfood",
            deadline_monotonic=monotonic() + 60,
        )
    return total_bytes


def _find_connection(settings: LocalSettings, model_id: str):
    matches = tuple(
        item for item in settings.model_connections if item.target.model_id == model_id
    )
    if len(matches) != 1:
        raise RuntimeError(f"expected one saved connection for model {model_id!r}")
    connection = matches[0]
    if settings.model_api_key(connection.id) is None:
        raise RuntimeError(f"saved connection {model_id!r} has no API key")
    return connection


def _binding(runtime: ModelRuntime, connection) -> ModelCallBinding:
    target = runtime.freeze_resolution_snapshot().connection(connection.id).target
    return ModelCallBinding(
        connection.id,
        default_reasoning_selection(target.reasoning),
    )


def _database_evidence(session) -> dict[str, object]:
    with session.repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        block_rows = connection.execute(
            "SELECT block_kind, count(*) FROM pulsara_v3.assistant_message_blocks "
            "WHERE session_id=%s GROUP BY block_kind ORDER BY block_kind",
            (session.session_id,),
        ).fetchall()
        snapshot_count = connection.execute(
            "SELECT count(*) FROM pulsara_v3.context_snapshots WHERE session_id=%s",
            (session.session_id,),
        ).fetchone()[0]
        adopted_count = connection.execute(
            "SELECT count(*) FROM pulsara_v3.agent_events WHERE session_id=%s "
            "AND event_type='CompactionAdopted'",
            (session.session_id,),
        ).fetchone()[0]
        tool_result_count = connection.execute(
            "SELECT count(*) FROM pulsara_v3.tool_results WHERE session_id=%s",
            (session.session_id,),
        ).fetchone()[0]
    return {
        "assistant_blocks": dict(block_rows),
        "tool_result_count": tool_result_count,
        "context_snapshot_count": snapshot_count,
        "compaction_adopted_event_count": adopted_count,
    }


def _scrub(value: object, secrets: tuple[str, ...]) -> object:
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "<MODEL_API_KEY>")
        return value
    if isinstance(value, dict):
        return {str(_scrub(key, secrets)): _scrub(item, secrets) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(item, secrets) for item in value]
    return value


async def _run(
    *,
    source_model: str,
    destination_model: str,
    segments: int,
    repetitions: int,
    calls: list[dict[str, object]],
    force_source_summary_provider_error: bool,
) -> dict[str, object]:
    saved = LocalSettingsStore().read()
    source = _find_connection(saved, source_model)
    destination = _find_connection(saved, destination_model)
    database_name, _admin_root, ephemeral_admin, ephemeral_runtime = (
        _create_database(saved)
    )
    original_pulsara_home = os.environ.get("PULSARA_HOME")
    try:
        with TemporaryDirectory(prefix="pulsara-model-switch-dogfood-") as directory:
            root = Path(directory).resolve()
            product_home = root / "pulsara-home"
            workspace = root / "workspace"
            product_home.mkdir()
            workspace.mkdir()
            (workspace / "marker.txt").write_text(
                "MODEL_SWITCH_REAL_TOOL_OK\n", encoding="utf-8"
            )
            os.environ["PULSARA_HOME"] = os.fspath(product_home)
            runtime_settings = replace(
                saved,
                postgres=LocalPostgresConfig(ephemeral_runtime, ephemeral_admin),
            )
            settings_store = _ReadOnlySettingsStore(runtime_settings)
            catalog = ModelCatalogOwner(ModelsDevCatalogClient())
            await catalog.refresh()
            delegate = ModelRuntime.production(  # type: ignore[arg-type]
                settings=settings_store,
                catalog=catalog,
            )
            forced_source_summary_error = (
                _ForcedSourceSummaryProviderError(source.id.value)
                if force_source_summary_provider_error
                else None
            )
            runtime = _RecordingModelRuntime(
                delegate,
                calls,
                forced_source_summary_error,
            )
            source_binding = _binding(delegate, source)
            destination_binding = _binding(delegate, destination)
            source_target = delegate.freeze_resolution_snapshot().validate(
                source_binding
            ).target
            destination_target = delegate.freeze_resolution_snapshot().validate(
                destination_binding
            ).target
            if (
                source_target.target_facts.limits.total_context_tokens
                <= destination_target.target_facts.limits.total_context_tokens
            ):
                raise RuntimeError("source context is not larger than destination")
            core = KernelHostCore.production(model_runtime=runtime)  # type: ignore[arg-type]
            try:
                session = await core.open_session(
                    HostWorkspaceInput(
                        workspace_kind="project",
                        workspace_root=workspace,
                        trust_workspace_mcp_config=False,
                    ),
                    system_prompt=(
                        "This is a bounded real-provider model-switch dogfood. "
                        "Follow explicit tool instructions and keep final answers brief."
                    ),
                )
                controller_id = "model-switch-handover-dogfood-controller"
                if not await session.attach_controller(controller_id):
                    raise RuntimeError("could not attach the dogfood controller")
                switch_results: list[object] = []
                execute_switch = session._runner.compaction.execute_model_switch_active  # noqa: SLF001

                async def record_switch(**kwargs):
                    result = await execute_switch(**kwargs)
                    switch_results.append(result)
                    return result

                session._runner.compaction.execute_model_switch_active = record_switch  # noqa: SLF001
                await session.update_model_call_binding(source_binding)
                session._compaction.policy = ResolvedCompactionPolicy(  # noqa: SLF001
                    automatic_enabled=False,
                    manual_enabled=False,
                    auto_trigger_ratio=0.85,
                    minimum_reclaim_tokens=1,
                )
                source_result = await session.run_turn(
                    "Call read_file exactly once on marker.txt with offset 1 and "
                    "limit 200. After its result, reply briefly and include the "
                    "exact marker MODEL_SWITCH_REAL_TOOL_OK.",
                    command_id="command:model-switch-dogfood:source",
                )
                seeded_bytes = _seed_completed_history(
                    session,
                    segments=segments,
                    repetitions=repetitions,
                )
                before_switch_evidence = _database_evidence(session)
                await session.update_model_call_binding(destination_binding)
                destination_result = await session.run_turn(
                    "The model connection has changed. Call read_file exactly once on "
                    "marker.txt with offset 1 and limit 20. After its result, answer "
                    "exactly MODEL_SWITCH_DESTINATION_OK followed by one short sentence "
                    "explaining what marker.txt contained.",
                    command_id="command:model-switch-dogfood:destination",
                )
                presentation_notices = session.take_presentation_notices(controller_id)
                evidence = _database_evidence(session)
                purposes = Counter(str(item["purpose"]) for item in calls)
                summary_models = tuple(
                    str(item["model_id"])
                    for item in calls
                    if item["purpose"] == "context_compaction_summary"
                )
                normal_models = tuple(
                    str(item["model_id"])
                    for item in calls
                    if item["purpose"] == "agent_model_loop"
                )
                summary_calls = tuple(
                    item
                    for item in calls
                    if item["purpose"] == "context_compaction_summary"
                )
                destination_calls = tuple(
                    item
                    for item in calls
                    if item["purpose"] == "agent_model_loop"
                    and item["model_id"] == destination_model
                )
                model_switch_tiers = tuple(
                    getattr(item, "model_switch_tier", None)
                    for item in switch_results
                )
                destination_tool_call_count = sum(
                    1
                    for item in destination_calls
                    for block in item["normalized_blocks"]
                    if isinstance(block, dict) and block.get("kind") == "tool_call"
                )
                destination_tool_result_count = int(
                    evidence["tool_result_count"]
                ) - int(before_switch_evidence["tool_result_count"])
                expected_tier = 3 if force_source_summary_provider_error else 2
                expected_summary_models = (
                    (source_model, destination_model)
                    if force_source_summary_provider_error
                    else (source_model,)
                )
                report: dict[str, object] = {
                    "schema_version": _SCHEMA_VERSION,
                    "completed_at_utc": datetime.now(timezone.utc).isoformat(),
                    "ephemeral_database": database_name,
                    "source": {
                        "route_id": source.target.route_id,
                        "model_id": source.target.model_id,
                        "wire_api": source.target.wire_api.value,
                        "context_tokens": (
                            source_target.target_facts.limits.total_context_tokens
                        ),
                    },
                    "destination": {
                        "route_id": destination.target.route_id,
                        "model_id": destination.target.model_id,
                        "wire_api": destination.target.wire_api.value,
                        "context_tokens": (
                            destination_target.target_facts.limits.total_context_tokens
                        ),
                    },
                    "seeded_canonical_history_utf8_bytes": seeded_bytes,
                    "source_final_text": source_result.final_text,
                    "destination_final_text": destination_result.final_text,
                    "model_call_purposes": dict(purposes),
                    "normal_model_sequence": normal_models,
                    "summary_model_sequence": summary_models,
                    "model_switch_tiers": model_switch_tiers,
                    "presentation_notices": presentation_notices,
                    "forced_source_summary_provider_error": (
                        force_source_summary_provider_error
                    ),
                    "destination_tool_call_count": destination_tool_call_count,
                    "destination_tool_result_count": destination_tool_result_count,
                    "database_evidence_before_switch": before_switch_evidence,
                    "database_evidence": evidence,
                    "model_calls": calls,
                }
                report["passed"] = bool(
                    "MODEL_SWITCH_REAL_TOOL_OK" in source_result.final_text
                    and "MODEL_SWITCH_DESTINATION_OK" in destination_result.final_text
                    and int(evidence["tool_result_count"]) >= 1
                    and int(evidence["context_snapshot_count"]) == 1
                    and int(evidence["compaction_adopted_event_count"]) == 1
                    and summary_models == expected_summary_models
                    and model_switch_tiers == (expected_tier,)
                    and normal_models[-1:] == (destination_model,)
                    and len(summary_calls) == len(expected_summary_models)
                    and all(
                        int(item["final_wire_estimated_input_tokens"])
                        < int(item["effective_input_budget_tokens"])
                        for item in summary_calls
                    )
                    and bool(destination_calls)
                    and all(
                        isinstance(item["terminal"], dict)
                        and item["terminal"].get("kind") == "COMPLETED"
                        for item in destination_calls
                    )
                    and destination_tool_call_count == 1
                    and destination_tool_result_count == 1
                    and (
                        not force_source_summary_provider_error
                        or (
                            forced_source_summary_error is not None
                            and forced_source_summary_error.used
                            and bool(presentation_notices)
                        )
                    )
                )
                return report
            finally:
                await core.shutdown()
    finally:
        if original_pulsara_home is None:
            os.environ.pop("PULSARA_HOME", None)
        else:
            os.environ["PULSARA_HOME"] = original_pulsara_home
        _drop_database(saved, database_name)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-model", default=_SOURCE_MODEL)
    parser.add_argument("--destination-model", default=_DESTINATION_MODEL)
    parser.add_argument("--segments", type=int, default=24)
    parser.add_argument("--repetitions", type=int, default=1_000)
    parser.add_argument(
        "--force-source-summary-provider-error",
        action="store_true",
        help=(
            "inject one typed 429 at the source summary boundary so the "
            "destination model performs the real Tier 3 calls"
        ),
    )
    args = parser.parse_args()
    if args.segments < 1 or args.repetitions < 1:
        raise SystemExit("segments and repetitions must be positive")
    settings = LocalSettingsStore().read()
    secrets = tuple(item.value for item in settings.model_api_keys)
    calls: list[dict[str, object]] = []
    try:
        report = asyncio.run(
            _run(
                source_model=args.source_model,
                destination_model=args.destination_model,
                segments=args.segments,
                repetitions=args.repetitions,
                calls=calls,
                force_source_summary_provider_error=(
                    args.force_source_summary_provider_error
                ),
            )
        )
    except BaseException as exc:
        report = {
            "schema_version": _SCHEMA_VERSION,
            "completed_at_utc": datetime.now(timezone.utc).isoformat(),
            "passed": False,
            "failure_type": type(exc).__name__,
            "failure_message": str(exc),
            "model_calls": calls,
        }
    scrubbed = _scrub(report, secrets)
    encoded = json.dumps(scrubbed, ensure_ascii=False, sort_keys=True)
    if any(secret and secret in encoded for secret in secrets):
        raise RuntimeError("dogfood report retained a configured model API key")
    print(json.dumps(scrubbed, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if bool(report.get("passed")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
