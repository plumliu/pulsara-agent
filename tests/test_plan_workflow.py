from pulsara_agent.event import EventContext, PlanModeEnteredEvent, PlanModeExitedEvent
from pulsara_agent.event_log import InMemoryEventLog
from pulsara_agent.runtime import reduce_plan_workflow_state
from tests.conftest import run_start_permission_fields


def test_plan_workflow_reducer_tracks_active_and_exited_state() -> None:
    ctx = EventContext(run_id="run:plan", turn_id="turn:plan", reply_id="reply:plan")
    accepted_artifact_id = "artifact:plan:run_plan:plan_exit_1:accepted"
    log = InMemoryEventLog()
    log.extend(
        [
            PlanModeEnteredEvent(
                **ctx.event_fields(),
                source="user",
                previous_permission_mode="bypass-permissions",
                previous_permission_policy=run_start_permission_fields("run:plan")["permission_policy"],
                reason="plan first",
            ),
            PlanModeExitedEvent(
                **ctx.event_fields(),
                source="approved_exit_plan",
                exit_request_id="plan_exit:1",
                restored_permission_mode="bypass-permissions",
                restored_permission_policy=run_start_permission_fields("run:plan")["permission_policy"],
                accepted_plan_summary="accepted summary",
                accepted_plan_artifact_id=accepted_artifact_id,
                transition_owner="agent_run",
            ),
        ]
    )

    state = reduce_plan_workflow_state(log.iter())

    assert state.active is False
    assert state.pre_plan_permission_mode is None
    assert state.latest_accepted_plan_summary == "accepted summary"
    assert state.latest_accepted_plan_artifact_id == accepted_artifact_id


def test_plan_workflow_reducer_restores_active_state_after_enter() -> None:
    ctx = EventContext(run_id="run:plan", turn_id="turn:plan", reply_id="reply:plan")
    log = InMemoryEventLog()
    log.append(
        PlanModeEnteredEvent(
            **ctx.event_fields(),
            source="agent",
            previous_permission_mode="ask-permissions",
            previous_permission_policy=run_start_permission_fields("run:plan", mode="ask-permissions")["permission_policy"],
            reason="agent chose to plan",
        )
    )

    state = reduce_plan_workflow_state(log.iter())

    assert state.active is True
    assert state.entered_by == "agent"
    assert state.pre_plan_permission_mode == "ask-permissions"
    assert state.pre_plan_permission_policy == run_start_permission_fields(
        "run:plan",
        mode="ask-permissions",
    )["permission_policy"]
