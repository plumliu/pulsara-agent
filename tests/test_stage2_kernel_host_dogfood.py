"""Fresh-database production-composition dogfood for the Stage 2 cut."""

from __future__ import annotations

import asyncio
from pathlib import Path
from time import monotonic
from typing import AsyncIterator

import psycopg
import pytest

from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.conversation_kernel.direct_model import (
    DirectKernelModelPort as ActualDirectKernelModelPort,
)
from pulsara_agent.conversation_kernel.extensions import (
    ExtensionDelivery,
    ExtensionPlane,
    ExtensionProjectionProfile,
    ExtensionRegistrationRequest,
)
from pulsara_agent.conversation_kernel.vocabulary import CommittedEventType
from pulsara_agent.host import HostCore
from pulsara_agent.workspace_identity import HostWorkspaceInput
from pulsara_agent.ports.live_agent_event import (
    TextDeltaPayload,
    TextEndPayload,
    TextStartPayload,
    live_digest,
)
from pulsara_agent.settings import PulsaraSettings, StorageConfig
from pulsara_agent.llm.input import MessageRole
from tests.support.model_config import test_llm_config


pytestmark = pytest.mark.postgres


class _DogfoodModelPort:
    def __init__(self, **kwargs: object) -> None:
        self._preparer = ActualDirectKernelModelPort(**kwargs)  # type: ignore[arg-type]

    def prepare_call(self, request):
        return self._preparer.prepare_call(request)

    async def stream(self, _request: object) -> AsyncIterator[object]:
        text = "STAGE2_DOGFOOD_OK"
        yield TextStartPayload("text:dogfood")
        yield TextDeltaPayload("text:dogfood", text)
        yield TextEndPayload(
            "text:dogfood",
            text,
            len(text.encode("utf-8")),
            live_digest(text),
        )


class _SteerModelPort:
    def __init__(self, **_: object) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.requests: list[object] = []
        self._preparer = ActualDirectKernelModelPort(
            config=test_llm_config(
                api_key="test",
                base_url="https://example.invalid/v1",
                pro_model="test-pro",
                flash_model="test-flash",
                api="openai_chat_completions",
            )
        )

    def prepare_call(self, request):
        return self._preparer.prepare_call(request)

    async def stream(self, request: object) -> AsyncIterator[object]:
        self.requests.append(request)
        call_index = len(self.requests)
        if call_index == 1:
            self.started.set()
            await self.release.wait()
        text = "BEFORE_STEER" if call_index == 1 else "AFTER_STEER"
        block = f"text:steer:{call_index}"
        yield TextStartPayload(block)
        yield TextDeltaPayload(block, text)
        yield TextEndPayload(
            block,
            text,
            len(text.encode("utf-8")),
            live_digest(text),
        )


def test_stage2_public_host_fresh_open_run_and_canonical_rehydrate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage2_migrated_postgres_database,
) -> None:
    import pulsara_agent.conversation_kernel.host as kernel_host

    monkeypatch.setattr(kernel_host, "DirectKernelModelPort", _DogfoodModelPort)
    monkeypatch.setattr(kernel_host, "load_mcp_server_configs", lambda **_: ())
    settings = PulsaraSettings(
        llm=test_llm_config(
            api_key="test",
            base_url="https://example.invalid/v1",
            pro_model="test-pro",
            flash_model="test-flash",
            api="openai_chat_completions",
        ),
        storage=StorageConfig(
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
        ),
    )
    workspace = HostWorkspaceInput(
        workspace_kind="project",
        workspace_root=tmp_path,
    )

    async def scenario() -> None:
        assert HostCore is KernelHostCore
        core = HostCore.production(
            settings=settings,
            authenticated_first_party_extension_ids=frozenset({"extension:dogfood"}),
        )
        first = await core.open_session(workspace)
        committed_deliveries: list[ExtensionDelivery] = []
        turn_completed = asyncio.Event()

        async def committed_callback(delivery: ExtensionDelivery) -> None:
            committed_deliveries.append(delivery)
            if delivery.event_type == CommittedEventType.TURN_COMPLETED.value:
                turn_completed.set()

        await first.register_extension(
            ExtensionRegistrationRequest(
                principal=first.authenticate_extension_principal(
                    extension_principal_id="extension:dogfood",
                ),
                handler_id="handler:committed-dogfood",
                manifest_digest="sha256:" + "1" * 64,
                plane=ExtensionPlane.POST_COMMIT,
                session_id=first.session_id,
                turn_id=None,
                event_types=frozenset(
                    {
                        CommittedEventType.USER_MESSAGE_ACCEPTED.value,
                        CommittedEventType.ASSISTANT_MESSAGE_ACCEPTED.value,
                        CommittedEventType.TURN_COMPLETED.value,
                    }
                ),
                projection_major=1,
                projection_profile=ExtensionProjectionProfile.REDACTED,
                capability_set=frozenset(),
                lease_seconds=30,
                maximum_queue_events=8,
                maximum_queue_bytes=32 * 1024,
                callback_deadline_seconds=1,
                callback=committed_callback,
            )
        )
        session_id = first.session_id
        first_generation = first.writer_generation
        result = await first.run_turn("fresh database dogfood")
        assert result.final_text == "STAGE2_DOGFOOD_OK"
        await asyncio.wait_for(turn_completed.wait(), timeout=2)
        assert [item.event_type for item in committed_deliveries] == [
            CommittedEventType.USER_MESSAGE_ACCEPTED.value,
            CommittedEventType.ASSISTANT_MESSAGE_ACCEPTED.value,
            CommittedEventType.TURN_COMPLETED.value,
        ]
        assert [item.source_revision for item in committed_deliveries] == [1, 2, 3]
        await core.close_session(
            first.host_session_id,
            close_conversation=False,
        )

        resumed = await core.resume_session(session_id, workspace_input=workspace)
        assert resumed.writer_generation == first_generation + 1
        page = await asyncio.to_thread(
            resumed.query.page_entries,
            session_id=session_id,
            deadline_monotonic=monotonic() + 10.0,
        )
        assert [entry["entry_kind"] for entry in page.entries] == [
            "USER_MESSAGE",
            "ASSISTANT_MESSAGE",
        ]
        assert page.through_entry_sequence == 2
        assert page.through_event_sequence == 3
        await core.close_session(
            resumed.host_session_id,
            close_conversation=True,
        )
        await core.shutdown()

    asyncio.run(scenario())

    # The clean universe does not create any legacy public authority.
    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as connection:
        for relation in (
            "sessions",
            "agent_events",
            "prompt_queue_items",
            "durable_projection_jobs",
            "memory_nodes",
        ):
            assert (
                connection.execute(
                    "SELECT pg_catalog.to_regclass(%s)",
                    (f"public.{relation}",),
                ).fetchone()[0]
                is None
            ), relation


def test_stage2_host_consumes_exact_active_turn_steer_at_provider_safe_point(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage2_migrated_postgres_database,
) -> None:
    import pulsara_agent.conversation_kernel.host as kernel_host

    model = _SteerModelPort()
    monkeypatch.setattr(kernel_host, "DirectKernelModelPort", lambda **_: model)
    monkeypatch.setattr(kernel_host, "load_mcp_server_configs", lambda **_: ())
    settings = PulsaraSettings(
        llm=test_llm_config(
            api_key="test",
            base_url="https://example.invalid/v1",
            pro_model="test-pro",
            flash_model="test-flash",
            api="openai_chat_completions",
        ),
        storage=StorageConfig(
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
        ),
    )

    async def scenario() -> None:
        core = KernelHostCore.production(settings=settings)
        session = await core.open_session(
            HostWorkspaceInput(
                workspace_kind="project",
                workspace_root=tmp_path,
            )
        )
        running = asyncio.create_task(
            session.run_turn("initial", command_id="command:steer-root")
        )
        await asyncio.wait_for(model.started.wait(), timeout=2)
        assert session._active_turn_id is not None  # noqa: SLF001
        outcome = await session.steer_active_turn(
            command_id="command:steer",
            text="new direction",
            target_turn_id=session._active_turn_id,  # noqa: SLF001
        )
        assert outcome.status == "PENDING"
        model.release.set()
        result = await asyncio.wait_for(running, timeout=5)
        assert result.final_text == "AFTER_STEER"
        assert len(model.requests) == 2
        second = model.requests[1]
        assert any(
            item.role is MessageRole.USER and item.content == ("new direction",)
            for item in second.compiled_input.messages  # type: ignore[attr-defined]
        )
        rows = await asyncio.to_thread(
            session.repository.rehydrate_session,
            session_id=session.session_id,
            deadline_monotonic=monotonic() + 5,
        )
        assert [row["entry_kind"] for row in rows] == [
            "USER_MESSAGE",
            "ASSISTANT_MESSAGE",
            "USER_STEER",
            "ASSISTANT_MESSAGE",
        ]
        await core.close_session(
            session.host_session_id,
            close_conversation=True,
        )
        await core.shutdown()

    asyncio.run(scenario())
