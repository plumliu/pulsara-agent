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
    workspace_kind text NOT NULL CHECK (workspace_kind IN ('project', 'transient')),
    workspace_root text NOT NULL CHECK (workspace_root <> ''),
    workspace_label text NOT NULL CHECK (workspace_label <> ''),
    memory_domain_id text NOT NULL CHECK (
        memory_domain_id ~ '^[a-z0-9][a-z0-9._-]{0,127}$'
    ),
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
    UNIQUE (id, workspace_id, memory_domain_id),
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
    parent_turn_id text NOT NULL,
    batch_id text,
    task_key text,
    label text,
    profile_kind text NOT NULL CHECK (profile_kind IN (
        'general_worker', 'research_worker', 'review_worker',
        'verification_worker', 'synthesizer'
    )),
    display_role text,
    context_mode text NOT NULL CHECK (context_mode IN ('NONE', 'LAST_N')),
    context_last_n_turns integer,
    objective text NOT NULL,
    status text NOT NULL CHECK (status IN (
        'PENDING_START', 'WAITING_DEPENDENCY', 'ACTIVE', 'COMPLETED',
        'FAILED', 'INTERRUPTED', 'CANCELLED', 'BLOCKED_DEPENDENCY_FAILED'
    )),
    execution_writer_generation bigint NOT NULL CHECK (execution_writer_generation >= 1),
    pending_reason text,
    terminal_reason text,
    terminal_public_detail text,
    accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    terminal_at timestamptz,
    UNIQUE (session_id, id),
    UNIQUE (session_id, parent_turn_id, batch_id, task_key),
    FOREIGN KEY (session_id, workspace_id)
        REFERENCES pulsara_v3.sessions (id, workspace_id) ON DELETE RESTRICT,
    CHECK (octet_length(objective) BETWEEN 1 AND 65536),
    CHECK (task_key IS NULL OR task_key ~ '^[a-z][a-z0-9_-]{0,63}$'),
    CHECK (label IS NULL OR octet_length(label) BETWEEN 1 AND 256),
    CHECK (display_role IS NULL OR octet_length(display_role) BETWEEN 1 AND 256),
    CHECK (
        (context_mode = 'NONE' AND context_last_n_turns IS NULL) OR
        (context_mode = 'LAST_N' AND context_last_n_turns BETWEEN 1 AND 3)
    ),
    CHECK ((status IN (
        'COMPLETED', 'FAILED', 'INTERRUPTED', 'CANCELLED',
        'BLOCKED_DEPENDENCY_FAILED'
    )) = (terminal_at IS NOT NULL)),
    CHECK ((status IN ('PENDING_START', 'WAITING_DEPENDENCY')) =
           (pending_reason IS NOT NULL)),
    CHECK (status IN ('PENDING_START', 'WAITING_DEPENDENCY') OR
           pending_reason IS NULL),
    CHECK (
        (status IN ('FAILED', 'INTERRUPTED', 'CANCELLED',
                    'BLOCKED_DEPENDENCY_FAILED')) =
        (terminal_reason IS NOT NULL AND terminal_public_detail IS NOT NULL)
    ),
    CHECK (
        terminal_reason IS NULL OR terminal_reason IN (
            'DEPENDENCY_FAILED', 'DEPENDENCY_RESULT_INVARIANT',
            'CHILD_START_FAILED', 'CHILD_EXECUTION_FAILED',
            'HOOK_COMPACTION_BLOCKED', 'HOST_CLOSING', 'HOST_TAKEOVER',
            'USER_CANCELLED'
        )
    ),
    CHECK (
        terminal_public_detail IS NULL OR
        octet_length(terminal_public_detail) BETWEEN 1 AND 16384
    )
);

CREATE TABLE pulsara_v3.subagent_task_dependencies (
    session_id text NOT NULL,
    task_id text NOT NULL,
    dependency_task_id text NOT NULL,
    dependency_ordinal integer NOT NULL CHECK (dependency_ordinal >= 0),
    accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (session_id, task_id, dependency_task_id),
    UNIQUE (session_id, task_id, dependency_ordinal),
    FOREIGN KEY (session_id, task_id)
        REFERENCES pulsara_v3.subagent_tasks (session_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (session_id, dependency_task_id)
        REFERENCES pulsara_v3.subagent_tasks (session_id, id) ON DELETE RESTRICT,
    CHECK (task_id <> dependency_task_id),
    CHECK (dependency_ordinal < 16)
);

CREATE TABLE pulsara_v3.turns (
    id text PRIMARY KEY,
    session_id text NOT NULL,
    workspace_id text NOT NULL,
    conversation_scope_kind text NOT NULL CHECK (conversation_scope_kind IN ('ROOT', 'SUBAGENT_TASK')),
    scope_subagent_task_id text,
    status text NOT NULL CHECK (status IN ('RUNNING', 'COMPLETED', 'INTERRUPTED')),
    initial_entry_id text NOT NULL,
    final_entry_id text,
    current_context_binding_revision_id text,
    permission_snapshot_id text NOT NULL,
    requested_permission_mode text NOT NULL CHECK (requested_permission_mode IN (
        'read-only', 'ask-permissions', 'accept-edits', 'bypass-permissions'
    )),
    effective_permission_mode text NOT NULL CHECK (effective_permission_mode IN (
        'read-only', 'ask-permissions', 'accept-edits', 'bypass-permissions'
    )),
    permission_admission_source text NOT NULL CHECK (permission_admission_source IN (
        'USER_SUBMISSION', 'SUBAGENT_COMPLETION_COMMAND', 'TERMINAL_OBSERVATION',
        'SUBAGENT_INHERITANCE', 'RUNTIME_PLAN_CONTINUATION'
    )),
    permission_overlay text NOT NULL CHECK (permission_overlay IN ('NONE', 'PLAN_READ_ONLY')),
    permission_plan_context_ordinal bigint NOT NULL CHECK (permission_plan_context_ordinal >= 0),
    permission_plan_workflow_id text,
    permission_plan_revision_at_admission bigint,
    permission_inherited_from_turn_id text,
    permission_contract_id text NOT NULL CHECK (
        permission_contract_id = 'pulsara.permission-presets.v1'
    ),
    permission_contract_fingerprint text NOT NULL CHECK (
        permission_contract_fingerprint = 'sha256:3bd08888d117e2db5a170230def4f93761b2ae9f1a642ee349515992e5f6e371'
    ),
    permission_snapshot_fingerprint text NOT NULL CHECK (
        permission_snapshot_fingerprint ~ '^sha256:[0-9a-f]{64}$'
    ),
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
    CHECK ((status = 'RUNNING') = (terminal_at IS NULL)),
    CONSTRAINT ck_turn_permission_overlay_exact CHECK (
        (permission_overlay = 'NONE'
            AND effective_permission_mode = requested_permission_mode
            AND permission_plan_workflow_id IS NULL
            AND permission_plan_revision_at_admission IS NULL) OR
        (permission_overlay = 'PLAN_READ_ONLY'
            AND conversation_scope_kind = 'ROOT'
            AND effective_permission_mode = 'read-only'
            AND permission_plan_context_ordinal >= 1
            AND permission_plan_workflow_id IS NOT NULL
            AND permission_plan_revision_at_admission >= 1)
    ),
    CHECK (
        (permission_admission_source = 'SUBAGENT_INHERITANCE'
            AND conversation_scope_kind = 'SUBAGENT_TASK'
            AND permission_overlay = 'NONE'
            AND permission_inherited_from_turn_id IS NOT NULL) OR
        (permission_admission_source IN (
                'RUNTIME_PLAN_CONTINUATION', 'TERMINAL_OBSERVATION'
            )
            AND conversation_scope_kind = 'ROOT'
            AND permission_inherited_from_turn_id IS NOT NULL) OR
        (permission_admission_source NOT IN (
                'SUBAGENT_INHERITANCE', 'RUNTIME_PLAN_CONTINUATION',
                'TERMINAL_OBSERVATION'
            ) AND conversation_scope_kind = 'ROOT'
            AND permission_inherited_from_turn_id IS NULL)
    ),
    CHECK (
        (conversation_scope_kind = 'SUBAGENT_TASK'
            AND requested_permission_mode = effective_permission_mode
            AND permission_admission_source = 'SUBAGENT_INHERITANCE'
            AND permission_overlay = 'NONE'
            AND permission_plan_workflow_id IS NULL
            AND permission_plan_revision_at_admission IS NULL) OR
        conversation_scope_kind = 'ROOT'
    )
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
        'RESOLVE_INTERACTION', 'ACCEPT_SUBAGENT_COMPLETION',
        'ENTER_PLAN', 'CANCEL_PLAN', 'FORCE_EXIT_PLAN',
        'RESOLVE_PLAN_INTERACTION', 'COMPACT_CONTEXT'
    )),
    request_schema_version text NOT NULL,
    semantic_digest text NOT NULL CHECK (semantic_digest ~ '^sha256:[0-9a-f]{64}$'),
    target_kind text NOT NULL CHECK (target_kind IN (
        'TURN', 'ENTRY', 'QUEUE_ITEM', 'INTERACTION_DECISION',
        'PLAN_WORKFLOW', 'PLAN_INTERACTION'
    )),
    target_turn_id text,
    target_entry_id text,
    target_queue_item_id text,
    target_interaction_decision_id text,
    target_plan_workflow_id text,
    target_plan_interaction_id text,
    accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (session_id, command_id),
    FOREIGN KEY (session_id) REFERENCES pulsara_v3.sessions (id) ON DELETE RESTRICT,
    CHECK (num_nonnulls(
        target_turn_id, target_entry_id, target_queue_item_id,
        target_interaction_decision_id,
        target_plan_workflow_id, target_plan_interaction_id
    ) = 1),
    CHECK (
        (target_kind = 'TURN' AND target_turn_id IS NOT NULL) OR
        (target_kind = 'ENTRY' AND target_entry_id IS NOT NULL) OR
        (target_kind = 'QUEUE_ITEM' AND target_queue_item_id IS NOT NULL) OR
        (target_kind = 'INTERACTION_DECISION' AND target_interaction_decision_id IS NOT NULL) OR
        (target_kind = 'PLAN_WORKFLOW' AND target_plan_workflow_id IS NOT NULL) OR
        (target_kind = 'PLAN_INTERACTION' AND target_plan_interaction_id IS NOT NULL)
    ),
    CHECK (
        (command_kind = 'SUBMIT_PROMPT' AND target_kind = 'TURN') OR
        (command_kind = 'STEER' AND target_kind = 'ENTRY') OR
        (command_kind IN ('QUEUE_PROMPT', 'CANCEL_PROMPT') AND target_kind = 'QUEUE_ITEM') OR
        (command_kind = 'RESOLVE_INTERACTION' AND target_kind = 'INTERACTION_DECISION') OR
        (command_kind = 'ACCEPT_SUBAGENT_COMPLETION' AND target_kind = 'ENTRY') OR
        (command_kind IN ('ENTER_PLAN', 'CANCEL_PLAN', 'FORCE_EXIT_PLAN')
            AND target_kind = 'PLAN_WORKFLOW') OR
        (command_kind = 'RESOLVE_PLAN_INTERACTION'
            AND target_kind = 'PLAN_INTERACTION') OR
        (command_kind = 'COMPACT_CONTEXT' AND target_kind = 'TURN')
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
        'ASSISTANT_TOOL_REQUEST', 'TOOL_RESULT', 'TERMINAL_OBSERVATION',
        'PLAN_CONTINUATION', 'INTER_AGENT_MESSAGE'
    )),
    conversation_scope_kind text NOT NULL CHECK (conversation_scope_kind IN ('ROOT', 'SUBAGENT_TASK')),
    scope_subagent_task_id text,
    context_binding_revision_id text,
    provider_input_through_sequence bigint,
    provider_wire_api text,
    provider_replay_disposition text,
    provider_replay_fragment_id text,
    source_subagent_task_id text,
    source_inter_agent_tool_attempt_id text,
    source_plan_workflow_id text,
    source_plan_interaction_id text,
    source_plan_handoff_kind text CHECK (source_plan_handoff_kind IN (
        'ENTERED_PLAN', 'REVISION_REQUESTED', 'APPROVED_PLAN',
        'CANCELLED_PLAN', 'FORCE_EXITED_PLAN'
    )),
    inline_content bytea,
    blob_id text,
    content_digest text NOT NULL CHECK (content_digest ~ '^sha256:[0-9a-f]{64}$'),
    content_size bigint NOT NULL CHECK (content_size >= 0),
    content_media_type text NOT NULL,
    content_codec text NOT NULL,
    accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (session_id, id),
    UNIQUE (session_id, id, provider_wire_api, provider_replay_fragment_id),
    UNIQUE (session_id, entry_sequence),
    UNIQUE (session_id, source_subagent_task_id),
    UNIQUE (session_id, source_inter_agent_tool_attempt_id),
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
    CONSTRAINT transcript_entries_tool_result_inline_ck CHECK (
        entry_kind <> 'TOOL_RESULT'
        OR (inline_content IS NOT NULL AND blob_id IS NULL)
    ),
    CHECK (inline_content IS NULL OR octet_length(inline_content) = content_size),
    CHECK (
        (entry_kind IN ('ASSISTANT_MESSAGE', 'ASSISTANT_TOOL_REQUEST')
            AND context_binding_revision_id IS NOT NULL
            AND provider_input_through_sequence IS NOT NULL
            AND provider_input_through_sequence < entry_sequence
            AND provider_wire_api IN (
                'openai_chat_completions', 'openai_responses'
            )
            AND provider_replay_disposition IN (
                'PUBLIC_SEMANTIC_ONLY', 'NATIVE_REPLAY'
            )
            AND (provider_replay_fragment_id IS NOT NULL) =
                (provider_replay_disposition = 'NATIVE_REPLAY')
            AND (provider_wire_api <> 'openai_responses'
                OR provider_replay_disposition = 'NATIVE_REPLAY'))
        OR
        (entry_kind NOT IN ('ASSISTANT_MESSAGE', 'ASSISTANT_TOOL_REQUEST')
            AND context_binding_revision_id IS NULL
            AND provider_input_through_sequence IS NULL
            AND provider_wire_api IS NULL
            AND provider_replay_disposition IS NULL
            AND provider_replay_fragment_id IS NULL)
    ),
    CHECK (
        (entry_kind = 'INTER_AGENT_MESSAGE'
            AND conversation_scope_kind = 'SUBAGENT_TASK'
            AND source_inter_agent_tool_attempt_id IS NOT NULL
            AND source_subagent_task_id IS NULL) OR
        (entry_kind = 'INTER_AGENT_MESSAGE'
            AND conversation_scope_kind = 'ROOT'
            AND source_inter_agent_tool_attempt_id IS NULL
            AND source_subagent_task_id IS NOT NULL) OR
        (entry_kind <> 'INTER_AGENT_MESSAGE'
            AND source_inter_agent_tool_attempt_id IS NULL
            AND source_subagent_task_id IS NULL)
    ),
    CHECK (
        (entry_kind = 'PLAN_CONTINUATION'
            AND conversation_scope_kind = 'ROOT'
            AND source_plan_workflow_id IS NOT NULL
            AND source_plan_handoff_kind IN (
                'ENTERED_PLAN', 'REVISION_REQUESTED', 'APPROVED_PLAN'
            )
            AND source_subagent_task_id IS NULL) OR
        (entry_kind = 'USER_MESSAGE'
            AND source_plan_workflow_id IS NOT NULL
            AND conversation_scope_kind = 'ROOT'
            AND source_plan_handoff_kind IN ('CANCELLED_PLAN', 'FORCE_EXITED_PLAN')
            AND source_subagent_task_id IS NULL) OR
        (source_plan_workflow_id IS NULL
            AND source_plan_interaction_id IS NULL
            AND source_plan_handoff_kind IS NULL)
    ),
    CHECK (
        source_plan_handoff_kind NOT IN ('REVISION_REQUESTED', 'APPROVED_PLAN')
        OR source_plan_interaction_id IS NOT NULL
    ),
    CHECK (
        source_plan_handoff_kind <> 'ENTERED_PLAN'
        OR source_plan_interaction_id IS NULL
    )
);
CREATE UNIQUE INDEX uq_pulsara_v3_plan_handoff_without_interaction
    ON pulsara_v3.transcript_entries (
        session_id, source_plan_workflow_id, source_plan_handoff_kind
    ) WHERE source_plan_workflow_id IS NOT NULL
          AND source_plan_interaction_id IS NULL;
CREATE UNIQUE INDEX uq_pulsara_v3_plan_handoff_with_interaction
    ON pulsara_v3.transcript_entries (
        session_id, source_plan_workflow_id, source_plan_handoff_kind,
        source_plan_interaction_id
    ) WHERE source_plan_workflow_id IS NOT NULL
          AND source_plan_interaction_id IS NOT NULL;
ALTER TABLE pulsara_v3.turns ADD CONSTRAINT turns_initial_entry_fk
    FOREIGN KEY (session_id, initial_entry_id)
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

CREATE TABLE pulsara_v3.provider_assistant_replay_fragments (
    id text PRIMARY KEY,
    session_id text NOT NULL,
    workspace_id text NOT NULL,
    assistant_entry_id text NOT NULL,
    wire_api text NOT NULL CHECK (wire_api IN (
        'openai_chat_completions', 'openai_responses'
    )),
    codec_kind text NOT NULL CHECK (codec_kind IN (
        'CHAT_CLOSED_REASONING_FIELDS', 'RESPONSES_EXACT_OUTPUT_ITEMS'
    )),
    provider_replay_contract_fingerprint text NOT NULL,
    replay_target_fingerprint text NOT NULL,
    public_projection_fingerprint text NOT NULL,
    payload_bytes bytea NOT NULL,
    payload_digest text NOT NULL,
    payload_size bigint NOT NULL,
    item_count integer NOT NULL,
    fragment_fingerprint text NOT NULL,
    accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (session_id, id),
    UNIQUE (session_id, assistant_entry_id),
    UNIQUE (session_id, assistant_entry_id, id),
    UNIQUE (session_id, assistant_entry_id, wire_api, id),
    FOREIGN KEY (session_id, workspace_id)
        REFERENCES pulsara_v3.sessions (id, workspace_id) ON DELETE RESTRICT,
    CHECK ((wire_api = 'openai_chat_completions') =
           (codec_kind = 'CHAT_CLOSED_REASONING_FIELDS')),
    CHECK (payload_size = octet_length(payload_bytes)),
    CHECK (payload_size BETWEEN 2 AND 16777216),
    CHECK (
        (wire_api = 'openai_chat_completions' AND item_count = 1)
        OR
        (wire_api = 'openai_responses' AND item_count BETWEEN 1 AND 4096)
    ),
    CHECK (payload_digest ~ '^sha256:[0-9a-f]{64}$'),
    CHECK (provider_replay_contract_fingerprint ~ '^sha256:[0-9a-f]{64}$'),
    CHECK (replay_target_fingerprint ~ '^sha256:[0-9a-f]{64}$'),
    CHECK (public_projection_fingerprint ~ '^sha256:[0-9a-f]{64}$'),
    CHECK (fragment_fingerprint ~ '^sha256:[0-9a-f]{64}$')
);

ALTER TABLE pulsara_v3.transcript_entries
ADD CONSTRAINT transcript_entries_provider_replay_fk
FOREIGN KEY (
    session_id, id, provider_wire_api, provider_replay_fragment_id
)
REFERENCES pulsara_v3.provider_assistant_replay_fragments (
    session_id, assistant_entry_id, wire_api, id
)
DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE pulsara_v3.provider_assistant_replay_fragments
ADD CONSTRAINT provider_replay_assistant_entry_fk
FOREIGN KEY (session_id, assistant_entry_id, wire_api, id)
REFERENCES pulsara_v3.transcript_entries (
    session_id, id, provider_wire_api, provider_replay_fragment_id
)
DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE pulsara_v3.tool_execution_attempts (
    id text PRIMARY KEY,
    session_id text NOT NULL,
    assistant_entry_id text NOT NULL,
    tool_call_id text NOT NULL,
    authorization_kind text NOT NULL,
    authorization_reference text NOT NULL,
    permission_snapshot_fingerprint text NOT NULL CHECK (
        permission_snapshot_fingerprint ~ '^sha256:[0-9a-f]{64}$'
    ),
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
    workspace_id text NOT NULL,
    tool_call_entry_id text NOT NULL,
    tool_call_id text NOT NULL,
    attempt_id text,
    result_origin_kind text NOT NULL CHECK (result_origin_kind IN (
        'PHYSICAL_ATTEMPT', 'POLICY_NO_ATTEMPT', 'PLAN_CONTROL'
    )),
    control_plan_workflow_id text,
    control_plan_interaction_id text,
    permission_snapshot_fingerprint text NOT NULL CHECK (
        permission_snapshot_fingerprint ~ '^sha256:[0-9a-f]{64}$'
    ),
    result_entry_id text NOT NULL,
    result_state text NOT NULL CHECK (result_state IN (
        'SUCCESS', 'APPLICATION_ERROR', 'SYSTEM_ERROR', 'CANCELLED',
        'INVALID_ARGUMENTS', 'PERMISSION_DENIED', 'TOOL_UNAVAILABLE',
        'CANCELLED_BEFORE_DISPATCH'
    )),
    observed_at timestamptz NOT NULL,
    observation_duration_microseconds bigint,
    observation_origin_kind text NOT NULL CHECK (observation_origin_kind IN (
        'BUILTIN', 'TERMINAL_PROCESS', 'MCP_REMOTE',
        'POLICY', 'PLAN_CONTROL', 'CUSTOM_OR_UNKNOWN'
    )),
    tool_reported_duration_microseconds bigint,
    output_artifact_disposition text NOT NULL DEFAULT 'NOT_REQUIRED'
        CHECK (output_artifact_disposition IN (
            'NOT_REQUIRED', 'AVAILABLE', 'INCOMPLETE', 'UNAVAILABLE'
        )),
    output_artifact_id text,
    output_artifact_blob_id text,
    output_source_coverage text NOT NULL DEFAULT 'COMPLETE'
        CHECK (output_source_coverage IN ('COMPLETE', 'RETAINED_SNAPSHOT')),
    output_display_kind text NOT NULL DEFAULT 'COMPLETE'
        CHECK (output_display_kind IN ('COMPLETE', 'HEAD_TAIL')),
    output_source_coverage_reason text
        CHECK (output_source_coverage_reason IN (
            'TERMINAL_RETENTION_GAP', 'TERMINAL_SANITIZER_UNAVAILABLE'
        )),
    output_artifact_unavailability_reason text
        CHECK (output_artifact_unavailability_reason IN (
            'ARTIFACT_CONTENT_TOO_LARGE',
            'BLOB_PUBLICATION_FAILED',
            'BLOB_PUBLICATION_UNCONFIRMED'
        )),
    model_visible_memory_fact_ids text[] NOT NULL DEFAULT '{}'::text[] CHECK (
        cardinality(model_visible_memory_fact_ids) <= 50
        AND octet_length(array_to_json(model_visible_memory_fact_ids)::text) <= 8192
    ),
    accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (session_id, id),
    UNIQUE (session_id, tool_call_entry_id, tool_call_id),
    UNIQUE (session_id, result_entry_id),
    FOREIGN KEY (session_id, workspace_id)
        REFERENCES pulsara_v3.sessions (id, workspace_id) ON DELETE RESTRICT,
    FOREIGN KEY (session_id, tool_call_entry_id, tool_call_id)
        REFERENCES pulsara_v3.assistant_message_blocks (session_id, assistant_entry_id, tool_call_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (session_id, attempt_id, tool_call_entry_id, tool_call_id)
        REFERENCES pulsara_v3.tool_execution_attempts (
            session_id, id, assistant_entry_id, tool_call_id
        ) ON DELETE RESTRICT,
    FOREIGN KEY (session_id, result_entry_id)
        REFERENCES pulsara_v3.transcript_entries (session_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (output_artifact_blob_id, workspace_id)
        REFERENCES pulsara_v3.blobs (id, workspace_id) ON DELETE RESTRICT,
    CHECK (
        (result_origin_kind = 'POLICY_NO_ATTEMPT'
            AND attempt_id IS NULL
            AND control_plan_workflow_id IS NULL
            AND control_plan_interaction_id IS NULL
            AND result_state IN (
            'INVALID_ARGUMENTS', 'PERMISSION_DENIED', 'TOOL_UNAVAILABLE', 'CANCELLED_BEFORE_DISPATCH'
        )) OR
        (result_origin_kind = 'PHYSICAL_ATTEMPT'
            AND attempt_id IS NOT NULL
            AND control_plan_workflow_id IS NULL
            AND control_plan_interaction_id IS NULL
            AND result_state IN (
            'SUCCESS', 'APPLICATION_ERROR', 'SYSTEM_ERROR', 'CANCELLED'
        )) OR
        (result_origin_kind = 'PLAN_CONTROL'
            AND attempt_id IS NULL
            AND num_nonnulls(control_plan_workflow_id, control_plan_interaction_id) = 1
            AND result_state IN ('SUCCESS', 'APPLICATION_ERROR')
        )
    ),
    CHECK (
        observation_duration_microseconds IS NULL OR (
            result_origin_kind = 'PHYSICAL_ATTEMPT'
            AND observation_duration_microseconds >= 0
            AND observation_duration_microseconds <= 31536000000000
        )
    ),
    CHECK (
        tool_reported_duration_microseconds IS NULL OR (
            result_origin_kind = 'PHYSICAL_ATTEMPT'
            AND tool_reported_duration_microseconds >= 0
            AND tool_reported_duration_microseconds <= 31536000000000
        )
    ),
    CHECK (
        (result_origin_kind = 'POLICY_NO_ATTEMPT'
            AND observation_origin_kind = 'POLICY'
            AND observation_duration_microseconds IS NULL
            AND tool_reported_duration_microseconds IS NULL)
        OR
        (result_origin_kind = 'PLAN_CONTROL'
            AND observation_origin_kind = 'PLAN_CONTROL'
            AND observation_duration_microseconds IS NULL
            AND tool_reported_duration_microseconds IS NULL)
        OR
        (result_origin_kind = 'PHYSICAL_ATTEMPT'
            AND observation_origin_kind IN (
                'BUILTIN', 'TERMINAL_PROCESS', 'MCP_REMOTE',
                'CUSTOM_OR_UNKNOWN'
            ))
    ),
    CHECK (
        (output_source_coverage = 'COMPLETE'
            AND output_source_coverage_reason IS NULL) OR
        (output_source_coverage = 'RETAINED_SNAPSHOT'
            AND output_source_coverage_reason IN (
                'TERMINAL_RETENTION_GAP', 'TERMINAL_SANITIZER_UNAVAILABLE'
            ))
    ),
    CHECK (
        (output_artifact_disposition = 'NOT_REQUIRED'
            AND output_artifact_id IS NULL
            AND output_artifact_blob_id IS NULL
            AND output_artifact_unavailability_reason IS NULL
            AND output_source_coverage = 'COMPLETE') OR
        (output_artifact_disposition = 'AVAILABLE'
            AND output_artifact_id IS NOT NULL
            AND output_artifact_blob_id IS NOT NULL
            AND output_artifact_unavailability_reason IS NULL
            AND output_source_coverage = 'COMPLETE') OR
        (output_artifact_disposition = 'INCOMPLETE'
            AND output_artifact_id IS NOT NULL
            AND output_artifact_blob_id IS NOT NULL
            AND output_artifact_unavailability_reason IS NULL
            AND output_source_coverage = 'RETAINED_SNAPSHOT') OR
        (output_artifact_disposition = 'UNAVAILABLE'
            AND output_artifact_id IS NULL
            AND output_artifact_blob_id IS NULL
            AND output_artifact_unavailability_reason IS NOT NULL)
    )
);
CREATE UNIQUE INDEX uq_pulsara_v3_tool_result_output_artifact_id
    ON pulsara_v3.tool_results (output_artifact_id)
    WHERE output_artifact_id IS NOT NULL;

CREATE TABLE pulsara_v3.prompt_queue_items (
    id text PRIMARY KEY,
    session_id text NOT NULL,
    workspace_id text NOT NULL,
    queue_sequence bigint NOT NULL CHECK (queue_sequence >= 1),
    command_id text NOT NULL,
    client_submission_id text NOT NULL,
    delivery_mode text NOT NULL CHECK (delivery_mode IN ('NEW_TURN', 'STEER_ACTIVE_TURN')),
    target_turn_id text,
    permission_snapshot_id text,
    requested_permission_mode text CHECK (requested_permission_mode IN (
        'read-only', 'ask-permissions', 'accept-edits', 'bypass-permissions'
    )),
    effective_permission_mode text CHECK (effective_permission_mode IN (
        'read-only', 'ask-permissions', 'accept-edits', 'bypass-permissions'
    )),
    permission_admission_source text CHECK (permission_admission_source IN (
        'USER_SUBMISSION', 'SUBAGENT_COMPLETION_COMMAND', 'TERMINAL_OBSERVATION',
        'SUBAGENT_INHERITANCE', 'RUNTIME_PLAN_CONTINUATION'
    )),
    permission_overlay text CHECK (permission_overlay IN ('NONE', 'PLAN_READ_ONLY')),
    permission_plan_context_ordinal bigint CHECK (permission_plan_context_ordinal >= 0),
    permission_plan_workflow_id text,
    permission_plan_revision_at_admission bigint,
    permission_inherited_from_turn_id text,
    permission_contract_id text CHECK (
        permission_contract_id = 'pulsara.permission-presets.v1'
    ),
    permission_contract_fingerprint text CHECK (
        permission_contract_fingerprint = 'sha256:3bd08888d117e2db5a170230def4f93761b2ae9f1a642ee349515992e5f6e371'
    ),
    permission_snapshot_fingerprint text CHECK (
        permission_snapshot_fingerprint ~ '^sha256:[0-9a-f]{64}$'
    ),
    pending_plan_handoff_workflow_id text,
    pending_plan_handoff_interaction_id text,
    pending_plan_handoff_kind text CHECK (pending_plan_handoff_kind IN (
        'CANCELLED_PLAN', 'FORCE_EXITED_PLAN'
    )),
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
    CHECK ((status = 'CONSUMED') = (consumed_entry_id IS NOT NULL)),
    CHECK (
        (delivery_mode = 'STEER_ACTIVE_TURN'
            AND permission_snapshot_id IS NULL
            AND requested_permission_mode IS NULL
            AND effective_permission_mode IS NULL
            AND permission_admission_source IS NULL
            AND permission_overlay IS NULL
            AND permission_plan_context_ordinal IS NULL
            AND permission_contract_id IS NULL
            AND permission_contract_fingerprint IS NULL
            AND permission_snapshot_fingerprint IS NULL) OR
        (delivery_mode = 'NEW_TURN'
            AND permission_snapshot_id IS NOT NULL
            AND requested_permission_mode IS NOT NULL
            AND effective_permission_mode IS NOT NULL
            AND permission_admission_source = 'USER_SUBMISSION'
            AND permission_overlay IS NOT NULL
            AND permission_plan_context_ordinal IS NOT NULL
            AND permission_contract_id IS NOT NULL
            AND permission_contract_fingerprint IS NOT NULL
            AND permission_snapshot_fingerprint IS NOT NULL)
    ),
    CONSTRAINT ck_prompt_queue_permission_overlay_exact CHECK (
        (permission_overlay IS NULL) OR
        (permission_overlay = 'NONE'
            AND effective_permission_mode = requested_permission_mode
            AND permission_plan_workflow_id IS NULL
            AND permission_plan_revision_at_admission IS NULL) OR
        (permission_overlay = 'PLAN_READ_ONLY'
            AND effective_permission_mode = 'read-only'
            AND permission_plan_context_ordinal >= 1
            AND permission_plan_workflow_id IS NOT NULL
            AND permission_plan_revision_at_admission >= 1)
    ),
    CHECK (
        (pending_plan_handoff_kind IS NULL
            AND pending_plan_handoff_workflow_id IS NULL
            AND pending_plan_handoff_interaction_id IS NULL) OR
        (pending_plan_handoff_kind IS NOT NULL
            AND pending_plan_handoff_workflow_id IS NOT NULL
            AND delivery_mode = 'NEW_TURN')
    )
);
CREATE UNIQUE INDEX uq_pulsara_v3_queue_plan_handoff_claim
    ON pulsara_v3.prompt_queue_items (
        session_id, pending_plan_handoff_workflow_id
    ) WHERE pending_plan_handoff_workflow_id IS NOT NULL;

CREATE TABLE pulsara_v3.interaction_decisions (
    id text PRIMARY KEY,
    session_id text NOT NULL,
    command_id text,
    subject_kind text NOT NULL CHECK (subject_kind IN ('TOOL_CALL', 'MCP_INPUT')),
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
    permission_snapshot_fingerprint text NOT NULL CHECK (
        permission_snapshot_fingerprint ~ '^sha256:[0-9a-f]{64}$'
    ),
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
        (subject_kind = 'MCP_INPUT' AND subject_turn_id IS NOT NULL)
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

CREATE TABLE pulsara_v3.plan_workflows (
    id text PRIMARY KEY,
    session_id text NOT NULL,
    workspace_id text NOT NULL,
    workflow_ordinal bigint NOT NULL CHECK (workflow_ordinal >= 1),
    status text NOT NULL CHECK (status IN (
        'ACTIVE', 'APPROVED', 'CANCELLED', 'FORCE_EXITED'
    )),
    entered_by text NOT NULL CHECK (entered_by IN ('USER', 'AGENT')),
    entry_reason text NOT NULL CHECK (octet_length(entry_reason) <= 4096),
    entry_command_id text,
    entry_turn_id text,
    entry_assistant_entry_id text,
    entry_tool_call_id text,
    resume_permission_mode text NOT NULL CHECK (resume_permission_mode IN (
        'read-only', 'ask-permissions', 'accept-edits', 'bypass-permissions'
    )),
    permission_contract_id text NOT NULL CHECK (
        permission_contract_id = 'pulsara.permission-presets.v1'
    ),
    permission_contract_fingerprint text NOT NULL CHECK (
        permission_contract_fingerprint = 'sha256:3bd08888d117e2db5a170230def4f93761b2ae9f1a642ee349515992e5f6e371'
    ),
    workflow_revision bigint NOT NULL CHECK (workflow_revision >= 1),
    accepted_plan_interaction_id text,
    accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    terminal_at timestamptz,
    UNIQUE (session_id, id),
    UNIQUE (session_id, workflow_ordinal),
    FOREIGN KEY (session_id, workspace_id)
        REFERENCES pulsara_v3.sessions (id, workspace_id) ON DELETE RESTRICT,
    FOREIGN KEY (session_id, entry_command_id)
        REFERENCES pulsara_v3.session_commands (session_id, command_id)
        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (session_id, entry_turn_id)
        REFERENCES pulsara_v3.turns (session_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (session_id, entry_assistant_entry_id, entry_tool_call_id)
        REFERENCES pulsara_v3.assistant_message_blocks (
            session_id, assistant_entry_id, tool_call_id
        ) ON DELETE RESTRICT,
    CHECK ((status = 'ACTIVE') = (terminal_at IS NULL)),
    CHECK ((status = 'APPROVED') = (accepted_plan_interaction_id IS NOT NULL)),
    CHECK (
        (entered_by = 'USER' AND entry_command_id IS NOT NULL
            AND entry_turn_id IS NULL AND entry_assistant_entry_id IS NULL
            AND entry_tool_call_id IS NULL) OR
        (entered_by = 'AGENT' AND entry_command_id IS NULL
            AND entry_turn_id IS NOT NULL AND entry_assistant_entry_id IS NOT NULL
            AND entry_tool_call_id IS NOT NULL)
    )
);
CREATE UNIQUE INDEX uq_pulsara_v3_active_plan_workflow
    ON pulsara_v3.plan_workflows (session_id) WHERE status = 'ACTIVE';

CREATE TABLE pulsara_v3.plan_interactions (
    id text PRIMARY KEY,
    session_id text NOT NULL,
    workspace_id text NOT NULL,
    plan_workflow_id text NOT NULL,
    interaction_ordinal bigint NOT NULL CHECK (interaction_ordinal >= 1),
    kind text NOT NULL CHECK (kind IN ('QUESTION', 'DRAFT_REVIEW')),
    status text NOT NULL CHECK (status IN (
        'OPEN', 'ANSWERED', 'APPROVED', 'REVISION_REQUESTED',
        'CANCELLED', 'ABORTED'
    )),
    origin_turn_id text NOT NULL,
    assistant_entry_id text NOT NULL,
    tool_call_id text NOT NULL,
    request_contract_id text NOT NULL,
    request_contract_version text NOT NULL,
    request_contract_fingerprint text NOT NULL CHECK (
        request_contract_fingerprint ~ '^sha256:[0-9a-f]{64}$'
    ),
    request_semantic_digest text NOT NULL CHECK (
        request_semantic_digest ~ '^sha256:[0-9a-f]{64}$'
    ),
    control_tool_result_id text,
    resolution_command_id text,
    response_semantic_digest text CHECK (
        response_semantic_digest ~ '^sha256:[0-9a-f]{64}$'
    ),
    decision_continuation_entry_id text,
    answer_kind text CHECK (answer_kind IN ('OPTION', 'FREE_TEXT')),
    selected_option_ordinal integer CHECK (selected_option_ordinal >= 0),
    feedback_present boolean NOT NULL DEFAULT false,
    accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    resolved_at timestamptz,
    aborted_at timestamptz,
    UNIQUE (session_id, id),
    UNIQUE (plan_workflow_id, interaction_ordinal),
    UNIQUE (session_id, assistant_entry_id, tool_call_id),
    FOREIGN KEY (session_id, workspace_id)
        REFERENCES pulsara_v3.sessions (id, workspace_id) ON DELETE RESTRICT,
    FOREIGN KEY (session_id, plan_workflow_id)
        REFERENCES pulsara_v3.plan_workflows (session_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (session_id, origin_turn_id)
        REFERENCES pulsara_v3.turns (session_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (session_id, assistant_entry_id, tool_call_id)
        REFERENCES pulsara_v3.assistant_message_blocks (
            session_id, assistant_entry_id, tool_call_id
        ) ON DELETE RESTRICT,
    FOREIGN KEY (session_id, control_tool_result_id)
        REFERENCES pulsara_v3.tool_results (session_id, id)
        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (session_id, resolution_command_id)
        REFERENCES pulsara_v3.session_commands (session_id, command_id)
        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (session_id, decision_continuation_entry_id)
        REFERENCES pulsara_v3.transcript_entries (session_id, id)
        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    CHECK (
        (kind = 'QUESTION' AND status IN ('OPEN', 'ANSWERED', 'ABORTED')) OR
        (kind = 'DRAFT_REVIEW' AND status IN (
            'OPEN', 'APPROVED', 'REVISION_REQUESTED', 'CANCELLED', 'ABORTED'
        ))
    ),
    CHECK (
        (kind = 'QUESTION'
            AND request_contract_id = 'pulsara.workflow.ask_plan_question'
            AND request_contract_version = 'v1'
            AND request_contract_fingerprint = 'sha256:d19178a8bc3eaa69f03ce3151b1803b760be6afa55f5f58f489e58e45d5420b9') OR
        (kind = 'DRAFT_REVIEW'
            AND request_contract_id = 'pulsara.workflow.exit_plan'
            AND request_contract_version = 'v1'
            AND request_contract_fingerprint = 'sha256:98849b9f64ec6170cb509cdc9c4ff1292f99c0b98c657d518117340466dab336')
    ),
    CHECK (
        (status = 'OPEN' AND resolution_command_id IS NULL
            AND response_semantic_digest IS NULL
            AND resolved_at IS NULL AND aborted_at IS NULL) OR
        (status = 'ABORTED' AND resolution_command_id IS NULL
            AND response_semantic_digest IS NULL
            AND resolved_at IS NULL AND aborted_at IS NOT NULL) OR
        (status NOT IN ('OPEN', 'ABORTED') AND resolution_command_id IS NOT NULL
            AND response_semantic_digest IS NOT NULL
            AND resolved_at IS NOT NULL AND aborted_at IS NULL)
    ),
    CHECK (
        (kind = 'QUESTION' AND status = 'ANSWERED'
            AND control_tool_result_id IS NOT NULL AND answer_kind IS NOT NULL
            AND ((answer_kind = 'OPTION' AND selected_option_ordinal IS NOT NULL)
                OR (answer_kind = 'FREE_TEXT' AND selected_option_ordinal IS NULL))) OR
        (kind = 'QUESTION' AND status IN ('OPEN', 'ABORTED')
            AND control_tool_result_id IS NULL AND answer_kind IS NULL
            AND selected_option_ordinal IS NULL) OR
        (kind = 'DRAFT_REVIEW' AND control_tool_result_id IS NOT NULL
            AND answer_kind IS NULL AND selected_option_ordinal IS NULL)
    ),
    CHECK (
        (status IN ('APPROVED', 'REVISION_REQUESTED')
            AND decision_continuation_entry_id IS NOT NULL) OR
        (status NOT IN ('APPROVED', 'REVISION_REQUESTED')
            AND decision_continuation_entry_id IS NULL)
    ),
    CHECK (kind = 'DRAFT_REVIEW' OR feedback_present = false),
    CHECK (status = 'REVISION_REQUESTED' OR feedback_present = false)
);
CREATE UNIQUE INDEX uq_pulsara_v3_open_plan_interaction
    ON pulsara_v3.plan_interactions (plan_workflow_id) WHERE status = 'OPEN';

ALTER TABLE pulsara_v3.plan_workflows ADD CONSTRAINT plan_workflows_accepted_interaction_fk
    FOREIGN KEY (session_id, accepted_plan_interaction_id)
    REFERENCES pulsara_v3.plan_interactions (session_id, id)
    ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE pulsara_v3.turns ADD CONSTRAINT turns_permission_plan_workflow_fk
    FOREIGN KEY (session_id, permission_plan_workflow_id)
    REFERENCES pulsara_v3.plan_workflows (session_id, id) ON DELETE RESTRICT;
ALTER TABLE pulsara_v3.turns ADD CONSTRAINT turns_permission_inherited_turn_fk
    FOREIGN KEY (session_id, permission_inherited_from_turn_id)
    REFERENCES pulsara_v3.turns (session_id, id) ON DELETE RESTRICT;
ALTER TABLE pulsara_v3.transcript_entries ADD CONSTRAINT transcript_entries_plan_workflow_fk
    FOREIGN KEY (session_id, source_plan_workflow_id)
    REFERENCES pulsara_v3.plan_workflows (session_id, id) ON DELETE RESTRICT;
ALTER TABLE pulsara_v3.transcript_entries ADD CONSTRAINT transcript_entries_plan_interaction_fk
    FOREIGN KEY (session_id, source_plan_interaction_id)
    REFERENCES pulsara_v3.plan_interactions (session_id, id) ON DELETE RESTRICT;
CREATE UNIQUE INDEX uq_pulsara_v3_entry_plan_terminal_handoff_claim
    ON pulsara_v3.transcript_entries (session_id, source_plan_workflow_id)
    WHERE source_plan_handoff_kind IN ('CANCELLED_PLAN', 'FORCE_EXITED_PLAN');
ALTER TABLE pulsara_v3.tool_results ADD CONSTRAINT tool_results_plan_workflow_fk
    FOREIGN KEY (session_id, control_plan_workflow_id)
    REFERENCES pulsara_v3.plan_workflows (session_id, id)
    ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE pulsara_v3.tool_results ADD CONSTRAINT tool_results_plan_interaction_fk
    FOREIGN KEY (session_id, control_plan_interaction_id)
    REFERENCES pulsara_v3.plan_interactions (session_id, id)
    ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE pulsara_v3.prompt_queue_items ADD CONSTRAINT queue_permission_plan_workflow_fk
    FOREIGN KEY (session_id, permission_plan_workflow_id)
    REFERENCES pulsara_v3.plan_workflows (session_id, id) ON DELETE RESTRICT;
ALTER TABLE pulsara_v3.prompt_queue_items ADD CONSTRAINT queue_handoff_plan_workflow_fk
    FOREIGN KEY (session_id, pending_plan_handoff_workflow_id)
    REFERENCES pulsara_v3.plan_workflows (session_id, id) ON DELETE RESTRICT;
ALTER TABLE pulsara_v3.prompt_queue_items ADD CONSTRAINT queue_handoff_plan_interaction_fk
    FOREIGN KEY (session_id, pending_plan_handoff_interaction_id)
    REFERENCES pulsara_v3.plan_interactions (session_id, id) ON DELETE RESTRICT;

CREATE TABLE pulsara_v3.subagent_task_children (
    id text PRIMARY KEY,
    session_id text NOT NULL,
    task_id text NOT NULL,
    child_kind text NOT NULL CHECK (child_kind IN ('MESSAGE', 'RESULT')),
    child_ordinal integer NOT NULL CHECK (child_ordinal >= 0),
    entry_id text NOT NULL,
    result_source text CHECK (result_source IN ('EXPLICIT', 'INFERRED')),
    summary text,
    output_preview text,
    diagnostics jsonb,
    result_fingerprint text,
    accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (session_id, id),
    UNIQUE (session_id, id, child_kind),
    UNIQUE (task_id, child_ordinal),
    FOREIGN KEY (session_id, task_id)
        REFERENCES pulsara_v3.subagent_tasks (session_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (session_id, entry_id)
        REFERENCES pulsara_v3.transcript_entries (session_id, id) ON DELETE RESTRICT,
    CHECK (
        (child_kind = 'MESSAGE' AND result_source IS NULL AND summary IS NULL
            AND output_preview IS NULL AND diagnostics IS NULL
            AND result_fingerprint IS NULL) OR
        (child_kind = 'RESULT' AND result_source IS NOT NULL
            AND summary IS NOT NULL AND octet_length(summary) BETWEEN 1 AND 16384
            AND (output_preview IS NULL OR octet_length(output_preview) <= 32768)
            AND diagnostics IS NOT NULL AND jsonb_typeof(diagnostics) = 'array'
            AND jsonb_array_length(diagnostics) <= 32
            AND octet_length(diagnostics::text) <= 65536
            AND result_fingerprint ~ '^sha256:[0-9a-f]{64}$')
    )
);

CREATE UNIQUE INDEX uq_pulsara_v3_subagent_task_terminal_result
    ON pulsara_v3.subagent_task_children (session_id, task_id)
    WHERE child_kind = 'RESULT';

ALTER TABLE pulsara_v3.transcript_entries ADD CONSTRAINT transcript_entries_source_subagent_task_fk
    FOREIGN KEY (session_id, source_subagent_task_id)
    REFERENCES pulsara_v3.subagent_tasks (session_id, id) ON DELETE RESTRICT
    DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE pulsara_v3.transcript_entries
ADD CONSTRAINT transcript_entries_source_inter_agent_attempt_fk
    FOREIGN KEY (session_id, source_inter_agent_tool_attempt_id)
    REFERENCES pulsara_v3.tool_execution_attempts (session_id, id)
    ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE pulsara_v3.memory_candidates (
    id text PRIMARY KEY,
    memory_domain_id text NOT NULL,
    origin_workspace_id text NOT NULL,
    origin_session_id text NOT NULL,
    producer_kind text NOT NULL CHECK (producer_kind IN (
        'MAIN_AGENT_REMEMBER', 'CHEAP_HINT_REFLECTION'
    )),
    producer_entry_id text,
    producer_tool_call_id text,
    trigger_user_entry_id text,
    producer_candidate_ordinal integer,
    scope_kind text NOT NULL CHECK (scope_kind IN ('USER', 'WORKSPACE')),
    scope_id text NOT NULL,
    kind_hint text NOT NULL CHECK (kind_hint IN (
        'AUTO', 'FACT', 'USER_PROFILE', 'RESPONSE_PREFERENCE',
        'ACTION_RULE', 'DECISION'
    )),
    statement text NOT NULL CHECK (
        octet_length(statement) BETWEEN 1 AND 8192
    ),
    applies_when text CHECK (
        applies_when IS NULL OR octet_length(applies_when) BETWEEN 1 AND 4096
    ),
    do_not_apply_when text[] NOT NULL DEFAULT '{}'::text[] CHECK (
        cardinality(do_not_apply_when) <= 8
    ),
    candidate_acceptance_digest text NOT NULL UNIQUE CHECK (
        candidate_acceptance_digest ~ '^sha256:[0-9a-f]{64}$'
    ),
    model_visible_memory_provenance_disposition text NOT NULL CHECK (
        model_visible_memory_provenance_disposition IN ('COMPLETE', 'OVERFLOW')
    ),
    model_visible_memory_fact_ids text[] NOT NULL DEFAULT '{}'::text[] CHECK (
        cardinality(model_visible_memory_fact_ids) <= 128
        AND octet_length(array_to_json(model_visible_memory_fact_ids)::text) <= 16384
        AND (model_visible_memory_provenance_disposition = 'COMPLETE'
             OR cardinality(model_visible_memory_fact_ids) = 0)
    ),
    status text NOT NULL CHECK (status IN (
        'PENDING', 'PROCESSING', 'ACCEPTED', 'APPLIED_TO_EXISTING',
        'SKIPPED', 'ABANDONED'
    )),
    decision_kind text CHECK (decision_kind IN (
        'SKIP', 'ACCEPT', 'ACCEPT_AND_SUPERSEDE', 'ACCEPT_AND_CONTRADICT'
    )),
    final_kind text CHECK (final_kind IN (
        'FACT', 'USER_PROFILE', 'RESPONSE_PREFERENCE', 'ACTION_RULE', 'DECISION'
    )),
    decision_reason_code text CHECK (decision_reason_code IN (
        'DUPLICATE', 'TEMPORARY_OR_EPHEMERAL', 'LOW_VALUE',
        'MULTI_ATOM_STATEMENT', 'USER_PROFILE_SCOPE_OR_KIND_MISMATCH',
        'UNSAFE_RESPONSE_PREFERENCE', 'UNSUPPORTED_STRUCTURE',
        'RECALLED_MEMORY_ECHO', 'MODEL_VISIBLE_MEMORY_PROVENANCE_OVERFLOW',
        'RESPONSE_PREFERENCE_CAPACITY_EXCEEDED',
        'SKIPPED_DUPLICATE', 'SKIPPED_DUPLICATE_BASIS_UNAPPLIED',
        'SKIPPED_DUPLICATE_RELATION_ALREADY_PRESENT',
        'ABANDONED_GOVERNANCE_FAILURE', 'ABANDONED_INVALID_OUTPUT',
        'ABANDONED_KIND_CONFLICT', 'ABANDONED_REFERENCE_DRIFT',
        'ABANDONED_RELATION_CONTRACT_CONFLICT', 'ABANDONED_TARGET_DRIFT',
        'ABANDONED_RETRIEVAL_INPUT_UNSUPPORTED'
    )),
    decision_public_summary text CHECK (
        decision_public_summary IS NULL OR octet_length(decision_public_summary) <= 2048
    ),
    related_target_fact_id text,
    duplicate_winner_fact_id text,
    accepted_fact_id text,
    applied_existing_fact_id text,
    processing_started_at timestamptz,
    decided_at timestamptz,
    accepted_fact_at timestamptz,
    accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (id, origin_session_id),
    UNIQUE (id, memory_domain_id),
    UNIQUE (id, memory_domain_id, scope_kind, scope_id),
    UNIQUE (id, accepted_fact_id),
    FOREIGN KEY (origin_session_id, origin_workspace_id, memory_domain_id)
        REFERENCES pulsara_v3.sessions (id, workspace_id, memory_domain_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (origin_session_id, producer_entry_id)
        REFERENCES pulsara_v3.transcript_entries (session_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (origin_session_id, producer_entry_id, producer_tool_call_id)
        REFERENCES pulsara_v3.assistant_message_blocks (
            session_id, assistant_entry_id, tool_call_id
        ) ON DELETE RESTRICT,
    FOREIGN KEY (origin_session_id, trigger_user_entry_id)
        REFERENCES pulsara_v3.transcript_entries (session_id, id) ON DELETE RESTRICT,
    CHECK (
        (producer_kind = 'MAIN_AGENT_REMEMBER'
            AND producer_entry_id IS NOT NULL
            AND producer_tool_call_id IS NOT NULL
            AND trigger_user_entry_id IS NULL
            AND producer_candidate_ordinal IS NULL)
        OR
        (producer_kind = 'CHEAP_HINT_REFLECTION'
            AND producer_entry_id IS NULL
            AND producer_tool_call_id IS NULL
            AND trigger_user_entry_id IS NOT NULL
            AND producer_candidate_ordinal BETWEEN 0 AND 3)
    ),
    CHECK (
        (scope_kind = 'USER' AND scope_id = 'ctx:user') OR
        (scope_kind = 'WORKSPACE'
            AND scope_id = origin_workspace_id
            AND scope_id ~ '^ctx:workspace/[a-z0-9][a-z0-9._-]{0,127}$')
    ),
    CHECK (kind_hint <> 'USER_PROFILE' OR scope_kind = 'USER'),
    CHECK (
        (status = 'PENDING' AND processing_started_at IS NULL AND decided_at IS NULL
            AND decision_kind IS NULL AND final_kind IS NULL
            AND decision_reason_code IS NULL AND decision_public_summary IS NULL
            AND related_target_fact_id IS NULL AND duplicate_winner_fact_id IS NULL
            AND accepted_fact_id IS NULL AND accepted_fact_at IS NULL
            AND applied_existing_fact_id IS NULL)
        OR
        (status = 'PROCESSING' AND processing_started_at IS NOT NULL AND decided_at IS NULL
            AND decision_kind IS NULL AND final_kind IS NULL
            AND decision_reason_code IS NULL AND decision_public_summary IS NULL
            AND related_target_fact_id IS NULL AND duplicate_winner_fact_id IS NULL
            AND accepted_fact_id IS NULL AND accepted_fact_at IS NULL
            AND applied_existing_fact_id IS NULL)
        OR
        (status = 'ACCEPTED' AND processing_started_at IS NOT NULL AND decided_at IS NOT NULL
            AND decision_kind IN ('ACCEPT', 'ACCEPT_AND_SUPERSEDE', 'ACCEPT_AND_CONTRADICT')
            AND final_kind IS NOT NULL AND accepted_fact_id IS NOT NULL
            AND accepted_fact_at IS NOT NULL AND applied_existing_fact_id IS NULL
            AND decision_reason_code IS NULL AND duplicate_winner_fact_id IS NULL
            AND ((decision_kind = 'ACCEPT' AND related_target_fact_id IS NULL)
                OR (decision_kind IN ('ACCEPT_AND_SUPERSEDE', 'ACCEPT_AND_CONTRADICT')
                    AND related_target_fact_id IS NOT NULL)))
        OR
        (status = 'APPLIED_TO_EXISTING' AND processing_started_at IS NOT NULL
            AND decided_at IS NOT NULL AND decision_kind IN (
                'ACCEPT_AND_SUPERSEDE', 'ACCEPT_AND_CONTRADICT'
            ) AND final_kind IS NOT NULL AND accepted_fact_id IS NULL
            AND accepted_fact_at IS NULL AND applied_existing_fact_id IS NOT NULL
            AND related_target_fact_id IS NOT NULL
            AND decision_reason_code IS NULL AND duplicate_winner_fact_id IS NULL)
        OR
        (status = 'SKIPPED' AND processing_started_at IS NOT NULL
            AND decided_at IS NOT NULL AND decision_kind = 'SKIP'
            AND decision_reason_code IS NOT NULL AND final_kind IS NULL
            AND accepted_fact_id IS NULL AND accepted_fact_at IS NULL
            AND applied_existing_fact_id IS NULL
            AND ((decision_reason_code IN (
                    'SKIPPED_DUPLICATE', 'SKIPPED_DUPLICATE_BASIS_UNAPPLIED'
                ) AND duplicate_winner_fact_id IS NOT NULL
                AND related_target_fact_id IS NULL)
              OR (decision_reason_code = 'SKIPPED_DUPLICATE_RELATION_ALREADY_PRESENT'
                AND duplicate_winner_fact_id IS NOT NULL
                AND related_target_fact_id IS NOT NULL)
              OR (decision_reason_code NOT LIKE 'SKIPPED_DUPLICATE%'
                AND duplicate_winner_fact_id IS NULL
                AND related_target_fact_id IS NULL)))
        OR
        (status = 'ABANDONED' AND processing_started_at IS NOT NULL
            AND decided_at IS NOT NULL AND decision_kind = 'SKIP'
            AND decision_reason_code IS NOT NULL AND final_kind IS NULL
            AND related_target_fact_id IS NULL AND duplicate_winner_fact_id IS NULL
            AND accepted_fact_id IS NULL AND accepted_fact_at IS NULL
            AND applied_existing_fact_id IS NULL)
    )
);

CREATE TABLE pulsara_v3.memory_candidate_tool_result_refs (
    candidate_id text NOT NULL,
    origin_session_id text NOT NULL,
    tool_result_id text NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal BETWEEN 0 AND 7),
    evidence_kind text NOT NULL CHECK (evidence_kind IN (
        'PRIMARY_OBSERVATION', 'MEMORY_READ_EXPOSURE'
    )),
    citation_visibility text NOT NULL CHECK (citation_visibility IN (
        'USER_SAFE', 'WORKSPACE_BOUND'
    )),
    PRIMARY KEY (candidate_id, ordinal),
    UNIQUE (candidate_id, tool_result_id),
    FOREIGN KEY (candidate_id, origin_session_id)
        REFERENCES pulsara_v3.memory_candidates (id, origin_session_id) ON DELETE RESTRICT,
    FOREIGN KEY (origin_session_id, tool_result_id)
        REFERENCES pulsara_v3.tool_results (session_id, id) ON DELETE RESTRICT
);

CREATE TABLE pulsara_v3.memory_facts (
    id text PRIMARY KEY,
    memory_domain_id text NOT NULL,
    scope_kind text NOT NULL CHECK (scope_kind IN ('USER', 'WORKSPACE')),
    scope_id text NOT NULL,
    source_candidate_id text NOT NULL UNIQUE,
    lifecycle text NOT NULL CHECK (lifecycle IN ('ACTIVE', 'SUPERSEDED')),
    fact_kind text NOT NULL CHECK (fact_kind IN (
        'FACT', 'USER_PROFILE', 'RESPONSE_PREFERENCE', 'ACTION_RULE', 'DECISION'
    )),
    statement text NOT NULL CHECK (octet_length(statement) BETWEEN 1 AND 8192),
    applies_when text CHECK (
        applies_when IS NULL OR octet_length(applies_when) BETWEEN 1 AND 4096
    ),
    do_not_apply_when text[] NOT NULL DEFAULT '{}'::text[] CHECK (
        cardinality(do_not_apply_when) <= 8
    ),
    fact_semantic_digest text NOT NULL CHECK (
        fact_semantic_digest ~ '^sha256:[0-9a-f]{64}$'
    ),
    accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    search_contract_id text NOT NULL CHECK (
        search_contract_id = 'pulsara.memory-retrieval-tokenizer'
    ),
    search_contract_version integer NOT NULL CHECK (search_contract_version = 2),
    search_terms text[] NOT NULL CHECK (
        cardinality(search_terms) <= 256
        AND octet_length(array_to_json(search_terms)::text) <= 16384
    ),
    search_document tsvector NOT NULL,
    UNIQUE (memory_domain_id, id),
    UNIQUE (memory_domain_id, scope_kind, scope_id, id),
    UNIQUE (source_candidate_id, id),
    FOREIGN KEY (source_candidate_id, id)
        REFERENCES pulsara_v3.memory_candidates (id, accepted_fact_id)
        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    CHECK (
        (scope_kind = 'USER' AND scope_id = 'ctx:user') OR
        (scope_kind = 'WORKSPACE' AND scope_id ~ '^ctx:workspace/[a-z0-9][a-z0-9._-]{0,127}$')
    ),
    CHECK (fact_kind <> 'USER_PROFILE' OR scope_kind = 'USER'),
    CHECK (fact_kind <> 'RESPONSE_PREFERENCE' OR octet_length(statement) <= 2048),
    CHECK (
        (fact_kind = 'ACTION_RULE' AND applies_when IS NOT NULL)
        OR
        (fact_kind <> 'ACTION_RULE' AND applies_when IS NULL
            AND cardinality(do_not_apply_when) = 0)
    )
);
CREATE UNIQUE INDEX uq_pulsara_v3_memory_active_semantic
    ON pulsara_v3.memory_facts (
        memory_domain_id, scope_kind, scope_id, fact_semantic_digest
    ) WHERE lifecycle = 'ACTIVE';
CREATE INDEX idx_pulsara_v3_memory_search_document_gin
    ON pulsara_v3.memory_facts USING gin (search_document);
CREATE INDEX idx_pulsara_v3_memory_search_terms_gin
    ON pulsara_v3.memory_facts USING gin (search_terms);

ALTER TABLE pulsara_v3.memory_candidates ADD CONSTRAINT memory_candidate_fact_fk
    FOREIGN KEY (id, accepted_fact_id)
    REFERENCES pulsara_v3.memory_facts (source_candidate_id, id)
    ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE pulsara_v3.memory_candidates ADD CONSTRAINT memory_candidate_related_target_fk
    FOREIGN KEY (memory_domain_id, scope_kind, scope_id, related_target_fact_id)
    REFERENCES pulsara_v3.memory_facts (memory_domain_id, scope_kind, scope_id, id)
    ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE pulsara_v3.memory_candidates ADD CONSTRAINT memory_candidate_duplicate_winner_fk
    FOREIGN KEY (memory_domain_id, scope_kind, scope_id, duplicate_winner_fact_id)
    REFERENCES pulsara_v3.memory_facts (memory_domain_id, scope_kind, scope_id, id)
    ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE pulsara_v3.memory_candidates ADD CONSTRAINT memory_candidate_applied_existing_fk
    FOREIGN KEY (memory_domain_id, scope_kind, scope_id, applied_existing_fact_id)
    REFERENCES pulsara_v3.memory_facts (memory_domain_id, scope_kind, scope_id, id)
    ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE pulsara_v3.memory_candidate_basis_refs (
    candidate_id text NOT NULL,
    memory_domain_id text NOT NULL,
    source_scope_kind text NOT NULL,
    source_scope_id text NOT NULL,
    target_scope_kind text NOT NULL,
    target_scope_id text NOT NULL,
    target_fact_id text NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal BETWEEN 0 AND 7),
    PRIMARY KEY (candidate_id, ordinal),
    UNIQUE (candidate_id, target_fact_id),
    FOREIGN KEY (candidate_id, memory_domain_id, source_scope_kind, source_scope_id)
        REFERENCES pulsara_v3.memory_candidates (
            id, memory_domain_id, scope_kind, scope_id
        ) ON DELETE RESTRICT,
    FOREIGN KEY (memory_domain_id, target_scope_kind, target_scope_id, target_fact_id)
        REFERENCES pulsara_v3.memory_facts (
            memory_domain_id, scope_kind, scope_id, id
        ) ON DELETE RESTRICT,
    CHECK (
        (source_scope_kind = 'USER' AND target_scope_kind = 'USER') OR
        (source_scope_kind = 'WORKSPACE' AND (
            target_scope_kind = 'USER' OR
            (target_scope_kind = 'WORKSPACE' AND source_scope_id = target_scope_id)
        ))
    )
);

CREATE TABLE pulsara_v3.memory_relations (
    id text PRIMARY KEY,
    memory_domain_id text NOT NULL,
    decision_candidate_id text NOT NULL,
    source_scope_kind text NOT NULL CHECK (source_scope_kind IN ('USER', 'WORKSPACE')),
    source_scope_id text NOT NULL,
    source_fact_id text NOT NULL,
    source_fact_kind text NOT NULL CHECK (source_fact_kind IN (
        'FACT', 'USER_PROFILE', 'RESPONSE_PREFERENCE', 'ACTION_RULE', 'DECISION'
    )),
    relation_kind text NOT NULL CHECK (relation_kind IN (
        'BASED_ON', 'SUPERSEDES', 'CONTRADICTS'
    )),
    target_scope_kind text NOT NULL CHECK (target_scope_kind IN ('USER', 'WORKSPACE')),
    target_scope_id text NOT NULL,
    target_fact_id text NOT NULL,
    target_fact_kind text NOT NULL CHECK (target_fact_kind IN (
        'FACT', 'USER_PROFILE', 'RESPONSE_PREFERENCE', 'ACTION_RULE', 'DECISION'
    )),
    supersede_mode text CHECK (supersede_mode IN (
        'SAME_KIND_REPLACEMENT', 'TAXONOMY_CORRECTION'
    )),
    ordinal integer,
    accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (memory_domain_id, id),
    UNIQUE (
        memory_domain_id, source_scope_kind, source_scope_id, source_fact_id,
        relation_kind, target_scope_kind, target_scope_id, target_fact_id
    ),
    FOREIGN KEY (decision_candidate_id, memory_domain_id)
        REFERENCES pulsara_v3.memory_candidates (id, memory_domain_id) ON DELETE RESTRICT,
    FOREIGN KEY (memory_domain_id, source_scope_kind, source_scope_id, source_fact_id)
        REFERENCES pulsara_v3.memory_facts (
            memory_domain_id, scope_kind, scope_id, id
        ) ON DELETE RESTRICT,
    FOREIGN KEY (memory_domain_id, target_scope_kind, target_scope_id, target_fact_id)
        REFERENCES pulsara_v3.memory_facts (
            memory_domain_id, scope_kind, scope_id, id
        ) ON DELETE RESTRICT,
    CHECK (source_fact_id <> target_fact_id),
    CHECK (
        (relation_kind = 'BASED_ON' AND supersede_mode IS NULL
            AND ordinal BETWEEN 0 AND 7 AND source_fact_kind = 'DECISION'
            AND ((source_scope_kind = 'USER' AND target_scope_kind = 'USER')
                OR (source_scope_kind = 'WORKSPACE' AND (
                    target_scope_kind = 'USER' OR
                    (target_scope_kind = 'WORKSPACE' AND source_scope_id = target_scope_id)
                ))))
        OR
        (relation_kind = 'SUPERSEDES' AND ordinal IS NULL
            AND supersede_mode IS NOT NULL
            AND source_scope_kind = target_scope_kind
            AND source_scope_id = target_scope_id
            AND ((supersede_mode = 'SAME_KIND_REPLACEMENT'
                    AND source_fact_kind = target_fact_kind)
                OR (supersede_mode = 'TAXONOMY_CORRECTION'
                    AND source_fact_kind <> target_fact_kind)))
        OR
        (relation_kind = 'CONTRADICTS' AND supersede_mode IS NULL
            AND ordinal IS NULL AND source_scope_kind = target_scope_kind
            AND source_scope_id = target_scope_id
            AND source_fact_kind = target_fact_kind)
    )
);
CREATE INDEX idx_pulsara_v3_memory_relation_outgoing
    ON pulsara_v3.memory_relations (
        memory_domain_id, source_scope_kind, source_scope_id,
        source_fact_id, relation_kind
    );
CREATE INDEX idx_pulsara_v3_memory_relation_incoming
    ON pulsara_v3.memory_relations (
        memory_domain_id, target_scope_kind, target_scope_id,
        target_fact_id, relation_kind
    );
CREATE UNIQUE INDEX uq_pulsara_v3_memory_contradiction_unordered
    ON pulsara_v3.memory_relations (
        memory_domain_id, source_scope_kind, source_scope_id,
        least(source_fact_id, target_fact_id),
        greatest(source_fact_id, target_fact_id)
    ) WHERE relation_kind = 'CONTRADICTS';

CREATE TABLE pulsara_v3.memory_embeddings (
    memory_domain_id text NOT NULL,
    fact_id text NOT NULL,
    fact_semantic_digest text NOT NULL CHECK (
        fact_semantic_digest ~ '^sha256:[0-9a-f]{64}$'
    ),
    embedding_contract_id text NOT NULL,
    embedding_contract_version integer NOT NULL CHECK (embedding_contract_version >= 1),
    embedding public.vector(1024) NOT NULL,
    embedded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (memory_domain_id, fact_id),
    FOREIGN KEY (memory_domain_id, fact_id)
        REFERENCES pulsara_v3.memory_facts (memory_domain_id, id) ON DELETE CASCADE
);
CREATE INDEX idx_pulsara_v3_memory_embeddings_hnsw
    ON pulsara_v3.memory_embeddings USING hnsw (embedding public.vector_cosine_ops);

CREATE FUNCTION pulsara_v3.memory_terms_to_tsquery(terms text[])
RETURNS tsquery
LANGUAGE sql
IMMUTABLE
STRICT
SET search_path = pg_catalog, pg_temp
AS $$
    SELECT COALESCE(
        to_tsquery(
            'pg_catalog.simple'::regconfig,
            string_agg(
                '(' || plainto_tsquery('pg_catalog.simple'::regconfig, term)::text || ')',
                ' | ' ORDER BY ordinal
            )
        ),
        ''::tsquery
    )
    FROM unnest(terms) WITH ORDINALITY AS value(term, ordinal)
    WHERE plainto_tsquery('pg_catalog.simple'::regconfig, term) <> ''::tsquery;
$$;

CREATE FUNCTION pulsara_v3.seal_memory_fact_search_document()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND (
        OLD.memory_domain_id IS DISTINCT FROM NEW.memory_domain_id OR
        OLD.scope_kind IS DISTINCT FROM NEW.scope_kind OR
        OLD.scope_id IS DISTINCT FROM NEW.scope_id OR
        OLD.source_candidate_id IS DISTINCT FROM NEW.source_candidate_id OR
        OLD.fact_kind IS DISTINCT FROM NEW.fact_kind OR
        OLD.statement IS DISTINCT FROM NEW.statement OR
        OLD.applies_when IS DISTINCT FROM NEW.applies_when OR
        OLD.do_not_apply_when IS DISTINCT FROM NEW.do_not_apply_when OR
        OLD.fact_semantic_digest IS DISTINCT FROM NEW.fact_semantic_digest OR
        OLD.search_contract_id IS DISTINCT FROM NEW.search_contract_id OR
        OLD.search_contract_version IS DISTINCT FROM NEW.search_contract_version OR
        OLD.search_terms IS DISTINCT FROM NEW.search_terms OR
        OLD.search_document IS DISTINCT FROM NEW.search_document
    ) THEN
        RAISE EXCEPTION 'memory fact immutable fields changed' USING ERRCODE = '23514';
    END IF;
    NEW.search_document := to_tsvector(
        'pg_catalog.simple'::regconfig,
        array_to_string(NEW.search_terms, ' ')
    );
    RETURN NEW;
END;
$$;
CREATE TRIGGER trg_pulsara_v3_memory_fact_search_document
BEFORE INSERT OR UPDATE ON pulsara_v3.memory_facts
FOR EACH ROW EXECUTE FUNCTION pulsara_v3.seal_memory_fact_search_document();

CREATE FUNCTION pulsara_v3.enforce_memory_candidate_lineage()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    candidate_row pulsara_v3.memory_candidates%ROWTYPE;
    fact_row pulsara_v3.memory_facts%ROWTYPE;
    producer_entry_row pulsara_v3.transcript_entries%ROWTYPE;
    producer_turn_row pulsara_v3.turns%ROWTYPE;
    relation_count integer;
BEGIN
    IF TG_TABLE_NAME = 'memory_facts' THEN
        SELECT * INTO candidate_row FROM pulsara_v3.memory_candidates
        WHERE id = NEW.source_candidate_id;
        IF candidate_row.status IS DISTINCT FROM 'ACCEPTED'
           OR candidate_row.accepted_fact_id IS DISTINCT FROM NEW.id
           OR candidate_row.memory_domain_id IS DISTINCT FROM NEW.memory_domain_id
           OR candidate_row.scope_kind IS DISTINCT FROM NEW.scope_kind
           OR candidate_row.scope_id IS DISTINCT FROM NEW.scope_id
           OR candidate_row.final_kind IS DISTINCT FROM NEW.fact_kind THEN
            RAISE EXCEPTION 'memory fact does not exact-join accepted candidate'
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;

    IF TG_TABLE_NAME = 'memory_relations' THEN
        SELECT * INTO candidate_row FROM pulsara_v3.memory_candidates
        WHERE id = NEW.decision_candidate_id;
        SELECT * INTO fact_row FROM pulsara_v3.memory_facts
        WHERE memory_domain_id = NEW.memory_domain_id AND id = NEW.source_fact_id;
        IF candidate_row.status = 'ACCEPTED' THEN
            IF candidate_row.accepted_fact_id IS DISTINCT FROM NEW.source_fact_id
               OR fact_row.source_candidate_id IS DISTINCT FROM candidate_row.id THEN
                RAISE EXCEPTION 'accepted relation source attribution drifted'
                    USING ERRCODE = '23514';
            END IF;
        ELSIF candidate_row.status = 'APPLIED_TO_EXISTING' THEN
            IF NEW.relation_kind NOT IN ('SUPERSEDES', 'CONTRADICTS')
               OR candidate_row.applied_existing_fact_id IS DISTINCT FROM NEW.source_fact_id THEN
                RAISE EXCEPTION 'existing-source relation attribution drifted'
                    USING ERRCODE = '23514';
            END IF;
            SELECT count(*) INTO relation_count FROM pulsara_v3.memory_relations
            WHERE decision_candidate_id = candidate_row.id;
            IF relation_count <> 1 THEN
                RAISE EXCEPTION 'existing-source candidate must own exact one relation'
                    USING ERRCODE = '23514';
            END IF;
        ELSE
            RAISE EXCEPTION 'non-terminal candidate cannot own memory relation'
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;

    IF TG_TABLE_NAME = 'memory_candidates'
       AND NEW.producer_kind = 'CHEAP_HINT_REFLECTION' THEN
        SELECT * INTO producer_entry_row FROM pulsara_v3.transcript_entries
        WHERE session_id = NEW.origin_session_id
          AND id = NEW.trigger_user_entry_id;
        SELECT * INTO producer_turn_row FROM pulsara_v3.turns
        WHERE session_id = NEW.origin_session_id
          AND id = producer_entry_row.turn_id;
        IF producer_entry_row.id IS NULL
           OR producer_entry_row.entry_kind NOT IN ('USER_MESSAGE', 'USER_STEER')
           OR producer_turn_row.id IS NULL
           OR producer_turn_row.conversation_scope_kind <> 'ROOT'
           OR producer_turn_row.status <> 'COMPLETED' THEN
            RAISE EXCEPTION 'reflection candidate lacks completed ROOT human trigger'
                USING ERRCODE = '23514';
        END IF;
    END IF;

    IF NEW.status = 'ACCEPTED' THEN
        SELECT * INTO fact_row FROM pulsara_v3.memory_facts
        WHERE source_candidate_id = NEW.id AND id = NEW.accepted_fact_id;
        IF fact_row.id IS NULL THEN
            RAISE EXCEPTION 'accepted memory candidate lacks exact fact'
                USING ERRCODE = '23514';
        END IF;
    ELSIF NEW.status = 'APPLIED_TO_EXISTING' THEN
        SELECT count(*) INTO relation_count FROM pulsara_v3.memory_relations
        WHERE decision_candidate_id = NEW.id
          AND source_fact_id = NEW.applied_existing_fact_id
          AND target_fact_id = NEW.related_target_fact_id
          AND relation_kind IN ('SUPERSEDES', 'CONTRADICTS');
        IF relation_count <> 1 THEN
            RAISE EXCEPTION 'applied memory candidate lacks exact relation'
                USING ERRCODE = '23514';
        END IF;
    ELSE
        IF EXISTS (SELECT 1 FROM pulsara_v3.memory_facts WHERE source_candidate_id=NEW.id)
           OR EXISTS (SELECT 1 FROM pulsara_v3.memory_relations WHERE decision_candidate_id=NEW.id) THEN
            RAISE EXCEPTION 'non-accepting memory candidate owns canonical rows'
                USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
REVOKE ALL ON FUNCTION pulsara_v3.memory_terms_to_tsquery(text[]) FROM PUBLIC;
REVOKE ALL ON FUNCTION pulsara_v3.seal_memory_fact_search_document() FROM PUBLIC;
REVOKE ALL ON FUNCTION pulsara_v3.enforce_memory_candidate_lineage() FROM PUBLIC;
CREATE CONSTRAINT TRIGGER trg_pulsara_v3_memory_candidate_lineage
AFTER INSERT OR UPDATE ON pulsara_v3.memory_candidates
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION pulsara_v3.enforce_memory_candidate_lineage();
CREATE CONSTRAINT TRIGGER trg_pulsara_v3_memory_fact_lineage
AFTER INSERT OR UPDATE ON pulsara_v3.memory_facts
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION pulsara_v3.enforce_memory_candidate_lineage();
CREATE CONSTRAINT TRIGGER trg_pulsara_v3_memory_relation_lineage
AFTER INSERT ON pulsara_v3.memory_relations
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION pulsara_v3.enforce_memory_candidate_lineage();

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
        'InterAgentMessageAccepted',
        'TerminalObservationAccepted', 'PlanWorkflowEntered',
        'PlanQuestionAsked', 'PlanQuestionAnswered', 'PlanDraftSubmitted',
        'PlanDraftDecisionAccepted', 'PlanWorkflowExited',
        'PlanContinuationAccepted'
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
    subject_queue_item_id text,
    subject_interaction_decision_id text,
    subject_context_binding_revision_id text,
    subject_subagent_task_id text,
    subject_subagent_message_id text,
    subject_subagent_result_id text,
    subject_subagent_child_kind text CHECK (
        subject_subagent_child_kind IN ('MESSAGE', 'RESULT')
    ),
    subject_plan_workflow_id text,
    subject_plan_interaction_id text,
    UNIQUE (session_id, event_sequence),
    FOREIGN KEY (session_id, workspace_id)
        REFERENCES pulsara_v3.sessions (id, workspace_id) ON DELETE RESTRICT,
    FOREIGN KEY (session_id, subject_turn_id)
        REFERENCES pulsara_v3.turns (session_id, id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (session_id, subject_entry_id)
        REFERENCES pulsara_v3.transcript_entries (session_id, id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (session_id, subject_tool_attempt_id)
        REFERENCES pulsara_v3.tool_execution_attempts (session_id, id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
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
    FOREIGN KEY (session_id, subject_plan_workflow_id)
        REFERENCES pulsara_v3.plan_workflows (session_id, id)
        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (session_id, subject_plan_interaction_id)
        REFERENCES pulsara_v3.plan_interactions (session_id, id)
        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    CHECK (num_nonnulls(
        subject_turn_id, subject_entry_id, subject_tool_attempt_id,
        subject_queue_item_id, subject_interaction_decision_id,
        subject_context_binding_revision_id, subject_subagent_task_id,
        subject_subagent_message_id, subject_subagent_result_id,
        subject_plan_workflow_id, subject_plan_interaction_id
    ) = 1),
    CHECK (
        (subject_subagent_message_id IS NOT NULL AND subject_subagent_child_kind = 'MESSAGE') OR
        (subject_subagent_result_id IS NOT NULL AND subject_subagent_child_kind = 'RESULT') OR
        (subject_subagent_message_id IS NULL AND subject_subagent_result_id IS NULL
            AND subject_subagent_child_kind IS NULL)
    ),
    CHECK (
        (event_type IN ('UserMessageAccepted', 'AssistantMessageAccepted',
            'AssistantToolRequestAccepted', 'ToolResultAccepted', 'UserSteerAccepted',
            'TerminalObservationAccepted', 'InterAgentMessageAccepted')
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
        (event_type IN ('PlanWorkflowEntered', 'PlanWorkflowExited')
            AND subject_plan_workflow_id IS NOT NULL)
        OR (event_type IN ('PlanQuestionAsked', 'PlanQuestionAnswered',
                'PlanDraftSubmitted', 'PlanDraftDecisionAccepted')
            AND subject_plan_interaction_id IS NOT NULL)
        OR (event_type = 'PlanContinuationAccepted' AND subject_entry_id IS NOT NULL)
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
ALTER TABLE pulsara_v3.session_commands ADD CONSTRAINT session_commands_target_plan_workflow_fk
    FOREIGN KEY (session_id, target_plan_workflow_id)
    REFERENCES pulsara_v3.plan_workflows (session_id, id)
    ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE pulsara_v3.session_commands ADD CONSTRAINT session_commands_target_plan_interaction_fk
    FOREIGN KEY (session_id, target_plan_interaction_id)
    REFERENCES pulsara_v3.plan_interactions (session_id, id)
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
    observed_workflow_id text;
    observed_interaction_id text;
    observed_handoff_kind text;
    observed_tool_name text;
    observed_message text;
    observed_permission_mode text;
    observed_overlay text;
    observed_contract_id text;
    observed_contract_fingerprint text;
    observed_ordinal bigint;
    observed_revision bigint;
    observed_count bigint;
    observed_workspace_id text;
BEGIN
    IF TG_TABLE_NAME = 'subagent_tasks' THEN
        IF TG_OP = 'UPDATE' AND (
            OLD.id IS DISTINCT FROM NEW.id OR
            OLD.session_id IS DISTINCT FROM NEW.session_id OR
            OLD.workspace_id IS DISTINCT FROM NEW.workspace_id OR
            OLD.parent_turn_id IS DISTINCT FROM NEW.parent_turn_id OR
            OLD.batch_id IS DISTINCT FROM NEW.batch_id OR
            OLD.task_key IS DISTINCT FROM NEW.task_key OR
            OLD.label IS DISTINCT FROM NEW.label OR
            OLD.profile_kind IS DISTINCT FROM NEW.profile_kind OR
            OLD.display_role IS DISTINCT FROM NEW.display_role OR
            OLD.context_mode IS DISTINCT FROM NEW.context_mode OR
            OLD.context_last_n_turns IS DISTINCT FROM NEW.context_last_n_turns OR
            OLD.objective IS DISTINCT FROM NEW.objective OR
            OLD.execution_writer_generation IS DISTINCT FROM
                NEW.execution_writer_generation OR
            OLD.accepted_at IS DISTINCT FROM NEW.accepted_at OR
            OLD.status IN (
                'COMPLETED', 'FAILED', 'CANCELLED', 'INTERRUPTED',
                'BLOCKED_DEPENDENCY_FAILED'
            )
        ) THEN
            RAISE EXCEPTION 'subagent task immutable identity or lifecycle changed'
                USING ERRCODE = '23514';
        END IF;
        SELECT conversation_scope_kind, workspace_id, effective_permission_mode
          INTO observed_scope, observed_workspace_id, observed_permission_mode
        FROM pulsara_v3.turns
        WHERE session_id = NEW.session_id AND id = NEW.parent_turn_id;
        IF observed_scope IS DISTINCT FROM 'ROOT'
           OR observed_workspace_id IS DISTINCT FROM NEW.workspace_id
           OR observed_permission_mode IS DISTINCT FROM 'bypass-permissions' THEN
            RAISE EXCEPTION 'subagent task parent must be exact bypass ROOT turn'
                USING ERRCODE = '23514';
        END IF;
        IF NEW.status = 'COMPLETED' THEN
            SELECT count(*) INTO observed_count
            FROM pulsara_v3.subagent_task_children
            WHERE session_id = NEW.session_id
              AND task_id = NEW.id
              AND child_kind = 'RESULT';
            IF observed_count IS DISTINCT FROM 1 THEN
                RAISE EXCEPTION 'completed subagent task requires one exact result'
                    USING ERRCODE = '23514';
            END IF;
        END IF;
        RETURN NEW;
    END IF;

    IF TG_TABLE_NAME = 'subagent_task_dependencies' THEN
        SELECT task.workspace_id
          INTO observed_workspace_id
        FROM pulsara_v3.subagent_tasks AS task
        JOIN pulsara_v3.subagent_tasks AS dependency
          ON dependency.session_id = task.session_id
         AND dependency.id = NEW.dependency_task_id
        WHERE task.session_id = NEW.session_id
          AND task.id = NEW.task_id
          AND task.workspace_id = dependency.workspace_id;
        IF observed_workspace_id IS NULL THEN
            RAISE EXCEPTION 'subagent dependency crosses task authority'
                USING ERRCODE = '23514';
        END IF;
        SELECT count(*) INTO observed_count
        FROM pulsara_v3.subagent_task_dependencies
        WHERE session_id = NEW.session_id AND task_id = NEW.task_id;
        IF observed_count > 16 THEN
            RAISE EXCEPTION 'subagent dependency count exceeds bound'
                USING ERRCODE = '23514';
        END IF;
        IF EXISTS (
            WITH RECURSIVE upstream(task_id) AS (
                SELECT NEW.dependency_task_id
                UNION
                SELECT edge.dependency_task_id
                FROM pulsara_v3.subagent_task_dependencies AS edge
                JOIN upstream ON upstream.task_id = edge.task_id
                WHERE edge.session_id = NEW.session_id
            )
            SELECT 1 FROM upstream WHERE task_id = NEW.task_id
        ) THEN
            RAISE EXCEPTION 'subagent dependency graph contains a cycle'
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;

    IF TG_TABLE_NAME = 'turns' THEN
        IF TG_OP = 'UPDATE' AND (
            OLD.permission_snapshot_id IS DISTINCT FROM NEW.permission_snapshot_id OR
            OLD.requested_permission_mode IS DISTINCT FROM NEW.requested_permission_mode OR
            OLD.effective_permission_mode IS DISTINCT FROM NEW.effective_permission_mode OR
            OLD.permission_admission_source IS DISTINCT FROM NEW.permission_admission_source OR
            OLD.permission_overlay IS DISTINCT FROM NEW.permission_overlay OR
            OLD.permission_plan_context_ordinal IS DISTINCT FROM NEW.permission_plan_context_ordinal OR
            OLD.permission_plan_workflow_id IS DISTINCT FROM NEW.permission_plan_workflow_id OR
            OLD.permission_plan_revision_at_admission IS DISTINCT FROM NEW.permission_plan_revision_at_admission OR
            OLD.permission_inherited_from_turn_id IS DISTINCT FROM NEW.permission_inherited_from_turn_id OR
            OLD.permission_contract_id IS DISTINCT FROM NEW.permission_contract_id OR
            OLD.permission_contract_fingerprint IS DISTINCT FROM NEW.permission_contract_fingerprint OR
            OLD.permission_snapshot_fingerprint IS DISTINCT FROM NEW.permission_snapshot_fingerprint
        ) THEN
            RAISE EXCEPTION 'turn permission snapshot is immutable'
                USING ERRCODE = '23514';
        END IF;
        SELECT entry_kind, turn_id, conversation_scope_kind,
               scope_subagent_task_id
          INTO observed_kind, observed_turn_id, observed_scope, observed_task_id
        FROM pulsara_v3.transcript_entries
        WHERE session_id = NEW.session_id AND id = NEW.initial_entry_id;
        IF observed_turn_id IS DISTINCT FROM NEW.id
           OR observed_scope IS DISTINCT FROM NEW.conversation_scope_kind
           OR observed_task_id IS DISTINCT FROM NEW.scope_subagent_task_id THEN
            RAISE EXCEPTION 'turn initial entry must belong to its exact turn and scope'
                USING ERRCODE = '23514';
        END IF;
        IF (NEW.conversation_scope_kind = 'ROOT'
                AND observed_kind NOT IN (
                    'USER_MESSAGE', 'TERMINAL_OBSERVATION', 'PLAN_CONTINUATION',
                    'INTER_AGENT_MESSAGE'
                ))
           OR (NEW.conversation_scope_kind = 'SUBAGENT_TASK'
                AND observed_kind IS DISTINCT FROM 'USER_MESSAGE') THEN
            RAISE EXCEPTION 'turn initial entry kind is invalid for its scope'
                USING ERRCODE = '23514';
        END IF;
        IF NEW.permission_overlay = 'PLAN_READ_ONLY' THEN
            SELECT workflow_ordinal, workflow_revision
              INTO observed_ordinal, observed_revision
            FROM pulsara_v3.plan_workflows
            WHERE session_id = NEW.session_id
              AND id = NEW.permission_plan_workflow_id;
            IF observed_ordinal IS DISTINCT FROM NEW.permission_plan_context_ordinal
               OR observed_revision IS NULL
               OR observed_revision < NEW.permission_plan_revision_at_admission THEN
                RAISE EXCEPTION 'turn Plan permission cut does not exact-join workflow'
                    USING ERRCODE = '23514';
            END IF;
        END IF;
        IF NEW.permission_admission_source = 'SUBAGENT_INHERITANCE' THEN
            SELECT effective_permission_mode, permission_plan_context_ordinal
              INTO observed_permission_mode, observed_ordinal
            FROM pulsara_v3.turns
            WHERE session_id = NEW.session_id
              AND id = NEW.permission_inherited_from_turn_id;
            IF observed_permission_mode IS DISTINCT FROM NEW.requested_permission_mode
               OR observed_permission_mode IS DISTINCT FROM NEW.effective_permission_mode
               OR observed_ordinal IS DISTINCT FROM NEW.permission_plan_context_ordinal THEN
                RAISE EXCEPTION 'subagent permission must exact-inherit parent cut'
                    USING ERRCODE = '23514';
            END IF;
        END IF;
        RETURN NEW;
    END IF;

    IF TG_TABLE_NAME = 'transcript_entries' THEN
        IF NEW.source_inter_agent_tool_attempt_id IS NOT NULL THEN
            SELECT b.tool_name, e.conversation_scope_kind,
                   b.tool_arguments ->> 'task_id',
                   b.tool_arguments ->> 'message'
              INTO observed_tool_name, observed_scope,
                   observed_task_id, observed_message
            FROM pulsara_v3.tool_execution_attempts AS a
            JOIN pulsara_v3.assistant_message_blocks AS b
              ON b.session_id = a.session_id
             AND b.assistant_entry_id = a.assistant_entry_id
             AND b.tool_call_id = a.tool_call_id
            JOIN pulsara_v3.transcript_entries AS e
              ON e.session_id = b.session_id AND e.id = b.assistant_entry_id
            WHERE a.session_id = NEW.session_id
              AND a.id = NEW.source_inter_agent_tool_attempt_id;
            IF NEW.entry_kind IS DISTINCT FROM 'INTER_AGENT_MESSAGE'
               OR NEW.conversation_scope_kind IS DISTINCT FROM 'SUBAGENT_TASK'
               OR observed_tool_name IS DISTINCT FROM 'send_agent_message'
               OR observed_scope IS DISTINCT FROM 'ROOT'
               OR observed_task_id IS DISTINCT FROM NEW.scope_subagent_task_id
               OR observed_message IS DISTINCT FROM convert_from(NEW.inline_content, 'UTF8')
               THEN
                RAISE EXCEPTION 'inter-agent message lineage is invalid'
                    USING ERRCODE = '23514';
            END IF;
        END IF;
        IF NEW.source_subagent_task_id IS NOT NULL THEN
            SELECT status INTO observed_status
            FROM pulsara_v3.subagent_tasks
            WHERE session_id = NEW.session_id AND id = NEW.source_subagent_task_id;
            IF observed_status NOT IN (
                'COMPLETED', 'FAILED', 'INTERRUPTED', 'CANCELLED',
                'BLOCKED_DEPENDENCY_FAILED'
            ) THEN
                RAISE EXCEPTION 'conversation subagent source must be a terminal task'
                USING ERRCODE = '23514';
            END IF;
        END IF;
        IF NEW.source_plan_workflow_id IS NOT NULL THEN
            SELECT w.status, i.status
              INTO observed_status, observed_kind
            FROM pulsara_v3.plan_workflows AS w
            LEFT JOIN pulsara_v3.plan_interactions AS i
              ON i.session_id = w.session_id
             AND i.id = NEW.source_plan_interaction_id
             AND i.plan_workflow_id = w.id
            WHERE w.session_id = NEW.session_id
              AND w.id = NEW.source_plan_workflow_id;
            IF NEW.source_plan_handoff_kind = 'ENTERED_PLAN'
               AND observed_status IS DISTINCT FROM 'ACTIVE' THEN
                RAISE EXCEPTION 'Plan entry handoff requires ACTIVE workflow'
                    USING ERRCODE = '23514';
            ELSIF NEW.source_plan_handoff_kind = 'REVISION_REQUESTED'
               AND (observed_status IS DISTINCT FROM 'ACTIVE'
                    OR observed_kind IS DISTINCT FROM 'REVISION_REQUESTED') THEN
                RAISE EXCEPTION 'Plan revision handoff does not exact-join decision'
                    USING ERRCODE = '23514';
            ELSIF NEW.source_plan_handoff_kind = 'APPROVED_PLAN'
               AND (observed_status IS DISTINCT FROM 'APPROVED'
                    OR observed_kind IS DISTINCT FROM 'APPROVED') THEN
                RAISE EXCEPTION 'approved Plan handoff does not exact-join decision'
                    USING ERRCODE = '23514';
            ELSIF NEW.source_plan_handoff_kind = 'CANCELLED_PLAN'
               AND observed_status IS DISTINCT FROM 'CANCELLED' THEN
                RAISE EXCEPTION 'cancelled Plan handoff does not exact-join workflow'
                    USING ERRCODE = '23514';
            ELSIF NEW.source_plan_handoff_kind = 'FORCE_EXITED_PLAN'
               AND observed_status IS DISTINCT FROM 'FORCE_EXITED' THEN
                RAISE EXCEPTION 'force-exited Plan handoff does not exact-join workflow'
                    USING ERRCODE = '23514';
            END IF;
            IF NEW.source_plan_handoff_kind IN (
                    'CANCELLED_PLAN', 'FORCE_EXITED_PLAN'
                )
               AND NEW.source_plan_interaction_id IS NOT NULL
               AND observed_kind NOT IN ('CANCELLED', 'ABORTED') THEN
                RAISE EXCEPTION 'Plan terminal handoff interaction is invalid'
                    USING ERRCODE = '23514';
            END IF;
            IF NEW.source_plan_handoff_kind IN (
                'CANCELLED_PLAN', 'FORCE_EXITED_PLAN'
            ) THEN
                SELECT consumed_entry_id INTO observed_turn_id
                FROM pulsara_v3.prompt_queue_items
                WHERE session_id = NEW.session_id
                  AND pending_plan_handoff_workflow_id = NEW.source_plan_workflow_id;
                IF FOUND AND observed_turn_id IS DISTINCT FROM NEW.id THEN
                    RAISE EXCEPTION 'Plan terminal handoff is owned by another prompt'
                        USING ERRCODE = '23514';
                END IF;
            END IF;
        END IF;
        RETURN NEW;
    END IF;

    IF TG_TABLE_NAME = 'plan_workflows' THEN
        IF TG_OP = 'UPDATE' AND (
            OLD.id IS DISTINCT FROM NEW.id OR
            OLD.session_id IS DISTINCT FROM NEW.session_id OR
            OLD.workspace_id IS DISTINCT FROM NEW.workspace_id OR
            OLD.workflow_ordinal IS DISTINCT FROM NEW.workflow_ordinal OR
            OLD.entered_by IS DISTINCT FROM NEW.entered_by OR
            OLD.entry_reason IS DISTINCT FROM NEW.entry_reason OR
            OLD.entry_command_id IS DISTINCT FROM NEW.entry_command_id OR
            OLD.entry_turn_id IS DISTINCT FROM NEW.entry_turn_id OR
            OLD.entry_assistant_entry_id IS DISTINCT FROM NEW.entry_assistant_entry_id OR
            OLD.entry_tool_call_id IS DISTINCT FROM NEW.entry_tool_call_id OR
            OLD.resume_permission_mode IS DISTINCT FROM NEW.resume_permission_mode OR
            OLD.permission_contract_id IS DISTINCT FROM NEW.permission_contract_id OR
            OLD.permission_contract_fingerprint IS DISTINCT FROM NEW.permission_contract_fingerprint OR
            NEW.workflow_revision <> OLD.workflow_revision + 1 OR
            OLD.status <> 'ACTIVE'
        ) THEN
            RAISE EXCEPTION 'Plan workflow immutable identity or lifecycle changed'
                USING ERRCODE = '23514';
        END IF;
        IF NEW.entered_by = 'AGENT' THEN
            SELECT e.turn_id, e.conversation_scope_kind, b.tool_name
              INTO observed_turn_id, observed_scope, observed_tool_name
            FROM pulsara_v3.assistant_message_blocks AS b
            JOIN pulsara_v3.transcript_entries AS e
              ON e.session_id = b.session_id
             AND e.id = b.assistant_entry_id
            WHERE b.session_id = NEW.session_id
              AND b.assistant_entry_id = NEW.entry_assistant_entry_id
              AND b.tool_call_id = NEW.entry_tool_call_id;
            IF observed_turn_id IS DISTINCT FROM NEW.entry_turn_id
               OR observed_scope IS DISTINCT FROM 'ROOT'
               OR observed_tool_name IS DISTINCT FROM 'enter_plan' THEN
                RAISE EXCEPTION 'Agent Plan workflow origin is not exact ROOT enter_plan'
                    USING ERRCODE = '23514';
            END IF;
        ELSE
            SELECT command_kind, target_plan_workflow_id
              INTO observed_kind, observed_workflow_id
            FROM pulsara_v3.session_commands
            WHERE session_id = NEW.session_id
              AND command_id = NEW.entry_command_id;
            IF observed_kind IS DISTINCT FROM 'ENTER_PLAN'
               OR observed_workflow_id IS DISTINCT FROM NEW.id THEN
                RAISE EXCEPTION 'user Plan workflow origin does not exact-join command'
                    USING ERRCODE = '23514';
            END IF;
        END IF;
        IF NEW.status = 'APPROVED' THEN
            SELECT plan_workflow_id, kind, status
              INTO observed_workflow_id, observed_kind, observed_status
            FROM pulsara_v3.plan_interactions
            WHERE session_id = NEW.session_id
              AND id = NEW.accepted_plan_interaction_id;
            IF observed_workflow_id IS DISTINCT FROM NEW.id
               OR observed_kind IS DISTINCT FROM 'DRAFT_REVIEW'
               OR observed_status IS DISTINCT FROM 'APPROVED' THEN
                RAISE EXCEPTION 'approved Plan workflow does not exact-join draft'
                    USING ERRCODE = '23514';
            END IF;
        END IF;
        RETURN NEW;
    END IF;

    IF TG_TABLE_NAME = 'plan_interactions' THEN
        IF TG_OP = 'UPDATE' AND (
            OLD.id IS DISTINCT FROM NEW.id OR
            OLD.session_id IS DISTINCT FROM NEW.session_id OR
            OLD.workspace_id IS DISTINCT FROM NEW.workspace_id OR
            OLD.plan_workflow_id IS DISTINCT FROM NEW.plan_workflow_id OR
            OLD.interaction_ordinal IS DISTINCT FROM NEW.interaction_ordinal OR
            OLD.kind IS DISTINCT FROM NEW.kind OR
            OLD.origin_turn_id IS DISTINCT FROM NEW.origin_turn_id OR
            OLD.assistant_entry_id IS DISTINCT FROM NEW.assistant_entry_id OR
            OLD.tool_call_id IS DISTINCT FROM NEW.tool_call_id OR
            OLD.request_contract_id IS DISTINCT FROM NEW.request_contract_id OR
            OLD.request_contract_version IS DISTINCT FROM NEW.request_contract_version OR
            OLD.request_contract_fingerprint IS DISTINCT FROM NEW.request_contract_fingerprint OR
            OLD.request_semantic_digest IS DISTINCT FROM NEW.request_semantic_digest OR
            (OLD.control_tool_result_id IS DISTINCT FROM NEW.control_tool_result_id
                AND NOT (
                    OLD.kind = 'QUESTION'
                    AND OLD.status = 'OPEN'
                    AND NEW.status = 'ANSWERED'
                    AND OLD.control_tool_result_id IS NULL
                    AND NEW.control_tool_result_id IS NOT NULL
                )) OR
            OLD.status <> 'OPEN'
        ) THEN
            RAISE EXCEPTION 'Plan interaction immutable identity or lifecycle changed'
                USING ERRCODE = '23514';
        END IF;
        SELECT e.turn_id, e.conversation_scope_kind, b.tool_name, t.status
          INTO observed_turn_id, observed_scope, observed_tool_name, observed_status
        FROM pulsara_v3.assistant_message_blocks AS b
        JOIN pulsara_v3.transcript_entries AS e
          ON e.session_id = b.session_id AND e.id = b.assistant_entry_id
        JOIN pulsara_v3.turns AS t
          ON t.session_id = e.session_id AND t.id = e.turn_id
        WHERE b.session_id = NEW.session_id
          AND b.assistant_entry_id = NEW.assistant_entry_id
          AND b.tool_call_id = NEW.tool_call_id;
        IF observed_turn_id IS DISTINCT FROM NEW.origin_turn_id
           OR observed_scope IS DISTINCT FROM 'ROOT'
           OR observed_tool_name IS DISTINCT FROM (
               CASE NEW.kind
                   WHEN 'QUESTION' THEN 'ask_plan_question'
                   ELSE 'exit_plan'
               END
           )
           OR (NEW.kind = 'QUESTION' AND NEW.status IN ('OPEN', 'ANSWERED')
               AND observed_status IS DISTINCT FROM 'RUNNING')
           OR (NEW.kind = 'DRAFT_REVIEW' AND observed_status IS DISTINCT FROM 'COMPLETED') THEN
            RAISE EXCEPTION 'Plan interaction origin is not its exact ROOT tool call'
                USING ERRCODE = '23514';
        END IF;
        IF NEW.control_tool_result_id IS NOT NULL THEN
            SELECT control_plan_interaction_id
              INTO observed_interaction_id
            FROM pulsara_v3.tool_results
            WHERE session_id = NEW.session_id AND id = NEW.control_tool_result_id;
            IF observed_interaction_id IS DISTINCT FROM NEW.id THEN
                RAISE EXCEPTION 'Plan interaction result does not exact-join control edge'
                    USING ERRCODE = '23514';
            END IF;
        END IF;
        IF NEW.decision_continuation_entry_id IS NOT NULL THEN
            SELECT source_plan_workflow_id, source_plan_interaction_id,
                   source_plan_handoff_kind
              INTO observed_workflow_id, observed_interaction_id,
                   observed_handoff_kind
            FROM pulsara_v3.transcript_entries
            WHERE session_id = NEW.session_id
              AND id = NEW.decision_continuation_entry_id;
            IF observed_workflow_id IS DISTINCT FROM NEW.plan_workflow_id
               OR observed_interaction_id IS DISTINCT FROM NEW.id
               OR observed_handoff_kind IS DISTINCT FROM (
                   CASE NEW.status
                       WHEN 'APPROVED' THEN 'APPROVED_PLAN'
                       ELSE 'REVISION_REQUESTED'
                   END
               ) THEN
                RAISE EXCEPTION 'Plan decision continuation does not exact-join interaction'
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
        WHERE session_id = NEW.session_id AND workspace_id = NEW.workspace_id
          AND id = NEW.result_entry_id
          AND inline_content IS NOT NULL AND blob_id IS NULL;
        IF observed_kind IS DISTINCT FROM 'TOOL_RESULT' THEN
            RAISE EXCEPTION 'tool result relation requires inline TOOL_RESULT entry'
                USING ERRCODE = '23514';
        END IF;
        PERFORM 1
        FROM pulsara_v3.transcript_entries AS call_entry
        JOIN pulsara_v3.turns AS target_turn
          ON target_turn.session_id = call_entry.session_id
         AND target_turn.id = call_entry.turn_id
        WHERE call_entry.session_id = NEW.session_id
          AND call_entry.id = NEW.tool_call_entry_id
          AND call_entry.turn_id = observed_turn_id
          AND target_turn.permission_snapshot_fingerprint
              = NEW.permission_snapshot_fingerprint;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'tool result, request, and permission must exact-join'
                USING ERRCODE = '23514';
        END IF;
        IF NEW.result_origin_kind = 'PLAN_CONTROL' THEN
            SELECT b.tool_name
              INTO observed_tool_name
            FROM pulsara_v3.assistant_message_blocks AS b
            WHERE b.session_id = NEW.session_id
              AND b.assistant_entry_id = NEW.tool_call_entry_id
              AND b.tool_call_id = NEW.tool_call_id;
            IF (NEW.control_plan_workflow_id IS NOT NULL
                    AND observed_tool_name IS DISTINCT FROM 'enter_plan')
               OR (NEW.control_plan_interaction_id IS NOT NULL
                    AND observed_tool_name NOT IN ('ask_plan_question', 'exit_plan')) THEN
                RAISE EXCEPTION 'Plan control result does not name a Plan tool'
                    USING ERRCODE = '23514';
            END IF;
            IF NEW.control_plan_interaction_id IS NOT NULL THEN
                SELECT control_tool_result_id
                  INTO observed_interaction_id
                FROM pulsara_v3.plan_interactions
                WHERE session_id = NEW.session_id
                  AND id = NEW.control_plan_interaction_id;
                IF observed_interaction_id IS DISTINCT FROM NEW.id THEN
                    RAISE EXCEPTION 'Plan control result is not the interaction result'
                        USING ERRCODE = '23514';
                END IF;
            END IF;
        END IF;
        RETURN NEW;
    END IF;

    IF TG_TABLE_NAME = 'prompt_queue_items' THEN
        -- Keep the table discriminator in its own branch.  PL/pgSQL record
        -- field lookup is dynamic and an `AND NEW.target_turn_id ...` guard
        -- can still resolve that field for trigger rows from another table.
        -- Only a pending steer is an admission promise against a live ROOT
        -- target.  Terminal queue rows retain that immutable target as
        -- historical attribution after the turn itself becomes terminal.
        IF NEW.status = 'PENDING' AND NEW.target_turn_id IS NOT NULL THEN
            SELECT conversation_scope_kind, status INTO observed_scope, observed_status
            FROM pulsara_v3.turns
            WHERE session_id = NEW.session_id AND id = NEW.target_turn_id;
            IF observed_scope IS DISTINCT FROM 'ROOT' OR observed_status IS DISTINCT FROM 'RUNNING' THEN
                RAISE EXCEPTION 'steer target must be a RUNNING ROOT turn'
                    USING ERRCODE = '23514';
            END IF;
        END IF;
        IF NEW.permission_overlay = 'PLAN_READ_ONLY' THEN
            SELECT workflow_ordinal, workflow_revision
              INTO observed_ordinal, observed_revision
            FROM pulsara_v3.plan_workflows
            WHERE session_id = NEW.session_id
              AND id = NEW.permission_plan_workflow_id;
            IF observed_ordinal IS DISTINCT FROM NEW.permission_plan_context_ordinal
               OR observed_revision IS NULL
               OR observed_revision < NEW.permission_plan_revision_at_admission THEN
                RAISE EXCEPTION 'queued Plan permission cut does not exact-join workflow'
                    USING ERRCODE = '23514';
            END IF;
        END IF;
        IF NEW.pending_plan_handoff_workflow_id IS NOT NULL THEN
            SELECT w.status, i.plan_workflow_id, i.status
              INTO observed_status, observed_workflow_id, observed_kind
            FROM pulsara_v3.plan_workflows AS w
            LEFT JOIN pulsara_v3.plan_interactions AS i
              ON i.session_id = w.session_id
             AND i.id = NEW.pending_plan_handoff_interaction_id
            WHERE w.session_id = NEW.session_id
              AND w.id = NEW.pending_plan_handoff_workflow_id;
            IF (NEW.pending_plan_handoff_kind = 'CANCELLED_PLAN'
                    AND observed_status IS DISTINCT FROM 'CANCELLED')
               OR (NEW.pending_plan_handoff_kind = 'FORCE_EXITED_PLAN'
                    AND observed_status IS DISTINCT FROM 'FORCE_EXITED')
               OR (NEW.pending_plan_handoff_interaction_id IS NOT NULL
                    AND (observed_workflow_id IS DISTINCT FROM
                            NEW.pending_plan_handoff_workflow_id
                         OR observed_kind NOT IN ('CANCELLED', 'ABORTED'))) THEN
                RAISE EXCEPTION 'queued Plan terminal handoff is invalid'
                    USING ERRCODE = '23514';
            END IF;
            SELECT id INTO observed_turn_id
            FROM pulsara_v3.transcript_entries
            WHERE session_id = NEW.session_id
              AND source_plan_workflow_id = NEW.pending_plan_handoff_workflow_id
              AND source_plan_handoff_kind = NEW.pending_plan_handoff_kind;
            IF FOUND AND (
                NEW.status IS DISTINCT FROM 'CONSUMED'
                OR NEW.consumed_entry_id IS DISTINCT FROM observed_turn_id
            ) THEN
                RAISE EXCEPTION 'queued Plan handoff conflicts with an entry claim'
                    USING ERRCODE = '23514';
            END IF;
        END IF;
        IF NEW.status = 'CONSUMED'
           AND NEW.pending_plan_handoff_workflow_id IS NOT NULL THEN
            SELECT source_plan_workflow_id, source_plan_interaction_id,
                   source_plan_handoff_kind
              INTO observed_workflow_id, observed_interaction_id,
                   observed_handoff_kind
            FROM pulsara_v3.transcript_entries
            WHERE session_id = NEW.session_id AND id = NEW.consumed_entry_id;
            IF observed_workflow_id IS DISTINCT FROM
                    NEW.pending_plan_handoff_workflow_id
               OR observed_interaction_id IS DISTINCT FROM
                    NEW.pending_plan_handoff_interaction_id
               OR observed_handoff_kind IS DISTINCT FROM
                    NEW.pending_plan_handoff_kind THEN
                RAISE EXCEPTION 'consumed queue handoff does not exact-join entry'
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
        IF NEW.child_kind = 'RESULT' THEN
            SELECT status INTO observed_status
            FROM pulsara_v3.subagent_tasks
            WHERE session_id = NEW.session_id AND id = NEW.task_id;
            SELECT entry.turn_id, turn.status, turn.final_entry_id
              INTO observed_turn_id, observed_kind, observed_task_id
            FROM pulsara_v3.transcript_entries AS entry
            JOIN pulsara_v3.turns AS turn
              ON turn.session_id = entry.session_id AND turn.id = entry.turn_id
            WHERE entry.session_id = NEW.session_id AND entry.id = NEW.entry_id;
            IF observed_status IS DISTINCT FROM 'COMPLETED'
               OR observed_kind IS DISTINCT FROM 'COMPLETED'
               OR observed_task_id IS DISTINCT FROM NEW.entry_id THEN
                RAISE EXCEPTION 'subagent result must terminalize its exact task and turn'
                    USING ERRCODE = '23514';
            END IF;
            IF NEW.result_source = 'EXPLICIT' THEN
                SELECT tr.result_entry_id, b.tool_name
                  INTO observed_turn_id, observed_tool_name
                FROM pulsara_v3.tool_results AS tr
                JOIN pulsara_v3.tool_execution_attempts AS a
                  ON a.session_id = tr.session_id AND a.id = tr.attempt_id
                JOIN pulsara_v3.assistant_message_blocks AS b
                  ON b.session_id = a.session_id
                 AND b.assistant_entry_id = a.assistant_entry_id
                 AND b.tool_call_id = a.tool_call_id
                WHERE tr.session_id = NEW.session_id
                  AND tr.result_entry_id = NEW.entry_id;
                IF observed_turn_id IS DISTINCT FROM NEW.entry_id
                   OR observed_tool_name IS DISTINCT FROM 'report_agent_result' THEN
                    RAISE EXCEPTION 'explicit subagent result lacks exact acknowledgement'
                        USING ERRCODE = '23514';
                END IF;
            ELSIF NEW.result_source = 'INFERRED' THEN
                SELECT entry_kind INTO observed_kind
                FROM pulsara_v3.transcript_entries
                WHERE session_id = NEW.session_id AND id = NEW.entry_id;
                IF observed_kind IS DISTINCT FROM 'ASSISTANT_MESSAGE' THEN
                    RAISE EXCEPTION 'inferred subagent result must cite final assistant'
                        USING ERRCODE = '23514';
                END IF;
            END IF;
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

CREATE CONSTRAINT TRIGGER trg_pulsara_v3_turn_initial_entry_integrity
AFTER INSERT OR UPDATE ON pulsara_v3.turns
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
AFTER INSERT OR UPDATE ON pulsara_v3.prompt_queue_items
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION pulsara_v3.enforce_conversation_kernel_invariants();

CREATE CONSTRAINT TRIGGER trg_pulsara_v3_subagent_child_integrity
AFTER INSERT ON pulsara_v3.subagent_task_children
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION pulsara_v3.enforce_conversation_kernel_invariants();

CREATE CONSTRAINT TRIGGER trg_pulsara_v3_subagent_task_integrity
AFTER INSERT OR UPDATE ON pulsara_v3.subagent_tasks
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION pulsara_v3.enforce_conversation_kernel_invariants();

CREATE CONSTRAINT TRIGGER trg_pulsara_v3_subagent_dependency_integrity
AFTER INSERT ON pulsara_v3.subagent_task_dependencies
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION pulsara_v3.enforce_conversation_kernel_invariants();

CREATE CONSTRAINT TRIGGER trg_pulsara_v3_plan_workflow_integrity
AFTER INSERT OR UPDATE ON pulsara_v3.plan_workflows
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION pulsara_v3.enforce_conversation_kernel_invariants();

CREATE CONSTRAINT TRIGGER trg_pulsara_v3_plan_interaction_integrity
AFTER INSERT OR UPDATE ON pulsara_v3.plan_interactions
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION pulsara_v3.enforce_conversation_kernel_invariants();

CREATE INDEX idx_pulsara_v3_entries_session_scope_sequence
    ON pulsara_v3.transcript_entries (session_id, conversation_scope_kind, scope_subagent_task_id, entry_sequence);
CREATE INDEX idx_pulsara_v3_events_session_sequence
    ON pulsara_v3.agent_events (session_id, event_sequence);
CREATE INDEX idx_pulsara_v3_queue_pending
    ON pulsara_v3.prompt_queue_items (session_id, queue_sequence, id) WHERE status = 'PENDING';
REVOKE ALL ON ALL TABLES IN SCHEMA pulsara_v3 FROM PUBLIC;
