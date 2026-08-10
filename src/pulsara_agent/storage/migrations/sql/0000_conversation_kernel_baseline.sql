CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;

CREATE TABLE public.pulsara_schema_migrations (
    universe_id text NOT NULL CHECK (universe_id = 'pulsara.conversation-kernel.v1'),
    universe_generation integer NOT NULL CHECK (universe_generation = 1),
    universe_fingerprint text NOT NULL CHECK (universe_fingerprint ~ '^sha256:[0-9a-f]{64}$'),
    version integer PRIMARY KEY CHECK (version >= 0),
    name text NOT NULL UNIQUE,
    resource_sha256 text NOT NULL CHECK (resource_sha256 ~ '^[0-9a-f]{64}$'),
    migration_contract_fingerprint text NOT NULL CHECK (migration_contract_fingerprint ~ '^sha256:[0-9a-f]{64}$'),
    registry_prefix_fingerprint text NOT NULL CHECK (registry_prefix_fingerprint ~ '^sha256:[0-9a-f]{64}$'),
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    application_version text NOT NULL
);
REVOKE ALL ON public.pulsara_schema_migrations FROM PUBLIC;

CREATE SCHEMA pulsara_v3;
REVOKE ALL ON SCHEMA pulsara_v3 FROM PUBLIC;

CREATE TABLE pulsara_v3.sessions (
    id text PRIMARY KEY,
    workspace_id text NOT NULL,
    lifecycle text NOT NULL CHECK (lifecycle IN ('OPEN', 'CLOSED')),
    writer_generation bigint NOT NULL CHECK (writer_generation >= 1),
    writer_lease_owner_id text,
    writer_lease_expires_at timestamptz,
    latest_entry_sequence bigint NOT NULL DEFAULT 0 CHECK (latest_entry_sequence >= 0),
    latest_event_sequence bigint NOT NULL DEFAULT 0 CHECK (latest_event_sequence >= 0),
    latest_prompt_queue_sequence bigint NOT NULL DEFAULT 0 CHECK (latest_prompt_queue_sequence >= 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (id, workspace_id),
    CHECK ((writer_lease_owner_id IS NULL) = (writer_lease_expires_at IS NULL))
);

CREATE TABLE pulsara_v3.blobs (
    id text PRIMARY KEY,
    workspace_id text NOT NULL,
    storage_identity text NOT NULL UNIQUE,
    logical_digest text NOT NULL CHECK (logical_digest ~ '^sha256:[0-9a-f]{64}$'),
    logical_size bigint NOT NULL CHECK (logical_size >= 0),
    media_type text NOT NULL,
    codec text NOT NULL,
    body bytea NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (id, workspace_id),
    CHECK (octet_length(body) = logical_size)
);

CREATE TABLE pulsara_v3.context_snapshots (
    id text PRIMARY KEY,
    session_id text NOT NULL,
    workspace_id text NOT NULL,
    source_through_sequence bigint NOT NULL CHECK (source_through_sequence >= 0),
    source_digest text NOT NULL CHECK (source_digest ~ '^sha256:[0-9a-f]{64}$'),
    compiler_contract text NOT NULL,
    prompt_contract text NOT NULL,
    model_contract text NOT NULL,
    inline_content bytea,
    blob_id text,
    content_digest text NOT NULL CHECK (content_digest ~ '^sha256:[0-9a-f]{64}$'),
    content_size bigint NOT NULL CHECK (content_size >= 0),
    content_media_type text NOT NULL,
    content_codec text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (session_id, id),
    FOREIGN KEY (session_id, workspace_id)
        REFERENCES pulsara_v3.sessions (id, workspace_id) ON DELETE RESTRICT,
    FOREIGN KEY (blob_id, workspace_id)
        REFERENCES pulsara_v3.blobs (id, workspace_id) ON DELETE RESTRICT,
    CHECK ((inline_content IS NULL) <> (blob_id IS NULL)),
    CHECK (inline_content IS NULL OR octet_length(inline_content) = content_size)
);

CREATE TABLE pulsara_v3.subagent_tasks (
    id text PRIMARY KEY,
    session_id text NOT NULL,
    workspace_id text NOT NULL,
    parent_turn_id text,
    objective text NOT NULL,
    status text NOT NULL CHECK (status IN (
        'PENDING', 'ACTIVE', 'COMPLETED', 'FAILED', 'INTERRUPTED', 'CANCELLED'
    )),
    execution_writer_generation bigint NOT NULL CHECK (execution_writer_generation >= 1),
    terminal_reason text,
    accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    terminal_at timestamptz,
    UNIQUE (session_id, id),
    FOREIGN KEY (session_id, workspace_id)
        REFERENCES pulsara_v3.sessions (id, workspace_id) ON DELETE RESTRICT,
    CHECK ((status IN ('COMPLETED', 'FAILED', 'INTERRUPTED', 'CANCELLED')) = (terminal_at IS NOT NULL))
);

CREATE TABLE pulsara_v3.turns (
    id text PRIMARY KEY,
    session_id text NOT NULL,
    workspace_id text NOT NULL,
    conversation_scope_kind text NOT NULL CHECK (conversation_scope_kind IN ('ROOT', 'SUBAGENT_TASK')),
    scope_subagent_task_id text,
    status text NOT NULL CHECK (status IN ('RUNNING', 'COMPLETED', 'INTERRUPTED')),
    user_entry_id text,
    final_entry_id text,
    current_context_binding_revision_id text,
    terminal_reason text,
    accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    terminal_at timestamptz,
    UNIQUE (session_id, id),
    FOREIGN KEY (session_id, workspace_id)
        REFERENCES pulsara_v3.sessions (id, workspace_id) ON DELETE RESTRICT,
    FOREIGN KEY (session_id, scope_subagent_task_id)
        REFERENCES pulsara_v3.subagent_tasks (session_id, id) ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED,
    CHECK ((conversation_scope_kind = 'ROOT') = (scope_subagent_task_id IS NULL)),
    CHECK ((status = 'RUNNING') = (terminal_at IS NULL))
);
CREATE UNIQUE INDEX uq_pulsara_v3_running_root_turn
    ON pulsara_v3.turns (session_id) WHERE status = 'RUNNING' AND conversation_scope_kind = 'ROOT';
CREATE UNIQUE INDEX uq_pulsara_v3_running_task_turn
    ON pulsara_v3.turns (session_id, scope_subagent_task_id)
    WHERE status = 'RUNNING' AND conversation_scope_kind = 'SUBAGENT_TASK';

ALTER TABLE pulsara_v3.subagent_tasks ADD CONSTRAINT subagent_tasks_parent_turn_fk
    FOREIGN KEY (session_id, parent_turn_id)
    REFERENCES pulsara_v3.turns (session_id, id) ON DELETE RESTRICT
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE pulsara_v3.turn_context_binding_revisions (
    id text PRIMARY KEY,
    session_id text NOT NULL,
    turn_id text NOT NULL,
    revision_ordinal integer NOT NULL CHECK (revision_ordinal >= 0),
    base_kind text NOT NULL CHECK (base_kind IN ('FULL_HISTORY', 'SNAPSHOT')),
    context_snapshot_id text,
    source_through_sequence bigint NOT NULL CHECK (source_through_sequence >= 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (session_id, id),
    UNIQUE (turn_id, revision_ordinal),
    FOREIGN KEY (session_id, turn_id)
        REFERENCES pulsara_v3.turns (session_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (session_id, context_snapshot_id)
        REFERENCES pulsara_v3.context_snapshots (session_id, id) ON DELETE RESTRICT,
    CHECK ((base_kind = 'FULL_HISTORY') = (context_snapshot_id IS NULL))
);
ALTER TABLE pulsara_v3.turns ADD CONSTRAINT turns_current_context_revision_fk
    FOREIGN KEY (session_id, current_context_binding_revision_id)
    REFERENCES pulsara_v3.turn_context_binding_revisions (session_id, id)
    ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE pulsara_v3.session_commands (
    session_id text NOT NULL,
    command_id text NOT NULL,
    command_kind text NOT NULL CHECK (command_kind IN (
        'SUBMIT_PROMPT', 'STEER', 'QUEUE_PROMPT', 'CANCEL_PROMPT',
        'RESOLVE_INTERACTION', 'ACCEPT_JOB_RESULT', 'ACCEPT_SUBAGENT_RESULT'
    )),
    request_schema_version text NOT NULL,
    semantic_digest text NOT NULL CHECK (semantic_digest ~ '^sha256:[0-9a-f]{64}$'),
    target_kind text NOT NULL CHECK (target_kind IN (
        'TURN', 'ENTRY', 'QUEUE_ITEM', 'INTERACTION_DECISION', 'JOB'
    )),
    target_turn_id text,
    target_entry_id text,
    target_queue_item_id text,
    target_interaction_decision_id text,
    target_job_id text,
    accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (session_id, command_id),
    FOREIGN KEY (session_id) REFERENCES pulsara_v3.sessions (id) ON DELETE RESTRICT,
    CHECK (num_nonnulls(
        target_turn_id, target_entry_id, target_queue_item_id,
        target_interaction_decision_id, target_job_id
    ) = 1),
    CHECK (
        (target_kind = 'TURN' AND target_turn_id IS NOT NULL) OR
        (target_kind = 'ENTRY' AND target_entry_id IS NOT NULL) OR
        (target_kind = 'QUEUE_ITEM' AND target_queue_item_id IS NOT NULL) OR
        (target_kind = 'INTERACTION_DECISION' AND target_interaction_decision_id IS NOT NULL) OR
        (target_kind = 'JOB' AND target_job_id IS NOT NULL)
    ),
    CHECK (
        (command_kind = 'SUBMIT_PROMPT' AND target_kind = 'TURN') OR
        (command_kind = 'STEER' AND target_kind = 'ENTRY') OR
        (command_kind IN ('QUEUE_PROMPT', 'CANCEL_PROMPT') AND target_kind = 'QUEUE_ITEM') OR
        (command_kind = 'RESOLVE_INTERACTION' AND target_kind = 'INTERACTION_DECISION') OR
        (command_kind IN ('ACCEPT_JOB_RESULT', 'ACCEPT_SUBAGENT_RESULT')
            AND target_kind = 'ENTRY')
    )
);

CREATE TABLE pulsara_v3.transcript_entries (
    id text PRIMARY KEY,
    session_id text NOT NULL,
    workspace_id text NOT NULL,
    turn_id text NOT NULL,
    entry_sequence bigint NOT NULL CHECK (entry_sequence >= 1),
    entry_kind text NOT NULL CHECK (entry_kind IN (
        'USER_MESSAGE', 'USER_STEER', 'ASSISTANT_MESSAGE',
        'ASSISTANT_TOOL_REQUEST', 'TOOL_RESULT'
    )),
    conversation_scope_kind text NOT NULL CHECK (conversation_scope_kind IN ('ROOT', 'SUBAGENT_TASK')),
    scope_subagent_task_id text,
    context_binding_revision_id text,
    provider_input_through_sequence bigint,
    source_job_id text,
    source_subagent_result_id text,
    inline_content bytea,
    blob_id text,
    content_digest text NOT NULL CHECK (content_digest ~ '^sha256:[0-9a-f]{64}$'),
    content_size bigint NOT NULL CHECK (content_size >= 0),
    content_media_type text NOT NULL,
    content_codec text NOT NULL,
    accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (session_id, id),
    UNIQUE (session_id, entry_sequence),
    UNIQUE (session_id, source_job_id),
    UNIQUE (session_id, source_subagent_result_id),
    FOREIGN KEY (session_id, workspace_id)
        REFERENCES pulsara_v3.sessions (id, workspace_id) ON DELETE RESTRICT,
    FOREIGN KEY (session_id, turn_id)
        REFERENCES pulsara_v3.turns (session_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (session_id, scope_subagent_task_id)
        REFERENCES pulsara_v3.subagent_tasks (session_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (session_id, context_binding_revision_id)
        REFERENCES pulsara_v3.turn_context_binding_revisions (session_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (blob_id, workspace_id)
        REFERENCES pulsara_v3.blobs (id, workspace_id) ON DELETE RESTRICT,
    CHECK ((conversation_scope_kind = 'ROOT') = (scope_subagent_task_id IS NULL)),
    CHECK ((inline_content IS NULL) <> (blob_id IS NULL)),
    CHECK (inline_content IS NULL OR octet_length(inline_content) = content_size),
    CHECK (
        (entry_kind IN ('ASSISTANT_MESSAGE', 'ASSISTANT_TOOL_REQUEST')
            AND context_binding_revision_id IS NOT NULL
            AND provider_input_through_sequence IS NOT NULL
            AND provider_input_through_sequence < entry_sequence)
        OR
        (entry_kind NOT IN ('ASSISTANT_MESSAGE', 'ASSISTANT_TOOL_REQUEST')
            AND context_binding_revision_id IS NULL
            AND provider_input_through_sequence IS NULL)
    ),
    CHECK (num_nonnulls(source_job_id, source_subagent_result_id) <= 1),
    CHECK ((source_job_id IS NULL AND source_subagent_result_id IS NULL) OR
        (conversation_scope_kind = 'ROOT' AND entry_kind = 'USER_MESSAGE'))
);
ALTER TABLE pulsara_v3.turns ADD CONSTRAINT turns_user_entry_fk
    FOREIGN KEY (session_id, user_entry_id)
    REFERENCES pulsara_v3.transcript_entries (session_id, id)
    ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE pulsara_v3.turns ADD CONSTRAINT turns_final_entry_fk
    FOREIGN KEY (session_id, final_entry_id)
    REFERENCES pulsara_v3.transcript_entries (session_id, id)
    ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE pulsara_v3.assistant_message_blocks (
    id text PRIMARY KEY,
    session_id text NOT NULL,
    workspace_id text NOT NULL,
    assistant_entry_id text NOT NULL,
    block_ordinal integer NOT NULL CHECK (block_ordinal >= 0),
    block_kind text NOT NULL CHECK (block_kind IN ('TEXT', 'DATA', 'TOOL_CALL')),
    tool_call_id text,
    tool_name text,
    tool_arguments jsonb,
    inline_content bytea,
    blob_id text,
    content_digest text,
    content_size bigint,
    content_media_type text,
    content_codec text,
    UNIQUE (session_id, id),
    UNIQUE (assistant_entry_id, block_ordinal),
    UNIQUE (session_id, assistant_entry_id, tool_call_id),
    FOREIGN KEY (session_id, workspace_id)
        REFERENCES pulsara_v3.sessions (id, workspace_id) ON DELETE RESTRICT,
    FOREIGN KEY (session_id, assistant_entry_id)
        REFERENCES pulsara_v3.transcript_entries (session_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (blob_id, workspace_id)
        REFERENCES pulsara_v3.blobs (id, workspace_id) ON DELETE RESTRICT,
    CHECK (
        (block_kind = 'TOOL_CALL' AND tool_call_id IS NOT NULL AND tool_name IS NOT NULL
            AND tool_arguments IS NOT NULL AND inline_content IS NULL AND blob_id IS NULL)
        OR
        (block_kind IN ('TEXT', 'DATA') AND tool_call_id IS NULL AND tool_name IS NULL
            AND tool_arguments IS NULL AND ((inline_content IS NULL) <> (blob_id IS NULL))
            AND content_digest IS NOT NULL AND content_size IS NOT NULL
            AND content_media_type IS NOT NULL AND content_codec IS NOT NULL)
    )
);

CREATE TABLE pulsara_v3.tool_execution_attempts (
    id text PRIMARY KEY,
    session_id text NOT NULL,
    assistant_entry_id text NOT NULL,
    tool_call_id text NOT NULL,
    authorization_kind text NOT NULL,
    authorization_reference text NOT NULL,
    actor_kind text NOT NULL,
    actor_id text NOT NULL,
    remote_idempotency_key text,
    remote_identity text,
    retry_of_attempt_id text,
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    remote_identity_published_at timestamptz,
    UNIQUE (session_id, id),
    UNIQUE (session_id, assistant_entry_id, tool_call_id),
    UNIQUE (session_id, id, assistant_entry_id, tool_call_id),
    FOREIGN KEY (session_id, assistant_entry_id, tool_call_id)
        REFERENCES pulsara_v3.assistant_message_blocks (session_id, assistant_entry_id, tool_call_id)
        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (session_id, retry_of_attempt_id)
        REFERENCES pulsara_v3.tool_execution_attempts (session_id, id) ON DELETE RESTRICT,
    CHECK ((remote_identity IS NULL) = (remote_identity_published_at IS NULL))
);

CREATE TABLE pulsara_v3.tool_results (
    id text PRIMARY KEY,
    session_id text NOT NULL,
    tool_call_entry_id text NOT NULL,
    tool_call_id text NOT NULL,
    attempt_id text,
    result_entry_id text NOT NULL,
    result_state text NOT NULL CHECK (result_state IN (
        'SUCCESS', 'APPLICATION_ERROR', 'SYSTEM_ERROR', 'CANCELLED',
        'INVALID_ARGUMENTS', 'PERMISSION_DENIED', 'TOOL_UNAVAILABLE',
        'CANCELLED_BEFORE_DISPATCH'
    )),
    accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (session_id, id),
    UNIQUE (session_id, tool_call_entry_id, tool_call_id),
    UNIQUE (session_id, result_entry_id),
    FOREIGN KEY (session_id, tool_call_entry_id, tool_call_id)
        REFERENCES pulsara_v3.assistant_message_blocks (session_id, assistant_entry_id, tool_call_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (session_id, attempt_id, tool_call_entry_id, tool_call_id)
        REFERENCES pulsara_v3.tool_execution_attempts (
            session_id, id, assistant_entry_id, tool_call_id
        ) ON DELETE RESTRICT,
    FOREIGN KEY (session_id, result_entry_id)
        REFERENCES pulsara_v3.transcript_entries (session_id, id) ON DELETE RESTRICT,
    CHECK (
        (attempt_id IS NULL AND result_state IN (
            'INVALID_ARGUMENTS', 'PERMISSION_DENIED', 'TOOL_UNAVAILABLE', 'CANCELLED_BEFORE_DISPATCH'
        )) OR
        (attempt_id IS NOT NULL AND result_state IN (
            'SUCCESS', 'APPLICATION_ERROR', 'SYSTEM_ERROR', 'CANCELLED'
        ))
    )
);

CREATE TABLE pulsara_v3.prompt_queue_items (
    id text PRIMARY KEY,
    session_id text NOT NULL,
    workspace_id text NOT NULL,
    queue_sequence bigint NOT NULL CHECK (queue_sequence >= 1),
    command_id text NOT NULL,
    client_submission_id text NOT NULL,
    delivery_mode text NOT NULL CHECK (delivery_mode IN ('NEW_TURN', 'STEER_ACTIVE_TURN')),
    target_turn_id text,
    status text NOT NULL CHECK (status IN ('PENDING', 'CONSUMED', 'CANCELLED', 'REJECTED')),
    inline_content bytea,
    blob_id text,
    content_digest text NOT NULL CHECK (content_digest ~ '^sha256:[0-9a-f]{64}$'),
    content_size bigint NOT NULL CHECK (content_size >= 0),
    content_media_type text NOT NULL,
    content_codec text NOT NULL,
    consumed_entry_id text,
    terminal_reason text,
    accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    terminal_at timestamptz,
    UNIQUE (session_id, id),
    UNIQUE (session_id, queue_sequence),
    FOREIGN KEY (session_id, workspace_id)
        REFERENCES pulsara_v3.sessions (id, workspace_id) ON DELETE RESTRICT,
    FOREIGN KEY (session_id, command_id)
        REFERENCES pulsara_v3.session_commands (session_id, command_id) ON DELETE RESTRICT,
    FOREIGN KEY (session_id, target_turn_id)
        REFERENCES pulsara_v3.turns (session_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (session_id, consumed_entry_id)
        REFERENCES pulsara_v3.transcript_entries (session_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (blob_id, workspace_id)
        REFERENCES pulsara_v3.blobs (id, workspace_id) ON DELETE RESTRICT,
    CHECK ((delivery_mode = 'NEW_TURN') = (target_turn_id IS NULL)),
    CHECK ((inline_content IS NULL) <> (blob_id IS NULL)),
    CHECK ((status = 'PENDING') = (terminal_at IS NULL)),
    CHECK ((status = 'CONSUMED') = (consumed_entry_id IS NOT NULL))
);

CREATE TABLE pulsara_v3.interaction_decisions (
    id text PRIMARY KEY,
    session_id text NOT NULL,
    command_id text,
    subject_kind text NOT NULL CHECK (subject_kind IN ('TOOL_CALL', 'PLAN', 'MCP_INPUT')),
    subject_tool_call_entry_id text,
    subject_tool_call_id text,
    subject_turn_id text,
    decision text NOT NULL CHECK (decision IN (
        'ALLOW', 'DENY', 'REQUIRE_CONFIRMATION', 'ACCEPT', 'REJECT', 'CANCEL'
    )),
    actor_kind text NOT NULL CHECK (actor_kind IN ('machine', 'human')),
    actor_id text NOT NULL,
    redacted_subject text NOT NULL,
    secret_commitment text,
    accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (session_id, id),
    FOREIGN KEY (session_id) REFERENCES pulsara_v3.sessions (id) ON DELETE RESTRICT,
    FOREIGN KEY (session_id, command_id)
        REFERENCES pulsara_v3.session_commands (session_id, command_id)
        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (session_id, subject_tool_call_entry_id, subject_tool_call_id)
        REFERENCES pulsara_v3.assistant_message_blocks (
            session_id, assistant_entry_id, tool_call_id
        ) ON DELETE RESTRICT,
    FOREIGN KEY (session_id, subject_turn_id)
        REFERENCES pulsara_v3.turns (session_id, id) ON DELETE RESTRICT,
    CHECK ((subject_tool_call_entry_id IS NULL) = (subject_tool_call_id IS NULL)),
    CHECK (num_nonnulls(subject_tool_call_entry_id, subject_turn_id) = 1),
    CHECK (
        (subject_kind = 'TOOL_CALL' AND subject_tool_call_entry_id IS NOT NULL) OR
        (subject_kind IN ('PLAN', 'MCP_INPUT') AND subject_turn_id IS NOT NULL)
    ),
    CHECK (
        (actor_kind = 'machine' AND command_id IS NULL
            AND subject_kind = 'TOOL_CALL'
            AND decision IN ('ALLOW', 'DENY', 'REQUIRE_CONFIRMATION')) OR
        (actor_kind = 'human' AND command_id IS NOT NULL
            AND decision IN ('ALLOW', 'DENY', 'ACCEPT', 'REJECT', 'CANCEL'))
    )
);
CREATE UNIQUE INDEX uq_pulsara_v3_tool_call_machine_decision
    ON pulsara_v3.interaction_decisions (
        session_id, subject_tool_call_entry_id, subject_tool_call_id
    ) WHERE subject_kind = 'TOOL_CALL' AND actor_kind = 'machine';
CREATE UNIQUE INDEX uq_pulsara_v3_tool_call_human_decision
    ON pulsara_v3.interaction_decisions (
        session_id, subject_tool_call_entry_id, subject_tool_call_id
    ) WHERE subject_kind = 'TOOL_CALL' AND actor_kind = 'human';

CREATE TABLE pulsara_v3.subagent_task_children (
    id text PRIMARY KEY,
    session_id text NOT NULL,
    task_id text NOT NULL,
    child_kind text NOT NULL CHECK (child_kind IN ('MESSAGE', 'RESULT')),
    child_ordinal integer NOT NULL CHECK (child_ordinal >= 0),
    entry_id text NOT NULL,
    accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (session_id, id),
    UNIQUE (session_id, id, child_kind),
    UNIQUE (task_id, child_ordinal),
    FOREIGN KEY (session_id, task_id)
        REFERENCES pulsara_v3.subagent_tasks (session_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (session_id, entry_id)
        REFERENCES pulsara_v3.transcript_entries (session_id, id) ON DELETE RESTRICT
);

CREATE TABLE pulsara_v3.durable_jobs (
    id text PRIMARY KEY,
    workspace_id text NOT NULL,
    origin_session_id text,
    origin_command_id text,
    handler_type text NOT NULL CHECK (handler_type IN (
        'BACKGROUND_COMPACTION', 'POST_COMPACTION_MEMORY_EXTRACTION',
        'MEMORY_GOVERNANCE', 'MEMORY_INDEX_REFRESH'
    )),
    intent_schema_version text NOT NULL,
    intent_digest text NOT NULL CHECK (intent_digest ~ '^sha256:[0-9a-f]{64}$'),
    intent_payload jsonb NOT NULL,
    automatic_intent_key text,
    safety_class text NOT NULL CHECK (safety_class IN ('RETRY_SAFE', 'REMOTE_QUERYABLE', 'NON_IDEMPOTENT')),
    status text NOT NULL CHECK (status IN (
        'PENDING', 'ACTIVE', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'OUTCOME_UNKNOWN'
    )),
    retry_policy_id text NOT NULL,
    retry_policy_version integer NOT NULL CHECK (retry_policy_version >= 1),
    maximum_attempts integer NOT NULL CHECK (maximum_attempts >= 1),
    attempt_timeout_ms integer NOT NULL CHECK (attempt_timeout_ms > 0),
    provider_input_token_limit_per_attempt integer,
    provider_output_token_limit_per_attempt integer,
    next_eligible_at timestamptz NOT NULL,
    result_blob_id text,
    terminal_reason text,
    cancel_requested_at timestamptz,
    cancel_requested_by text,
    cancel_request_reason text,
    accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    terminal_at timestamptz,
    UNIQUE (origin_session_id, id),
    UNIQUE (handler_type, automatic_intent_key),
    FOREIGN KEY (origin_session_id) REFERENCES pulsara_v3.sessions (id) ON DELETE RESTRICT,
    FOREIGN KEY (result_blob_id, workspace_id)
        REFERENCES pulsara_v3.blobs (id, workspace_id) ON DELETE RESTRICT,
    CHECK ((handler_type IN (
        'BACKGROUND_COMPACTION', 'POST_COMPACTION_MEMORY_EXTRACTION', 'MEMORY_GOVERNANCE'
    )) = (provider_input_token_limit_per_attempt IS NOT NULL
        AND provider_output_token_limit_per_attempt IS NOT NULL)),
    CHECK (provider_input_token_limit_per_attempt IS NULL OR provider_input_token_limit_per_attempt > 0),
    CHECK (provider_output_token_limit_per_attempt IS NULL OR provider_output_token_limit_per_attempt > 0),
    CHECK ((status IN ('SUCCEEDED', 'FAILED', 'CANCELLED', 'OUTCOME_UNKNOWN')) = (terminal_at IS NOT NULL)),
    CHECK (
        (cancel_requested_at IS NULL AND cancel_requested_by IS NULL
            AND cancel_request_reason IS NULL)
        OR
        (cancel_requested_at IS NOT NULL AND cancel_requested_by IS NOT NULL
            AND cancel_request_reason IS NOT NULL)
    )
);

CREATE TABLE pulsara_v3.durable_job_attempts (
    id text PRIMARY KEY,
    job_id text NOT NULL,
    origin_session_id text,
    attempt_ordinal integer NOT NULL CHECK (attempt_ordinal >= 1),
    claim_generation bigint NOT NULL CHECK (claim_generation >= 1),
    claim_owner_id text NOT NULL,
    lease_expires_at timestamptz NOT NULL,
    deadline_at timestamptz NOT NULL,
    retry_of_attempt_id text,
    provider_call_started_at timestamptz,
    provider_input_tokens integer,
    provider_requested_output_tokens integer,
    remote_identity text,
    terminal_status text CHECK (terminal_status IN ('SUCCEEDED', 'FAILED', 'CANCELLED', 'OUTCOME_UNKNOWN')),
    result_payload jsonb,
    error_code text,
    accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    terminal_at timestamptz,
    UNIQUE (job_id, attempt_ordinal),
    UNIQUE (origin_session_id, id),
    FOREIGN KEY (job_id) REFERENCES pulsara_v3.durable_jobs (id) ON DELETE RESTRICT,
    FOREIGN KEY (retry_of_attempt_id) REFERENCES pulsara_v3.durable_job_attempts (id) ON DELETE RESTRICT,
    CHECK ((provider_call_started_at IS NULL) = (provider_input_tokens IS NULL)),
    CHECK ((provider_call_started_at IS NULL) = (provider_requested_output_tokens IS NULL)),
    CHECK (provider_input_tokens IS NULL OR provider_input_tokens >= 0),
    CHECK (provider_requested_output_tokens IS NULL OR provider_requested_output_tokens > 0),
    CHECK ((terminal_status IS NULL) = (terminal_at IS NULL))
);

ALTER TABLE pulsara_v3.transcript_entries ADD CONSTRAINT transcript_entries_source_job_fk
    FOREIGN KEY (session_id, source_job_id)
    REFERENCES pulsara_v3.durable_jobs (origin_session_id, id) ON DELETE RESTRICT
    DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE pulsara_v3.transcript_entries ADD CONSTRAINT transcript_entries_source_subagent_result_fk
    FOREIGN KEY (session_id, source_subagent_result_id)
    REFERENCES pulsara_v3.subagent_task_children (session_id, id) ON DELETE RESTRICT
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE pulsara_v3.memory_candidates (
    id text PRIMARY KEY,
    workspace_id text NOT NULL,
    origin_session_id text,
    source_entry_id text,
    proposal_kind text NOT NULL CHECK (proposal_kind IN (
        'FACT', 'PREFERENCE', 'RELATION', 'CORRECTION', 'LIFECYCLE'
    )),
    semantic_digest text NOT NULL CHECK (semantic_digest ~ '^sha256:[0-9a-f]{64}$'),
    proposal_payload jsonb NOT NULL,
    status text NOT NULL CHECK (status IN ('PENDING', 'DECIDED')),
    accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (origin_session_id, id),
    FOREIGN KEY (origin_session_id, source_entry_id)
        REFERENCES pulsara_v3.transcript_entries (session_id, id) ON DELETE RESTRICT
);

CREATE TABLE pulsara_v3.memory_governance_decisions (
    id text PRIMARY KEY,
    candidate_id text NOT NULL UNIQUE,
    job_id text NOT NULL,
    decision text NOT NULL CHECK (decision IN (
        'SKIP', 'SUBMIT', 'CORRECT', 'MERGE', 'SUPERSEDE', 'CONTRADICT'
    )),
    lineage_payload jsonb NOT NULL,
    accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (candidate_id) REFERENCES pulsara_v3.memory_candidates (id) ON DELETE RESTRICT,
    FOREIGN KEY (job_id) REFERENCES pulsara_v3.durable_jobs (id) ON DELETE RESTRICT
);

CREATE TABLE pulsara_v3.memory_facts (
    id text PRIMARY KEY,
    workspace_id text NOT NULL,
    governance_decision_id text NOT NULL UNIQUE,
    lifecycle text NOT NULL CHECK (lifecycle IN ('ACTIVE', 'SUPERSEDED', 'STALE')),
    fact_kind text NOT NULL,
    fact_payload jsonb NOT NULL,
    semantic_digest text NOT NULL CHECK (semantic_digest ~ '^sha256:[0-9a-f]{64}$'),
    accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (workspace_id, id),
    FOREIGN KEY (governance_decision_id)
        REFERENCES pulsara_v3.memory_governance_decisions (id) ON DELETE RESTRICT
);

CREATE TABLE pulsara_v3.memory_relations (
    id text PRIMARY KEY,
    workspace_id text NOT NULL,
    source_fact_id text NOT NULL,
    target_fact_id text NOT NULL,
    relation_kind text NOT NULL,
    accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (workspace_id, id),
    UNIQUE (workspace_id, source_fact_id, target_fact_id, relation_kind),
    FOREIGN KEY (workspace_id, source_fact_id)
        REFERENCES pulsara_v3.memory_facts (workspace_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (workspace_id, target_fact_id)
        REFERENCES pulsara_v3.memory_facts (workspace_id, id) ON DELETE RESTRICT
);

CREATE TABLE pulsara_v3.memory_search_index (
    workspace_id text NOT NULL,
    fact_id text NOT NULL,
    generation bigint NOT NULL CHECK (generation >= 0),
    search_document tsvector NOT NULL,
    PRIMARY KEY (workspace_id, fact_id),
    FOREIGN KEY (workspace_id, fact_id)
        REFERENCES pulsara_v3.memory_facts (workspace_id, id) ON DELETE CASCADE
);
CREATE INDEX idx_pulsara_v3_memory_search_gin
    ON pulsara_v3.memory_search_index USING gin (search_document);

CREATE TABLE pulsara_v3.memory_vector_index (
    workspace_id text NOT NULL,
    fact_id text NOT NULL,
    generation bigint NOT NULL CHECK (generation >= 0),
    embedding public.vector NOT NULL,
    PRIMARY KEY (workspace_id, fact_id),
    FOREIGN KEY (workspace_id, fact_id)
        REFERENCES pulsara_v3.memory_facts (workspace_id, id) ON DELETE CASCADE
);

CREATE TABLE pulsara_v3.memory_index_state (
    workspace_id text NOT NULL,
    channel text NOT NULL CHECK (channel IN ('FTS', 'VECTOR')),
    desired_generation bigint NOT NULL CHECK (desired_generation >= 0),
    desired_handler_contract_id text NOT NULL,
    desired_handler_contract_version integer NOT NULL CHECK (desired_handler_contract_version >= 1),
    applied_generation bigint NOT NULL CHECK (applied_generation >= 0),
    applied_handler_contract_id text NOT NULL,
    applied_handler_contract_version integer NOT NULL CHECK (applied_handler_contract_version >= 1),
    PRIMARY KEY (workspace_id, channel),
    CHECK (applied_generation <= desired_generation)
);

CREATE TABLE pulsara_v3.agent_events (
    event_id text PRIMARY KEY,
    workspace_id text NOT NULL,
    session_id text NOT NULL,
    event_sequence bigint NOT NULL CHECK (event_sequence >= 1),
    namespace text NOT NULL CHECK (namespace = 'pulsara.core'),
    event_type text NOT NULL CHECK (event_type IN (
        'UserMessageAccepted', 'AssistantMessageAccepted', 'AssistantToolRequestAccepted',
        'ToolResultAccepted', 'TurnCompleted', 'TurnInterrupted', 'UserSteerAccepted',
        'CapabilityDecisionAccepted', 'InteractionDecisionAccepted', 'ToolAttemptAccepted',
        'ToolRemoteIdentityPublished', 'PromptQueued', 'PromptConsumed', 'PromptCancelled',
        'PromptRejected', 'CompactionAdopted', 'SubagentTaskAccepted',
        'SubagentTaskStatusAccepted', 'SubagentMessageAccepted', 'SubagentResultAccepted',
        'JobQueued', 'JobAttemptAccepted', 'JobTerminalAccepted', 'MemoryFactAccepted',
        'MemoryFactLifecycleChanged', 'MemoryRelationAccepted'
    )),
    schema_major integer NOT NULL CHECK (schema_major = 1),
    schema_minor integer NOT NULL CHECK (schema_minor >= 0),
    accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    occurred_at timestamptz NOT NULL,
    actor_kind text NOT NULL,
    actor_id text NOT NULL,
    sensitivity_class text NOT NULL,
    projection_profile text NOT NULL,
    payload jsonb NOT NULL CHECK (octet_length(payload::text) <= 65536),
    subject_turn_id text,
    subject_entry_id text,
    subject_tool_attempt_id text,
    subject_job_id text,
    subject_job_attempt_id text,
    subject_queue_item_id text,
    subject_interaction_decision_id text,
    subject_context_binding_revision_id text,
    subject_subagent_task_id text,
    subject_subagent_message_id text,
    subject_subagent_result_id text,
    subject_subagent_child_kind text CHECK (
        subject_subagent_child_kind IN ('MESSAGE', 'RESULT')
    ),
    subject_memory_fact_id text,
    subject_memory_relation_id text,
    UNIQUE (session_id, event_sequence),
    FOREIGN KEY (session_id, workspace_id)
        REFERENCES pulsara_v3.sessions (id, workspace_id) ON DELETE RESTRICT,
    FOREIGN KEY (session_id, subject_turn_id)
        REFERENCES pulsara_v3.turns (session_id, id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (session_id, subject_entry_id)
        REFERENCES pulsara_v3.transcript_entries (session_id, id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (session_id, subject_tool_attempt_id)
        REFERENCES pulsara_v3.tool_execution_attempts (session_id, id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (session_id, subject_job_id)
        REFERENCES pulsara_v3.durable_jobs (origin_session_id, id)
        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (session_id, subject_job_attempt_id)
        REFERENCES pulsara_v3.durable_job_attempts (origin_session_id, id)
        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (session_id, subject_queue_item_id)
        REFERENCES pulsara_v3.prompt_queue_items (session_id, id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (session_id, subject_interaction_decision_id)
        REFERENCES pulsara_v3.interaction_decisions (session_id, id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (session_id, subject_context_binding_revision_id)
        REFERENCES pulsara_v3.turn_context_binding_revisions (session_id, id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (session_id, subject_subagent_task_id)
        REFERENCES pulsara_v3.subagent_tasks (session_id, id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (session_id, subject_subagent_message_id, subject_subagent_child_kind)
        REFERENCES pulsara_v3.subagent_task_children (session_id, id, child_kind)
        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (session_id, subject_subagent_result_id, subject_subagent_child_kind)
        REFERENCES pulsara_v3.subagent_task_children (session_id, id, child_kind)
        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (workspace_id, subject_memory_fact_id)
        REFERENCES pulsara_v3.memory_facts (workspace_id, id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (workspace_id, subject_memory_relation_id)
        REFERENCES pulsara_v3.memory_relations (workspace_id, id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    CHECK (num_nonnulls(
        subject_turn_id, subject_entry_id, subject_tool_attempt_id, subject_job_id,
        subject_job_attempt_id, subject_queue_item_id, subject_interaction_decision_id,
        subject_context_binding_revision_id, subject_subagent_task_id,
        subject_subagent_message_id, subject_subagent_result_id,
        subject_memory_fact_id, subject_memory_relation_id
    ) = 1),
    CHECK (
        (subject_subagent_message_id IS NOT NULL AND subject_subagent_child_kind = 'MESSAGE') OR
        (subject_subagent_result_id IS NOT NULL AND subject_subagent_child_kind = 'RESULT') OR
        (subject_subagent_message_id IS NULL AND subject_subagent_result_id IS NULL
            AND subject_subagent_child_kind IS NULL)
    ),
    CHECK (
        (event_type IN ('UserMessageAccepted', 'AssistantMessageAccepted',
            'AssistantToolRequestAccepted', 'ToolResultAccepted', 'UserSteerAccepted')
            AND subject_entry_id IS NOT NULL) OR
        (event_type IN ('TurnCompleted', 'TurnInterrupted') AND subject_turn_id IS NOT NULL) OR
        (event_type IN ('CapabilityDecisionAccepted', 'InteractionDecisionAccepted')
            AND subject_interaction_decision_id IS NOT NULL) OR
        (event_type IN ('ToolAttemptAccepted', 'ToolRemoteIdentityPublished')
            AND subject_tool_attempt_id IS NOT NULL) OR
        (event_type IN ('PromptQueued', 'PromptConsumed', 'PromptCancelled', 'PromptRejected')
            AND subject_queue_item_id IS NOT NULL) OR
        (event_type = 'CompactionAdopted' AND subject_context_binding_revision_id IS NOT NULL) OR
        (event_type IN ('SubagentTaskAccepted', 'SubagentTaskStatusAccepted')
            AND subject_subagent_task_id IS NOT NULL) OR
        (event_type = 'SubagentMessageAccepted' AND subject_subagent_message_id IS NOT NULL) OR
        (event_type = 'SubagentResultAccepted' AND subject_subagent_result_id IS NOT NULL) OR
        (event_type IN ('JobQueued', 'JobTerminalAccepted') AND subject_job_id IS NOT NULL) OR
        (event_type = 'JobAttemptAccepted' AND subject_job_attempt_id IS NOT NULL) OR
        (event_type IN ('MemoryFactAccepted', 'MemoryFactLifecycleChanged')
            AND subject_memory_fact_id IS NOT NULL) OR
        (event_type = 'MemoryRelationAccepted' AND subject_memory_relation_id IS NOT NULL)
    )
);

ALTER TABLE pulsara_v3.session_commands ADD CONSTRAINT session_commands_target_turn_fk
    FOREIGN KEY (session_id, target_turn_id) REFERENCES pulsara_v3.turns (session_id, id)
    ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE pulsara_v3.session_commands ADD CONSTRAINT session_commands_target_entry_fk
    FOREIGN KEY (session_id, target_entry_id) REFERENCES pulsara_v3.transcript_entries (session_id, id)
    ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE pulsara_v3.session_commands ADD CONSTRAINT session_commands_target_queue_fk
    FOREIGN KEY (session_id, target_queue_item_id) REFERENCES pulsara_v3.prompt_queue_items (session_id, id)
    ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE pulsara_v3.session_commands ADD CONSTRAINT session_commands_target_interaction_fk
    FOREIGN KEY (session_id, target_interaction_decision_id) REFERENCES pulsara_v3.interaction_decisions (session_id, id)
    ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE pulsara_v3.session_commands ADD CONSTRAINT session_commands_target_job_fk
    FOREIGN KEY (session_id, target_job_id)
    REFERENCES pulsara_v3.durable_jobs (origin_session_id, id)
    ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;

CREATE FUNCTION pulsara_v3.enforce_conversation_kernel_invariants()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    observed_kind text;
    observed_scope text;
    observed_status text;
    observed_task_id text;
    observed_turn_id text;
BEGIN
    IF TG_TABLE_NAME = 'transcript_entries' THEN
        IF NEW.source_job_id IS NOT NULL THEN
            SELECT status INTO observed_status
            FROM pulsara_v3.durable_jobs
            WHERE origin_session_id = NEW.session_id AND id = NEW.source_job_id;
            IF observed_status IS DISTINCT FROM 'SUCCEEDED' THEN
                RAISE EXCEPTION 'conversation source job must be SUCCEEDED'
                    USING ERRCODE = '23514';
            END IF;
        END IF;
        IF NEW.source_subagent_result_id IS NOT NULL THEN
            SELECT child_kind INTO observed_kind
            FROM pulsara_v3.subagent_task_children
            WHERE session_id = NEW.session_id AND id = NEW.source_subagent_result_id;
            IF observed_kind IS DISTINCT FROM 'RESULT' THEN
                RAISE EXCEPTION 'conversation subagent source must be a RESULT child'
                    USING ERRCODE = '23514';
            END IF;
        END IF;
        RETURN NEW;
    END IF;

    IF TG_TABLE_NAME = 'assistant_message_blocks' THEN
        SELECT entry_kind INTO observed_kind
        FROM pulsara_v3.transcript_entries
        WHERE session_id = NEW.session_id AND id = NEW.assistant_entry_id;
        IF observed_kind NOT IN ('ASSISTANT_MESSAGE', 'ASSISTANT_TOOL_REQUEST') THEN
            RAISE EXCEPTION 'assistant block parent has invalid entry kind'
                USING ERRCODE = '23514';
        END IF;
        IF NEW.block_kind = 'TOOL_CALL' AND observed_kind <> 'ASSISTANT_TOOL_REQUEST' THEN
            RAISE EXCEPTION 'tool-call block requires ASSISTANT_TOOL_REQUEST parent'
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;

    IF TG_TABLE_NAME = 'tool_results' THEN
        SELECT entry_kind, turn_id INTO observed_kind, observed_turn_id
        FROM pulsara_v3.transcript_entries
        WHERE session_id = NEW.session_id AND id = NEW.result_entry_id;
        IF observed_kind IS DISTINCT FROM 'TOOL_RESULT' THEN
            RAISE EXCEPTION 'tool result relation requires TOOL_RESULT entry'
                USING ERRCODE = '23514';
        END IF;
        PERFORM 1
        FROM pulsara_v3.transcript_entries AS call_entry
        WHERE call_entry.session_id = NEW.session_id
          AND call_entry.id = NEW.tool_call_entry_id
          AND call_entry.turn_id = observed_turn_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'tool result and tool request must belong to the same turn'
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;

    IF TG_TABLE_NAME = 'prompt_queue_items' THEN
        -- Keep the table discriminator in its own branch.  PL/pgSQL record
        -- field lookup is dynamic and an `AND NEW.target_turn_id ...` guard
        -- can still resolve that field for trigger rows from another table.
        IF NEW.target_turn_id IS NOT NULL THEN
            SELECT conversation_scope_kind, status INTO observed_scope, observed_status
            FROM pulsara_v3.turns
            WHERE session_id = NEW.session_id AND id = NEW.target_turn_id;
            IF observed_scope IS DISTINCT FROM 'ROOT' OR observed_status IS DISTINCT FROM 'RUNNING' THEN
                RAISE EXCEPTION 'steer target must be a RUNNING ROOT turn'
                    USING ERRCODE = '23514';
            END IF;
        END IF;
        RETURN NEW;
    END IF;

    IF TG_TABLE_NAME = 'subagent_task_children' THEN
        SELECT conversation_scope_kind, scope_subagent_task_id
          INTO observed_scope, observed_task_id
        FROM pulsara_v3.transcript_entries
        WHERE session_id = NEW.session_id AND id = NEW.entry_id;
        IF observed_scope IS DISTINCT FROM 'SUBAGENT_TASK'
           OR observed_task_id IS DISTINCT FROM NEW.task_id THEN
            RAISE EXCEPTION 'subagent child entry must belong to its exact task scope'
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;

    RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION pulsara_v3.enforce_conversation_kernel_invariants()
    FROM PUBLIC;

CREATE CONSTRAINT TRIGGER trg_pulsara_v3_entry_source_integrity
AFTER INSERT ON pulsara_v3.transcript_entries
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION pulsara_v3.enforce_conversation_kernel_invariants();

CREATE CONSTRAINT TRIGGER trg_pulsara_v3_assistant_block_integrity
AFTER INSERT ON pulsara_v3.assistant_message_blocks
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION pulsara_v3.enforce_conversation_kernel_invariants();

CREATE CONSTRAINT TRIGGER trg_pulsara_v3_tool_result_integrity
AFTER INSERT ON pulsara_v3.tool_results
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION pulsara_v3.enforce_conversation_kernel_invariants();

CREATE CONSTRAINT TRIGGER trg_pulsara_v3_prompt_target_integrity
AFTER INSERT ON pulsara_v3.prompt_queue_items
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION pulsara_v3.enforce_conversation_kernel_invariants();

CREATE CONSTRAINT TRIGGER trg_pulsara_v3_subagent_child_integrity
AFTER INSERT ON pulsara_v3.subagent_task_children
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION pulsara_v3.enforce_conversation_kernel_invariants();

CREATE INDEX idx_pulsara_v3_entries_session_scope_sequence
    ON pulsara_v3.transcript_entries (session_id, conversation_scope_kind, scope_subagent_task_id, entry_sequence);
CREATE INDEX idx_pulsara_v3_events_session_sequence
    ON pulsara_v3.agent_events (session_id, event_sequence);
CREATE INDEX idx_pulsara_v3_queue_pending
    ON pulsara_v3.prompt_queue_items (session_id, queue_sequence, id) WHERE status = 'PENDING';
CREATE INDEX idx_pulsara_v3_jobs_due
    ON pulsara_v3.durable_jobs (status, next_eligible_at, id) WHERE status = 'PENDING';
CREATE INDEX idx_pulsara_v3_job_attempt_claim
    ON pulsara_v3.durable_job_attempts (job_id, claim_generation);

REVOKE ALL ON ALL TABLES IN SCHEMA pulsara_v3 FROM PUBLIC;
