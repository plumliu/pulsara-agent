from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from time import monotonic
from uuid import uuid4

import pytest

from pulsara_agent.event import EventContext
from pulsara_agent.host import HostCore, HostWorkspaceInput
from pulsara_agent.inspector.store import PostgresInspectorStore
from pulsara_agent.llm import LLMRuntime, ModelRole
from pulsara_agent.llm.raw_provider import RawProviderStreamItem
from pulsara_agent.llm.registry import LLMTransportRegistry
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.retrieval.runtime import RetrievalRuntimeResources
from pulsara_agent.runtime.permission import preset_to_policy
from pulsara_agent.runtime.projection_jobs.inspection import (
    inspect_durable_projection_state,
)
from pulsara_agent.settings import PulsaraSettings, StorageConfig
from tests.support import test_llm_config
from tests.support.host import host_process_resource_lease
from tests.support.postgres import verified_postgres_provider
from tests.support.postgres_database import MigratedPostgresTestDatabase
from tests.support.raw_provider import (
    RawProviderTextBlockEnd,
    RawProviderTextBlockStart,
    RawProviderTextDelta,
    RawProviderToolCallDelta,
    RawProviderToolCallEnd,
    RawProviderToolCallStart,
)


pytestmark = pytest.mark.postgres


class _ScriptedTransport:
    api = "scripted"
    binding_id = "test.durable-projection-dogfood"
    contract_version = "v1"

    def __init__(self, replies: list[dict[str, object]]) -> None:
        self.replies = replies

    async def stream(
        self,
        *,
        call: object,
        context: LLMContext,
        event_context: EventContext,
    ) -> AsyncIterator[RawProviderStreamItem]:
        del call, context
        reply = self.replies.pop(0)
        text = reply.get("text")
        if isinstance(text, str):
            yield RawProviderTextBlockStart(
                **event_context.event_fields(),
                block_id="text:dogfood",
            )
            yield RawProviderTextDelta(
                **event_context.event_fields(),
                block_id="text:dogfood",
                delta=text,
            )
            yield RawProviderTextBlockEnd(
                **event_context.event_fields(),
                block_id="text:dogfood",
            )
        tool_calls = reply.get("tool_calls", [])
        assert isinstance(tool_calls, list)
        for raw_call in tool_calls:
            assert isinstance(raw_call, dict)
            tool_call_id = str(raw_call["id"])
            yield RawProviderToolCallStart(
                **event_context.event_fields(),
                tool_call_id=tool_call_id,
                tool_call_name=str(raw_call["name"]),
            )
            yield RawProviderToolCallDelta(
                **event_context.event_fields(),
                tool_call_id=tool_call_id,
                delta=str(raw_call["arguments"]),
            )
            yield RawProviderToolCallEnd(
                **event_context.event_fields(),
                tool_call_id=tool_call_id,
            )


def test_durable_host_projection_backlog_recovers_after_core_restart(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_postgres_database: MigratedPostgresTestDatabase,
) -> None:
    transport = _ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:dpj-dogfood",
                        "name": "terminal",
                        "arguments": json.dumps(
                            {
                                "command": "printf DPJ_DOGFOOD",
                                "yield-time_ms": 10_000,
                            }
                        ),
                    }
                ]
            },
            {"text": "durable projection dogfood complete"},
        ]
    )
    registry = LLMTransportRegistry()
    registry.register(transport)
    settings = PulsaraSettings(
        llm=test_llm_config(
            api_key="test",
            base_url="https://example.invalid/v1",
            pro_model="test-pro",
            flash_model="test-flash",
            api="scripted",
        ),
        storage=StorageConfig(
            postgres_dsn=migrated_postgres_database.runtime_dsn,
            oxigraph_url="http://127.0.0.1:1",
        ),
    )

    def build_runtime(_config: object) -> LLMRuntime:
        return LLMRuntime(config=settings.llm, registry=registry)

    monkeypatch.setattr(
        "pulsara_agent.runtime.wiring.build_llm_runtime",
        build_runtime,
    )
    monkeypatch.setattr(
        "pulsara_agent.host.production_composition.build_retrieval_runtime_resources",
        lambda _config: RetrievalRuntimeResources(),
    )

    async def scenario() -> tuple[str, str]:
        first_core = HostCore.production(settings=settings)
        session = await first_core.open_session(
            HostWorkspaceInput(
                workspace_kind="project",
                workspace_root=tmp_path,
                memory_domain_id=f"u_dpj_dogfood_{uuid4().hex}",
            ),
            model_role=ModelRole.FLASH,
            memory_reflection=False,
            permission_policy=preset_to_policy(PermissionMode.BYPASS_PERMISSIONS),
        )
        first_resources = await host_process_resource_lease(first_core)
        await first_resources.projection_service.aclose(
            deadline_monotonic=monotonic() + 10.0
        )

        result = await session.run_turn("run the durable projection dogfood")
        assert result.status.value == "finished", result.error_message
        assert result.final_text == "durable projection dogfood complete"
        runtime_session_id = session.runtime_session_id
        run_id = result.run_id

        store = PostgresInspectorStore(
            verified_postgres_provider(migrated_postgres_database.runtime_dsn)
        )
        before_restart = inspect_durable_projection_state(
            store,
            session_id=runtime_session_id,
            run_id=run_id,
            limit=32,
        )
        assert before_restart["jobs"] == []
        await first_core.shutdown()

        second_core = HostCore.production(settings=settings)
        await second_core.open_session(
            HostWorkspaceInput(
                workspace_kind="project",
                workspace_root=tmp_path,
                memory_domain_id=f"u_dpj_restart_{uuid4().hex}",
            ),
            model_role=ModelRole.FLASH,
            memory_reflection=False,
        )
        deadline = monotonic() + 20.0
        snapshot: dict[str, object] = {}
        while monotonic() < deadline:
            snapshot = inspect_durable_projection_state(
                store,
                session_id=runtime_session_id,
                run_id=run_id,
                limit=32,
            )
            jobs = snapshot["jobs"]
            assert isinstance(jobs, list)
            if jobs and all(
                item.get("state", {}).get("status") in {"succeeded", "superseded"}
                for item in jobs
            ):
                break
            await asyncio.sleep(0.05)
        second_resources = await host_process_resource_lease(second_core)
        service_snapshot = second_resources.projection_service.snapshot()
        await second_core.shutdown()

        jobs = snapshot["jobs"]
        assert isinstance(jobs, list)
        assert jobs
        assert {item["projection_kind"] for item in jobs} == {
            "run_timeline.v1",
            "tool_result_execution_evidence.v1",
        }
        assert all(item["authority_status"] == "trusted" for item in jobs)
        assert all(
            item["source_horizon"]["through_sequence"]
            == item["source_event_reference"]["sequence"]
            for item in jobs
        )
        failed_jobs = [
            (
                item["projection_kind"],
                item["state"]["status"],
                (
                    item["state"]["last_failure"]["error_type"]
                    if item["state"]["last_failure"] is not None
                    else None
                ),
                (
                    item["state"]["last_failure"]["redacted_message"]
                    if item["state"]["last_failure"] is not None
                    else None
                ),
            )
            for item in jobs
            if item["state"]["status"] not in {"succeeded", "superseded"}
        ]
        assert failed_jobs == [], service_snapshot
        assert {
            item["projection_kind"]
            for item in jobs
            if item["state"]["status"] == "succeeded"
        } == {
            "run_timeline.v1",
            "tool_result_execution_evidence.v1",
        }
        assert snapshot["result_receipts"]
        assert snapshot["target_heads"]
        assert snapshot["diagnostics"] == []
        return runtime_session_id, run_id

    runtime_session_id, run_id = asyncio.run(scenario())
    assert runtime_session_id
    assert run_id
