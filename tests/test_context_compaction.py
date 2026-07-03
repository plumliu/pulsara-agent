import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import AsyncIterator

from pulsara_agent.event import (
    ContextCompactionCompletedEvent,
    ContextCompactionFailedEvent,
    ContextCompactionStartedEvent,
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
)
from pulsara_agent.event_log import InMemoryEventLog, dump_agent_event, load_agent_event
from pulsara_agent.host import HostSession, HostWorkspaceInput, resolve_workspace
from pulsara_agent.host.transcript import rebuild_prior_messages
from pulsara_agent.llm import LLMConfig, LLMRuntime, ModelProfile
from pulsara_agent.llm.registry import LLMTransportRegistry
from pulsara_agent.llm.request import LLMContext, LLMOptions
from pulsara_agent.memory.artifacts.archive import InMemoryArchiveStore
from pulsara_agent.capability.runtime import CapabilityRuntime
from pulsara_agent.runtime.agent import AgentRuntime
from pulsara_agent.runtime.compaction.planner import strip_compaction_analysis
from pulsara_agent.runtime.compaction.service import (
    ContextCompactionPolicy,
    ContextCompactionService,
    _events_text_for_estimate,
    estimate_compaction_window_tokens,
    estimate_context_tokens,
)
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
            RunStartEvent(**ctx.event_fields(), user_input_chars=len(user_input), metadata={"user_input": user_input}),
            ReplyStartEvent(**ctx.event_fields(), name="assistant"),
            TextBlockStartEvent(**ctx.event_fields(), block_id=f"text:{label}"),
            TextBlockDeltaEvent(**ctx.event_fields(), block_id=f"text:{label}", delta=assistant_text),
            TextBlockEndEvent(**ctx.event_fields(), block_id=f"text:{label}"),
            ReplyEndEvent(**ctx.event_fields()),
        ]
    )


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


def test_strip_compaction_analysis_keeps_summary_only() -> None:
    raw = "<analysis>private checklist</analysis>\n<summary>\nUseful handoff.\n</summary>"

    assert strip_compaction_analysis(raw) == "Useful handoff."


def test_strip_compaction_analysis_rejects_unclosed_private_blocks() -> None:
    assert strip_compaction_analysis("<analysis>private checklist with no close") == ""
    assert strip_compaction_analysis("<summary>official handoff with no close") == ""


def test_compaction_estimate_treats_event_log_as_token_dense_with_margin() -> None:
    log = InMemoryEventLog()
    _append_turn(log, "dense", "plain user input", "assistant reply")
    events = log.iter()
    event_text = _events_text_for_estimate(events)
    policy = ContextCompactionPolicy(
        chars_per_token=4.0,
        event_chars_per_token=2.0,
        estimate_safety_margin=1.25,
    )

    plain_estimate = estimate_context_tokens(event_text, chars_per_token=policy.chars_per_token)
    conservative_estimate = estimate_compaction_window_tokens(events, policy=policy)

    assert conservative_estimate > plain_estimate
    assert conservative_estimate >= int(plain_estimate * 2 * policy.estimate_safety_margin * 0.9)


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
    assert asyncio.run(service.compact_if_needed(reason="run_end_context_threshold")) is False
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


def test_host_session_invokes_compaction_at_preflight_and_run_end_safe_points(tmp_path) -> None:
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
            await session._drain_auto_compaction()
            return fake.calls
        finally:
            await session.aclose()

    calls = asyncio.run(run())

    assert {
        "method": "compact_if_needed",
        "current_user_input": "hello compaction",
        "reason": "preflight_context_threshold",
    } in calls
    assert {"method": "compact_if_needed", "reason": "run_end_context_threshold"} in calls


def test_host_session_notifies_auto_compaction_failure(tmp_path) -> None:
    runtime_wiring = build_in_memory_runtime_wiring(tmp_path)
    fake = _FakeFailingAutoCompactionService(runtime_wiring.event_log)
    session = HostSession(
        host_session_id="host:test",
        conversation_id="conversation:test",
        workspace=resolve_workspace(HostWorkspaceInput(workspace_root=tmp_path, workspace_kind="project")),
        wiring=AgentRuntimeWiring(
            agent_runtime=AgentRuntime(capability_runtime=CapabilityRuntime(), 
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
            await session._compact_if_needed_and_notify(fake, reason="run_end_context_threshold")
        finally:
            await session.aclose()

    asyncio.run(run())

    assert len(observed) == 1
    assert isinstance(observed[0], ContextCompactionFailedEvent)
    assert observed[0].compaction_id == "context_compaction:failed"
