"""Actual first ordinary wire preserves the initial trigger after compaction."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from pulsara_agent.conversation_kernel.compaction.contracts import ResolvedCompactionPolicy
from pulsara_agent.conversation_kernel.context_sources import build_memory_context_source
from pulsara_agent.conversation_kernel.direct_model import DirectKernelModelPort
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.conversation_kernel.memory.contracts import MemoryUsePolicy
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import PromptContent, text_part_values
from pulsara_agent.model_input.contracts import ContextSourceKind
from pulsara_agent.workspace_identity import HostWorkspaceInput
from tests.dogfood.run_compaction_request_start_dogfood import _seed_history
from tests.support.model_config import test_model_binding, test_model_runtime
from tests.test_compaction_request_start_integration import (
    _SimpleModel, _eventually, _wait_summary,
)


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("disable_memory", [False, True], ids=["recall-once", "disabled"])
def test_initial_trigger_is_prepared_once_after_summary_without_rebuilding_roots(
    tmp_path, monkeypatch, stage2_migrated_postgres_database, disable_memory,
):
    async def scenario():
        import pulsara_agent.conversation_kernel.host as kernel_host

        monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
        model = _SimpleModel()
        runtime = test_model_runtime(
            model_id="test-pro", wire_api="openai_responses",
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
        )
        model._model_runtime = runtime
        model._preparer = DirectKernelModelPort(model_runtime=runtime)
        monkeypatch.setattr(kernel_host, "DirectKernelModelPort", lambda **_: model)
        monkeypatch.setattr(kernel_host.LocalMcpManagementService, "load_configs", lambda *_a, **_k: ())
        core = KernelHostCore.production(model_runtime=runtime)
        root = await core.open_session(HostWorkspaceInput(
            workspace_kind="project", workspace_root=tmp_path
        ))
        queries = []
        frozen_roots = []
        marker = "INITIAL_TRIGGER_RECALL_FROM_REAL_MEMORY_PORT"

        async def recall(query, *, canonical_facts=None):
            queries.append((query, canonical_facts))
            return build_memory_context_source(
                kind=ContextSourceKind.MEMORY_RECALL,
                texts=(marker, marker, marker),
            )

        monkeypatch.setattr(root._memory_tools, "freeze_automatic_recall_source", recall)
        original_finalize = root._runner._provider_dispatch.finalize_compaction_input

        async def observe_base(base, decision, **kwargs):
            compiled = base.append_result.compiled_input
            frozen_roots.append((compiled.system_prompt, compiled.tools))
            return await original_finalize(base, decision, **kwargs)

        monkeypatch.setattr(root._runner._provider_dispatch, "finalize_compaction_input", observe_base)
        try:
            await root.update_model_call_binding(test_model_binding(runtime))
            root._compaction.policy = ResolvedCompactionPolicy(
                automatic_enabled=False, minimum_reclaim_tokens=1
            )
            _seed_history(root)
            usage = await root.read_context_usage()
            ratio = usage["input_tokens"] / usage["input_budget_tokens"]
            root._compaction.policy = replace(
                root._compaction.policy, automatic_enabled=True,
                auto_trigger_ratio=ratio * 0.95,
                post_compaction_target_ratio=ratio * 0.85,
            )
            command = random_id("command")
            text = "Use the prior investigation to answer my question about the implementation."
            if disable_memory:
                text += " Don't use saved memory for this answer."
            await root.submit_prompt(command_id=command, content=PromptContent.text(text))
            await _wait_summary(model, root, command, None)
            assert queries == []
            assert model.root_requests == []
            model.summary.release.set()

            async def completed():
                return (await root.query_command(command)).public_code == "TURN_COMPLETED"

            await _eventually(completed)
            assert len(model.root_requests) == 1
            request = model.root_requests[0]
            compiled = request.compiled_input
            assert frozen_roots == [(compiled.system_prompt, compiled.tools)]
            rendered = "\n".join(
                value for message in compiled.messages
                for value in text_part_values(message.content)
            )
            if disable_memory:
                assert queries == []
                assert marker not in rendered
                assert request.memory_context.memory_use_policy is MemoryUsePolicy.ALL_DISABLED_BY_USER
            else:
                assert len(queries) == 1
                assert queries[0][0] == text
                assert queries[0][1].canonical_input.identity.turn_id == request.turn_id
                assert marker in rendered
                assert request.memory_context.memory_use_policy is MemoryUsePolicy.ENABLED
            assert len(model.summary.calls) == 1
        finally:
            model.summary.release.set()
            await core.shutdown()

    asyncio.run(scenario())
