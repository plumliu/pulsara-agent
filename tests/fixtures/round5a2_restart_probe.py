"""Fresh-process probe for durable provider-native assistant replay.

Production objects cross the process boundary exclusively through PostgreSQL;
no continuity or replay object is serialized between invocations.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os

from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.repository import ConversationKernelRepository
from pulsara_agent.conversation_kernel.runner import ConversationKernelRunner
from pulsara_agent.llm.provider import (
    ProviderProfile,
    ThinkingProfile,
    ThinkingReplayPolicy,
)
from tests.support.model_config import test_llm_config
from tests.support.postgres import verified_postgres_provider
from tests.support.round3 import StaticContextSourceCollector, StructuredToolPort
from tests.test_stage2_conversation_runner import (
    _AssertingTool,
    _SequencedDirectKernelModel,
    _round5a1_chat_scripts,
    _round5a1_responses_scripts,
)


def _model(api: str) -> _SequencedDirectKernelModel:
    profile = ProviderProfile(
        id=f"test:{api}:fresh-process",
        wire_api=api,
        thinking=(
            ThinkingProfile(
                enabled=True,
                message_field="reasoning_content",
                replay_policy=ThinkingReplayPolicy.ALWAYS,
            )
            if api == "openai_chat_completions"
            else ThinkingProfile()
        ),
    )
    script = (
        _round5a1_chat_scripts()[1]
        if api == "openai_chat_completions"
        else _round5a1_responses_scripts()[1]
    )
    return _SequencedDirectKernelModel(
        config=test_llm_config(
            api_key="test",
            base_url="https://example.invalid/v1",
            pro_model="test-pro",
            flash_model="test-flash",
            api=api,
            provider_profile=profile,
        ),
        scripts=(script,),
    )


async def _run(args: argparse.Namespace) -> dict[str, object]:
    provider = verified_postgres_provider(os.environ["ROUND5A2_TEST_RUNTIME_DSN"])
    repository = ConversationKernelRepository(provider)
    lease = repository.acquire_host_writer(
        session_id=args.session_id,
        workspace_id=args.workspace_id,
        writer_owner_id=f"round5a2:{args.mode}:{os.getpid()}",
        lease_seconds=30,
        deadline_monotonic=asyncio.get_running_loop().time() + 30,
    )
    model = _model(args.api)
    runner = ConversationKernelRunner(
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(
            _AssertingTool(provider, args.session_id), tool_names=()
        ),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
    )
    result = await runner.run_turn(
        "create durable native history"
        if args.mode == "create"
        else "continue from durable native history"
    )
    wire_plan = model.requests[0].wire_input_plan
    return {
        "mode": args.mode,
        "completed": bool(result.final_entry_id),
        "hydrated": wire_plan.provider_replay_hydration_fingerprint is not None,
        "replacement_count": len(wire_plan.replacements),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("create", "continue"))
    parser.add_argument(
        "api", choices=("openai_chat_completions", "openai_responses")
    )
    parser.add_argument("session_id")
    parser.add_argument("workspace_id")
    parser.add_argument("--abrupt", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(_run(args))
    print(json.dumps(result, sort_keys=True), flush=True)
    if args.abrupt:
        os._exit(0)


if __name__ == "__main__":
    main()
