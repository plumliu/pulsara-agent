"""Real SDK -> Host planner -> provider wire -> native tool regression."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
from time import monotonic

import pytest
from psycopg.rows import dict_row

from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import PromptContent, ToolSpec, text_part_values
from pulsara_agent.primitives.context import thaw_json
from pulsara_agent.llm.estimator import PulsaraHeuristicTokenEstimatorV3
from pulsara_agent.llm.adapters.openai.function_tools import openai_chat_function_tool
from pulsara_agent.mcp_config import load_mcp_server_configs
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from tests.test_round4_plan_host import (
    _DetachedDraftModel,
    _open_test_session,
    _text,
    _tool_call,
)

pytestmark = pytest.mark.postgres


def test_recorded_entire_notion_catalog_is_native_with_builtins_and_real_sdk(
    tmp_path, monkeypatch, stage2_migrated_postgres_database
):
    fixture = Path(__file__).parent / "fixtures/notion_schema_replay_server.py"
    config_path = tmp_path / "mcp.yaml"
    config_path.write_text(
        "servers:\n  notion:\n    required: true\n    enabled: true\n    catalog_refresh_interval_ms: DISABLED\n    transport: "
        + json.dumps(
            {
                "type": "stdio",
                "command": sys.executable,
                "args": [str(fixture)],
            }
        )
        + "\n"
    )
    configs = load_mcp_server_configs(user_config_path=config_path)

    class Model(_DetachedDraftModel):
        async def stream(self, request):
            self.requests.append(request)
            native = [
                t
                for t in request.compiled_input.tools
                if t.name.startswith("mcp__notion__")
            ]
            assert len(native) == 65
            assert any(
                t.name == "mcp__notion__notion_query_data_sources" for t in native
            )
            assert len(request.compiled_input.tools) > 65
            quote = sum(
                PulsaraHeuristicTokenEstimatorV3().estimate_json(
                    openai_chat_function_tool(
                        ToolSpec(
                            name=t.name,
                            description=t.description,
                            parameters=thaw_json(t.parameters),
                        )
                    )
                )
                for t in native
            )
            assert 63_000 < quote < 64_000
            if len(self.requests) == 1:
                events = _tool_call("mcp__notion__notion_get_self", "call:real-sdk", {})
            else:
                assert len(self.requests) == 2
                initial = self.requests[0].compiled_input
                current = request.compiled_input
                assert current.tools == initial.tools
                assert current.system_prompt == initial.system_prompt
                assert current.messages[: len(initial.messages)] == initial.messages
                rendered = "\n".join(
                    p
                    for message in current.messages
                    for p in text_part_values(message.content)
                )
                assert "NOTION_SCHEMA_REPLAY_OK" in rendered, rendered
                assert "MCP_DESCRIPTOR_OVERBOUND" not in rendered
                events = _text("native Notion schema replay passed")
            for event in events:
                yield event

    async def scenario():
        model = Model()
        core, session = await _open_test_session(
            tmp_path=tmp_path,
            model=model,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
            monkeypatch=monkeypatch,
        )
        try:
            await session.reload_mcp_configs(
                configs, deadline_monotonic=monotonic() + 30
            )
            await session._mcp_supervisor._wait_for_required_startup(("notion",))
            session._tools.prepare_tool_surface_safe_point()
            await asyncio.wait_for(
                session.run_turn(
                    PromptContent.text("Run the local schema replay probe."),
                    command_id=random_id("command"),
                    requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
                ),
                30,
            )
            assert len(model.requests) == 2
            with session.repository.connection_provider.connection(
                lane=PostgresConnectionLane.INSPECTOR,
                row_factory=dict_row,
                deadline_monotonic=monotonic() + 5,
            ) as conn:
                result = conn.execute(
                    "SELECT result_state FROM pulsara_v3.tool_results WHERE session_id=%s AND tool_call_id='call:real-sdk'",
                    (session.session_id,),
                ).fetchone()
                assert result == {"result_state": "SUCCESS"}
        finally:
            await core.shutdown()

    asyncio.run(scenario())
