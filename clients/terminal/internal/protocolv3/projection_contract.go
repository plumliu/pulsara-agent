package protocolv3

// ProjectionContract is the generated-language view of the Python-owned
// 31-type event descriptor. It validates server lowering; it is not an event
// reducer or a second conversation authority.
type ProjectionContract struct {
	Kind        ObservationProjectionKind
	SubjectSlot string
}

var projectionContracts = map[CommittedEventType]ProjectionContract{
	CommittedEventType_USER_MESSAGE_ACCEPTED:           {ObservationProjectionKind_IMMUTABLE_ENTRY, "subject_entry_id"},
	CommittedEventType_ASSISTANT_MESSAGE_ACCEPTED:      {ObservationProjectionKind_IMMUTABLE_ENTRY, "subject_entry_id"},
	CommittedEventType_ASSISTANT_TOOL_REQUEST_ACCEPTED: {ObservationProjectionKind_IMMUTABLE_ENTRY, "subject_entry_id"},
	CommittedEventType_TOOL_RESULT_ACCEPTED:            {ObservationProjectionKind_IMMUTABLE_ENTRY, "subject_entry_id"},
	CommittedEventType_TURN_COMPLETED:                  {ObservationProjectionKind_CURRENT_CONTROL, "subject_turn_id"},
	CommittedEventType_TURN_INTERRUPTED:                {ObservationProjectionKind_CURRENT_CONTROL, "subject_turn_id"},
	CommittedEventType_USER_STEER_ACCEPTED:             {ObservationProjectionKind_IMMUTABLE_ENTRY, "subject_entry_id"},
	CommittedEventType_TERMINAL_OBSERVATION_ACCEPTED:   {ObservationProjectionKind_IMMUTABLE_ENTRY, "subject_entry_id"},
	CommittedEventType_CAPABILITY_DECISION_ACCEPTED:    {ObservationProjectionKind_CURRENT_CONTROL, "subject_interaction_decision_id"},
	CommittedEventType_INTERACTION_DECISION_ACCEPTED:   {ObservationProjectionKind_CURRENT_CONTROL, "subject_interaction_decision_id"},
	CommittedEventType_TOOL_ATTEMPT_ACCEPTED:           {ObservationProjectionKind_CURRENT_CONTROL, "subject_tool_attempt_id"},
	CommittedEventType_TOOL_REMOTE_IDENTITY_PUBLISHED:  {ObservationProjectionKind_CURRENT_CONTROL, "subject_tool_attempt_id"},
	CommittedEventType_PROMPT_QUEUED:                   {ObservationProjectionKind_CURRENT_CONTROL, "subject_queue_item_id"},
	CommittedEventType_PROMPT_CONSUMED:                 {ObservationProjectionKind_CURRENT_CONTROL, "subject_queue_item_id"},
	CommittedEventType_PROMPT_CANCELLED:                {ObservationProjectionKind_CURRENT_CONTROL, "subject_queue_item_id"},
	CommittedEventType_PROMPT_REJECTED:                 {ObservationProjectionKind_CURRENT_CONTROL, "subject_queue_item_id"},
	CommittedEventType_COMPACTION_ADOPTED:              {ObservationProjectionKind_CURRENT_CONTROL, "subject_context_binding_revision_id"},
	CommittedEventType_SUBAGENT_TASK_ACCEPTED:          {ObservationProjectionKind_CURRENT_CONTROL, "subject_subagent_task_id"},
	CommittedEventType_SUBAGENT_TASK_STATUS_ACCEPTED:   {ObservationProjectionKind_CURRENT_CONTROL, "subject_subagent_task_id"},
	CommittedEventType_SUBAGENT_MESSAGE_ACCEPTED:       {ObservationProjectionKind_EVENT_ONLY, "subject_subagent_message_id"},
	CommittedEventType_SUBAGENT_RESULT_ACCEPTED:        {ObservationProjectionKind_EVENT_ONLY, "subject_subagent_result_id"},
	CommittedEventType_JOB_QUEUED:                      {ObservationProjectionKind_CURRENT_CONTROL, "subject_job_id"},
	CommittedEventType_JOB_ATTEMPT_ACCEPTED:            {ObservationProjectionKind_EVENT_ONLY, "subject_job_attempt_id"},
	CommittedEventType_JOB_TERMINAL_ACCEPTED:           {ObservationProjectionKind_CURRENT_CONTROL, "subject_job_id"},
	CommittedEventType_PLAN_WORKFLOW_ENTERED:           {ObservationProjectionKind_CURRENT_CONTROL, "subject_plan_workflow_id"},
	CommittedEventType_PLAN_QUESTION_ASKED:             {ObservationProjectionKind_CURRENT_CONTROL, "subject_plan_interaction_id"},
	CommittedEventType_PLAN_QUESTION_ANSWERED:          {ObservationProjectionKind_CURRENT_CONTROL, "subject_plan_interaction_id"},
	CommittedEventType_PLAN_DRAFT_SUBMITTED:            {ObservationProjectionKind_CURRENT_CONTROL, "subject_plan_interaction_id"},
	CommittedEventType_PLAN_DRAFT_DECISION_ACCEPTED:    {ObservationProjectionKind_CURRENT_CONTROL, "subject_plan_interaction_id"},
	CommittedEventType_PLAN_WORKFLOW_EXITED:            {ObservationProjectionKind_CURRENT_CONTROL, "subject_plan_workflow_id"},
	CommittedEventType_PLAN_CONTINUATION_ACCEPTED:      {ObservationProjectionKind_IMMUTABLE_ENTRY, "subject_entry_id"},
}

func ExpectedProjectionContract(eventType CommittedEventType) (ProjectionContract, bool) {
	value, ok := projectionContracts[eventType]
	return value, ok
}

func ProjectionContractCount() int { return len(projectionContracts) }
