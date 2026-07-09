import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import AsyncIterator

from tests.conftest import run_start_permission_fields

from pulsara_agent.event import (
    ContextCompiledEvent,
    ContextCompactionCompletedEvent,
    ContextCompactionFailedEvent,
    ContextCompactionMemoryCandidatesProposedEvent,
    ContextCompactionStartedEvent,
    CustomEvent,
    EventContext,
    ModelCallEndEvent,
    ModelCallStartEvent,
    ReplyEndEvent,
    ReplyStartEvent,
    RunErrorEvent,
    RunStartEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
    ThinkingBlockDeltaEvent,
    ThinkingBlockEndEvent,
    ThinkingBlockStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
)
from pulsara_agent.event_log import InMemoryEventLog, dump_agent_event, load_agent_event
from pulsara_agent.host import HostSession, HostWorkspaceInput, resolve_workspace
from pulsara_agent.host.transcript import rebuild_prior_messages
from pulsara_agent.llm import LLMConfig, LLMRuntime, ModelProfile
from pulsara_agent.llm.registry import LLMTransportRegistry
from pulsara_agent.llm.request import LLMContext, LLMOptions
from pulsara_agent.memory import InMemoryCandidatePool, MemoryDomainContext
from pulsara_agent.memory.artifacts.archive import InMemoryArchiveStore
from pulsara_agent.memory.candidates.pool import CandidateOrigin, PooledMemoryCandidate
from pulsara_agent.ontology import memory
from pulsara_agent.capability.runtime import CapabilityRuntime
from pulsara_agent.message import (
    AssistantMsg,
    Msg,
    TextBlock,
    ToolCallBlock,
    ToolCallState,
    ToolResultArtifactRef,
    ToolResultBlock,
    ToolResultState,
    UserMsg,
)
from pulsara_agent.runtime.agent import AgentRunResult, AgentRuntime
from pulsara_agent.runtime.approval import ApprovalResolution, PendingApproval, ToolApprovalDecision
from pulsara_agent.runtime.compaction.planner import strip_compaction_analysis
from pulsara_agent.runtime.compaction.candidates import (
    CandidatePoolCompactionMemoryCandidateSink,
    ContextCompactionMemoryCandidatePolicy,
    parse_compaction_memory_candidates,
)
from pulsara_agent.runtime.compaction.inline import RuntimeContextCompactor
from pulsara_agent.runtime.compaction.service import (
    ContextCompactionPolicy,
    ContextCompactionService,
    build_compaction_input,
    _events_text_for_estimate,
    estimate_compaction_window_tokens,
    estimate_context_tokens,
    production_compaction_prompt,
)
from pulsara_agent.runtime.plan import McpElicitationResolution, PendingMcpElicitation, PendingPlanInteraction, PlanQuestionResolution
from pulsara_agent.runtime.state import LoopState, LoopStatus, LoopTransition
from pulsara_agent.runtime.transcript import rebuild_prior_messages_before_sequence
from pulsara_agent.runtime.wiring import AgentRuntimeWiring, build_in_memory_runtime_wiring


class CompactScriptedTransport:
    api = "compact_scripted"

    def __init__(self, text: str) -> None:
        self.text = text
        self.contexts: list[LLMContext] = []

    async def stream(
        self,
        *,
        model: ModelProfile,
        context: LLMContext,
        event_context: EventContext,
        options: LLMOptions | None = None,
    ) -> AsyncIterator:
        self.contexts.append(context)
        yield ModelCallStartEvent(
            **event_context.event_fields(),
            model_name=model.id,
            model_role=model.role.value,
            provider=model.provider,
        )
        yield TextBlockStartEvent(**event_context.event_fields(), block_id="text:compact")
        yield TextBlockDeltaEvent(**event_context.event_fields(), block_id="text:compact", delta=self.text)
        yield TextBlockEndEvent(**event_context.event_fields(), block_id="text:compact")
        yield ModelCallEndEvent(**event_context.event_fields())


class CompactErrorAfterTextTransport(CompactScriptedTransport):
    async def stream(
        self,
        *,
        model: ModelProfile,
        context: LLMContext,
        event_context: EventContext,
        options: LLMOptions | None = None,
    ) -> AsyncIterator:
        self.contexts.append(context)
        yield ModelCallStartEvent(
            **event_context.event_fields(),
            model_name=model.id,
            model_role=model.role.value,
            provider=model.provider,
        )
        yield TextBlockStartEvent(**event_context.event_fields(), block_id="text:compact")
        yield TextBlockDeltaEvent(**event_context.event_fields(), block_id="text:compact", delta=self.text)
        yield RunErrorEvent(**event_context.event_fields(), message="provider failed mid-summary", code="provider_error")


def _ctx(label: str) -> EventContext:
    return EventContext(
        run_id=f"run:{label}",
        turn_id=f"turn:{label}",
        reply_id=f"reply:{label}",
    )


def _append_turn(log: InMemoryEventLog, label: str, user_input: str, assistant_text: str) -> None:
    ctx = _ctx(label)
    log.extend(
        [
            RunStartEvent(**ctx.event_fields(), **run_start_permission_fields(ctx.run_id), user_input_chars=len(user_input), metadata={"user_input": user_input}),
            ReplyStartEvent(**ctx.event_fields(), name="assistant"),
            TextBlockStartEvent(**ctx.event_fields(), block_id=f"text:{label}"),
            TextBlockDeltaEvent(**ctx.event_fields(), block_id=f"text:{label}", delta=assistant_text),
            TextBlockEndEvent(**ctx.event_fields(), block_id=f"text:{label}"),
            ReplyEndEvent(**ctx.event_fields()),
        ]
    )


async def _emit_turn(runtime_session, label: str, user_input: str, assistant_text: str) -> None:
    ctx = _ctx(label)
    for event in [
        RunStartEvent(**ctx.event_fields(), **run_start_permission_fields(ctx.run_id), user_input_chars=len(user_input), metadata={"user_input": user_input}),
        ReplyStartEvent(**ctx.event_fields(), name="assistant"),
        TextBlockStartEvent(**ctx.event_fields(), block_id=f"text:{label}"),
        TextBlockDeltaEvent(**ctx.event_fields(), block_id=f"text:{label}", delta=assistant_text),
        TextBlockEndEvent(**ctx.event_fields(), block_id=f"text:{label}"),
        ReplyEndEvent(**ctx.event_fields()),
    ]:
        await runtime_session.emit(event)


def _current_tail_state(runtime_session_id: str, *, run_id: str = "run:current") -> LoopState:
    state = LoopState(session_id=runtime_session_id, run_id=run_id)
    state.messages = [
        UserMsg(
            name="user",
            content="current request",
            id=f"user-message:{run_id}",
            metadata={"run_id": run_id},
        ),
        AssistantMsg(
            name="assistant",
            id=state.reply_id,
            content=[
                ToolCallBlock(
                    id="call:current",
                    name="terminal",
                    input='{"command":"printf current"}',
                    state=ToolCallState.FINISHED,
                )
            ],
        ),
        Msg(
            role="tool_result",
            name="terminal",
            id="tool-result-message:call:current",
            content=[
                ToolResultBlock(
                    id="call:current",
                    name="terminal",
                    state=ToolResultState.SUCCESS,
                    output=[TextBlock(text="current tool result")],
                )
            ],
        ),
    ]
    state.pending_tool_calls = [
        ToolCallBlock(
            id="call:current",
            name="terminal",
            input='{"command":"printf current"}',
            state=ToolCallState.FINISHED,
        )
    ]
    state.tool_results = [
        ToolResultBlock(
            id="call:current",
            name="terminal",
            state=ToolResultState.SUCCESS,
            output=[TextBlock(text="current tool result")],
        )
    ]
    state.transition(LoopTransition.CONTINUE_AFTER_TOOL)
    return state


def _llm_runtime(transport: CompactScriptedTransport) -> LLMRuntime:
    registry = LLMTransportRegistry()
    registry.register(transport)
    return LLMRuntime(
        config=LLMConfig(
            api_key="sk-test",
            base_url="https://example.test/v1",
            pro_model="pro",
            flash_model="flash",
            api=transport.api,
        ),
        registry=registry,
    )


class _FakeHostCompactionService:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def compact(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            compaction_id="context_compaction:host",
            summary_artifact_id="context_compaction_host:summary",
            window_id="context_window:host",
            through_sequence=10,
            keep_after_sequence=10,
        )

    async def compact_if_needed(self, **kwargs) -> bool:
        self.calls.append({"method": "compact_if_needed", **kwargs})
        return False


class _FakeFailingAutoCompactionService:
    def __init__(self, event_log: InMemoryEventLog) -> None:
        self.event_log = event_log

    async def compact_if_needed(self, **kwargs) -> bool:
        ctx = _ctx("compaction:auto:failed")
        self.event_log.append(
            ContextCompactionFailedEvent(
                **ctx.event_fields(),
                compaction_id="context_compaction:failed",
                trigger="auto",
                reason=str(kwargs.get("reason", "context_threshold")),
                window_number=1,
                window_id="context_window:failed",
                estimated_tokens_before=200_001,
                threshold_tokens=200_000,
                context_window_tokens=256_000,
                through_sequence=10,
                keep_after_sequence=5,
                error_type="RuntimeError",
                message="boom",
            )
        )
        return False


class _FakeWritingAutoCompactionService:
    def __init__(self, event_log: InMemoryEventLog) -> None:
        self.event_log = event_log

    async def compact_if_needed(self, **kwargs) -> bool:
        del kwargs
        ctx = _ctx("compaction:auto:completed")
        self.event_log.extend(
            [
                ContextCompactionStartedEvent(
                    **ctx.event_fields(),
                    compaction_id="context_compaction:completed",
                    trigger="auto",
                    reason="preflight_context_threshold",
                    window_number=1,
                    window_id="context_window:completed",
                    estimated_tokens_before=200_001,
                    threshold_tokens=200_000,
                    context_window_tokens=256_000,
                    through_sequence=10,
                    keep_after_sequence=5,
                ),
                ContextCompactionCompletedEvent(
                    **ctx.event_fields(),
                    compaction_id="context_compaction:completed",
                    trigger="auto",
                    reason="preflight_context_threshold",
                    window_number=1,
                    window_id="context_window:completed",
                    summary_artifact_id="context_compaction_completed:summary",
                    summary_chars=12,
                    estimated_tokens_before=200_001,
                    estimated_tokens_after=4_000,
                    threshold_tokens=200_000,
                    context_window_tokens=256_000,
                    through_sequence=10,
                    keep_after_sequence=5,
                ),
            ]
        )
        return True


class _FakeWritingManualCompactionService:
    def __init__(self, event_log: InMemoryEventLog) -> None:
        self.event_log = event_log

    async def compact(self, **kwargs):
        ctx = _ctx("compaction:manual:completed")
        self.event_log.extend(
            [
                ContextCompactionStartedEvent(
                    **ctx.event_fields(),
                    compaction_id="context_compaction:manual",
                    trigger="manual",
                    reason=str(kwargs.get("reason", "user_requested")),
                    window_number=1,
                    window_id="context_window:manual",
                    estimated_tokens_before=200_001,
                    threshold_tokens=200_000,
                    context_window_tokens=256_000,
                    through_sequence=10,
                    keep_after_sequence=5,
                    force=bool(kwargs.get("force", False)),
                ),
                ContextCompactionCompletedEvent(
                    **ctx.event_fields(),
                    compaction_id="context_compaction:manual",
                    trigger="manual",
                    reason=str(kwargs.get("reason", "user_requested")),
                    window_number=1,
                    window_id="context_window:manual",
                    summary_artifact_id="context_compaction_manual:summary",
                    summary_chars=12,
                    estimated_tokens_before=200_001,
                    estimated_tokens_after=4_000,
                    threshold_tokens=200_000,
                    context_window_tokens=256_000,
                    through_sequence=10,
                    keep_after_sequence=5,
                ),
            ]
        )
        return SimpleNamespace(
            compaction_id="context_compaction:manual",
            summary_artifact_id="context_compaction_manual:summary",
            window_id="context_window:manual",
            through_sequence=10,
            keep_after_sequence=5,
        )


class _FakeFailingManualCompactionService:
    def __init__(self, event_log: InMemoryEventLog) -> None:
        self.event_log = event_log

    async def compact(self, **kwargs):
        ctx = _ctx("compaction:manual:failed")
        self.event_log.extend(
            [
                ContextCompactionStartedEvent(
                    **ctx.event_fields(),
                    compaction_id="context_compaction:manual_failed",
                    trigger="manual",
                    reason=str(kwargs.get("reason", "user_requested")),
                    window_number=1,
                    window_id="context_window:manual_failed",
                    estimated_tokens_before=200_001,
                    threshold_tokens=200_000,
                    context_window_tokens=256_000,
                    through_sequence=10,
                    keep_after_sequence=5,
                    force=bool(kwargs.get("force", False)),
                ),
                ContextCompactionFailedEvent(
                    **ctx.event_fields(),
                    compaction_id="context_compaction:manual_failed",
                    trigger="manual",
                    reason=str(kwargs.get("reason", "user_requested")),
                    window_number=1,
                    window_id="context_window:manual_failed",
                    estimated_tokens_before=200_001,
                    threshold_tokens=200_000,
                    context_window_tokens=256_000,
                    through_sequence=10,
                    keep_after_sequence=5,
                    error_type="RuntimeError",
                    message="manual compact exploded",
                ),
            ]
        )
        raise RuntimeError("manual compact exploded")


class _FakePreflightBoundaryCompactionService:
    def __init__(self, event_log: InMemoryEventLog, archive: InMemoryArchiveStore, runtime_session_id: str) -> None:
        self.event_log = event_log
        self.archive = archive
        self.runtime_session_id = runtime_session_id
        self.calls: list[dict[str, object]] = []

    async def compact_if_needed(self, **kwargs) -> bool:
        self.calls.append({"method": "compact_if_needed", **kwargs})
        ctx = _ctx("compaction:preflight:completed")
        artifact_id = "context_compaction_preflight:summary"
        self.archive.put_text(
            artifact_id,
            "Preflight summary sentinel.",
            session_id=self.runtime_session_id,
            metadata={"kind": "context_compaction_summary", "do_not_write_back": True},
        )
        through_sequence = self.event_log.next_sequence() - 1
        self.event_log.append(
            ContextCompactionCompletedEvent(
                **ctx.event_fields(),
                compaction_id="context_compaction:preflight",
                trigger="auto",
                reason=str(kwargs.get("reason", "preflight_context_threshold")),
                window_number=1,
                window_id="context_window:preflight",
                summary_artifact_id=artifact_id,
                summary_chars=27,
                estimated_tokens_before=200_001,
                estimated_tokens_after=4_000,
                threshold_tokens=200_000,
                context_window_tokens=256_000,
                through_sequence=through_sequence,
                keep_after_sequence=through_sequence,
            )
        )
        return True


def test_context_compaction_events_round_trip() -> None:
    ctx = _ctx("compaction:event")
    started = ContextCompactionStartedEvent(
        **ctx.event_fields(),
        compaction_id="context_compaction:test",
        trigger="auto",
        reason="context_threshold",
        window_number=1,
        window_id="context_window:1",
        estimated_tokens_before=200_001,
        threshold_tokens=200_000,
        context_window_tokens=256_000,
        through_sequence=10,
        keep_after_sequence=10,
    )
    completed = ContextCompactionCompletedEvent(
        **ctx.event_fields(),
        compaction_id="context_compaction:test",
        trigger="auto",
        reason="context_threshold",
        window_number=1,
        window_id="context_window:1",
        summary_artifact_id="artifact:summary",
        summary_chars=12,
        estimated_tokens_before=200_001,
        estimated_tokens_after=4_000,
        threshold_tokens=200_000,
        context_window_tokens=256_000,
        through_sequence=10,
        keep_after_sequence=10,
        included_run_ids=["run:a"],
        included_artifact_ids=["artifact:a"],
    )

    assert load_agent_event(dump_agent_event(started)) == started
    assert load_agent_event(dump_agent_event(completed)) == completed

    proposed = ContextCompactionMemoryCandidatesProposedEvent(
        **ctx.event_fields(),
        compaction_id="context_compaction:test",
        source_event_id=completed.id,
        source_event_sequence=2,
        summary_artifact_id="artifact:summary",
        candidate_entry_ids=["pool:test"],
        attempted_count=1,
        proposed_count=1,
        skipped_count=0,
        duplicate_count=0,
        error_count=0,
        extractor_version="compaction-memory-candidates:v1",
    )
    assert load_agent_event(dump_agent_event(proposed)) == proposed


def test_strip_compaction_analysis_keeps_summary_only() -> None:
    raw = "<analysis>private checklist</analysis>\n<summary>\nUseful handoff.\n</summary>"

    assert strip_compaction_analysis(raw) == "Useful handoff."


def test_strip_compaction_analysis_rejects_unclosed_private_blocks() -> None:
    assert strip_compaction_analysis("<analysis>private checklist with no close") == ""
    assert strip_compaction_analysis("<summary>official handoff with no close") == ""


def test_strip_compaction_analysis_rejects_tagless_summary_with_memory_candidates() -> None:
    raw = """
Useful handoff.
<memory_candidates_json>{"candidates": []}</memory_candidates_json>
"""

    assert strip_compaction_analysis(raw) == ""


def test_parse_compaction_summary_and_memory_candidates() -> None:
    raw = """
<analysis>private draft</analysis>
<summary>Useful handoff.</summary>
<memory_candidates_json>
{
  "candidates": [
    {
      "kind": "Preference",
      "statement": "The user prefers syncing release before pushing GitHub.",
      "scope": "ctx:user",
      "source_authority": "explicit_user_instruction"
    }
  ]
}
</memory_candidates_json>
"""

    result = parse_compaction_memory_candidates(
        raw,
        workspace_scope="ctx:workspace/pulsara_agent",
        workspace_kind="project",
    )

    assert result.attempted_count == 1
    assert result.diagnostics == ()
    assert result.skipped == ()
    assert len(result.candidates) == 1
    candidate = result.candidates[0].payload.candidate
    assert candidate.kind == "Preference"
    assert candidate.statement == "The user prefers syncing release before pushing GitHub."
    assert candidate.scope == "ctx:workspace/pulsara_agent"
    assert candidate.source_authority is memory.SourceAuthority.CONVERSATION_EVIDENCE
    assert candidate.verification_status is memory.VerificationStatus.INFERRED
    assert candidate.evidence_ids == ()
    assert result.candidates[0].intent_fingerprint.startswith("sha256:")


def test_parse_compaction_candidate_failure_does_not_drop_summary() -> None:
    raw = "<summary>Useful handoff.</summary><memory_candidates_json>{broken</memory_candidates_json>"

    assert strip_compaction_analysis(raw) == "Useful handoff."
    result = parse_compaction_memory_candidates(
        raw,
        workspace_scope="ctx:workspace/pulsara_agent",
        workspace_kind="project",
    )

    assert result.candidates == ()
    assert result.diagnostics[0].code == "compaction_candidate_json_malformed"


def test_parse_compaction_candidate_secret_filter_redacts() -> None:
    raw = """
<summary>Useful handoff.</summary>
<memory_candidates_json>
{
  "candidates": [
    {"kind": "Preference", "statement": "The API key is sk-1234567890SECRET"}
  ]
}
</memory_candidates_json>
"""

    result = parse_compaction_memory_candidates(
        raw,
        workspace_scope="ctx:workspace/pulsara_agent",
        workspace_kind="project",
    )

    assert result.candidates == ()
    assert result.skipped[0].code == "compaction_candidate_secret_like_content"
    assert result.skipped[0].redacted is True
    assert "sk-123" not in result.skipped[0].reason
    assert result.diagnostics[0].redacted is True


def test_parse_compaction_candidate_ignores_missing_block_by_default() -> None:
    result = parse_compaction_memory_candidates(
        "<summary>Useful handoff.</summary>",
        workspace_scope="ctx:workspace/pulsara_agent",
        workspace_kind="project",
    )

    assert result.attempted_count == 0
    assert result.candidates == ()
    assert result.diagnostics == ()


def test_parse_compaction_candidate_missing_block_can_be_diagnostic() -> None:
    result = parse_compaction_memory_candidates(
        "<summary>Useful handoff.</summary>",
        workspace_scope="ctx:workspace/pulsara_agent",
        workspace_kind="project",
        policy=ContextCompactionMemoryCandidatePolicy(missing_candidates_block_policy="diagnostic"),
    )

    assert result.attempted_count == 0
    assert result.candidates == ()
    assert result.diagnostics[0].code == "compaction_candidate_block_missing"


def test_parse_compaction_candidate_transient_workspace_disabled() -> None:
    raw = """
<summary>Useful handoff.</summary>
<memory_candidates_json>{"candidates": [{"kind": "Preference", "statement": "The user prefers concise output."}]}</memory_candidates_json>
"""

    result = parse_compaction_memory_candidates(
        raw,
        workspace_scope="ctx:workspace/transient",
        workspace_kind="transient",
    )

    assert result.candidates == ()
    assert result.diagnostics[0].code == "compaction_candidates_disabled_for_transient_workspace"


def test_context_compaction_memory_candidate_policy_defaults() -> None:
    policy = ContextCompactionMemoryCandidatePolicy()

    assert policy.enabled is True
    assert policy.extract_on_manual is True
    assert policy.extract_on_preflight is True
    assert policy.extract_on_mid_turn is False
    assert policy.missing_candidates_block_policy == "ignore"
    assert policy.max_candidates_per_compaction == 3


def test_compaction_prompt_preserves_yielded_terminal_process_continuation() -> None:
    prompt = production_compaction_prompt()

    assert "process_id" in prompt
    assert "long-running or background process" in prompt
    assert "continue with terminal_process" in prompt
    assert "rather than restarting the command" in prompt


def test_compaction_prompt_can_omit_memory_candidate_instructions() -> None:
    prompt = production_compaction_prompt(memory_candidates_enabled=False)

    assert "memory_candidates_json" not in prompt
    assert "durable-memory candidates" not in prompt
    assert "an <analysis> block followed by a <summary> block." in prompt


def test_compaction_input_estimate_uses_coalesced_observations_not_raw_delta_envelope() -> None:
    log = InMemoryEventLog()
    _append_turn(log, "dense", "plain user input", "assistant reply")
    ctx = _ctx("dense")
    log.extend(
        [
            ToolCallStartEvent(**ctx.event_fields(), tool_call_id="call:one", tool_call_name="terminal"),
            *[
                ToolCallDeltaEvent(**ctx.event_fields(), tool_call_id="call:one", delta="{\"cmd\":\"echo hi\"}"[:1])
                for _ in range(50)
            ],
            ToolCallEndEvent(**ctx.event_fields(), tool_call_id="call:one"),
        ]
    )
    events = log.iter()
    event_text = _events_text_for_estimate(events)
    policy = ContextCompactionPolicy(
        chars_per_token=4.0,
        event_chars_per_token=2.0,
        estimate_safety_margin=1.25,
    )

    raw_event_estimate = estimate_context_tokens(event_text, chars_per_token=policy.event_chars_per_token)
    coalesced_estimate = estimate_compaction_window_tokens(events, policy=policy)

    assert coalesced_estimate < raw_event_estimate


def test_compaction_plan_collects_tool_result_artifact_ids() -> None:
    log = InMemoryEventLog()
    archive = InMemoryArchiveStore()
    _append_turn(log, "artifact", "user asked for a web search", "tool produced a large artifact")
    log.append(
        ToolResultEndEvent(
            **_ctx("artifact").event_fields(),
            tool_call_id="call:firecrawl",
            state=ToolResultState.SUCCESS,
            metadata={"tool_observation_timing": {"observed_at": "2026-01-01T00:00:00Z"}},
            artifacts=[
                ToolResultArtifactRef(
                    artifact_id="artifact:tool-result:run:call:firecrawl:output:0",
                    role="output",
                    media_type="text/markdown; charset=utf-8",
                    size_bytes=1234,
                )
            ],
        )
    )
    transport = CompactScriptedTransport("<summary>Search result was summarized.</summary>")
    service = ContextCompactionService(
        event_log=log,
        archive=archive,
        llm_runtime=_llm_runtime(transport),
        runtime_session_id="runtime:test",
        policy=ContextCompactionPolicy(min_events_after_last_compact=1),
    )

    event = asyncio.run(service.compact(trigger="manual", reason="user_requested", force=True))

    assert event is not None
    assert event.included_artifact_ids == ["artifact:tool-result:run:call:firecrawl:output:0"]


def test_manual_context_compaction_writes_summary_artifact_and_events() -> None:
    log = InMemoryEventLog()
    archive = InMemoryArchiveStore()
    _append_turn(log, "one", "first request", "first reply")
    transport = CompactScriptedTransport(
        "<analysis>draft</analysis><summary>Task state: first request was handled.</summary>"
    )
    service = ContextCompactionService(
        event_log=log,
        archive=archive,
        llm_runtime=_llm_runtime(transport),
        runtime_session_id="runtime:test",
        policy=ContextCompactionPolicy(min_events_after_last_compact=1),
    )

    event = asyncio.run(service.compact(trigger="manual", reason="user_requested", force=True))

    assert event is not None
    assert event.trigger == "manual"
    assert event.summary_artifact_id in archive.blobs
    assert archive.get_text(event.summary_artifact_id, session_id="runtime:test") == "Task state: first request was handled."
    assert any(isinstance(stored, ContextCompactionStartedEvent) for stored in log.iter())
    assert any(isinstance(stored, ContextCompactionCompletedEvent) for stored in log.iter())
    assert transport.contexts
    assert transport.contexts[0].tools == ()
    assert "Do NOT call any tools" in (transport.contexts[0].messages[0].content[0])


def test_context_compaction_appends_pending_memory_candidate() -> None:
    log = InMemoryEventLog()
    archive = InMemoryArchiveStore()
    candidate_pool = InMemoryCandidatePool()
    _append_turn(log, "one", "please remember my workflow", "noted")
    raw = """
<analysis>draft</analysis>
<summary>Task state: user repeatedly syncs release before pushing.</summary>
<memory_candidates_json>
{
  "candidates": [
    {
      "kind": "Preference",
      "statement": "The user prefers syncing release before pushing GitHub in this workspace.",
      "reason": "Observed repeated workflow."
    }
  ]
}
</memory_candidates_json>
"""
    service = ContextCompactionService(
        event_log=log,
        archive=archive,
        llm_runtime=_llm_runtime(CompactScriptedTransport(raw)),
        runtime_session_id="runtime:test",
        policy=ContextCompactionPolicy(min_events_after_last_compact=1),
        candidate_sink=CandidatePoolCompactionMemoryCandidateSink(
            candidate_pool=candidate_pool,
            memory_domain=MemoryDomainContext(
                memory_domain_id="u_test",
                workspace_kind="project",
                stable_project_key="/tmp/pulsara_agent",
            ),
            runtime_session_id="runtime:test",
        ),
    )

    event = asyncio.run(service.compact(trigger="manual", reason="user_requested", force=True))

    assert event is not None
    pending = candidate_pool.list_pending()
    assert len(pending) == 1
    candidate = pending[0]
    assert candidate.origin is CandidateOrigin.COMPACTION
    assert candidate.source_event_id == event.id
    assert candidate.source_artifact_id == event.summary_artifact_id
    assert candidate.intent_fingerprint is not None
    assert candidate.metadata["compaction_id"] == event.compaction_id
    assert candidate.metadata["summary_artifact_id"] == event.summary_artifact_id
    assert candidate.metadata["summary_excerpt"].startswith("Task state:")
    assert candidate.metadata["included_run_ids"] == ["run:one"]
    audit_events = [stored for stored in log.iter() if isinstance(stored, ContextCompactionMemoryCandidatesProposedEvent)]
    assert len(audit_events) == 1
    audit = audit_events[0]
    assert audit.source_event_id == event.id
    assert audit.candidate_entry_ids == [candidate.entry_id]
    assert audit.attempted_count == 1
    assert audit.proposed_count == 1


def test_context_compaction_zero_proposal_audit_event_for_all_skipped() -> None:
    log = InMemoryEventLog()
    archive = InMemoryArchiveStore()
    candidate_pool = InMemoryCandidatePool()
    _append_turn(log, "one", "first request", "first reply")
    raw = """
<summary>Task state: first request was handled.</summary>
<memory_candidates_json>
{
  "candidates": [
    {"kind": "Claim", "statement": "This unsupported claim should be skipped."}
  ]
}
</memory_candidates_json>
"""
    service = ContextCompactionService(
        event_log=log,
        archive=archive,
        llm_runtime=_llm_runtime(CompactScriptedTransport(raw)),
        runtime_session_id="runtime:test",
        policy=ContextCompactionPolicy(min_events_after_last_compact=1),
        candidate_sink=CandidatePoolCompactionMemoryCandidateSink(
            candidate_pool=candidate_pool,
            memory_domain=MemoryDomainContext(
                memory_domain_id="u_test",
                workspace_kind="project",
                stable_project_key="/tmp/pulsara_agent",
            ),
            runtime_session_id="runtime:test",
        ),
    )

    event = asyncio.run(service.compact(trigger="manual", reason="user_requested", force=True))

    assert event is not None
    assert candidate_pool.list_pending() == []
    audit_events = [stored for stored in log.iter() if isinstance(stored, ContextCompactionMemoryCandidatesProposedEvent)]
    assert len(audit_events) == 1
    audit = audit_events[0]
    assert audit.source_event_id == event.id
    assert audit.attempted_count == 1
    assert audit.proposed_count == 0
    assert audit.candidate_entry_ids == []
    assert audit.skipped_count == 1
    assert audit.diagnostics[0].code == "compaction_candidate_skipped:compaction_candidate_kind_not_supported"
    assert "only accepts Preference" in audit.diagnostics[0].message


def test_context_compaction_zero_proposal_audit_event_for_all_duplicate() -> None:
    log = InMemoryEventLog()
    archive = InMemoryArchiveStore()
    candidate_pool = InMemoryCandidatePool()
    _append_turn(log, "one", "first request", "first reply")
    raw = """
<summary>Task state: user repeatedly syncs release before pushing.</summary>
<memory_candidates_json>
{
  "candidates": [
    {
      "kind": "Preference",
      "statement": "The user prefers syncing release before pushing GitHub in this workspace."
    }
  ]
}
</memory_candidates_json>
"""
    sink = CandidatePoolCompactionMemoryCandidateSink(
        candidate_pool=candidate_pool,
        memory_domain=MemoryDomainContext(
            memory_domain_id="u_test",
            workspace_kind="project",
            stable_project_key="/tmp/pulsara_agent",
        ),
        runtime_session_id="runtime:test",
    )
    service = ContextCompactionService(
        event_log=log,
        archive=archive,
        llm_runtime=_llm_runtime(CompactScriptedTransport(raw)),
        runtime_session_id="runtime:test",
        policy=ContextCompactionPolicy(min_events_after_last_compact=1),
        candidate_sink=sink,
    )

    first = asyncio.run(service.compact(trigger="manual", reason="user_requested", force=True))
    second = asyncio.run(service.compact(trigger="manual", reason="user_requested", force=True))

    assert first is not None
    assert second is not None
    assert len(candidate_pool.list_pending()) == 1
    audit_events = [stored for stored in log.iter() if isinstance(stored, ContextCompactionMemoryCandidatesProposedEvent)]
    assert len(audit_events) == 2
    duplicate_audit = audit_events[-1]
    assert duplicate_audit.source_event_id == second.id
    assert duplicate_audit.attempted_count == 1
    assert duplicate_audit.proposed_count == 0
    assert duplicate_audit.candidate_entry_ids == []
    assert duplicate_audit.duplicate_count == 1
    assert duplicate_audit.skipped_count == 1
    assert duplicate_audit.diagnostics[0].code == "compaction_candidate_skipped:duplicate_pending_compaction_candidate"
    assert "same intent fingerprint" in duplicate_audit.diagnostics[0].message


def test_context_compaction_partial_candidate_append_failure_keeps_successful_entries() -> None:
    class FailingSecondAppendPool(InMemoryCandidatePool):
        def __init__(self) -> None:
            super().__init__()
            self.append_attempts = 0

        def append_candidate(self, candidate: PooledMemoryCandidate) -> PooledMemoryCandidate:
            self.append_attempts += 1
            if self.append_attempts == 2:
                raise RuntimeError("database rejected candidate with secret sk-LEAKSHOULDNOTAPPEAR")
            return super().append_candidate(candidate)

    log = InMemoryEventLog()
    archive = InMemoryArchiveStore()
    candidate_pool = FailingSecondAppendPool()
    _append_turn(log, "one", "first request", "first reply")
    raw = """
<summary>Task state: user repeatedly syncs release before pushing.</summary>
<memory_candidates_json>
{
  "candidates": [
    {
      "kind": "Preference",
      "statement": "The user prefers syncing release before pushing GitHub in this workspace."
    },
    {
      "kind": "Preference",
      "statement": "The user prefers staging and committing before syncing release in this workspace."
    }
  ]
}
</memory_candidates_json>
"""
    service = ContextCompactionService(
        event_log=log,
        archive=archive,
        llm_runtime=_llm_runtime(CompactScriptedTransport(raw)),
        runtime_session_id="runtime:test",
        policy=ContextCompactionPolicy(min_events_after_last_compact=1),
        candidate_sink=CandidatePoolCompactionMemoryCandidateSink(
            candidate_pool=candidate_pool,
            memory_domain=MemoryDomainContext(
                memory_domain_id="u_test",
                workspace_kind="project",
                stable_project_key="/tmp/pulsara_agent",
            ),
            runtime_session_id="runtime:test",
        ),
    )

    event = asyncio.run(service.compact(trigger="manual", reason="user_requested", force=True))

    assert event is not None
    pending = candidate_pool.list_pending()
    assert len(pending) == 1
    audit_events = [stored for stored in log.iter() if isinstance(stored, ContextCompactionMemoryCandidatesProposedEvent)]
    assert len(audit_events) == 1
    audit = audit_events[0]
    assert audit.proposed_count == 1
    assert audit.candidate_entry_ids == [pending[0].entry_id]
    assert audit.skipped_count == 1
    assert audit.error_count == 1
    diagnostic_codes = {diagnostic.code for diagnostic in audit.diagnostics}
    assert "compaction_candidate_append_failed" in diagnostic_codes
    assert "compaction_candidate_skipped:compaction_candidate_append_failed" in diagnostic_codes
    append_diagnostic = next(
        diagnostic for diagnostic in audit.diagnostics if diagnostic.code == "compaction_candidate_append_failed"
    )
    assert append_diagnostic.message == "RuntimeError"
    assert append_diagnostic.redacted is True
    assert "sk-LEAK" not in "".join(diagnostic.model_dump_json() for diagnostic in audit.diagnostics)


def test_context_compaction_sink_failure_records_single_redacted_diagnostic() -> None:
    class FailingSink(CandidatePoolCompactionMemoryCandidateSink):
        def append_compaction_candidates(self, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("database rejected candidate with secret sk-LEAKSHOULDNOTAPPEAR")

    log = InMemoryEventLog()
    archive = InMemoryArchiveStore()
    candidate_pool = InMemoryCandidatePool()
    _append_turn(log, "one", "first request", "first reply")
    raw = """
<summary>Task state: user repeatedly syncs release before pushing.</summary>
<memory_candidates_json>
{
  "candidates": [
    {
      "kind": "Preference",
      "statement": "The user prefers syncing release before pushing GitHub in this workspace."
    }
  ]
}
</memory_candidates_json>
"""
    service = ContextCompactionService(
        event_log=log,
        archive=archive,
        llm_runtime=_llm_runtime(CompactScriptedTransport(raw)),
        runtime_session_id="runtime:test",
        policy=ContextCompactionPolicy(min_events_after_last_compact=1),
        candidate_sink=FailingSink(
            candidate_pool=candidate_pool,
            memory_domain=MemoryDomainContext(
                memory_domain_id="u_test",
                workspace_kind="project",
                stable_project_key="/tmp/pulsara_agent",
            ),
            runtime_session_id="runtime:test",
        ),
    )

    event = asyncio.run(service.compact(trigger="manual", reason="user_requested", force=True))

    assert event is not None
    audit_events = [stored for stored in log.iter() if isinstance(stored, ContextCompactionMemoryCandidatesProposedEvent)]
    assert len(audit_events) == 1
    audit = audit_events[0]
    assert audit.proposed_count == 0
    assert audit.error_count == 1
    assert [diagnostic.code for diagnostic in audit.diagnostics] == ["compaction_candidate_append_failed"]
    assert audit.diagnostics[0].message == "RuntimeError"
    assert audit.diagnostics[0].redacted is True
    assert "sk-LEAK" not in audit.diagnostics[0].model_dump_json()


def test_context_compaction_memory_candidate_policy_disabled_omits_prompt_and_audit() -> None:
    log = InMemoryEventLog()
    archive = InMemoryArchiveStore()
    candidate_pool = InMemoryCandidatePool()
    _append_turn(log, "one", "first request", "first reply")
    raw = """
<summary>Task state: first request was handled.</summary>
<memory_candidates_json>
{"candidates": [{"kind": "Preference", "statement": "The user prefers concise output."}]}
</memory_candidates_json>
"""
    transport = CompactScriptedTransport(raw)
    service = ContextCompactionService(
        event_log=log,
        archive=archive,
        llm_runtime=_llm_runtime(transport),
        runtime_session_id="runtime:test",
        policy=ContextCompactionPolicy(
            min_events_after_last_compact=1,
            memory_candidates=ContextCompactionMemoryCandidatePolicy(enabled=False),
        ),
        candidate_sink=CandidatePoolCompactionMemoryCandidateSink(
            candidate_pool=candidate_pool,
            memory_domain=MemoryDomainContext(
                memory_domain_id="u_test",
                workspace_kind="project",
                stable_project_key="/tmp/pulsara_agent",
            ),
            runtime_session_id="runtime:test",
        ),
    )

    event = asyncio.run(service.compact(trigger="manual", reason="user_requested", force=True))

    assert event is not None
    assert "memory_candidates_json" not in transport.contexts[0].messages[0].content[0]
    assert candidate_pool.list_pending() == []
    assert not any(isinstance(stored, ContextCompactionMemoryCandidatesProposedEvent) for stored in log.iter())


def test_context_compaction_transient_workspace_does_not_write_candidate_audit_event() -> None:
    log = InMemoryEventLog()
    archive = InMemoryArchiveStore()
    candidate_pool = InMemoryCandidatePool()
    _append_turn(log, "one", "first request", "first reply")
    raw = """
<summary>Task state: first request was handled.</summary>
<memory_candidates_json>
{"candidates": [{"kind": "Preference", "statement": "The user prefers concise output."}]}
</memory_candidates_json>
"""
    transport = CompactScriptedTransport(raw)
    service = ContextCompactionService(
        event_log=log,
        archive=archive,
        llm_runtime=_llm_runtime(transport),
        runtime_session_id="runtime:test",
        policy=ContextCompactionPolicy(min_events_after_last_compact=1),
        candidate_sink=CandidatePoolCompactionMemoryCandidateSink(
            candidate_pool=candidate_pool,
            memory_domain=MemoryDomainContext(
                memory_domain_id="u_test",
                workspace_kind="transient",
                stable_project_key=None,
            ),
            runtime_session_id="runtime:test",
        ),
    )

    event = asyncio.run(service.compact(trigger="manual", reason="user_requested", force=True))

    assert event is not None
    assert "memory_candidates_json" not in transport.contexts[0].messages[0].content[0]
    assert candidate_pool.list_pending() == []
    assert not any(isinstance(stored, ContextCompactionMemoryCandidatesProposedEvent) for stored in log.iter())


def test_repeated_compaction_carries_previous_summary_forward() -> None:
    log = InMemoryEventLog()
    archive = InMemoryArchiveStore()
    _append_turn(log, "first", "first old request", "first old reply")
    _append_turn(log, "second", "second middle request", "second middle reply")
    transport = CompactScriptedTransport("<summary>FIRST_SENTINEL old context.</summary>")
    service = ContextCompactionService(
        event_log=log,
        archive=archive,
        llm_runtime=_llm_runtime(transport),
        runtime_session_id="runtime:test",
        policy=ContextCompactionPolicy(min_events_after_last_compact=1, keep_recent_runs=1),
    )

    first = asyncio.run(service.compact(trigger="manual", reason="user_requested", force=True))
    assert first is not None
    _append_turn(log, "third", "third recent request", "third recent reply")
    transport.text = "<summary>FIRST_SENTINEL old context plus SECOND_SENTINEL middle context.</summary>"

    second = asyncio.run(service.compact(trigger="manual", reason="user_requested", force=True))

    assert second is not None
    second_input = transport.contexts[-1].messages[1].content[0]
    assert "Previous compact summary to carry forward" in second_input
    assert "FIRST_SENTINEL old context." in second_input
    messages = rebuild_prior_messages(log, archive=archive, session_id="runtime:test")
    rendered = "\n".join(block.text for message in messages for block in message.content if hasattr(block, "text"))
    assert "FIRST_SENTINEL old context" in rendered
    assert "SECOND_SENTINEL middle context" in rendered
    assert "third recent request" in rendered


def test_malformed_compaction_output_records_failed_event_without_artifact() -> None:
    log = InMemoryEventLog()
    archive = InMemoryArchiveStore()
    _append_turn(log, "bad", "bad request", "bad reply")
    transport = CompactScriptedTransport("<analysis>private draft without close")
    service = ContextCompactionService(
        event_log=log,
        archive=archive,
        llm_runtime=_llm_runtime(transport),
        runtime_session_id="runtime:test",
        policy=ContextCompactionPolicy(min_events_after_last_compact=1, auto_threshold_tokens=1),
    )

    assert asyncio.run(service.compact_if_needed(reason="preflight_context_threshold")) is False

    events = log.iter()
    assert any(isinstance(event, ContextCompactionFailedEvent) for event in events)
    assert not any(isinstance(event, ContextCompactionCompletedEvent) for event in events)
    assert not archive.blobs


def test_compaction_model_run_error_fails_even_after_partial_text() -> None:
    log = InMemoryEventLog()
    archive = InMemoryArchiveStore()
    _append_turn(log, "error", "error request", "error reply")
    transport = CompactErrorAfterTextTransport("<summary>partial summary that must not be stored</summary>")
    service = ContextCompactionService(
        event_log=log,
        archive=archive,
        llm_runtime=_llm_runtime(transport),
        runtime_session_id="runtime:test",
        policy=ContextCompactionPolicy(min_events_after_last_compact=1, auto_threshold_tokens=1),
    )

    assert asyncio.run(service.compact_if_needed(reason="preflight_context_threshold")) is False

    events = log.iter()
    failed = [event for event in events if isinstance(event, ContextCompactionFailedEvent)]
    assert len(failed) == 1
    assert "provider failed mid-summary" in failed[0].message
    assert not any(isinstance(event, ContextCompactionCompletedEvent) for event in events)
    assert not archive.blobs


def test_rebuild_prior_messages_uses_completed_boundary_and_replays_tail() -> None:
    log = InMemoryEventLog()
    archive = InMemoryArchiveStore()
    _append_turn(log, "old", "old request", "old reply")
    _append_turn(log, "new", "new request", "new reply")
    transport = CompactScriptedTransport("<summary>Old request was completed.</summary>")
    service = ContextCompactionService(
        event_log=log,
        archive=archive,
        llm_runtime=_llm_runtime(transport),
        runtime_session_id="runtime:test",
        policy=ContextCompactionPolicy(min_events_after_last_compact=1, keep_recent_runs=1),
    )
    event = asyncio.run(service.compact(trigger="manual", reason="user_requested", force=True))
    assert event is not None

    messages = rebuild_prior_messages(log, archive=archive, session_id="runtime:test")
    rendered = "\n".join(block.text for message in messages for block in message.content if hasattr(block, "text"))

    assert "<context-compaction-summary" in rendered
    assert "Old request was completed." in rendered
    assert "new request" in rendered
    assert "new reply" in rendered
    assert "old request" not in rendered


def test_rebuild_prior_messages_before_sequence_uses_mid_turn_boundary_without_replaying_current_run() -> None:
    log = InMemoryEventLog()
    archive = InMemoryArchiveStore()
    _append_turn(log, "old", "old request", "old reply")
    current = _ctx("current")
    current_start = log.append(
        RunStartEvent(
            **current.event_fields(),
            **run_start_permission_fields(current.run_id),
            user_input_chars=len("current request"),
            metadata={"user_input": "current request"},
        )
    )
    assert current_start.sequence is not None
    transport = CompactScriptedTransport("<summary>Old request was compacted mid-turn.</summary>")
    service = ContextCompactionService(
        event_log=log,
        archive=archive,
        llm_runtime=_llm_runtime(transport),
        runtime_session_id="runtime:test",
        policy=ContextCompactionPolicy(min_events_after_last_compact=1, keep_recent_runs=1),
    )
    completed = asyncio.run(
        service.compact(
            trigger="auto",
            reason="mid_turn_context_threshold",
            force=True,
            max_compactable_sequence=current_start.sequence - 1,
            keep_recent_runs_override=1,
            event_metadata={"phase": "mid_turn"},
        )
    )
    assert completed is not None
    assert completed.sequence is not None
    assert completed.sequence > current_start.sequence
    compact_input = transport.contexts[-1].messages[1].content[0]
    assert "old request" in compact_input
    assert "current request" not in compact_input

    messages = rebuild_prior_messages_before_sequence(
        log,
        archive=archive,
        session_id="runtime:test",
        before_sequence=current_start.sequence,
    )
    rendered = "\n".join(block.text for message in messages for block in message.content if hasattr(block, "text"))

    assert "Old request was compacted mid-turn." in rendered
    assert "current request" not in rendered


def test_runtime_context_compactor_rewrites_prefix_and_preserves_current_run_tail(tmp_path) -> None:
    async def run():
        wiring = build_in_memory_runtime_wiring(tmp_path)
        runtime_session = wiring.runtime_session
        await _emit_turn(runtime_session, "old-a", "old-a request", "old-a reply")
        await _emit_turn(runtime_session, "old-b", "old-b request", "old-b reply")
        state = _current_tail_state(runtime_session.runtime_session_id)
        current_start = await runtime_session.emit(
            RunStartEvent(
                run_id=state.run_id,
                turn_id=state.turn_id,
                reply_id=state.reply_id,
                **run_start_permission_fields(state.run_id),
                user_input_chars=len("current request"),
                metadata={"user_input": "current request"},
            ),
            state=state,
        )
        transport = CompactScriptedTransport("<summary>Old request was summarized.</summary>")
        service = ContextCompactionService(
            event_log=wiring.event_log,
            archive=wiring.archive,
            llm_runtime=_llm_runtime(transport),
            runtime_session_id=runtime_session.runtime_session_id,
            policy=ContextCompactionPolicy(min_events_after_last_compact=1, auto_threshold_tokens=1),
        )
        compactor = RuntimeContextCompactor(
            event_log=wiring.event_log,
            archive=wiring.archive,
            runtime_session=runtime_session,
            service=service,
        )

        result = await compactor.maybe_compact_before_followup(
            state=state,
            model_visible_messages=state.messages,
        )

        assert result.compacted is True
        assert current_start.sequence is not None
        compact_input = transport.contexts[-1].messages[1].content[0]
        assert "old-a request" in compact_input
        assert "old-b request" not in compact_input
        assert "current request" not in compact_input
        assert result.rewritten_messages is not None
        rendered = "\n".join(
            block.text
            for message in result.rewritten_messages
            for block in message.content
            if hasattr(block, "text")
        )
        assert "Old request was summarized." in rendered
        assert "old-a request" not in rendered
        assert "old-b request" in rendered
        assert "current request" in rendered
        assert any(
            isinstance(block, ToolResultBlock)
            and any(isinstance(item, TextBlock) and item.text == "current tool result" for item in block.output)
            for message in result.rewritten_messages
            for block in message.content
        )
        completed = [event for event in result.events if isinstance(event, ContextCompactionCompletedEvent)]
        assert completed
        assert completed[-1].metadata["phase"] == "mid_turn"
        assert completed[-1].metadata["current_run_start_sequence"] == current_start.sequence
        assert state.scratchpad["mid_turn_compaction"]["compaction_id"] == completed[-1].compaction_id

    asyncio.run(run())


def test_runtime_context_compactor_failure_publishes_events_and_keeps_state_messages(tmp_path) -> None:
    async def run():
        wiring = build_in_memory_runtime_wiring(tmp_path)
        runtime_session = wiring.runtime_session
        await _emit_turn(runtime_session, "old-a", "old-a request", "old-a reply")
        await _emit_turn(runtime_session, "old-b", "old-b request", "old-b reply")
        state = _current_tail_state(runtime_session.runtime_session_id)
        await runtime_session.emit(
            RunStartEvent(
                run_id=state.run_id,
                turn_id=state.turn_id,
                reply_id=state.reply_id,
                **run_start_permission_fields(state.run_id),
                user_input_chars=len("current request"),
                metadata={"user_input": "current request"},
            ),
            state=state,
        )
        original_message_ids = [message.id for message in state.messages]
        service = ContextCompactionService(
            event_log=wiring.event_log,
            archive=wiring.archive,
            llm_runtime=_llm_runtime(CompactErrorAfterTextTransport("<summary>partial</summary>")),
            runtime_session_id=runtime_session.runtime_session_id,
            policy=ContextCompactionPolicy(min_events_after_last_compact=1, auto_threshold_tokens=1),
        )
        compactor = RuntimeContextCompactor(
            event_log=wiring.event_log,
            archive=wiring.archive,
            runtime_session=runtime_session,
            service=service,
        )

        result = await compactor.maybe_compact_before_followup(
            state=state,
            model_visible_messages=state.messages,
        )

        assert result.compacted is False
        assert [message.id for message in state.messages] == original_message_ids
        assert any(isinstance(event, ContextCompactionFailedEvent) for event in result.events)
        emitted = await asyncio.wait_for(
            runtime_session.emit(
                CustomEvent(
                    run_id=state.run_id,
                    turn_id=state.turn_id,
                    reply_id=state.reply_id,
                    name="after_failed_mid_turn_compaction",
                    value={},
                ),
                state=state,
            ),
            timeout=1,
        )
        assert emitted.name == "after_failed_mid_turn_compaction"

    asyncio.run(run())


def test_missing_summary_artifact_falls_back_to_full_event_replay() -> None:
    log = InMemoryEventLog()
    archive = InMemoryArchiveStore()
    _append_turn(log, "old", "old request", "old reply")
    transport = CompactScriptedTransport("<summary>Old summary.</summary>")
    service = ContextCompactionService(
        event_log=log,
        archive=archive,
        llm_runtime=_llm_runtime(transport),
        runtime_session_id="runtime:test",
        policy=ContextCompactionPolicy(min_events_after_last_compact=1),
    )
    event = asyncio.run(service.compact(trigger="manual", reason="user_requested", force=True))
    assert event is not None
    archive.blobs.pop(event.summary_artifact_id)

    messages = rebuild_prior_messages(log, archive=archive, session_id="runtime:test")
    rendered = "\n".join(block.text for message in messages for block in message.content if hasattr(block, "text"))

    assert "<context-compaction-summary" not in rendered
    assert "old request" in rendered
    assert "old reply" in rendered


def test_auto_context_compaction_is_threshold_driven_not_run_end_unconditional() -> None:
    log = InMemoryEventLog()
    archive = InMemoryArchiveStore()
    _append_turn(log, "tiny", "short", "ok")
    transport = CompactScriptedTransport("<summary>tiny</summary>")
    service = ContextCompactionService(
        event_log=log,
        archive=archive,
        llm_runtime=_llm_runtime(transport),
        runtime_session_id="runtime:test",
        policy=ContextCompactionPolicy(
            min_events_after_last_compact=1,
            auto_threshold_tokens=10_000,
            chars_per_token=1.0,
        ),
    )

    assert service.should_auto_compact() is False
    assert asyncio.run(service.compact_if_needed(reason="preflight_context_threshold")) is False
    assert not any(isinstance(stored, ContextCompactionStartedEvent) for stored in log.iter())


def test_auto_context_compaction_can_compact_single_huge_completed_run() -> None:
    log = InMemoryEventLog()
    archive = InMemoryArchiveStore()
    _append_turn(log, "huge", "x" * 200, "y" * 200)
    transport = CompactScriptedTransport("<summary>Huge run summarized.</summary>")
    service = ContextCompactionService(
        event_log=log,
        archive=archive,
        llm_runtime=_llm_runtime(transport),
        runtime_session_id="runtime:test",
        policy=ContextCompactionPolicy(
            min_events_after_last_compact=1,
            keep_recent_runs=3,
            auto_threshold_tokens=100,
            chars_per_token=1.0,
        ),
    )

    assert service.should_auto_compact() is True
    assert asyncio.run(service.compact_if_needed(reason="preflight_context_threshold")) is True
    completed = [event for event in log.iter() if isinstance(event, ContextCompactionCompletedEvent)]

    assert len(completed) == 1
    assert completed[0].trigger == "auto"
    assert archive.get_text(completed[0].summary_artifact_id, session_id="runtime:test") == "Huge run summarized."


def test_auto_context_compaction_uses_model_visible_messages_not_raw_streaming_events() -> None:
    log = InMemoryEventLog()
    archive = InMemoryArchiveStore()
    ctx = _ctx("streamy")
    log.extend(
        [
            RunStartEvent(**ctx.event_fields(), **run_start_permission_fields(ctx.run_id), user_input_chars=5, metadata={"user_input": "short"}),
            ReplyStartEvent(**ctx.event_fields(), name="assistant"),
            TextBlockStartEvent(**ctx.event_fields(), block_id="text:streamy"),
            *[
                TextBlockDeltaEvent(**ctx.event_fields(), block_id="text:streamy", delta="x")
                for _ in range(500)
            ],
            TextBlockEndEvent(**ctx.event_fields(), block_id="text:streamy"),
            ThinkingBlockStartEvent(**ctx.event_fields(), block_id="thinking:streamy"),
            *[
                ThinkingBlockDeltaEvent(**ctx.event_fields(), block_id="thinking:streamy", delta="private")
                for _ in range(500)
            ],
            ThinkingBlockEndEvent(**ctx.event_fields(), block_id="thinking:streamy"),
            ReplyEndEvent(**ctx.event_fields()),
        ]
    )
    transport = CompactScriptedTransport("<summary>should not run</summary>")
    service = ContextCompactionService(
        event_log=log,
        archive=archive,
        llm_runtime=_llm_runtime(transport),
        runtime_session_id="runtime:test",
        policy=ContextCompactionPolicy(
            min_events_after_last_compact=1,
            auto_threshold_tokens=1_000,
            chars_per_token=4.0,
            event_chars_per_token=0.5,
        ),
    )

    assert service.should_auto_compact(model_visible_messages=[]) is False
    assert asyncio.run(service.compact_if_needed(reason="preflight_context_threshold", model_visible_messages=[])) is False
    assert not [event for event in log.iter() if isinstance(event, ContextCompactionStartedEvent)]


def test_auto_context_compaction_triggers_on_long_model_visible_messages() -> None:
    log = InMemoryEventLog()
    archive = InMemoryArchiveStore()
    _append_turn(log, "tiny-events", "short", "ok")
    transport = CompactScriptedTransport("<summary>Visible transcript was summarized.</summary>")
    service = ContextCompactionService(
        event_log=log,
        archive=archive,
        llm_runtime=_llm_runtime(transport),
        runtime_session_id="runtime:test",
        policy=ContextCompactionPolicy(
            min_events_after_last_compact=1,
            auto_threshold_tokens=100,
            chars_per_token=1.0,
        ),
    )
    visible = rebuild_prior_messages(log, archive=archive, session_id="runtime:test")
    visible[0].content[0].text = "visible " * 200

    assert service.should_auto_compact(model_visible_messages=visible) is True
    assert asyncio.run(service.compact_if_needed(reason="preflight_context_threshold", model_visible_messages=visible))


def test_auto_context_compaction_can_use_context_compiled_estimate() -> None:
    log = InMemoryEventLog()
    archive = InMemoryArchiveStore()
    ctx = _ctx("compiled-estimate")
    log.extend(
        [
            RunStartEvent(**ctx.event_fields(), **run_start_permission_fields(ctx.run_id), user_input_chars=5, metadata={"user_input": "hello"}),
            ContextCompiledEvent(
                **ctx.event_fields(),
                context_id="context:compiled-estimate",
                model_role="pro",
                model_call_index=1,
                estimated_tokens=250,
                context_window_tokens=256_000,
                reserved_output_tokens=8_000,
                tools_estimated_tokens=30,
                sections=[],
                tool_specs=[],
                diagnostics=[],
                lifecycle_decisions=[],
            ),
            ReplyStartEvent(**ctx.event_fields(), name="assistant"),
            TextBlockStartEvent(**ctx.event_fields(), block_id="text:compiled-estimate"),
            TextBlockDeltaEvent(**ctx.event_fields(), block_id="text:compiled-estimate", delta="ok"),
            TextBlockEndEvent(**ctx.event_fields(), block_id="text:compiled-estimate"),
            ReplyEndEvent(**ctx.event_fields()),
        ]
    )
    service = ContextCompactionService(
        event_log=log,
        archive=archive,
        llm_runtime=_llm_runtime(CompactScriptedTransport("<summary>Compiled estimate summarized.</summary>")),
        runtime_session_id="runtime:test",
        policy=ContextCompactionPolicy(
            min_events_after_last_compact=1,
            auto_threshold_tokens=200,
            chars_per_token=1.0,
        ),
    )

    assert service.should_auto_compact() is True
    assert asyncio.run(service.compact_if_needed(reason="preflight_context_threshold")) is True
    completed = [event for event in log.iter() if isinstance(event, ContextCompactionCompletedEvent)]
    assert completed[-1].metadata["estimate_source"] == "compiled_context_event"
    assert completed[-1].estimated_tokens_before > 250


def test_auto_context_compaction_compiled_prefix_uses_safety_margin() -> None:
    log = InMemoryEventLog()
    archive = InMemoryArchiveStore()
    ctx = _ctx("compiled-margin")
    log.extend(
        [
            RunStartEvent(**ctx.event_fields(), **run_start_permission_fields(ctx.run_id), user_input_chars=5, metadata={"user_input": "hello"}),
            ContextCompiledEvent(
                **ctx.event_fields(),
                context_id="context:compiled-margin",
                model_role="pro",
                model_call_index=1,
                estimated_tokens=90,
                context_window_tokens=256_000,
                reserved_output_tokens=8_000,
                tools_estimated_tokens=0,
                sections=[],
                tool_specs=[],
                diagnostics=[],
                lifecycle_decisions=[],
            ),
            ReplyStartEvent(**ctx.event_fields(), name="assistant"),
            TextBlockStartEvent(**ctx.event_fields(), block_id="text:compiled-margin"),
            TextBlockDeltaEvent(**ctx.event_fields(), block_id="text:compiled-margin", delta="ok"),
            TextBlockEndEvent(**ctx.event_fields(), block_id="text:compiled-margin"),
            ReplyEndEvent(**ctx.event_fields()),
        ]
    )
    service = ContextCompactionService(
        event_log=log,
        archive=archive,
        llm_runtime=_llm_runtime(CompactScriptedTransport("<summary>Compiled margin.</summary>")),
        runtime_session_id="runtime:test",
        policy=ContextCompactionPolicy(
            min_events_after_last_compact=1,
            auto_threshold_tokens=100,
            chars_per_token=1.0,
            estimate_safety_margin=1.25,
        ),
    )

    assert service.should_auto_compact() is True


def test_auto_context_compaction_compiled_estimate_includes_post_model_output() -> None:
    log = InMemoryEventLog()
    archive = InMemoryArchiveStore()
    ctx = _ctx("compiled-post-output")
    log.extend(
        [
            RunStartEvent(**ctx.event_fields(), **run_start_permission_fields(ctx.run_id), user_input_chars=5, metadata={"user_input": "hello"}),
            ContextCompiledEvent(
                **ctx.event_fields(),
                context_id="context:compiled-post-output",
                model_role="pro",
                model_call_index=1,
                estimated_tokens=10,
                context_window_tokens=256_000,
                reserved_output_tokens=8_000,
                tools_estimated_tokens=0,
                sections=[],
                tool_specs=[],
                diagnostics=[],
                lifecycle_decisions=[],
            ),
            ReplyStartEvent(**ctx.event_fields(), name="assistant"),
            TextBlockStartEvent(**ctx.event_fields(), block_id="text:compiled-post-output"),
            TextBlockDeltaEvent(
                **ctx.event_fields(),
                block_id="text:compiled-post-output",
                delta="POST_COMPILED_OUTPUT_SENTINEL " + ("x" * 250),
            ),
            TextBlockEndEvent(**ctx.event_fields(), block_id="text:compiled-post-output"),
            ReplyEndEvent(**ctx.event_fields()),
        ]
    )
    service = ContextCompactionService(
        event_log=log,
        archive=archive,
        llm_runtime=_llm_runtime(CompactScriptedTransport("<summary>Compiled estimate plus output.</summary>")),
        runtime_session_id="runtime:test",
        policy=ContextCompactionPolicy(
            min_events_after_last_compact=1,
            auto_threshold_tokens=100,
            chars_per_token=1.0,
        ),
    )

    assert service.should_auto_compact() is True
    assert asyncio.run(service.compact_if_needed(reason="preflight_context_threshold")) is True
    completed = [event for event in log.iter() if isinstance(event, ContextCompactionCompletedEvent)]
    assert completed[-1].metadata["estimate_source"] == "compiled_context_event"
    assert completed[-1].estimated_tokens_before > 100


def test_compaction_input_coalesces_deltas_and_clips_large_tool_result() -> None:
    log = InMemoryEventLog()
    archive = InMemoryArchiveStore()
    ctx = _ctx("coalesce")
    log.extend(
        [
            RunStartEvent(**ctx.event_fields(), **run_start_permission_fields(ctx.run_id), user_input_chars=11, metadata={"user_input": "search news"}),
            ReplyStartEvent(**ctx.event_fields(), name="assistant"),
            TextBlockStartEvent(**ctx.event_fields(), block_id="text:coalesce"),
            TextBlockDeltaEvent(**ctx.event_fields(), block_id="text:coalesce", delta="hello "),
            TextBlockDeltaEvent(**ctx.event_fields(), block_id="text:coalesce", delta="world"),
            TextBlockEndEvent(**ctx.event_fields(), block_id="text:coalesce"),
            ToolCallStartEvent(**ctx.event_fields(), tool_call_id="call:search", tool_call_name="terminal"),
            ToolCallDeltaEvent(**ctx.event_fields(), tool_call_id="call:search", delta='{"cmd":"search"}'),
            ToolCallEndEvent(**ctx.event_fields(), tool_call_id="call:search"),
            ToolResultStartEvent(**ctx.event_fields(), tool_call_id="call:search", tool_call_name="terminal"),
            ToolResultTextDeltaEvent(**ctx.event_fields(), tool_call_id="call:search", delta="R" * 5_000),
            ToolResultEndEvent(
                **ctx.event_fields(),
                tool_call_id="call:search",
                state=ToolResultState.SUCCESS,
                metadata={"tool_observation_timing": {"observed_at": "2026-01-01T00:00:00Z"}},
                artifacts=[
                    ToolResultArtifactRef(
                        artifact_id="artifact:search:full",
                        role="output",
                        media_type="text/plain",
                        size_bytes=5000,
                    )
                ],
            ),
            ReplyEndEvent(**ctx.event_fields()),
        ]
    )
    service = ContextCompactionService(
        event_log=log,
        archive=archive,
        llm_runtime=_llm_runtime(CompactScriptedTransport("<summary>done</summary>")),
        runtime_session_id="runtime:test",
        policy=ContextCompactionPolicy(min_events_after_last_compact=1),
    )
    plan = service._build_plan(log.iter(), force=True)
    assert plan is not None

    compact_input = build_compaction_input(plan)

    assert "hello world" in compact_input
    assert "artifact:search:full" in compact_input
    assert "[CLIPPED:" in compact_input
    assert "TEXT_BLOCK_DELTA" not in compact_input
    assert "TOOL_RESULT_TEXT_DELTA" not in compact_input


def test_auto_context_compaction_failure_trips_circuit_breaker_without_completed_boundary() -> None:
    log = InMemoryEventLog()
    archive = InMemoryArchiveStore()
    _append_turn(log, "huge", "x" * 200, "y" * 200)
    transport = CompactScriptedTransport("<analysis>draft</analysis>")
    service = ContextCompactionService(
        event_log=log,
        archive=archive,
        llm_runtime=_llm_runtime(transport),
        runtime_session_id="runtime:test",
        policy=ContextCompactionPolicy(
            min_events_after_last_compact=1,
            auto_threshold_tokens=100,
            max_consecutive_failures=1,
            chars_per_token=1.0,
        ),
    )

    assert asyncio.run(service.compact_if_needed(reason="preflight_context_threshold")) is False
    assert service.should_auto_compact() is False
    assert len([event for event in log.iter() if isinstance(event, ContextCompactionFailedEvent)]) == 1
    assert not [event for event in log.iter() if isinstance(event, ContextCompactionCompletedEvent)]


def test_preflight_current_user_input_affects_threshold_but_not_summary_input() -> None:
    log = InMemoryEventLog()
    archive = InMemoryArchiveStore()
    _append_turn(log, "old", "old", "old reply")
    transport = CompactScriptedTransport("<summary>Old summarized.</summary>")
    service = ContextCompactionService(
        event_log=log,
        archive=archive,
        llm_runtime=_llm_runtime(transport),
        runtime_session_id="runtime:test",
        policy=ContextCompactionPolicy(
            min_events_after_last_compact=1,
            auto_threshold_tokens=100,
            chars_per_token=1.0,
        ),
    )

    assert asyncio.run(
        service.compact_if_needed(
            current_user_input="CURRENT_USER_INPUT_SHOULD_NOT_BE_SUMMARIZED" * 4,
            reason="preflight_context_threshold",
        )
    ) is True

    compact_input = transport.contexts[0].messages[1].content[0]
    assert "CURRENT_USER_INPUT_SHOULD_NOT_BE_SUMMARIZED" not in compact_input
    completed = [event for event in log.iter() if isinstance(event, ContextCompactionCompletedEvent)]
    assert completed[0].reason == "preflight_context_threshold"


def test_host_session_compact_now_uses_manual_force_entrypoint(tmp_path) -> None:
    transport = CompactScriptedTransport("unused")
    runtime_wiring = build_in_memory_runtime_wiring(tmp_path)
    fake = _FakeHostCompactionService()
    runtime_wiring = replace(runtime_wiring, compaction_service=fake)
    session = HostSession(
        host_session_id="host:test",
        conversation_id="conversation:test",
        workspace=resolve_workspace(HostWorkspaceInput(workspace_root=tmp_path, workspace_kind="project")),
        wiring=AgentRuntimeWiring(
            agent_runtime=AgentRuntime(capability_runtime=CapabilityRuntime(),
                runtime_session=runtime_wiring.runtime_session,
                llm_runtime=_llm_runtime(transport),
            ),
            runtime_wiring=runtime_wiring,
        ),
    )

    async def run() -> tuple[dict[str, object], list[dict[str, object]]]:
        try:
            return await session.compact_now(), fake.calls
        finally:
            await session.aclose()

    result, calls = asyncio.run(run())

    assert result == {
        "compacted": True,
        "compaction_id": "context_compaction:host",
        "summary_artifact_id": "context_compaction_host:summary",
        "window_id": "context_window:host",
        "through_sequence": 10,
        "keep_after_sequence": 10,
    }
    assert calls == [{"trigger": "manual", "reason": "user_requested", "force": True}]


def test_host_session_invokes_compaction_at_preflight_only(tmp_path) -> None:
    transport = CompactScriptedTransport("final answer")
    runtime_wiring = build_in_memory_runtime_wiring(tmp_path)
    fake = _FakeHostCompactionService()
    runtime_wiring = replace(runtime_wiring, compaction_service=fake)
    session = HostSession(
        host_session_id="host:test",
        conversation_id="conversation:test",
        workspace=resolve_workspace(HostWorkspaceInput(workspace_root=tmp_path, workspace_kind="project")),
        wiring=AgentRuntimeWiring(
            agent_runtime=AgentRuntime(capability_runtime=CapabilityRuntime(),
                runtime_session=runtime_wiring.runtime_session,
                llm_runtime=_llm_runtime(transport),
            ),
            runtime_wiring=runtime_wiring,
        ),
    )

    async def run() -> list[dict[str, object]]:
        try:
            result = await session.run_turn("hello compaction")
            assert result.final_text == "final answer"
            return fake.calls
        finally:
            await session.aclose()

    calls = asyncio.run(run())

    preflight = next(call for call in calls if call.get("reason") == "preflight_context_threshold")
    assert preflight["method"] == "compact_if_needed"
    assert preflight["current_user_input"] == "hello compaction"
    assert preflight["model_visible_messages"] == []
    assert [call.get("reason") for call in calls] == ["preflight_context_threshold"]


def test_host_session_does_not_notify_compaction_listener_after_run_end(tmp_path) -> None:
    transport = CompactScriptedTransport("final answer")
    runtime_wiring = build_in_memory_runtime_wiring(tmp_path)
    fake = _FakeHostCompactionService()
    runtime_wiring = replace(runtime_wiring, compaction_service=fake)
    session = HostSession(
        host_session_id="host:test",
        conversation_id="conversation:test",
        workspace=resolve_workspace(HostWorkspaceInput(workspace_root=tmp_path, workspace_kind="project")),
        wiring=AgentRuntimeWiring(
            agent_runtime=AgentRuntime(
                capability_runtime=CapabilityRuntime(),
                runtime_session=runtime_wiring.runtime_session,
                llm_runtime=_llm_runtime(transport),
            ),
            runtime_wiring=runtime_wiring,
        ),
    )
    observed = []
    session.add_compaction_listener(observed.append)

    async def run() -> None:
        try:
            result = await session.run_turn("hello compaction")
            assert result.final_text == "final answer"
            await asyncio.sleep(0)
        finally:
            await session.aclose()

    asyncio.run(run())

    assert observed == []
    assert [call.get("reason") for call in fake.calls] == ["preflight_context_threshold"]


def test_preflight_compaction_rebuilds_prior_messages_and_continues_original_user_input(tmp_path) -> None:
    transport = CompactScriptedTransport("final answer")
    runtime_wiring = build_in_memory_runtime_wiring(tmp_path)
    _append_turn(runtime_wiring.event_log, "old-host", "old host request", "old host reply")
    fake = _FakePreflightBoundaryCompactionService(
        runtime_wiring.event_log,
        runtime_wiring.archive,
        runtime_wiring.runtime_session.runtime_session_id,
    )
    runtime_wiring = replace(runtime_wiring, compaction_service=fake)
    session = HostSession(
        host_session_id="host:test",
        conversation_id="conversation:test",
        workspace=resolve_workspace(HostWorkspaceInput(workspace_root=tmp_path, workspace_kind="project")),
        wiring=AgentRuntimeWiring(
            agent_runtime=AgentRuntime(
                capability_runtime=CapabilityRuntime(),
                runtime_session=runtime_wiring.runtime_session,
                llm_runtime=_llm_runtime(transport),
            ),
            runtime_wiring=runtime_wiring,
        ),
    )

    async def run() -> None:
        try:
            result = await session.run_turn("CURRENT_USER_INPUT")
            assert result.final_text == "final answer"
        finally:
            await session.aclose()

    asyncio.run(run())

    assert fake.calls[0]["current_user_input"] == "CURRENT_USER_INPUT"
    assert transport.contexts
    rendered = "\n".join(part for message in transport.contexts[0].messages for part in message.content)
    assert "Preflight summary sentinel." in rendered
    assert "CURRENT_USER_INPUT" in rendered
    assert "old host request" not in rendered


def test_host_session_notifies_preflight_auto_compaction_failure(tmp_path) -> None:
    runtime_wiring = build_in_memory_runtime_wiring(tmp_path)
    fake = _FakeFailingAutoCompactionService(runtime_wiring.event_log)
    session = HostSession(
        host_session_id="host:test",
        conversation_id="conversation:test",
        workspace=resolve_workspace(HostWorkspaceInput(workspace_root=tmp_path, workspace_kind="project")),
        wiring=AgentRuntimeWiring(
            agent_runtime=AgentRuntime(
                capability_runtime=CapabilityRuntime(),
                runtime_session=runtime_wiring.runtime_session,
                llm_runtime=_llm_runtime(CompactScriptedTransport("unused")),
            ),
            runtime_wiring=replace(runtime_wiring, compaction_service=fake),
        ),
    )
    observed = []
    session.add_compaction_listener(observed.append)

    async def run() -> None:
        try:
            await session._compact_if_needed_and_notify(fake, reason="preflight_context_threshold")
        finally:
            await session.aclose()

    asyncio.run(run())

    assert len(observed) == 1
    assert isinstance(observed[0], ContextCompactionFailedEvent)
    assert observed[0].compaction_id == "context_compaction:failed"


def test_host_session_publishes_directly_written_preflight_compaction_events_to_avoid_sequence_gap(tmp_path) -> None:
    runtime_wiring = build_in_memory_runtime_wiring(tmp_path)
    fake = _FakeWritingAutoCompactionService(runtime_wiring.event_log)
    session = HostSession(
        host_session_id="host:test",
        conversation_id="conversation:test",
        workspace=resolve_workspace(HostWorkspaceInput(workspace_root=tmp_path, workspace_kind="project")),
        wiring=AgentRuntimeWiring(
            agent_runtime=AgentRuntime(capability_runtime=CapabilityRuntime(),
                runtime_session=runtime_wiring.runtime_session,
                llm_runtime=_llm_runtime(CompactScriptedTransport("unused")),
            ),
            runtime_wiring=runtime_wiring,
        ),
    )
    observed = []
    session.add_compaction_listener(observed.append)

    async def run() -> None:
        try:
            await runtime_wiring.runtime_session.emit(
                RunStartEvent(
                    **_ctx("before-gap").event_fields(),
                    **run_start_permission_fields(_ctx("before-gap").run_id),
                    user_input_chars=1,
                )
            )
            assert await session._compact_if_needed_and_notify(fake, reason="preflight_context_threshold") is True
            await asyncio.wait_for(
                runtime_wiring.runtime_session.emit(
                    RunStartEvent(
                        **_ctx("after-gap").event_fields(),
                        **run_start_permission_fields(_ctx("after-gap").run_id),
                        user_input_chars=1,
                    )
                ),
                timeout=1,
            )
        finally:
            await session.aclose()

    asyncio.run(run())

    assert len(observed) == 1
    assert isinstance(observed[0], ContextCompactionCompletedEvent)


def test_host_session_compact_now_publishes_directly_written_events_without_notifying_listener(tmp_path) -> None:
    runtime_wiring = build_in_memory_runtime_wiring(tmp_path)
    fake = _FakeWritingManualCompactionService(runtime_wiring.event_log)
    session = HostSession(
        host_session_id="host:test",
        conversation_id="conversation:test",
        workspace=resolve_workspace(HostWorkspaceInput(workspace_root=tmp_path, workspace_kind="project")),
        wiring=AgentRuntimeWiring(
            agent_runtime=AgentRuntime(
                capability_runtime=CapabilityRuntime(),
                runtime_session=runtime_wiring.runtime_session,
                llm_runtime=_llm_runtime(CompactScriptedTransport("unused")),
            ),
            runtime_wiring=replace(runtime_wiring, compaction_service=fake),
        ),
    )
    observed = []
    session.add_compaction_listener(observed.append)

    async def run() -> dict[str, object]:
        try:
            await runtime_wiring.runtime_session.emit(
                RunStartEvent(
                    **_ctx("manual-before-gap").event_fields(),
                    **run_start_permission_fields(_ctx("manual-before-gap").run_id),
                    user_input_chars=1,
                )
            )
            result = await session.compact_now()
            await asyncio.wait_for(
                runtime_wiring.runtime_session.emit(
                    RunStartEvent(
                        **_ctx("manual-after-gap").event_fields(),
                        **run_start_permission_fields(_ctx("manual-after-gap").run_id),
                        user_input_chars=1,
                    )
                ),
                timeout=1,
            )
            return result
        finally:
            await session.aclose()

    result = asyncio.run(run())

    assert result["compacted"] is True
    assert result["compaction_id"] == "context_compaction:manual"
    assert observed == []


def test_host_session_compact_now_failure_publishes_events_to_avoid_sequence_gap(tmp_path) -> None:
    runtime_wiring = build_in_memory_runtime_wiring(tmp_path)
    fake = _FakeFailingManualCompactionService(runtime_wiring.event_log)
    session = HostSession(
        host_session_id="host:test",
        conversation_id="conversation:test",
        workspace=resolve_workspace(HostWorkspaceInput(workspace_root=tmp_path, workspace_kind="project")),
        wiring=AgentRuntimeWiring(
            agent_runtime=AgentRuntime(
                capability_runtime=CapabilityRuntime(),
                runtime_session=runtime_wiring.runtime_session,
                llm_runtime=_llm_runtime(CompactScriptedTransport("unused")),
            ),
            runtime_wiring=replace(runtime_wiring, compaction_service=fake),
        ),
    )
    observed = []
    session.add_compaction_listener(observed.append)

    async def run() -> None:
        try:
            await runtime_wiring.runtime_session.emit(
                RunStartEvent(
                    **_ctx("manual-fail-before-gap").event_fields(),
                    **run_start_permission_fields(_ctx("manual-fail-before-gap").run_id),
                    user_input_chars=1,
                )
            )
            try:
                await session.compact_now()
            except RuntimeError as exc:
                assert str(exc) == "manual compact exploded"
            else:
                raise AssertionError("manual compact failure did not propagate")
            await asyncio.wait_for(
                runtime_wiring.runtime_session.emit(
                    RunStartEvent(
                        **_ctx("manual-fail-after-gap").event_fields(),
                        **run_start_permission_fields(_ctx("manual-fail-after-gap").run_id),
                        user_input_chars=1,
                    )
                ),
                timeout=1,
            )
        finally:
            await session.aclose()

    asyncio.run(run())

    failed = [event for event in runtime_wiring.event_log.iter() if isinstance(event, ContextCompactionFailedEvent)]
    assert len(failed) == 1
    assert failed[0].compaction_id == "context_compaction:manual_failed"
    assert observed == []


def test_pending_approval_resume_does_not_auto_compact(tmp_path) -> None:
    runtime_wiring = build_in_memory_runtime_wiring(tmp_path)
    fake = _FakeHostCompactionService()
    runtime_wiring = replace(runtime_wiring, compaction_service=fake)
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_wiring.runtime_session,
        llm_runtime=_llm_runtime(CompactScriptedTransport("unused")),
    )
    session = HostSession(
        host_session_id="host:test",
        conversation_id="conversation:test",
        workspace=resolve_workspace(HostWorkspaceInput(workspace_root=tmp_path, workspace_kind="project")),
        wiring=AgentRuntimeWiring(agent_runtime=agent, runtime_wiring=runtime_wiring),
    )
    state = agent.new_state()
    state.status = LoopStatus.WAITING_USER
    pending = PendingApproval(
        approval_id="approval:test",
        host_session_id=session.host_session_id,
        runtime_session_id=session.runtime_session_id,
        run_id=state.run_id,
        turn_id=state.turn_id,
        reply_id=state.reply_id,
        tool_calls=(ToolCallBlock(id="call:test", name="terminal", state=ToolCallState.ASKING),),
    )
    session.pending_interaction = pending
    session._suspended_state = state
    session.suspended_run_id = state.run_id

    async def fake_resume(resume_state, resolution):
        resume_state.status = LoopStatus.FINISHED
        resume_state.stop_reason = "final"
        return AgentRunResult(
            status=resume_state.status,
            stop_reason=resume_state.stop_reason,
            state=resume_state,
            messages=resume_state.messages,
            final_text="resumed",
        )

    agent.resume_after_approval = fake_resume

    async def run() -> None:
        try:
            await session.resolve_approval(
                ApprovalResolution(
                    approval_id="approval:test",
                    decisions=(ToolApprovalDecision(tool_call_id="call:test", confirmed=False),),
                )
            )
        finally:
            await session.aclose()

    asyncio.run(run())

    assert fake.calls == []


def test_plan_interaction_resume_does_not_auto_compact(tmp_path) -> None:
    runtime_wiring = build_in_memory_runtime_wiring(tmp_path)
    fake = _FakeHostCompactionService()
    runtime_wiring = replace(runtime_wiring, compaction_service=fake)
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_wiring.runtime_session,
        llm_runtime=_llm_runtime(CompactScriptedTransport("unused")),
    )
    session = HostSession(
        host_session_id="host:test",
        conversation_id="conversation:test",
        workspace=resolve_workspace(HostWorkspaceInput(workspace_root=tmp_path, workspace_kind="project")),
        wiring=AgentRuntimeWiring(agent_runtime=agent, runtime_wiring=runtime_wiring),
    )
    state = agent.new_state()
    state.status = LoopStatus.WAITING_USER
    state.pending_interaction_kind = "plan"
    state.pending_interaction_payload = {"interaction_id": "plan:test", "kind": "question", "tool_call_id": "call:plan"}
    pending = PendingPlanInteraction(
        interaction_id="plan:test",
        kind="question",
        host_session_id=session.host_session_id,
        runtime_session_id=session.runtime_session_id,
        run_id=state.run_id,
        turn_id=state.turn_id,
        reply_id=state.reply_id,
        tool_call_id="call:plan",
        question="choose",
    )
    session.pending_interaction = pending
    session._suspended_state = state
    session.suspended_run_id = state.run_id

    async def fake_resume(resume_state, resolution):
        resume_state.status = LoopStatus.FINISHED
        resume_state.stop_reason = "final"
        return AgentRunResult(
            status=resume_state.status,
            stop_reason=resume_state.stop_reason,
            state=resume_state,
            messages=resume_state.messages,
            final_text="resumed",
        )

    agent.resume_after_plan_interaction = fake_resume

    async def run() -> None:
        try:
            await session.resolve_plan_interaction(PlanQuestionResolution(interaction_id="plan:test", answer_text="A"))
        finally:
            await session.aclose()

    asyncio.run(run())

    assert fake.calls == []


def test_mcp_elicitation_resume_does_not_auto_compact(tmp_path) -> None:
    runtime_wiring = build_in_memory_runtime_wiring(tmp_path)
    fake = _FakeHostCompactionService()
    runtime_wiring = replace(runtime_wiring, compaction_service=fake)
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_wiring.runtime_session,
        llm_runtime=_llm_runtime(CompactScriptedTransport("unused")),
    )
    session = HostSession(
        host_session_id="host:test",
        conversation_id="conversation:test",
        workspace=resolve_workspace(HostWorkspaceInput(workspace_root=tmp_path, workspace_kind="project")),
        wiring=AgentRuntimeWiring(agent_runtime=agent, runtime_wiring=runtime_wiring),
    )
    state = agent.new_state()
    state.status = LoopStatus.WAITING_USER
    state.pending_interaction_kind = "mcp_elicitation"
    state.pending_interaction_payload = {
        "interaction_id": "mcp:test",
        "tool_call_id": "call:mcp",
        "tool_name": "mcp__docs__lookup",
        "server_id": "docs",
        "request_id": "request:test",
    }
    pending = PendingMcpElicitation(
        interaction_id="mcp:test",
        kind="mcp_elicitation",
        host_session_id=session.host_session_id,
        runtime_session_id=session.runtime_session_id,
        run_id=state.run_id,
        turn_id=state.turn_id,
        reply_id=state.reply_id,
        tool_call_id="call:mcp",
        tool_name="mcp__docs__lookup",
        server_id="docs",
        request_id="request:test",
        prompt="secret?",
    )
    session.pending_interaction = pending
    session._suspended_state = state
    session.suspended_run_id = state.run_id

    async def fake_resume(resume_state, resolution):
        resume_state.status = LoopStatus.FINISHED
        resume_state.stop_reason = "final"
        return AgentRunResult(
            status=resume_state.status,
            stop_reason=resume_state.stop_reason,
            state=resume_state,
            messages=resume_state.messages,
            final_text="resumed",
        )

    agent.resume_after_mcp_elicitation = fake_resume

    async def run() -> None:
        try:
            await session.resolve_mcp_elicitation(
                McpElicitationResolution(interaction_id="mcp:test", answer={"value": "secret"})
            )
        finally:
            await session.aclose()

    asyncio.run(run())

    assert fake.calls == []
