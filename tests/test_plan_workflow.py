from pulsara_agent.event import EventContext, PlanModeEnteredEvent, PlanModeExitedEvent
from pulsara_agent.event_log import InMemoryEventLog
from pulsara_agent.runtime import reduce_plan_workflow_state


def test_plan_workflow_reducer_tracks_active_and_exited_state() -> None:
    ctx = EventContext(run_id="run:plan", turn_id="turn:plan", reply_id="reply:plan")
    log = InMemoryEventLog()
    log.extend(
        [
            PlanModeEnteredEvent(
                **ctx.event_fields(),
                source="user",
                previous_permission_mode="bypass-permissions",
                previous_permission_policy={
                    "profile": "trusted_host",
                    "approval_policy": "never",
                    "terminal_access": "allow",
                },
                reason="plan first",
            ),
            PlanModeExitedEvent(
                **ctx.event_fields(),
                source="approved_exit_plan",
                exit_request_id="plan_exit:1",
                restored_permission_mode="bypass-permissions",
                restored_permission_policy={
                    "profile": "trusted_host",
                    "approval_policy": "never",
                    "terminal_access": "allow",
                },
                accepted_plan_summary="accepted summary",
                accepted_plan_artifact_id="artifact:plan",
            ),
        ]
    )

    state = reduce_plan_workflow_state(log.iter())

    assert state.active is False
    assert state.pre_plan_permission_mode is None
    assert state.latest_accepted_plan_summary == "accepted summary"
    assert state.latest_accepted_plan_artifact_id == "artifact:plan"


def test_plan_workflow_reducer_restores_active_state_after_enter() -> None:
    ctx = EventContext(run_id="run:plan", turn_id="turn:plan", reply_id="reply:plan")
    log = InMemoryEventLog()
    log.append(
        PlanModeEnteredEvent(
            **ctx.event_fields(),
            source="agent",
            previous_permission_mode=None,
            previous_permission_policy={
                "profile": "trusted_host",
                "approval_policy": "risky_only",
                "terminal_access": "ask",
            },
            reason="agent chose to plan",
        )
    )

    state = reduce_plan_workflow_state(log.iter())

    assert state.active is True
    assert state.entered_by == "agent"
    assert state.pre_plan_permission_mode is None
    assert state.pre_plan_permission_policy == {
        "profile": "trusted_host",
        "approval_policy": "risky_only",
        "terminal_access": "ask",
    }
