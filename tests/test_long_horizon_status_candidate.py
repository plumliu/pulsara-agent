from __future__ import annotations

from types import SimpleNamespace

from pulsara_agent.event import (
    CapabilityGateDecisionEvent,
    CustomEvent,
    EventContext,
    RolloutBudgetAccountOpenedEvent,
    RolloutBudgetReservationCreatedEvent,
    RolloutBudgetReservationSettledEvent,
    RolloutPhaseTransitionedEvent,
    ToolResultEndEvent,
    ToolResultTextDeltaEvent,
)
from pulsara_agent.llm.input import LLMMessage, MessageRole
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.long_horizon import (
    LongHorizonActionClass,
    RolloutBudgetAccountFact,
    RolloutBudgetBucket,
    RolloutPhase,
    RolloutReservationFact,
    RolloutBudgetStateFact,
    RolloutTransitionReason,
    default_rollout_budget_policy,
    default_rollout_status_hint_policy,
)
from pulsara_agent.runtime.context_engine.types import AllocatedContextSection
from pulsara_agent.runtime.context_input.candidate import _source_spec
from pulsara_agent.runtime.context_input.compiler import _lower_messages
from pulsara_agent.runtime.context_input.event_slice import (
    ContextEventSlice,
    FrozenStoredEvent,
)
from pulsara_agent.runtime.long_horizon.rollout import (
    initial_rollout_budget_state,
    rollout_state_with_phase,
)
from pulsara_agent.runtime.long_horizon.status import (
    derive_rollout_status_candidate,
    render_rollout_status_candidate,
    rollout_status_candidate_is_required,
)
from pulsara_agent.runtime.tool_action import (
    default_tool_action_classifier_registry,
    fixed_tool_action_policy,
)
from pulsara_agent.tools import ToolCall
from tests.conftest import tool_result_end_contract_fields


CTX = EventContext(
    run_id="run:status-candidate",
    turn_id="turn:status-candidate",
    reply_id="reply:status-candidate",
)
RUNTIME_SESSION_ID = "runtime:status-candidate"


def test_exploration_without_recurrence_has_no_model_visible_candidate() -> None:
    account = _account()
    event_slice = _slice([_account_open(account, sequence=1)])

    assert (
        derive_rollout_status_candidate(
            event_slice=event_slice,
            account_id=account.account_id,
            policy=default_rollout_status_hint_policy(),
        )
        is None
    )


def test_non_exploration_candidate_is_required_neutral_and_checkpoint_stable() -> None:
    account = _account()
    opening = _account_open(account, sequence=1)
    state = initial_rollout_budget_state(account=account, through_sequence=1)
    warning_state = rollout_state_with_phase(
        state,
        phase=RolloutPhase.WARNING,
        through_sequence=2,
    )
    transition = RolloutPhaseTransitionedEvent(
        **CTX.event_fields(),
        id="rollout-phase:warning",
        sequence=2,
        account_id=account.account_id,
        from_phase=RolloutPhase.EXPLORATION,
        to_phase=RolloutPhase.WARNING,
        source_through_sequence=state.through_sequence,
        state_before_fingerprint=state.state_fingerprint,
        state_after_fingerprint=warning_state.state_fingerprint,
        reason_code=RolloutTransitionReason.WEIGHTED_TOKEN_THRESHOLD,
    )
    baseline = derive_rollout_status_candidate(
        event_slice=_slice([opening, transition]),
        account_id=account.account_id,
        policy=default_rollout_status_hint_policy(),
    )
    assert baseline is not None
    assert rollout_status_candidate_is_required(baseline) is True
    assert baseline.rollout_phase is RolloutPhase.WARNING

    rendered = render_rollout_status_candidate(baseline)
    lowered = rendered.lower()
    assert "phase=warning" in rendered
    assert "settled_model_calls=0" in rendered
    assert "should" not in lowered
    assert "next step" not in lowered
    assert "continue working" not in lowered
    assert "stop working" not in lowered

    checkpoint = CustomEvent(
        **CTX.event_fields(),
        id="projection-checkpoint:unrelated",
        sequence=3,
        name="projection_checkpoint_written",
        value={"checkpoint_id": "checkpoint:unrelated"},
    )
    after_checkpoint = derive_rollout_status_candidate(
        event_slice=_slice([opening, transition, checkpoint]),
        account_id=account.account_id,
        policy=default_rollout_status_hint_policy(),
    )
    assert after_checkpoint is not None
    assert after_checkpoint.semantic_fingerprint == baseline.semantic_fingerprint
    assert after_checkpoint.source_event_refs == baseline.source_event_refs


def test_exact_recurrence_in_exploration_is_optional_and_uses_shadow_algorithm() -> None:
    account = _account()
    events: list = [_account_open(account, sequence=1)]
    for index in range(3):
        _append_settled_tool(events, account=account, index=index)

    candidate = derive_rollout_status_candidate(
        event_slice=_slice(events),
        account_id=account.account_id,
        policy=default_rollout_status_hint_policy(),
    )

    assert candidate is not None
    assert candidate.rollout_phase is RolloutPhase.EXPLORATION
    assert rollout_status_candidate_is_required(candidate) is False
    assert len(candidate.recurrence) == 1
    assert candidate.recurrence[0].action_occurrence_count == 3
    assert candidate.recurrence[0].equivalent_terminal_outcome_count == 3


def test_trailing_status_lowers_after_current_run_tail_as_runtime_observation() -> None:
    segmented = SimpleNamespace(
        prior_history_messages=(LLMMessage.user("prior"),),
        current_user_messages=(LLMMessage.user("current user"),),
        current_run_tail_messages=(LLMMessage.assistant("current run tail"),),
    )
    sections = (
        _section("transcript:prior_history", "history"),
        _section("transcript:current_user", "current_user"),
        _section("transcript:current_run_tail", "current_run_tail"),
        _section(
            "rollout:status",
            "trailing_status",
            text="[rollout status]\nphase=restricted",
        ),
    )

    messages, scopes = _lower_messages(segmented, sections=sections)

    assert tuple(message.role for message in messages) == (
        MessageRole.USER,
        MessageRole.USER,
        MessageRole.ASSISTANT,
        MessageRole.RUNTIME_OBSERVATION,
    )
    assert messages[-1].content == ("[rollout status]\nphase=restricted",)
    assert scopes == ("transcript", "transcript", "transcript", "non_transcript")
    spec = _source_spec("rollout:status")
    assert spec[0] == "rollout_status"
    assert spec[1].value == "trailing_status"
    assert spec[5] == "trailing_status"


def _account() -> RolloutBudgetAccountFact:
    policy = default_rollout_budget_policy()
    payload = {
        "account_id": "rollout:status-candidate",
        "owner_runtime_session_id": RUNTIME_SESSION_ID,
        "root_run_id": CTX.run_id,
        "policy": policy,
        "total_budget_milliunits": 100_000_000,
        "finalization_reserve_milliunits": 30_000_000,
        "finalization_agent_reserve_milliunits": 10_000_000,
        "finalization_compaction_reserve_milliunits": 10_000_000,
        "finalization_tool_reserve_milliunits": 10_000_000,
        "exploration_allowance_milliunits": 70_000_000,
    }
    return RolloutBudgetAccountFact(
        **payload,
        semantic_fingerprint=context_fingerprint(
            "rollout-budget-account:v1", payload
        ),
    )


def _account_open(
    account: RolloutBudgetAccountFact, *, sequence: int
) -> RolloutBudgetAccountOpenedEvent:
    return RolloutBudgetAccountOpenedEvent(
        **CTX.event_fields(),
        id="rollout-account-opened:status-candidate",
        sequence=sequence,
        account=account,
    )


def _state_at_sequence(
    state: RolloutBudgetStateFact, *, sequence: int
) -> RolloutBudgetStateFact:
    payload = state.model_dump(mode="python", exclude={"state_fingerprint"})
    payload["through_sequence"] = sequence
    return RolloutBudgetStateFact(
        **payload,
        state_fingerprint=context_fingerprint("rollout-budget-state:v1", payload),
    )


def _append_settled_tool(
    events: list,
    *,
    account: RolloutBudgetAccountFact,
    index: int,
) -> None:
    call_id = f"call:repeated:{index}"
    policy = fixed_tool_action_policy(LongHorizonActionClass.EVIDENCE_ACQUISITION)
    classification = default_tool_action_classifier_registry().classify(
        call=ToolCall(id=call_id, name="search_files", arguments={"query": "same"}),
        descriptor_id="descriptor:search_files",
        descriptor_fingerprint="descriptor-fingerprint:search_files",
        policy=policy,
    )
    reserved = account.policy.tool_cost_unit_weight_milli
    reservation_payload = {
        "reservation_id": f"reservation:{call_id}",
        "account_id": account.account_id,
        "owner_kind": "tool_call",
        "owner_id": call_id,
        "phase_at_reservation": RolloutPhase.EXPLORATION,
        "budget_bucket": RolloutBudgetBucket.EXPLORATION,
        "reserved_milliunits": reserved,
        "model_call_reservation_quote": None,
        "source_sequence": len(events),
    }
    reservation = RolloutReservationFact(
        **reservation_payload,
        semantic_fingerprint=context_fingerprint(
            "rollout-reservation:v1", reservation_payload
        ),
    )
    events.append(
        RolloutBudgetReservationCreatedEvent(
            **CTX.event_fields(),
            sequence=len(events) + 1,
            reservation=reservation,
        )
    )
    events.append(
        CapabilityGateDecisionEvent(
            **CTX.event_fields(),
            sequence=len(events) + 1,
            tool_call_id=call_id,
            tool_name="search_files",
            descriptor_id="descriptor:search_files",
            decision="allow",
            action_classification=classification,
        )
    )
    events.append(
        ToolResultTextDeltaEvent(
            **CTX.event_fields(),
            sequence=len(events) + 1,
            tool_call_id=call_id,
            delta="same-result",
        )
    )
    terminal = ToolResultEndEvent(
        **CTX.event_fields(),
        id=f"tool-result-end:{call_id}",
        sequence=len(events) + 1,
        created_at=f"2026-01-01T00:00:0{index + 1}Z",
        tool_call_id=call_id,
        state="success",
        **tool_result_end_contract_fields(
            call_id,
            tool_name="search_files",
            observed_at_utc=f"2026-01-01T00:00:0{index + 1}Z",
        ),
    )
    events.append(terminal)
    events.append(
        RolloutBudgetReservationSettledEvent(
            **CTX.event_fields(),
            sequence=len(events) + 1,
            reservation_id=reservation.reservation_id,
            charged_milliunits=reserved,
            usage_status="tool_terminal",
            usage_charge=None,
            source_model_call_end_event_id=None,
            source_tool_result_event_id=terminal.id,
            child_usage_handoff=None,
        )
    )


def _slice(events: list) -> ContextEventSlice:
    frozen = tuple(
        FrozenStoredEvent.from_stored_event(
            event,
            runtime_session_id=RUNTIME_SESSION_ID,
        )
        for event in events
    )
    return ContextEventSlice(
        runtime_session_id=RUNTIME_SESSION_ID,
        from_sequence=1,
        through_sequence=len(frozen),
        events=frozen,
        event_ids_fingerprint=context_fingerprint(
            "context-event-slice-ids:v1",
            tuple(event.event_id for event in frozen),
        ),
        event_payloads_fingerprint=context_fingerprint(
            "context-event-slice-payloads:v1",
            tuple(event.payload_fingerprint for event in frozen),
        ),
    )


def _section(
    section_id: str,
    channel: str,
    *,
    text: str = "",
) -> AllocatedContextSection:
    return AllocatedContextSection(
        id=section_id,
        source_id=section_id,
        channel=channel,
        priority=90,
        stability="step",
        budget_class="important",
        text=text,
        metadata={"lowering_kind": channel},
    )
