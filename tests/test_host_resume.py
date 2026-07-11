from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator
from uuid import uuid4

import psycopg
import pytest

from tests.conftest import run_start_permission_fields

from pulsara_agent.event import (
    AgentEvent,
    EventContext,
    RunEndEvent,
    RunStartEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from pulsara_agent.event_log import PostgresEventLog
from pulsara_agent.host import HostCore, HostWorkspaceInput
from pulsara_agent.host.identity import resolve_workspace
from pulsara_agent.host.session_manifest import (
    SessionManifest,
    SessionManifestStore,
    permission_policy_from_manifest,
)
from pulsara_agent.llm import LLMRuntime, ModelRole
from tests.support import test_llm_config
from pulsara_agent.llm.registry import LLMTransportRegistry
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.runtime.permission import (
    ApprovalPolicy,
    EffectivePermissionPolicy,
    PermissionMode,
    PermissionProfile,
    TerminalAccess,
    preset_to_policy,
)
from pulsara_agent.runtime.recovery import HOST_TEARDOWN_NOTE_TEXT
from pulsara_agent.settings import PulsaraSettings, StorageConfig


class ScriptedTransport:
    api = "scripted"
    binding_id = "test.scripted"
    contract_version = "v1"

    def __init__(self, replies: list[str]) -> None:
        self.replies = replies
        self.contexts: list[LLMContext] = []

    async def stream(
        self,
        *,
        call,
        context: LLMContext,
        event_context: EventContext,
    ) -> AsyncIterator[AgentEvent]:
        del call
        self.contexts.append(context)
        text = self.replies.pop(0)
        yield TextBlockStartEvent(
            **event_context.event_fields(), block_id=f"text:{len(self.contexts)}"
        )
        yield TextBlockDeltaEvent(
            **event_context.event_fields(),
            block_id=f"text:{len(self.contexts)}",
            delta=text,
        )
        yield TextBlockEndEvent(
            **event_context.event_fields(), block_id=f"text:{len(self.contexts)}"
        )


def _settings_or_skip() -> PulsaraSettings:
    storage = StorageConfig.from_env()
    try:
        with psycopg.connect(storage.postgres_dsn, connect_timeout=2) as connection:
            with connection.cursor() as cursor:
                cursor.execute("select 1")
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres is not available at configured DSN: {exc}")
    return PulsaraSettings(
        llm=test_llm_config(
            api_key="sk-test",
            base_url="https://example.test/v1",
            pro_model="pro",
            flash_model="flash",
            api="scripted",
        ),
        storage=storage,
    )


def _patch_llm(
    monkeypatch, settings: PulsaraSettings, transport: ScriptedTransport
) -> None:
    registry = LLMTransportRegistry()
    registry.register(transport)

    def _patched_runtime(_config):
        return LLMRuntime(config=settings.llm, registry=registry)

    import pulsara_agent.runtime.wiring as wiring

    monkeypatch.setattr(wiring, "build_llm_runtime", _patched_runtime)


def _workspace(tmp_path: Path) -> HostWorkspaceInput:
    return HostWorkspaceInput(
        workspace_kind="project",
        workspace_root=tmp_path,
        display_label="resume-test",
        memory_domain_id="u_resume_test",
    )


def _context_text(context: LLMContext) -> str:
    return "\n".join(part for message in context.messages for part in message.content)


def _delete_session(dsn: str, runtime_session_id: str) -> None:
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute("delete from sessions where id = %s", (runtime_session_id,))


def _session_row(dsn: str, runtime_session_id: str):
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "select metadata from sessions where id = %s", (runtime_session_id,)
            )
            return cursor.fetchone()


def test_resume_reopens_same_runtime_session_and_replays_prior_messages(
    tmp_path, monkeypatch
) -> None:
    settings = _settings_or_skip()
    transport = ScriptedTransport(["first durable reply", "second durable reply"])
    _patch_llm(monkeypatch, settings, transport)
    runtime_session_id = None

    async def run():
        nonlocal runtime_session_id
        first_core = HostCore(settings=settings)
        session = await first_core.open_session(
            _workspace(tmp_path),
            model_role=ModelRole.FLASH,
            permission_policy=preset_to_policy(PermissionMode.BYPASS_PERMISSIONS),
        )
        await session.run_turn("first durable user")
        runtime_session_id = session.runtime_session_id
        await (
            first_core.shutdown()
        )  # detach process resources, keep durable conversation resumable

        second_core = HostCore(settings=settings)
        resumed = await second_core.resume_session(
            runtime_session_id,
            model_role=ModelRole.FLASH,
            permission_policy=preset_to_policy(PermissionMode.BYPASS_PERMISSIONS),
        )
        assert resumed.runtime_session_id == runtime_session_id
        assert resumed.host_session_id != session.host_session_id
        await resumed.run_turn("second durable user")
        summaries = await second_core.list_resumable_sessions(
            workspace_input=_workspace(tmp_path), limit=5
        )
        await second_core.close_session(
            resumed.host_session_id, close_conversation=True
        )
        await second_core.shutdown()
        return summaries

    try:
        summaries = asyncio.run(run())
        assert runtime_session_id is not None
        assert "first durable user" in _context_text(transport.contexts[1])
        assert "first durable reply" in _context_text(transport.contexts[1])
        assert any(
            summary.runtime_session_id == runtime_session_id for summary in summaries
        )
        row = _session_row(settings.storage.postgres_dsn, runtime_session_id)
        assert row is not None
        assert row[0]["lifecycle"]["closed_at"] is not None
    finally:
        if runtime_session_id is not None:
            _delete_session(settings.storage.postgres_dsn, runtime_session_id)


def test_closed_runtime_session_is_not_resumable(tmp_path, monkeypatch) -> None:
    settings = _settings_or_skip()
    transport = ScriptedTransport(["done"])
    _patch_llm(monkeypatch, settings, transport)
    runtime_session_id = None

    async def run():
        nonlocal runtime_session_id
        core = HostCore(settings=settings)
        session = await core.open_session(
            _workspace(tmp_path),
            model_role=ModelRole.FLASH,
            permission_policy=preset_to_policy(PermissionMode.BYPASS_PERMISSIONS),
        )
        runtime_session_id = session.runtime_session_id
        await core.close_session(session.host_session_id, close_conversation=True)
        with pytest.raises(RuntimeError, match="closed or archived"):
            await core.resume_session(runtime_session_id)
        await core.shutdown()

    try:
        asyncio.run(run())
    finally:
        if runtime_session_id is not None:
            _delete_session(settings.storage.postgres_dsn, runtime_session_id)


def test_resume_restores_manifest_permission_mode_when_not_overridden(
    tmp_path, monkeypatch
) -> None:
    settings = _settings_or_skip()
    transport = ScriptedTransport(["first"])
    _patch_llm(monkeypatch, settings, transport)
    runtime_session_id = None

    async def run():
        nonlocal runtime_session_id
        first_core = HostCore(settings=settings)
        session = await first_core.open_session(
            _workspace(tmp_path),
            model_role=ModelRole.FLASH,
            permission_policy=preset_to_policy(PermissionMode.READ_ONLY),
        )
        runtime_session_id = session.runtime_session_id
        await first_core.shutdown()

        second_core = HostCore(settings=settings)
        resumed = await second_core.resume_session(
            runtime_session_id, model_role=ModelRole.FLASH
        )
        mode = resumed.current_permission_mode
        await second_core.close_session(
            resumed.host_session_id, close_conversation=True
        )
        await second_core.shutdown()
        return mode

    try:
        mode = asyncio.run(run())
        assert mode is PermissionMode.READ_ONLY
    finally:
        if runtime_session_id is not None:
            _delete_session(settings.storage.postgres_dsn, runtime_session_id)


def test_manifest_permission_mode_without_policy_recovers_that_preset_not_default() -> (
    None
):
    manifest = SessionManifest(
        runtime_session_id="runtime:manifest:read-only",
        conversation_id="conversation:manifest:read-only",
        workspace_kind="project",
        workspace_root="/tmp/project",
        display_label="project",
        memory_domain_id="u_test",
        model_role=ModelRole.FLASH.value,
        permission_mode=PermissionMode.READ_ONLY.value,
        permission_policy={},
        created_by="test",
        created_at=None,
        last_active_at=None,
        closed_at=None,
        archived=False,
        metadata={},
    )

    policy = permission_policy_from_manifest(manifest)

    assert policy == preset_to_policy(PermissionMode.READ_ONLY)


def test_manifest_without_permission_facts_is_contract_error() -> None:
    manifest = SessionManifest(
        runtime_session_id="runtime:manifest:legacy-missing-permission",
        conversation_id="conversation:manifest:legacy-missing-permission",
        workspace_kind="project",
        workspace_root="/tmp/project",
        display_label="project",
        memory_domain_id="u_test",
        model_role=ModelRole.FLASH.value,
        permission_mode=None,
        permission_policy={},
        created_by="test",
        created_at=None,
        last_active_at=None,
        closed_at=None,
        archived=False,
        metadata={},
    )

    with pytest.raises(
        ValueError, match="permission_mode and permission_policy are required"
    ):
        permission_policy_from_manifest(manifest)


def test_session_manifest_rejects_custom_policy_that_only_matches_preset_axes(
    tmp_path,
) -> None:
    settings = _settings_or_skip()
    store = SessionManifestStore(settings.storage.postgres_dsn)
    runtime_session_id = f"runtime:manifest-custom:{uuid4().hex}"
    custom_like_bypass = EffectivePermissionPolicy(
        profile=PermissionProfile.TRUSTED_HOST,
        approval=ApprovalPolicy.NEVER,
        terminal=TerminalAccess.ALLOW,
        network_isolated=True,
    )

    try:
        with pytest.raises(ValueError, match="preset permission mode"):
            store.upsert_open_manifest(
                runtime_session_id=runtime_session_id,
                conversation_id=f"conversation:{uuid4().hex}",
                workspace=resolve_workspace(_workspace(tmp_path)),
                model_role=ModelRole.FLASH,
                permission_policy=custom_like_bypass,
                created_by="test",
            )
    finally:
        _delete_session(settings.storage.postgres_dsn, runtime_session_id)


def test_resume_repairs_dangling_running_run_before_replay(
    tmp_path, monkeypatch
) -> None:
    settings = _settings_or_skip()
    transport = ScriptedTransport(["after repair"])
    _patch_llm(monkeypatch, settings, transport)
    runtime_session_id = f"runtime:resume-test:{uuid4().hex}"
    ctx = EventContext(
        run_id=f"run:resume-test:{uuid4().hex}",
        turn_id=f"turn:resume-test:{uuid4().hex}",
        reply_id=f"reply:resume-test:{uuid4().hex}",
    )
    workspace = _workspace(tmp_path)
    resolved = resolve_workspace(workspace)
    store = SessionManifestStore(settings.storage.postgres_dsn)
    store.upsert_open_manifest(
        runtime_session_id=runtime_session_id,
        conversation_id=f"conversation:{uuid4().hex}",
        workspace=resolved,
        model_role=ModelRole.FLASH,
        permission_policy=preset_to_policy(PermissionMode.BYPASS_PERMISSIONS),
        created_by="test",
    )
    log = PostgresEventLog(
        dsn=settings.storage.postgres_dsn,
        runtime_session_id=runtime_session_id,
        workspace_root=tmp_path,
    )
    log.extend(
        [
            RunStartEvent(
                **ctx.event_fields(),
                **run_start_permission_fields(ctx.run_id),
                user_input_chars=13,
                metadata={"user_input": "dangling user"},
            ),
            ToolCallStartEvent(
                **ctx.event_fields(),
                tool_call_id="call:dangling",
                tool_call_name="terminal",
            ),
            ToolCallEndEvent(**ctx.event_fields(), tool_call_id="call:dangling"),
        ]
    )

    async def run():
        core = HostCore(settings=settings)
        resumed = await core.resume_session(
            runtime_session_id,
            model_role=ModelRole.FLASH,
            permission_policy=preset_to_policy(PermissionMode.BYPASS_PERMISSIONS),
        )
        await resumed.run_turn("continue after crash")
        await core.close_session(resumed.host_session_id, close_conversation=True)
        await core.shutdown()

    try:
        asyncio.run(run())
        events = log.iter(run_id=ctx.run_id)
        assert any(
            isinstance(event, RunEndEvent) and event.status == "aborted"
            for event in events
        )
        assert HOST_TEARDOWN_NOTE_TEXT in _context_text(transport.contexts[0])
        assert "dangling user" in _context_text(transport.contexts[0])
    finally:
        _delete_session(settings.storage.postgres_dsn, runtime_session_id)
