from __future__ import annotations

import asyncio

from pulsara_agent.host.identity import HostWorkspaceInput, resolve_workspace
from pulsara_agent.host.session import HostSession
from pulsara_agent.llm import ModelRole
from pulsara_agent.llm.config import LLMConfig
from pulsara_agent.runtime.wiring import build_agent_runtime_wiring
from pulsara_agent.settings import PulsaraSettings
from tests.support.settings import compatibility_storage_config


def test_host_session_aclose_boundedly_cancels_inflight_retrieval_borrower(tmp_path) -> None:
    async def scenario() -> None:
        settings = PulsaraSettings(
            llm=LLMConfig(
                api_key="test",
                base_url="https://example.invalid/v1",
                pro_model="test-pro",
                flash_model="test-flash",
            ),
            storage=compatibility_storage_config(),
        )
        wiring = build_agent_runtime_wiring(
            settings,
            tmp_path,
            durable=False,
            model_role=ModelRole.FLASH,
        )
        session = HostSession(
            host_session_id="host:retrieval-close",
            conversation_id="conversation:retrieval-close",
            workspace=resolve_workspace(
                HostWorkspaceInput(workspace_kind="project", workspace_root=tmp_path)
            ),
            wiring=wiring,
        )
        cancelled = asyncio.Event()

        async def inflight_provider_request() -> None:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        task = asyncio.create_task(inflight_provider_request())
        await asyncio.sleep(0)
        session._active_task = task

        await session.aclose(drain_timeout_seconds=0.05)

        assert session.closed is True
        assert task.cancelled()
        assert cancelled.is_set()

    asyncio.run(scenario())
