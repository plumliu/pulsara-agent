from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from tests.conftest import (
    persist_test_run_transcript_seed,
    run_end_contract_fields,
    run_start_permission_fields,
    tool_result_end_candidate,
)
from tests.support import model_call_end_fields, model_call_start_fields
from tests.support.context_input import render_event_log_transcript

from pulsara_agent.event import (
    ContextCompactionCompletedEvent,
    ContextWindowClosedEvent,
    ContextWindowOpenedEvent,
    EventContext,
    ModelCallControlDispositionResolvedEvent,
    ModelCallEndEvent,
    ModelCallStartEvent,
    ReplyEndEvent,
    ReplyStartEvent,
    RunEndEvent,
    RunStartEvent,
    RolloutBudgetAccountOpenedEvent,
    RolloutBudgetAccountClosedEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
)
from pulsara_agent.event_log import PostgresEventLog
from pulsara_agent.host.transcript import rebuild_prior_messages
from pulsara_agent.llm import ModelRole, build_llm_runtime
from pulsara_agent.llm.result import TransportUsageReport
from pulsara_agent.llm.terminal_projection import (
    ModelTerminalProjectionReducer,
    bind_model_terminal_projection_to_session,
    persist_model_terminal_projection,
)
from pulsara_agent.memory.artifacts.postgres_archive import PostgresArtifactStore
from pulsara_agent.message import (
    AssistantMsg,
    Msg,
    TextBlock,
    ToolCallBlock,
    ToolCallState,
    ToolResultBlock,
    ToolResultState,
    UserMsg,
)
from pulsara_agent.runtime.compaction.inline import RuntimeContextCompactor
from pulsara_agent.runtime.compaction.commit import (
    RuntimeSessionCompactionEventCommitPort,
)
from pulsara_agent.runtime.compaction.service import (
    ContextCompactionPolicy,
    ContextCompactionService,
)
from pulsara_agent.runtime.session import RuntimeSession
from pulsara_agent.runtime.state import LoopBudget, LoopState, LoopTransition
from pulsara_agent.runtime.tool_artifacts import PostgresToolResultArtifactIndex
from pulsara_agent.primitives.model_call import (
    ModelCallControlDisposition,
    ModelStreamSemanticAttributionFact,
    sha256_fingerprint,
)
from pulsara_agent.runtime.long_horizon.run_contract import (
    empty_projection_state_fingerprint,
    prepare_root_long_horizon_run,
)
from pulsara_agent.runtime.long_horizon.rollout import apply_rollout_event
from pulsara_agent.settings import PulsaraSettings


pytestmark = pytest.mark.real_llm


class _CapturingLLMRuntime:
    def __init__(self, inner) -> None:
        self.inner = inner
        self.contexts = []
        self.raw_parts: list[str] = []
        self.model_ends: list[ModelCallEndEvent] = []

        self.capture_tasks: list[asyncio.Task[None]] = []

    def start_stream(self, **kwargs):
        self.contexts.append(kwargs.get("context"))
        handle = self.inner.start_stream(**kwargs)

        async def capture_committed() -> None:
            completion = await handle.wait_completed()
            for event in completion.committed_events:
                if isinstance(event, TextBlockDeltaEvent):
                    self.raw_parts.append(event.delta)
                elif isinstance(event, ModelCallEndEvent):
                    self.model_ends.append(event)

        self.capture_tasks.append(asyncio.create_task(capture_committed()))
        return handle

    def resolve_target(self, **kwargs):
        return self.inner.resolve_target(**kwargs)

    def resolve_call(self, **kwargs):
        return self.inner.resolve_call(**kwargs)


async def _compact_with_empty_summary_retry(
    service: ContextCompactionService,
    **kwargs,
) -> ContextCompactionCompletedEvent | None:
    for attempt in range(2):
        try:
            return await service.compact(**kwargs)
        except RuntimeError as exc:
            if str(exc) != "compact model returned an empty summary" or attempt == 1:
                raise
    raise AssertionError("unreachable")


def test_real_llm_context_compaction_summary_and_resume(tmp_path: Path) -> None:
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip(
            "Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider."
        )
    if os.getenv("PULSARA_RUN_DOGFOOD_COMPACTION") != "1":
        pytest.skip(
            "Set PULSARA_RUN_DOGFOOD_COMPACTION=1 to run context compaction dogfood."
        )
    settings = _load_settings()
    _connect_or_skip(settings.storage.postgres_dsn).close()

    runtime_session_id = f"runtime:real-compaction:{uuid4().hex}"
    try:
        log = PostgresEventLog(
            dsn=settings.storage.postgres_dsn,
            runtime_session_id=runtime_session_id,
            workspace_root=tmp_path,
        )
        archive = PostgresArtifactStore(settings.storage.postgres_dsn)
        runtime_session = RuntimeSession(
            tmp_path,
            runtime_session_id=runtime_session_id,
            event_log=log,
            archive=archive,
            tool_result_artifacts=PostgresToolResultArtifactIndex(
                settings.storage.postgres_dsn
            ),
        )
        _append_turn(
            runtime_session,
            "requirements",
            (
                "We are implementing Pulsara context compaction. Remember these exact constraints: "
                "auto compact triggers at 200k estimated tokens, context limit is 256k, and manual "
                "compact via :compact may happen before the threshold. The compact summary artifact "
                "must carry do_not_write_back=true."
            ),
            (
                "Acknowledged. I will keep event log as canonical truth, write summaries as artifacts, "
                "and use run-start preflight plus run-end safe points."
            ),
        )
        _append_turn(
            runtime_session,
            "implementation",
            (
                "Implementation detail: compaction summary must be do_not_write_back and must distinguish "
                "memory recall projection from direct user messages."
            ),
            (
                "Implemented typed events, summary artifact metadata, transcript rehydration, and inspector diagnostics."
            ),
        )
        service = ContextCompactionService(
            event_log=log,
            archive=archive,
            llm_runtime=build_llm_runtime(settings.llm),
            runtime_session_id=runtime_session_id,
            runtime_session=runtime_session,
            event_commit_port=RuntimeSessionCompactionEventCommitPort(
                runtime_session
            ),
            policy=ContextCompactionPolicy(
                min_events_after_last_compact=1,
                keep_recent_runs=1,
                max_summary_chars=8_000,
            ),
        )

        target = service.llm_runtime.resolve_target(role=ModelRole.PRO)
        completed = asyncio.run(
            _compact_with_empty_summary_retry(
                service,
                target_model_target=target,
                trigger="manual",
                reason="dogfood_manual_compact",
                force=True,
            )
        )

        assert completed is not None
        summary = archive.get_text(
            completed.summary_artifact_id, session_id=runtime_session_id
        )
        summary_lower = summary.casefold()
        assert "200k" in summary_lower or "200,000" in summary_lower
        assert "256k" in summary_lower or "256,000" in summary_lower
        assert "do_not_write_back" in summary_lower or "write back" in summary_lower
        assert "artifact" in summary_lower

        resumed_log = PostgresEventLog(
            dsn=settings.storage.postgres_dsn,
            runtime_session_id=runtime_session_id,
            workspace_root=tmp_path,
        )
        prior_messages = rebuild_prior_messages(
            resumed_log,
            archive=archive,
            session_id=runtime_session_id,
        )
        rendered = "\n".join(
            block.text
            for message in prior_messages
            for block in message.content
            if hasattr(block, "text")
        )
        assert "<context-compaction-summary" in rendered
        assert "do_not_write_back" in rendered
        assert "implementation detail" in rendered.casefold()
        assert any(
            isinstance(event, ContextCompactionCompletedEvent)
            for event in resumed_log.iter()
        )
    finally:
        if "runtime_session" in locals():
            runtime_session.close()
        _cleanup_session(settings.storage.postgres_dsn, runtime_session_id)


def test_real_llm_long_pr4_style_dogfood_manual_compaction(tmp_path: Path) -> None:
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip(
            "Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider."
        )
    if os.getenv("PULSARA_RUN_DOGFOOD_COMPACTION_LONG") != "1":
        pytest.skip(
            "Set PULSARA_RUN_DOGFOOD_COMPACTION_LONG=1 to run the long compaction dogfood."
        )
    settings = _load_settings()
    _connect_or_skip(settings.storage.postgres_dsn).close()

    runtime_session_id = f"runtime:real-compaction-long:{uuid4().hex}"
    try:
        log = PostgresEventLog(
            dsn=settings.storage.postgres_dsn,
            runtime_session_id=runtime_session_id,
            workspace_root=tmp_path,
        )
        archive = PostgresArtifactStore(settings.storage.postgres_dsn)
        runtime_session = RuntimeSession(
            tmp_path,
            runtime_session_id=runtime_session_id,
            event_log=log,
            archive=archive,
            tool_result_artifacts=PostgresToolResultArtifactIndex(
                settings.storage.postgres_dsn
            ),
        )
        _append_pr4_style_long_dogfood_transcript(runtime_session)
        capture = _CapturingLLMRuntime(build_llm_runtime(settings.llm))
        service = ContextCompactionService(
            event_log=log,
            archive=archive,
            llm_runtime=capture,
            runtime_session_id=runtime_session_id,
            runtime_session=runtime_session,
            event_commit_port=RuntimeSessionCompactionEventCommitPort(
                runtime_session
            ),
            policy=ContextCompactionPolicy(
                min_events_after_last_compact=1,
                keep_recent_runs=2,
                max_summary_chars=12_000,
            ),
        )

        target = service.llm_runtime.resolve_target(role=ModelRole.PRO)
        completed = asyncio.run(
            _compact_with_empty_summary_retry(
                service,
                target_model_target=target,
                trigger="manual",
                reason="long_pr4_dogfood_manual_compact",
                force=True,
            )
        )

        assert completed is not None
        summary = archive.get_text(
            completed.summary_artifact_id, session_id=runtime_session_id
        )
        summary_lower = summary.casefold()
        assert "pr4" in summary_lower
        assert "skill" in summary_lower
        assert "controlled" in summary_lower or "provider failure" in summary_lower
        assert (
            completed.target_estimate.estimated_tokens_after
            < completed.target_estimate.estimated_tokens_before
        )

        compact_context = capture.contexts[0]
        compact_user_input = compact_context.messages[1].content[0]
        raw_output = "".join(capture.raw_parts)
        assert "long_round1_orientation" in compact_user_input
        # keep_recent_runs=2 intentionally leaves the final inspection round in
        # the recent tail rather than asking the compact summary to duplicate it.
        assert "long_round10_final_inspect" not in compact_user_input
        assert "<analysis>" in raw_output.casefold()
        assert "<analysis>" not in summary.casefold()

        prior_messages = rebuild_prior_messages(
            PostgresEventLog(
                dsn=settings.storage.postgres_dsn,
                runtime_session_id=runtime_session_id,
                workspace_root=tmp_path,
            ),
            archive=archive,
            session_id=runtime_session_id,
        )
        rendered = "\n".join(
            block.text
            for message in prior_messages
            for block in message.content
            if hasattr(block, "text")
        )
        assert "<context-compaction-summary" in rendered
        assert "long_round10_final_inspect" in rendered

        usage = capture.model_ends[-1] if capture.model_ends else None
        print(
            "[long-compaction-dogfood] "
            f"before={completed.target_estimate.estimated_tokens_before} "
            f"after={completed.target_estimate.estimated_tokens_after} "
            f"summary_chars={completed.summary_chars} "
            f"input_chars={len(compact_user_input)} "
            f"raw_output_chars={len(raw_output)} "
            f"usage={usage.model_dump(mode='json') if usage else None}",
            flush=True,
        )
    finally:
        if "runtime_session" in locals():
            runtime_session.close()
        _cleanup_session(settings.storage.postgres_dsn, runtime_session_id)


def test_real_llm_mid_turn_inline_compaction_preserves_current_tail(
    tmp_path: Path,
) -> None:
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip(
            "Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider."
        )
    if os.getenv("PULSARA_RUN_DOGFOOD_COMPACTION_MID_TURN") != "1":
        pytest.skip(
            "Set PULSARA_RUN_DOGFOOD_COMPACTION_MID_TURN=1 to run mid-turn compaction dogfood."
        )
    settings = _load_settings()
    _connect_or_skip(settings.storage.postgres_dsn).close()

    runtime_session_id = f"runtime:real-compaction-midturn:{uuid4().hex}"
    try:
        log = PostgresEventLog(
            dsn=settings.storage.postgres_dsn,
            runtime_session_id=runtime_session_id,
            workspace_root=tmp_path,
        )
        archive = PostgresArtifactStore(settings.storage.postgres_dsn)
        runtime_session = RuntimeSession(
            tmp_path,
            runtime_session_id=runtime_session_id,
            event_log=log,
            archive=archive,
            tool_result_artifacts=PostgresToolResultArtifactIndex(
                settings.storage.postgres_dsn
            ),
        )
        _append_turn(
            runtime_session,
            "midturn-old-a",
            "Historical requirement A: mid-turn compaction may summarize old prefix facts.",
            "Acknowledged historical requirement A. "
            + ("Historical detail remains part of requirement A. " * 400),
        )
        _append_turn(
            runtime_session,
            "midturn-old-b",
            "Recent requirement B: keep the latest complete historical run outside the mid-turn summary.",
            "Acknowledged recent requirement B.",
        )
        state = _mid_turn_state(runtime_session_id)
        current_context = EventContext(
            run_id=state.run_id,
            turn_id=state.turn_id,
            reply_id=state.reply_id,
        )
        current_opened = asyncio.run(
            runtime_session.emit_many(
                _root_run_open_events(
                    runtime_session,
                    ctx=current_context,
                    user_input="Current request: inspect the just-finished tool output.",
                ),
                state=state,
            )
        )
        current_start = current_opened[0]
        capture = _CapturingLLMRuntime(build_llm_runtime(settings.llm))
        service = ContextCompactionService(
            event_log=log,
            archive=archive,
            llm_runtime=capture,
            runtime_session_id=runtime_session_id,
            runtime_session=runtime_session,
            event_commit_port=RuntimeSessionCompactionEventCommitPort(
                runtime_session
            ),
            policy=ContextCompactionPolicy(
                min_events_after_last_compact=1,
                auto_trigger_ratio=0.006,
                post_compaction_target_ratio=0.005,
                # This dogfood validates inline boundary preservation, not
                # the summary-length rejection path.  Real summarizers can
                # legitimately need more than 1k characters to preserve
                # the seeded historical/tool facts.
                max_summary_chars=4_000,
            ),
        )
        state.run_model_target = capture.resolve_target(role=ModelRole.PRO)
        asyncio.run(
            _append_accepted_reply(
                runtime_session,
                current_context,
                (
                    ToolCallStartEvent(
                        run_id=state.run_id,
                        turn_id=state.turn_id,
                        reply_id=state.reply_id,
                        tool_call_id="call:current",
                        tool_call_name="terminal",
                    ),
                    ToolCallDeltaEvent(
                        run_id=state.run_id,
                        turn_id=state.turn_id,
                        reply_id=state.reply_id,
                        tool_call_id="call:current",
                        delta='{"command":"printf current"}',
                    ),
                    ToolCallEndEvent(
                        run_id=state.run_id,
                        turn_id=state.turn_id,
                        reply_id=state.reply_id,
                        tool_call_id="call:current",
                    ),
                ),
            )
        )
        async def append_current_tool_result() -> None:
            await runtime_session.emit_many(
                (
                    ToolResultStartEvent(
                        run_id=state.run_id,
                        turn_id=state.turn_id,
                        reply_id=state.reply_id,
                        tool_call_id="call:current",
                        tool_call_name="terminal",
                    ),
                    ToolResultTextDeltaEvent(
                        run_id=state.run_id,
                        turn_id=state.turn_id,
                        reply_id=state.reply_id,
                        tool_call_id="call:current",
                        delta="current tool result",
                    ),
                )
            )
            candidate = tool_result_end_candidate(
                event_id=f"tool_result_end:{state.run_id}:call:current",
                run_id=state.run_id,
                turn_id=state.turn_id,
                reply_id=state.reply_id,
                tool_call_id="call:current",
                tool_name="terminal",
            )
            prepared = (
                await runtime_session.tool_terminal_projection_service.prepare_batch(
                    (candidate,)
                )
            )
            await runtime_session.emit_many(prepared)

        asyncio.run(append_current_tool_result())
        lowered = render_event_log_transcript(
            log,
            run_start_event_id=current_start.id,
            runtime_session_id=runtime_session_id,
            budget=LoopBudget(),
        ).lowered
        protected = (
            *lowered.current_user_messages,
            *lowered.current_run_tail_messages,
        )
        model_visible_messages = [
            message
            for message in rebuild_prior_messages(
                log,
                archive=archive,
                session_id=runtime_session_id,
            )
            if message.id != f"user-message:{state.run_id}"
        ]
        model_visible_messages.extend(state.messages)
        compactor = RuntimeContextCompactor(
            event_log=log,
            archive=archive,
            runtime_session=runtime_session,
            service=service,
        )

        result = asyncio.run(
            compactor.maybe_compact_before_followup(
                state=state,
                model_visible_messages=model_visible_messages,
                protected_model_visible_messages_after=protected,
            )
        )

        assert result.compacted is True
        assert current_start.sequence is not None
        completed = next(
            event
            for event in result.events
            if isinstance(event, ContextCompactionCompletedEvent)
        )
        summary = archive.get_text(
            completed.summary_artifact_id, session_id=runtime_session_id
        )
        compact_input = capture.contexts[0].messages[1].content[0]
        assert "Historical requirement A" in compact_input
        assert "Recent requirement B" not in compact_input
        assert "Current request" not in compact_input
        normalized_summary = summary.casefold()
        assert "mid-turn compaction" in normalized_summary
        assert "summarize old prefix" in normalized_summary
        assert result.rewritten_messages is not None
        rendered = "\n".join(
            block.text
            for message in result.rewritten_messages
            for block in message.content
            if hasattr(block, "text")
        )
        assert "Recent requirement B" in rendered
        assert "Current request" in rendered
        assert completed.metadata["phase"] == "mid_turn"
        assert (
            completed.metadata["current_run_start_sequence"] == current_start.sequence
        )
    finally:
        _cleanup_session(settings.storage.postgres_dsn, runtime_session_id)


def _root_run_open_events(
    runtime_session: RuntimeSession,
    *,
    ctx: EventContext,
    user_input: str,
) -> tuple[RunStartEvent, ContextWindowOpenedEvent, RolloutBudgetAccountOpenedEvent]:
    log = runtime_session.event_log
    if not isinstance(log, PostgresEventLog):
        raise TypeError("real compaction fixture requires PostgreSQL")
    log.ensure_runtime_session_owner()
    source_through_sequence = log.next_sequence() - 1
    seed = persist_test_run_transcript_seed(runtime_session, run_id=ctx.run_id)
    run_start = RunStartEvent(
        id=f"run_start:{ctx.run_id}",
        **ctx.event_fields(),
        **run_start_permission_fields(
            ctx.run_id,
            user_input=user_input,
            turn_id=ctx.turn_id,
            reply_id=ctx.reply_id,
            mcp_installation_owner_runtime_session_id=log.runtime_session_id,
            transcript_source_through_sequence=source_through_sequence,
        ),
        user_input_chars=len(user_input),
    )
    prepared = prepare_root_long_horizon_run(
        runtime_session_id=log.runtime_session_id,
        run_id=ctx.run_id,
        run_start_event_id=run_start.id,
        primary_target=run_start.model_target,
        summarizer_target=run_start.model_target,
        graph_reducer_contract=run_start.subagent_graph_reducer_contract,
        source_through_sequence_at_open=source_through_sequence,
        initial_projection_unit_count=0,
        initial_projection_state_fingerprint=empty_projection_state_fingerprint(),
    )
    run_start = run_start.model_copy(
        update={
            "long_horizon": prepared.contract,
            "run_transcript_seed_semantic": seed.seed_semantic,
            "run_transcript_seed_reference": seed.seed_reference,
        },
        deep=True,
    )
    account = prepared.root_account
    assert account is not None
    return (
        run_start,
        ContextWindowOpenedEvent(
            id=prepared.contract.initial_window_open_event_id,
            **ctx.event_fields(),
            window=prepared.initial_window,
            opening_batch_id=prepared.opening_batch_id,
        ),
        RolloutBudgetAccountOpenedEvent(
            id=f"rollout_budget_account_opened:{account.account_id}",
            **ctx.event_fields(),
            account=account,
        ),
    )


def _semantic_draft_kind(event: object) -> str:
    if isinstance(event, TextBlockStartEvent):
        return "text_block_start"
    if isinstance(event, TextBlockDeltaEvent):
        return "text_block_delta"
    if isinstance(event, TextBlockEndEvent):
        return "text_block_end"
    if isinstance(event, ToolCallStartEvent):
        return "tool_call_start"
    if isinstance(event, ToolCallDeltaEvent):
        return "tool_call_delta"
    if isinstance(event, ToolCallEndEvent):
        return "tool_call_end"
    raise TypeError(
        f"unsupported real compaction semantic event: {type(event).__name__}"
    )


def _bind_model_semantic_events(
    start: ModelCallStartEvent,
    semantic_events: tuple,
) -> tuple:
    bound = []
    for index, event in enumerate(semantic_events):
        draft_kind = _semantic_draft_kind(event)
        draft_fingerprint = sha256_fingerprint(
            "real-compaction-model-semantic-draft:v1",
            {
                "transport_sequence_index": index,
                "draft_kind": draft_kind,
                "event": event.model_dump(
                    mode="json",
                    exclude={"id", "sequence", "model_stream_attribution"},
                ),
            },
        )
        attribution_payload = {
            "schema_version": "model_stream_semantic_attribution.v1",
            "resolved_model_call_id": start.resolved_call.resolved_model_call_id,
            "model_call_start_event_id": start.id,
            "transport_sequence_index": index,
            "draft_schema_version": "provider_transport_semantic_draft.v1",
            "draft_kind": draft_kind,
            "draft_fingerprint": draft_fingerprint,
        }
        attribution = ModelStreamSemanticAttributionFact(
            **attribution_payload,
            attribution_fingerprint=sha256_fingerprint(
                "model-stream-semantic-attribution:v1",
                attribution_payload,
            ),
        )
        bound.append(
            event.model_copy(
                update={
                    "id": (
                        f"model_semantic:{start.resolved_call.resolved_model_call_id}:"
                        f"{index}:{draft_fingerprint[7:23]}"
                    ),
                    "model_stream_attribution": attribution,
                    "sequence": None,
                },
                deep=True,
            )
        )
    return tuple(bound)


def _accepted_disposition(
    ctx: EventContext,
    *,
    start: ModelCallStartEvent,
    end: ModelCallEndEvent,
) -> ModelCallControlDispositionResolvedEvent:
    disposition_fields = {
        "id": (
            f"model_call_control_disposition:{ctx.run_id}:"
            f"{start.resolved_call.resolved_model_call_id}:1"
        ),
        **ctx.event_fields(),
        "resolved_model_call_id": start.resolved_call.resolved_model_call_id,
        "model_call_start_event_id": start.id,
        "model_call_end_event_id": end.id,
        "model_call_index": 1,
        "source_result_fingerprint": "sha256:" + "e" * 64,
        "run_execution_activation": start.recovery_plan.run_execution_activation,
        "disposition": ModelCallControlDisposition.ACCEPTED,
        "termination_intent": None,
        "recovery_reason_code": None,
    }
    provisional = ModelCallControlDispositionResolvedEvent.model_construct(
        **disposition_fields,
        event_fingerprint="pending",
    )
    payload = provisional.model_dump(
        mode="json",
        exclude={"event_fingerprint", "sequence"},
    )
    return ModelCallControlDispositionResolvedEvent(
        **payload,
        event_fingerprint=sha256_fingerprint(
            "model-call-control-disposition-event:v1",
            payload,
        ),
    )


async def _append_accepted_reply(
    runtime_session: RuntimeSession,
    ctx: EventContext,
    semantic_events: tuple = (),
) -> tuple:
    start = ModelCallStartEvent(
        **ctx.event_fields(),
        **model_call_start_fields(),
    )
    opened = await runtime_session.emit_many(
        (
            ReplyStartEvent(
                id=start.recovery_plan.reply_start_event_id,
                **ctx.event_fields(),
                name="assistant",
            ),
            start,
        )
    )
    committed_start = next(
        event for event in opened if isinstance(event, ModelCallStartEvent)
    )
    bound_semantic = _bind_model_semantic_events(
        committed_start,
        semantic_events,
    )
    committed_semantic = (
        await runtime_session.emit_many(bound_semantic) if bound_semantic else ()
    )
    reducer = ModelTerminalProjectionReducer(
        runtime_session_id=runtime_session.runtime_session_id,
        start_event=committed_start,
        contracts=runtime_session.terminal_projection_contracts,
        model_stream_semantic_domain_contract_fingerprint=(
            runtime_session.authority_materialization_contracts.event_domain.contract.transcript_semantic_domain_contract_fingerprint
        ),
    )
    reducer.apply_committed(tuple(committed_semantic))
    projection = bind_model_terminal_projection_to_session(
        runtime_session,
        reducer.prepare_terminal(
            event_context=ctx,
            terminal_outcome="completed",
            usage_report=TransportUsageReport(usage_status="missing", usage=None),
        ),
    )
    await persist_model_terminal_projection(
        runtime_session,
        projection,
        run_id=ctx.run_id,
    )
    runtime_session.transcript_projection_document_registry.register(
        projection.projection_reference,
        projection.document,
    )
    end_fields = model_call_end_fields(
        resolved_call=committed_start.resolved_call
    )
    end_fields.update(
        {
            "reported_model_id": None,
            "usage_status": "missing",
            "usage": None,
            "terminal_projection": projection.end_reference,
        }
    )
    end = ModelCallEndEvent(
        id=committed_start.recovery_plan.stable_model_call_end_event_id,
        **ctx.event_fields(),
        **end_fields,
    )
    terminal = await runtime_session.emit_many(
        (
            projection.committed_event,
            end,
            ReplyEndEvent(
                id=committed_start.recovery_plan.stable_reply_end_event_id,
                **ctx.event_fields(),
                model_terminal_outcome="completed",
            ),
            _accepted_disposition(ctx, start=committed_start, end=end),
        )
    )
    return (*opened, *committed_semantic, *terminal)


async def _append_finished_run(
    runtime_session: RuntimeSession,
    ctx: EventContext,
) -> None:
    store = runtime_session.long_horizon_state_store
    window_state = store.window_state(ctx.run_id)
    assert window_state is not None and window_state.active_window_id is not None
    window = window_state.windows[window_state.active_window_id]
    projection_state = store.projection_state(window.window_id)
    assert projection_state is not None
    source_through_sequence = runtime_session.event_log.next_sequence() - 1
    run_end = RunEndEvent(
        **run_end_contract_fields(ctx.run_id, status="finished"),
        **ctx.event_fields(),
        status="finished",
        stop_reason="final",
    )
    window_close = ContextWindowClosedEvent(
        id=window.stable_close_event_id,
        **ctx.event_fields(),
        window_id=window.window_id,
        window_generation=window.generation,
        close_reason="run_finished",
        final_projection_generation=projection_state.projection_generation,
        final_projection_state_fingerprint=(
            projection_state.state_semantic_fingerprint
        ),
        source_through_sequence=source_through_sequence,
        next_window_id=None,
        compaction_terminal_event_id=None,
    )
    run_start = store.run_start(ctx.run_id)
    assert run_start is not None
    account = store.rollout_account(run_start.long_horizon.rollout_account_id)
    rollout_state = store.rollout_state(run_start.long_horizon.rollout_account_id)
    assert account is not None and rollout_state is not None
    assert not rollout_state.active_reservations
    _, state_before_close = apply_rollout_event(
        account=account,
        state=rollout_state,
        event=window_close.model_copy(
            update={"sequence": source_through_sequence + 1}
        ),
    )
    assert state_before_close is not None
    rollout_close = RolloutBudgetAccountClosedEvent(
        id=f"rollout_budget_account_closed:{account.account_id}",
        **ctx.event_fields(),
        account_id=account.account_id,
        final_state_fingerprint=state_before_close.state_fingerprint,
        charged_milliunits=state_before_close.charged_milliunits,
        model_call_count=state_before_close.model_call_count,
        tool_call_count=state_before_close.tool_call_count,
        active_reservation_count=0,
        run_end_event_id=run_end.id,
    )
    await runtime_session.emit_many((window_close, rollout_close, run_end))


def _append_turn(
    runtime_session: RuntimeSession,
    label: str,
    user_input: str,
    assistant_text: str,
) -> None:
    ctx = EventContext(
        run_id=f"run:real-compaction:{label}:{uuid4().hex}",
        turn_id=f"turn:real-compaction:{label}:{uuid4().hex}",
        reply_id=f"reply:real-compaction:{label}:{uuid4().hex}",
    )

    async def append() -> None:
        opened = await runtime_session.emit_many(
            _root_run_open_events(
                runtime_session,
                ctx=ctx,
                user_input=user_input,
            )
        )
        run_start = next(event for event in opened if isinstance(event, RunStartEvent))
        runtime_session.transcript_projection_checkpoint_service.adopt_committed_run_seed(
            run_start
        )
        await _append_accepted_reply(
            runtime_session,
            ctx,
            (
                TextBlockStartEvent(**ctx.event_fields(), block_id=f"text:{label}"),
                TextBlockDeltaEvent(
                    **ctx.event_fields(),
                    block_id=f"text:{label}",
                    delta=assistant_text,
                ),
                TextBlockEndEvent(**ctx.event_fields(), block_id=f"text:{label}"),
            ),
        )
        await _append_finished_run(runtime_session, ctx)

    asyncio.run(append())


def _mid_turn_state(runtime_session_id: str) -> LoopState:
    state = LoopState(
        session_id=runtime_session_id, run_id=f"run:midturn-current:{uuid4().hex}"
    )
    state.messages = [
        UserMsg(
            name="user",
            content="Current request: inspect the just-finished tool output.",
            id=f"user-message:{state.run_id}",
            metadata={"run_id": state.run_id},
        ),
        AssistantMsg(
            name="assistant",
            id=state.reply_id,
            content=[
                ToolCallBlock(
                    id="call:midturn",
                    name="terminal",
                    input='{"command":"printf CURRENT_TOOL_SENTINEL"}',
                    state=ToolCallState.FINISHED,
                )
            ],
        ),
        Msg(
            role="tool_result",
            name="terminal",
            id="tool-result-message:call:midturn",
            content=[
                ToolResultBlock(
                    id="call:midturn",
                    name="terminal",
                    state=ToolResultState.SUCCESS,
                    output=[TextBlock(text="CURRENT_TOOL_SENTINEL")],
                )
            ],
        ),
    ]
    state.pending_tool_calls = [
        ToolCallBlock(
            id="call:midturn",
            name="terminal",
            input='{"command":"printf CURRENT_TOOL_SENTINEL"}',
            state=ToolCallState.FINISHED,
        )
    ]
    state.tool_results = [
        ToolResultBlock(
            id="call:midturn",
            name="terminal",
            state=ToolResultState.SUCCESS,
            output=[TextBlock(text="CURRENT_TOOL_SENTINEL")],
        )
    ]
    state.transition(LoopTransition.CONTINUE_AFTER_TOOL)
    return state


def _append_pr4_style_long_dogfood_transcript(
    runtime_session: RuntimeSession,
) -> None:
    rounds = [
        (
            "long_round1_orientation",
            "Inspect this project by reading files, not by running commands.",
            (
                "Read pyproject.toml, src/pulsara_agent/runtime/agent.py, and host/session.py. "
                "Identified Pulsara as an agent runtime with HostSession, tool execution, approval gates, "
                "durable event logs, memory recall, and plan workflow support. "
            ),
        ),
        (
            "long_round2_create_skill",
            "Create a dogfood local skill named pulsara-long-dogfood-helper with a short SKILL.md.",
            (
                "Created a local skill under the workspace skill directory. The skill describes how to summarize "
                "Pulsara dogfood state and reminds the assistant to preserve approval and terminal lifecycle evidence. "
            ),
        ),
        (
            "long_round3_use_skill",
            "Use the new dogfood helper skill to summarize the project state.",
            (
                "Resolved the skill catalog, activated pulsara-long-dogfood-helper, and produced a concise project "
                "state summary covering agent runtime, HostCore, HostSession, approvals, and memory hooks. "
            ),
        ),
        (
            "long_round4_active_stop",
            "Start a longer inspection and then stop it while active.",
            (
                "Began a long read-only inspection, then received StopRequest. Finalized the active run with an "
                "auditable aborted status and preserved run-end recovery state for later continuation. "
            ),
        ),
        (
            "long_round5_continue_after_stop",
            "Continue after the stopped run and explain what was preserved.",
            (
                "Continued successfully after stop. Reported that prior context, event log continuity, and terminal "
                "ownership were preserved, while the aborted run was represented as runtime recovery state. "
            ),
        ),
        (
            "long_round6_stop_pending",
            "Trigger a safe terminal approval and then stop while approval is pending.",
            (
                "Produced a pending approval for a safe terminal command, then stopped the suspended run. The pending "
                "approval was cleared and the run ended with a stop reason rather than leaving a dangling interaction. "
            ),
        ),
        (
            "long_round7_deny_pending",
            "Trigger another safe terminal approval and deny it.",
            (
                "Produced a pending terminal approval, received denial, resumed the run, and reported that no command "
                "was executed. Permission denial was recorded as user-confirm-result state, not as tool success. "
            ),
        ),
        (
            "long_round8_controlled_failure_injected",
            "Inject a controlled provider failure into the dogfood trajectory.",
            (
                "A controlled provider failure was injected for dogfood purposes. The runtime recorded a failed run "
                "with error_message='controlled dogfood provider failure' and preserved diagnostics for inspection. "
            ),
        ),
        (
            "long_round9_failure_note",
            "Ask the assistant to explain the controlled failure and recovery posture.",
            (
                "Explained that the failure was a controlled dogfood provider failure, not a user preference or memory "
                "fact. Recommended inspecting event log diagnostics and continuing from canonical runtime events. "
            ),
        ),
        (
            "long_round10_final_inspect",
            "Run the final dogfood inspection and summarize whether the long session succeeded.",
            (
                "Final inspection confirmed registry visibility, skill catalog continuity, no pending approval, no "
                "pending interaction, preserved failure note, and stable HostSession runtime ownership. "
            ),
        ),
    ]
    for idx, (label, user_input, assistant_text) in enumerate(rounds, start=1):
        expanded_assistant = (
            assistant_text
            + (
                f" PR4 long dogfood evidence block {idx}: registry included write_file and terminal_process; "
                f"turn label {label}; workspace artifact checks and event replay diagnostics were preserved. "
                "This repeated diagnostic prose intentionally makes the transcript long enough to exercise real "
                "context compaction without changing the original PR4 long dogfood test. "
            )
            * 4
        )
        _append_turn(runtime_session, label, user_input, expanded_assistant)


def _load_settings() -> PulsaraSettings:
    env_file = Path(".env")
    return (
        PulsaraSettings.from_env_file(env_file)
        if env_file.exists()
        else PulsaraSettings.from_env()
    )


def _connect_or_skip(dsn: str):
    try:
        return psycopg.connect(dsn, connect_timeout=2)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres is not available at configured DSN: {exc}")


def _cleanup_session(dsn: str, runtime_session_id: str) -> None:
    with _connect_or_skip(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute("delete from sessions where id = %s", (runtime_session_id,))
