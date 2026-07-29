from __future__ import annotations

import asyncio

from pulsara_agent.host.identity import HostWorkspaceInput, resolve_workspace
from pulsara_agent.host.session import HostSession
from pulsara_agent.llm import ModelRole
from tests.support import test_llm_config
from tests.support.runtime_factory import build_component_agent_runtime_wiring
from pulsara_agent.settings import PulsaraSettings
from tests.support.settings import compatibility_storage_config


def test_host_session_close_has_no_legacy_active_task_owner(
    tmp_path,
) -> None:
    async def scenario() -> None:
        settings = PulsaraSettings(
            llm=test_llm_config(
                api_key="test",
                base_url="https://example.invalid/v1",
                pro_model="test-pro",
                flash_model="test-flash",
            ),
            storage=compatibility_storage_config(),
        )
        wiring = build_component_agent_runtime_wiring(
            settings,
            tmp_path,
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
        assert not hasattr(session, "_active_task")
        assert session.wiring.run_activation_service is not None

        await session.aclose(drain_timeout_seconds=0.05)

        assert session.closed is True

    asyncio.run(scenario())
