CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;
CREATE EXTENSION IF NOT EXISTS pg_search VERSION '0.26.1';

CREATE TABLE public.pulsara_schema_migrations (
    name text NOT NULL UNIQUE,
    resource_sha256 text NOT NULL CHECK (resource_sha256 ~ '^[0-9a-f]{64}$'),
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
REVOKE ALL ON public.pulsara_schema_migrations FROM PUBLIC;

CREATE SCHEMA pulsara_v3;
REVOKE ALL ON SCHEMA pulsara_v3 FROM PUBLIC;

CREATE TABLE pulsara_v3.workspaces (
    memory_domain_id text NOT NULL CHECK (
        memory_domain_id ~ '^[a-z0-9][a-z0-9._-]{0,127}$'
    ),
    id text CHECK (id ~ '^workspace:[a-z2-7]{25}[aeimquy4]$') NOT NULL,
    workspace_kind text NOT NULL CHECK (workspace_kind IN ('project', 'transient')),
    workspace_root text NOT NULL CHECK (workspace_root <> ''),
    workspace_label text NOT NULL CHECK (workspace_label <> ''),
    PRIMARY KEY (memory_domain_id, id),
    UNIQUE (memory_domain_id, workspace_kind, workspace_root),
    CHECK (
        (workspace_kind = 'project'
            AND id ~ '^workspace:[a-z2-7]{25}[aeimquy4]$')
        OR
        (workspace_kind = 'transient'
            AND id ~ '^workspace:[a-z2-7]{25}[aeimquy4]$')
    )
);

CREATE TABLE pulsara_v3.sessions (
    id text CHECK (id ~ '^session:[a-z2-7]{25}[aeimquy4]$') PRIMARY KEY,
    root_session_id text REFERENCES pulsara_v3.sessions(id) ON DELETE CASCADE,
    idle_since timestamptz,
    workspace_id text NOT NULL,
    memory_domain_id text NOT NULL CHECK (
        memory_domain_id ~ '^[a-z0-9][a-z0-9._-]{0,127}$'
    ),
    model_call_binding jsonb,
    lifecycle text NOT NULL CHECK (lifecycle IN ('OPEN', 'ARCHIVED')),
    writer_generation bigint NOT NULL CHECK (writer_generation >= 1),
    writer_lease_owner_id text CHECK (writer_lease_owner_id ~ '^host:[a-z2-7]{25}[aeimquy4]$'),
    writer_lease_expires_at timestamptz,
    latest_entry_sequence bigint NOT NULL DEFAULT 0 CHECK (latest_entry_sequence >= 0),
    latest_event_sequence bigint NOT NULL DEFAULT 0 CHECK (latest_event_sequence >= 0),
    latest_prompt_queue_sequence bigint NOT NULL DEFAULT 0 CHECK (latest_prompt_queue_sequence >= 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    title text CHECK (title IS NULL OR btrim(title) <> ''),
    UNIQUE (id, workspace_id),
    CHECK ((writer_lease_owner_id IS NULL) = (writer_lease_expires_at IS NULL)),
    CHECK (
        model_call_binding IS NULL OR (
            jsonb_typeof(model_call_binding) = 'object'
            AND model_call_binding ? 'connection_id'
            AND model_call_binding ? 'reasoning'
            AND model_call_binding - ARRAY['connection_id', 'reasoning']::text[] = '{}'::jsonb
            AND jsonb_typeof(model_call_binding->'connection_id') = 'string'
        )
    ),
    FOREIGN KEY (memory_domain_id, workspace_id)
        REFERENCES pulsara_v3.workspaces (memory_domain_id, id)
        ON DELETE RESTRICT
);
CREATE INDEX idx_pulsara_v3_sessions_workspace_fk
    ON pulsara_v3.sessions (memory_domain_id, workspace_id);

CREATE INDEX idx_pulsara_v3_sessions_root ON pulsara_v3.sessions(root_session_id);
CREATE FUNCTION pulsara_v3.enforce_session_root() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND OLD.root_session_id IS DISTINCT FROM NEW.root_session_id THEN
        RAISE EXCEPTION 'session ownership is immutable' USING ERRCODE='23514';
    END IF;
    IF NEW.root_session_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM pulsara_v3.sessions WHERE id=NEW.root_session_id
            AND root_session_id IS NULL AND memory_domain_id=NEW.memory_domain_id
            AND workspace_id=NEW.workspace_id
    ) THEN
        RAISE EXCEPTION 'agent must belong to a root in its workspace and memory domain' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END;
$$;
REVOKE ALL ON FUNCTION pulsara_v3.enforce_session_root() FROM PUBLIC;
CREATE CONSTRAINT TRIGGER trg_pulsara_v3_session_root
AFTER INSERT OR UPDATE ON pulsara_v3.sessions DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION pulsara_v3.enforce_session_root();

CREATE TABLE pulsara_v3.agent_known_sessions (
    observer_session_id text NOT NULL REFERENCES pulsara_v3.sessions(id) ON DELETE CASCADE,
    known_session_id text NOT NULL REFERENCES pulsara_v3.sessions(id) ON DELETE CASCADE,
    PRIMARY KEY (observer_session_id, known_session_id),
    CHECK (observer_session_id <> known_session_id)
);
CREATE INDEX idx_pulsara_v3_agent_known_sessions_known
    ON pulsara_v3.agent_known_sessions (known_session_id);

CREATE TABLE pulsara_v3.blobs (
    id text CHECK (id ~ '^(blob|image):[a-z2-7]{25}[aeimquy4]$') PRIMARY KEY,
    workspace_id text,
    storage_identity text NOT NULL UNIQUE,
    logical_digest text NOT NULL CHECK (logical_digest ~ '^sha256:[0-9a-f]{64}$'),
    logical_size bigint NOT NULL CHECK (logical_size >= 0),
    media_type text NOT NULL,
    codec text NOT NULL,
    body bytea NOT NULL,
    width integer,
    height integer,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (id, workspace_id),
    CHECK (octet_length(body) = logical_size),
    CHECK ((id LIKE 'image:%' AND workspace_id IS NULL AND media_type LIKE 'image/%'
            AND width IS NOT NULL AND height IS NOT NULL AND width > 0 AND height > 0)
        OR (id LIKE 'blob:%' AND workspace_id IS NOT NULL AND width IS NULL AND height IS NULL))
);

CREATE TABLE pulsara_v3.context_snapshots (
    id text CHECK (id ~ '^snapshot:[a-z2-7]{25}[aeimquy4]$') PRIMARY KEY,
    session_id text NOT NULL,
    workspace_id text NOT NULL,
    source_through_sequence bigint NOT NULL CHECK (source_through_sequence >= 0),
    source_digest text NOT NULL CHECK (source_digest ~ '^sha256:[0-9a-f]{64}$'),
    inline_content bytea,
    blob_id text,
    content_digest text NOT NULL CHECK (content_digest ~ '^sha256:[0-9a-f]{64}$'),
    content_size bigint NOT NULL CHECK (content_size >= 0),
    content_media_type text NOT NULL,
    content_codec text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (session_id, id),
    FOREIGN KEY (session_id, workspace_id)
        REFERENCES pulsara_v3.sessions (id, workspace_id) ON DELETE CASCADE,
    FOREIGN KEY (blob_id, workspace_id)
        REFERENCES pulsara_v3.blobs (id, workspace_id) ON DELETE RESTRICT,
    CHECK ((inline_content IS NULL) <> (blob_id IS NULL)),
    CHECK (inline_content IS NULL OR octet_length(inline_content) = content_size)
);

CREATE TABLE pulsara_v3.turns (
    id text CHECK (id ~ '^turn:[a-z2-7]{25}[aeimquy4]$') PRIMARY KEY,
    session_id text NOT NULL,
    workspace_id text NOT NULL,
    model_call_binding jsonb NOT NULL,
    status text NOT NULL CHECK (status IN ('RUNNING', 'COMPLETED', 'INTERRUPTED')),
    initial_entry_id text NOT NULL,
    final_entry_id text,
    current_context_binding_revision_id text,
    permission_snapshot_id text CHECK (permission_snapshot_id ~ '^permission:[a-z2-7]{25}[aeimquy4]$') NOT NULL,
    requested_permission_mode text NOT NULL CHECK (requested_permission_mode IN (
        'read-only', 'ask-permissions', 'accept-edits', 'bypass-permissions'
    )),
    effective_permission_mode text NOT NULL CHECK (effective_permission_mode IN (
        'read-only', 'ask-permissions', 'accept-edits', 'bypass-permissions'
    )),
    permission_admission_source text NOT NULL CHECK (permission_admission_source IN (
        'USER_SUBMISSION', 'TERMINAL_OBSERVATION',
        'RUNTIME_PLAN_CONTINUATION'
    )),
    permission_overlay text NOT NULL CHECK (permission_overlay IN ('NONE', 'PLAN_READ_ONLY')),
    permission_plan_context_ordinal bigint NOT NULL CHECK (permission_plan_context_ordinal >= 0),
    permission_plan_workflow_id text,
    permission_plan_revision_at_admission bigint,
    permission_inherited_from_turn_id text,
    permission_snapshot_fingerprint text NOT NULL CHECK (
        permission_snapshot_fingerprint ~ '^sha256:[0-9a-f]{64}$'
    ),
    terminal_public_detail text,
    terminal_reason text,
    accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    terminal_at timestamptz,
    UNIQUE (session_id, id),
    FOREIGN KEY (session_id, workspace_id)
        REFERENCES pulsara_v3.sessions (id, workspace_id) ON DELETE CASCADE,
    CHECK (
        jsonb_typeof(model_call_binding) = 'object'
        AND model_call_binding ? 'connection_id'
        AND model_call_binding ? 'reasoning'
        AND model_call_binding - ARRAY['connection_id', 'reasoning']::text[] = '{}'::jsonb
        AND jsonb_typeof(model_call_binding->'connection_id') = 'string'
    ),
    CHECK ((status = 'RUNNING') = (terminal_at IS NULL)),
    CONSTRAINT ck_turn_permission_overlay_exact CHECK (
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
        (permission_admission_source IN (
                'RUNTIME_PLAN_CONTINUATION', 'TERMINAL_OBSERVATION'
            )
            AND permission_inherited_from_turn_id IS NOT NULL) OR
        (permission_admission_source NOT IN (
                'RUNTIME_PLAN_CONTINUATION',
                'TERMINAL_OBSERVATION'
            )
            AND permission_inherited_from_turn_id IS NULL)
    )
);

CREATE TABLE pulsara_v3.provider_call_usage (
    resolved_model_call_id text PRIMARY KEY CHECK (
        resolved_model_call_id ~ '^call:[a-z2-7]{25}[aeimquy4]$'
    ),
    session_id text NOT NULL,
    turn_id text NOT NULL,
    model_call_index integer NOT NULL CHECK (model_call_index >= 0),
    connection_id text NOT NULL,
    route_id text NOT NULL,
    wire_api text NOT NULL CHECK (
        wire_api IN ('openai_chat_completions', 'openai_responses')
    ),
    requested_model_id text NOT NULL,
    reported_model_id text,
    observed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    normalized_terminal_kind text CHECK (
        normalized_terminal_kind IS NULL OR normalized_terminal_kind IN (
            'COMPLETED', 'OUTPUT_INCOMPLETE', 'PROVIDER_ERROR'
        )
    ),
    usage_status text NOT NULL CHECK (
        usage_status IN ('reported', 'partial', 'missing', 'invalid')
    ),
    input_tokens bigint CHECK (input_tokens IS NULL OR input_tokens >= 0),
    output_tokens bigint CHECK (output_tokens IS NULL OR output_tokens >= 0),
    cached_input_tokens bigint CHECK (
        cached_input_tokens IS NULL OR cached_input_tokens >= 0
    ),
    reasoning_output_tokens bigint CHECK (
        reasoning_output_tokens IS NULL OR reasoning_output_tokens >= 0
    ),
    reported_total_tokens bigint CHECK (
        reported_total_tokens IS NULL OR reported_total_tokens >= 0
    ),
    diagnostic_codes text[] NOT NULL DEFAULT '{}'::text[],
    FOREIGN KEY (session_id, turn_id)
        REFERENCES pulsara_v3.turns (session_id, id) ON DELETE CASCADE,
    CHECK (
        cached_input_tokens IS NULL OR input_tokens IS NULL
        OR cached_input_tokens <= input_tokens
    ),
    CHECK (
        reasoning_output_tokens IS NULL OR output_tokens IS NULL
        OR reasoning_output_tokens <= output_tokens
    ),
    CHECK (
        (usage_status = 'reported'
            AND input_tokens IS NOT NULL AND output_tokens IS NOT NULL) OR
        (usage_status = 'partial'
            AND NOT (input_tokens IS NOT NULL AND output_tokens IS NOT NULL)
            AND (input_tokens IS NOT NULL OR output_tokens IS NOT NULL
                OR cached_input_tokens IS NOT NULL
                OR reasoning_output_tokens IS NOT NULL
                OR reported_total_tokens IS NOT NULL)) OR
        (usage_status IN ('missing', 'invalid')
            AND input_tokens IS NULL AND output_tokens IS NULL
            AND cached_input_tokens IS NULL AND reasoning_output_tokens IS NULL
            AND reported_total_tokens IS NULL)
    )
);
CREATE INDEX ix_pulsara_v3_provider_call_usage_session_observed
    ON pulsara_v3.provider_call_usage (
        session_id, observed_at, resolved_model_call_id
    );

CREATE UNIQUE INDEX uq_pulsara_v3_running_root_turn
    ON pulsara_v3.turns (session_id) WHERE status = 'RUNNING';


CREATE TABLE pulsara_v3.turn_context_binding_revisions (
    id text CHECK (id ~ '^binding:[a-z2-7]{25}[aeimquy4]$') PRIMARY KEY,
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
        REFERENCES pulsara_v3.turns (session_id, id) ON DELETE CASCADE,
    FOREIGN KEY (session_id, context_snapshot_id)
        REFERENCES pulsara_v3.context_snapshots (session_id, id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    CHECK ((base_kind = 'FULL_HISTORY') = (context_snapshot_id IS NULL))
);
ALTER TABLE pulsara_v3.turns ADD CONSTRAINT turns_current_context_revision_fk
    FOREIGN KEY (session_id, current_context_binding_revision_id)
    REFERENCES pulsara_v3.turn_context_binding_revisions (session_id, id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE pulsara_v3.session_commands (
    session_id text NOT NULL,
    command_id text CHECK (command_id ~ '^command:[a-z2-7]{25}[aeimquy4]$') NOT NULL,
    command_kind text NOT NULL CHECK (command_kind IN (
        'SUBMIT_PROMPT', 'STEER', 'QUEUE_PROMPT', 'RUN_SCHEDULED_TASK', 'CANCEL_PROMPT', 'STEER_QUEUED_PROMPT',
        'RESOLVE_INTERACTION',
        'ENTER_PLAN', 'CANCEL_PLAN', 'FORCE_EXIT_PLAN',
        'RESOLVE_PLAN_INTERACTION', 'COMPACT_CONTEXT'
    )),
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
    FOREIGN KEY (session_id) REFERENCES pulsara_v3.sessions (id) ON DELETE CASCADE,
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
        (command_kind IN ('QUEUE_PROMPT', 'RUN_SCHEDULED_TASK', 'CANCEL_PROMPT', 'STEER_QUEUED_PROMPT') AND target_kind = 'QUEUE_ITEM') OR
        (command_kind = 'RESOLVE_INTERACTION' AND target_kind = 'INTERACTION_DECISION') OR
        (command_kind IN ('ENTER_PLAN', 'CANCEL_PLAN', 'FORCE_EXIT_PLAN')
            AND target_kind = 'PLAN_WORKFLOW') OR
        (command_kind = 'RESOLVE_PLAN_INTERACTION'
            AND target_kind = 'PLAN_INTERACTION') OR
        (command_kind = 'COMPACT_CONTEXT' AND target_kind = 'TURN')
    )
);

CREATE TABLE pulsara_v3.imported_history_groups (
    id text CHECK (id ~ '^history:[a-z2-7]{25}[aeimquy4]$') PRIMARY KEY,
    session_id text NOT NULL,
    workspace_id text NOT NULL,
    status text NOT NULL CHECK (status IN ('COMPLETED', 'INTERRUPTED')),
    final_entry_id text,
    accepted_at timestamptz NOT NULL,
    terminal_at timestamptz,
    interruption_outcome jsonb,
    CHECK (interruption_outcome IS NULL OR (status = 'INTERRUPTED' AND terminal_at IS NOT NULL AND jsonb_typeof(interruption_outcome) = 'object')),
    UNIQUE (session_id, id),
    FOREIGN KEY (session_id, workspace_id)
        REFERENCES pulsara_v3.sessions (id, workspace_id) ON DELETE CASCADE
);

CREATE TABLE pulsara_v3.transcript_entries (
    id text CHECK (id ~ '^entry:[a-z2-7]{25}[aeimquy4]$') PRIMARY KEY,
    origin_entry_id text NOT NULL CHECK (origin_entry_id <> ''),
    session_id text NOT NULL,
    workspace_id text NOT NULL,
    entry_owner_kind text NOT NULL CHECK (entry_owner_kind IN ('EXECUTED_TURN', 'IMPORTED_HISTORY')),
    turn_id text,
    imported_history_group_id text,
    entry_sequence bigint NOT NULL CHECK (entry_sequence >= 1),
    scheduled_input jsonb,
    input_origin text CHECK (input_origin IN ('HUMAN_MESSAGE','HUMAN_STEER','SCHEDULED_TASK','AGENT_INSTRUCTION','AGENT_MATERIAL','AGENT_MESSAGE','CELL_OUTPUT')),
    CHECK (scheduled_input IS NULL OR (entry_kind IN ('USER_MESSAGE','USER_STEER') AND jsonb_typeof(scheduled_input) = 'object')),
    entry_kind text NOT NULL CHECK (entry_kind IN (
        'USER_MESSAGE', 'USER_STEER', 'ASSISTANT_MESSAGE',
        'ASSISTANT_TOOL_REQUEST', 'TOOL_RESULT', 'TERMINAL_OBSERVATION',
        'PLAN_CONTINUATION'
    )),
    source_cell_id text CHECK (source_cell_id ~ '^cell:[a-z2-7]{25}[aeimquy4]$'),
    source_record_sequence bigint CHECK (source_record_sequence >= 1),
    cell_output_kind text CHECK (cell_output_kind IN ('log','return','diagnostic','tool_result')),
    CHECK ((entry_kind='TERMINAL_OBSERVATION' AND source_cell_id IS NOT NULL
            AND source_record_sequence IS NOT NULL AND cell_output_kind IS NOT NULL)
        OR (entry_kind<>'TERMINAL_OBSERVATION' AND source_cell_id IS NULL
            AND source_record_sequence IS NULL AND cell_output_kind IS NULL)),
    context_binding_revision_id text,
    provider_input_through_sequence bigint,
    source_plan_workflow_id text,
    source_plan_interaction_id text,
    source_plan_handoff_kind text CHECK (source_plan_handoff_kind IN (
        'ENTERED_PLAN', 'EXITED_PLAN', 'APPROVED_PLAN',
        'CANCELLED_PLAN', 'FORCE_EXITED_PLAN'
    )),
    imported_source_plan_workflow_id text,
    imported_source_plan_interaction_id text,
    imported_source_plan_handoff_kind text CHECK (imported_source_plan_handoff_kind IN (
        'ENTERED_PLAN', 'EXITED_PLAN', 'APPROVED_PLAN',
        'CANCELLED_PLAN', 'FORCE_EXITED_PLAN'
    )),
    accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (session_id, id),
    UNIQUE (session_id, workspace_id, id),
    UNIQUE (session_id, id, entry_kind),
    UNIQUE (session_id, id, turn_id),
    UNIQUE (session_id, id, imported_history_group_id),
    UNIQUE (session_id, entry_sequence),
    FOREIGN KEY (session_id, workspace_id)
        REFERENCES pulsara_v3.sessions (id, workspace_id) ON DELETE CASCADE,
    FOREIGN KEY (session_id, turn_id)
        REFERENCES pulsara_v3.turns (session_id, id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (session_id, imported_history_group_id)
        REFERENCES pulsara_v3.imported_history_groups (session_id, id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (session_id, context_binding_revision_id)
        REFERENCES pulsara_v3.turn_context_binding_revisions (session_id, id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT ck_entry_owner_exact CHECK (
        (entry_owner_kind = 'EXECUTED_TURN' AND turn_id IS NOT NULL
            AND imported_history_group_id IS NULL
            AND num_nonnulls(imported_source_plan_workflow_id, imported_source_plan_interaction_id,
                imported_source_plan_handoff_kind) = 0)
        OR
        (entry_owner_kind = 'IMPORTED_HISTORY' AND turn_id IS NULL
            AND imported_history_group_id IS NOT NULL
            AND num_nonnulls(source_plan_workflow_id, source_plan_interaction_id, source_plan_handoff_kind) = 0)
    ),
    CHECK (
        (entry_kind IN ('ASSISTANT_MESSAGE', 'ASSISTANT_TOOL_REQUEST')
            AND ((entry_owner_kind = 'EXECUTED_TURN' AND context_binding_revision_id IS NOT NULL)
                OR (entry_owner_kind = 'IMPORTED_HISTORY' AND context_binding_revision_id IS NULL))
            AND provider_input_through_sequence IS NOT NULL
            AND provider_input_through_sequence >= 0
            AND provider_input_through_sequence < entry_sequence)
        OR
        (entry_kind NOT IN ('ASSISTANT_MESSAGE', 'ASSISTANT_TOOL_REQUEST')
            AND context_binding_revision_id IS NULL
            AND provider_input_through_sequence IS NULL)
    ),
    CHECK (
        (entry_kind = 'PLAN_CONTINUATION'
            AND source_plan_workflow_id IS NOT NULL
            AND source_plan_handoff_kind IN (
                'ENTERED_PLAN', 'EXITED_PLAN', 'APPROVED_PLAN'
            )
) OR
        (entry_kind = 'USER_MESSAGE'
            AND source_plan_workflow_id IS NOT NULL
            AND source_plan_handoff_kind IN ('CANCELLED_PLAN', 'FORCE_EXITED_PLAN')
) OR
        (source_plan_workflow_id IS NULL
            AND source_plan_interaction_id IS NULL
            AND source_plan_handoff_kind IS NULL)
    ),
    CHECK (
        source_plan_handoff_kind NOT IN ('APPROVED_PLAN')
        OR source_plan_interaction_id IS NOT NULL
    ),
    CHECK (
        source_plan_handoff_kind NOT IN ('ENTERED_PLAN', 'EXITED_PLAN')
        OR source_plan_interaction_id IS NULL
    ),
    CONSTRAINT ck_imported_source_exact CHECK (
        entry_owner_kind = 'EXECUTED_TURN' OR ((
            (
                (entry_kind = 'PLAN_CONTINUATION' AND imported_source_plan_workflow_id IS NOT NULL
                    AND imported_source_plan_handoff_kind IN ('ENTERED_PLAN', 'EXITED_PLAN', 'APPROVED_PLAN'))
                OR (entry_kind = 'USER_MESSAGE' AND imported_source_plan_workflow_id IS NOT NULL
                    AND imported_source_plan_handoff_kind IN ('CANCELLED_PLAN', 'FORCE_EXITED_PLAN'))
                OR (entry_kind NOT IN ('PLAN_CONTINUATION') AND imported_source_plan_workflow_id IS NULL
                    AND imported_source_plan_handoff_kind IS NULL AND imported_source_plan_interaction_id IS NULL)
            )
            AND (imported_source_plan_handoff_kind IS NULL OR imported_source_plan_handoff_kind NOT IN ('APPROVED_PLAN')
                OR imported_source_plan_interaction_id IS NOT NULL)
            AND (imported_source_plan_handoff_kind IS NULL OR imported_source_plan_handoff_kind NOT IN ('ENTERED_PLAN', 'EXITED_PLAN') OR imported_source_plan_interaction_id IS NULL)
        ) IS TRUE)
    )
);
CREATE UNIQUE INDEX uq_pulsara_v3_imported_plan_without_interaction
    ON pulsara_v3.transcript_entries (session_id, imported_source_plan_workflow_id, imported_source_plan_handoff_kind)
    WHERE entry_owner_kind = 'IMPORTED_HISTORY' AND imported_source_plan_workflow_id IS NOT NULL
        AND imported_source_plan_interaction_id IS NULL;
CREATE UNIQUE INDEX uq_pulsara_v3_imported_plan_with_interaction
    ON pulsara_v3.transcript_entries (session_id, imported_source_plan_workflow_id, imported_source_plan_handoff_kind, imported_source_plan_interaction_id)
    WHERE entry_owner_kind = 'IMPORTED_HISTORY' AND imported_source_plan_workflow_id IS NOT NULL
        AND imported_source_plan_interaction_id IS NOT NULL;
CREATE UNIQUE INDEX uq_pulsara_v3_imported_plan_terminal_handoff
    ON pulsara_v3.transcript_entries (session_id, imported_source_plan_workflow_id)
    WHERE entry_owner_kind = 'IMPORTED_HISTORY' AND imported_source_plan_handoff_kind IN ('CANCELLED_PLAN', 'FORCE_EXITED_PLAN');
CREATE UNIQUE INDEX uq_pulsara_v3_plan_handoff_without_interaction
    ON pulsara_v3.transcript_entries (
        session_id, source_plan_workflow_id, source_plan_handoff_kind
    ) WHERE entry_owner_kind = 'EXECUTED_TURN' AND source_plan_workflow_id IS NOT NULL
          AND source_plan_interaction_id IS NULL;
CREATE UNIQUE INDEX uq_pulsara_v3_plan_handoff_with_interaction
    ON pulsara_v3.transcript_entries (
        session_id, source_plan_workflow_id, source_plan_handoff_kind,
        source_plan_interaction_id
    ) WHERE entry_owner_kind = 'EXECUTED_TURN' AND source_plan_workflow_id IS NOT NULL
          AND source_plan_interaction_id IS NOT NULL;
ALTER TABLE pulsara_v3.turns ADD CONSTRAINT turns_initial_entry_fk
    FOREIGN KEY (session_id, initial_entry_id)
    REFERENCES pulsara_v3.transcript_entries (session_id, id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE pulsara_v3.turns ADD CONSTRAINT turns_final_entry_fk
    FOREIGN KEY (session_id, final_entry_id)
    REFERENCES pulsara_v3.transcript_entries (session_id, id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE pulsara_v3.imported_history_groups ADD CONSTRAINT imported_group_final_fk
    FOREIGN KEY (session_id, final_entry_id)
    REFERENCES pulsara_v3.transcript_entries (session_id, id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE pulsara_v3.session_context_genesis (
    session_id text PRIMARY KEY,
    workspace_id text NOT NULL,
    anchor_entry_id text NOT NULL,
    model_call_binding jsonb NOT NULL,
    base_kind text NOT NULL CHECK (base_kind IN ('FULL_HISTORY', 'SNAPSHOT')),
    source_through_sequence bigint NOT NULL CHECK (source_through_sequence >= 0),
    context_snapshot_id text,
    FOREIGN KEY (session_id, workspace_id)
        REFERENCES pulsara_v3.sessions (id, workspace_id) ON DELETE CASCADE,
    FOREIGN KEY (session_id, anchor_entry_id)
        REFERENCES pulsara_v3.transcript_entries (session_id, id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (session_id, context_snapshot_id)
        REFERENCES pulsara_v3.context_snapshots (session_id, id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    CHECK ((base_kind = 'FULL_HISTORY' AND context_snapshot_id IS NULL AND source_through_sequence = 0)
        OR (base_kind = 'SNAPSHOT' AND context_snapshot_id IS NOT NULL)),
    CHECK (jsonb_typeof(model_call_binding) = 'object'
        AND model_call_binding ? 'connection_id' AND model_call_binding ? 'reasoning'
        AND model_call_binding - ARRAY['connection_id', 'reasoning']::text[] = '{}'::jsonb
        AND jsonb_typeof(model_call_binding->'connection_id') = 'string')
);

CREATE TABLE pulsara_v3.content_parts (
    id text CHECK (id ~ '^block:[a-z2-7]{25}[aeimquy4]$') PRIMARY KEY,
    session_id text NOT NULL,
    workspace_id text NOT NULL,
    entry_id text,
    queue_item_id text,
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    type text NOT NULL CHECK (type IN ('text','image','target','object','annotation','tool_call','data')),
    inline_value bytea,
    blob_id text REFERENCES pulsara_v3.blobs(id) ON DELETE RESTRICT,
    tool_call_id text,
    tool_name text,
    tool_arguments jsonb,
    data_media_type text,
    data_codec text,
    UNIQUE (session_id, id),
    UNIQUE (entry_id, ordinal),
    UNIQUE (queue_item_id, ordinal),
    UNIQUE (session_id, entry_id, tool_call_id),
    FOREIGN KEY (session_id, workspace_id)
        REFERENCES pulsara_v3.sessions(id,workspace_id) ON DELETE CASCADE,
    FOREIGN KEY (session_id, workspace_id, entry_id)
        REFERENCES pulsara_v3.transcript_entries(session_id,workspace_id,id) ON DELETE CASCADE,
    CHECK (num_nonnulls(entry_id,queue_item_id)=1),
    CHECK (
        (type='tool_call' AND entry_id IS NOT NULL
            AND tool_call_id IS NOT NULL AND tool_name IS NOT NULL
            AND tool_arguments IS NOT NULL AND jsonb_typeof(tool_arguments)='object'
            AND inline_value IS NULL AND blob_id IS NULL AND data_media_type IS NULL AND data_codec IS NULL)
        OR (type<>'tool_call' AND tool_call_id IS NULL AND tool_name IS NULL AND tool_arguments IS NULL
            AND ((type='image' AND inline_value IS NULL AND blob_id IS NOT NULL)
                OR (type<>'image' AND num_nonnulls(inline_value,blob_id)=1))
            AND ((type='data' AND inline_value IS NOT NULL AND data_media_type IS NOT NULL
                    AND data_codec IS NOT NULL AND data_codec<>'')
                OR ((type<>'data' OR inline_value IS NULL) AND data_media_type IS NULL AND data_codec IS NULL)))
    )
);

CREATE TABLE pulsara_v3.provider_assistant_replay_fragments (
    id text CHECK (id ~ '^replay:[a-z2-7]{25}[aeimquy4]$') PRIMARY KEY,
    session_id text NOT NULL,
    workspace_id text NOT NULL,
    assistant_entry_id text NOT NULL,
    assistant_entry_kind text NOT NULL CHECK (assistant_entry_kind IN (
        'ASSISTANT_MESSAGE', 'ASSISTANT_TOOL_REQUEST'
    )),
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
    FOREIGN KEY (session_id, workspace_id)
        REFERENCES pulsara_v3.sessions (id, workspace_id) ON DELETE CASCADE,
    FOREIGN KEY (session_id, assistant_entry_id, assistant_entry_kind)
        REFERENCES pulsara_v3.transcript_entries (session_id, id, entry_kind) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
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

CREATE TABLE pulsara_v3.tool_invocations (
    id text CHECK (id ~ '^call:[a-z2-7]{25}[aeimquy4]$') PRIMARY KEY,
    session_id text NOT NULL REFERENCES pulsara_v3.sessions(id) ON DELETE CASCADE,
    turn_id text NOT NULL,
    source_kind text NOT NULL CHECK (source_kind IN ('MODEL_TOOL_CALL','CODE_MODE_CALL')),
    assistant_entry_id text,
    tool_call_id text,
    source_exec_attempt_id text,
    call_sequence bigint CHECK (call_sequence >= 1),
    tool_name text,
    original_arguments jsonb,
    UNIQUE (session_id,id),
    UNIQUE (session_id,assistant_entry_id,tool_call_id),
    UNIQUE (session_id,source_exec_attempt_id,call_sequence),
    FOREIGN KEY (session_id,turn_id) REFERENCES pulsara_v3.turns(session_id,id) ON DELETE CASCADE,
    FOREIGN KEY (session_id,assistant_entry_id,tool_call_id)
        REFERENCES pulsara_v3.content_parts(session_id,entry_id,tool_call_id) ON DELETE CASCADE,
    CHECK (
        (source_kind='MODEL_TOOL_CALL' AND assistant_entry_id IS NOT NULL AND tool_call_id IS NOT NULL
            AND source_exec_attempt_id IS NULL AND call_sequence IS NULL AND tool_name IS NULL AND original_arguments IS NULL)
        OR (source_kind='CODE_MODE_CALL' AND assistant_entry_id IS NULL AND tool_call_id IS NULL
            AND source_exec_attempt_id IS NOT NULL AND call_sequence IS NOT NULL AND tool_name IS NOT NULL
            AND jsonb_typeof(original_arguments)='object')
    )
);

CREATE TABLE pulsara_v3.tool_execution_attempts (
    id text CHECK (id ~ '^attempt:[a-z2-7]{25}[aeimquy4]$') PRIMARY KEY,
    session_id text NOT NULL,
    invocation_id text NOT NULL,
    assistant_entry_id text,
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
    remote_identity_verification text,
    retry_of_attempt_id text,
    code_source_inline_content bytea,
    code_source_blob_id text REFERENCES pulsara_v3.blobs(id) ON DELETE RESTRICT,
    code_source_content_digest text CHECK (code_source_content_digest ~ '^sha256:[0-9a-f]{64}$'),
    code_source_content_size bigint CHECK (code_source_content_size >= 0),
    code_source_content_media_type text,
    code_source_content_codec text,
    code_cell_terminal_state text CHECK (code_cell_terminal_state IN ('completed', 'failed', 'terminated')),
    code_cell_diagnostic text,
    CHECK (code_cell_terminal_state IS NULL OR code_source_content_digest IS NOT NULL),
    CHECK (code_cell_diagnostic IS NULL OR code_cell_terminal_state IS NOT NULL),
    CHECK (
        (num_nonnulls(code_source_inline_content,code_source_blob_id,code_source_content_digest,
            code_source_content_size,code_source_content_media_type,code_source_content_codec)=0)
        OR ((code_source_inline_content IS NULL) <> (code_source_blob_id IS NULL)
            AND num_nonnulls(code_source_content_digest,code_source_content_size,code_source_content_media_type,code_source_content_codec)=4
            AND (code_source_inline_content IS NULL OR octet_length(code_source_inline_content)=code_source_content_size))
    ),
    FOREIGN KEY (session_id,invocation_id) REFERENCES pulsara_v3.tool_invocations(session_id,id) ON DELETE CASCADE,
    UNIQUE (session_id,invocation_id),
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    remote_identity_published_at timestamptz,
    UNIQUE (session_id, id),
    UNIQUE (session_id, assistant_entry_id, tool_call_id),
    UNIQUE (session_id, id, assistant_entry_id, tool_call_id),
    FOREIGN KEY (session_id, assistant_entry_id, tool_call_id)
        REFERENCES pulsara_v3.content_parts (session_id, entry_id, tool_call_id) ON DELETE CASCADE,
    FOREIGN KEY (session_id, retry_of_attempt_id)
        REFERENCES pulsara_v3.tool_execution_attempts (session_id, id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    CHECK ((remote_identity IS NULL) = (remote_identity_published_at IS NULL)),
    CHECK ((remote_identity IS NULL AND remote_identity_verification IS NULL)
        OR (remote_identity IS NOT NULL AND remote_identity ~ '^process:[a-z2-7]{25}[aeimquy4]$' AND remote_identity_verification IS NULL)
        OR (remote_identity IS NOT NULL AND remote_identity_verification IS NOT NULL AND remote_identity ~ '^mcp-tool:[a-z2-7]{25}[aeimquy4]$' AND remote_identity_verification ~ '^sha256:[0-9a-f]{64}$'))
);

ALTER TABLE pulsara_v3.tool_invocations ADD CONSTRAINT invocation_source_exec_fk
FOREIGN KEY (session_id,source_exec_attempt_id) REFERENCES pulsara_v3.tool_execution_attempts(session_id,id)
ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE pulsara_v3.tool_results (
    id text CHECK (id ~ '^result:[a-z2-7]{25}[aeimquy4]$') PRIMARY KEY,
    session_id text NOT NULL,
    workspace_id text NOT NULL,
    invocation_id text,
    tool_call_entry_id text,
    tool_call_id text NOT NULL,
    attempt_id text,
    result_record_kind text NOT NULL CHECK (result_record_kind IN ('EXECUTED', 'IMPORTED_HISTORY')),
    result_origin_kind text NOT NULL CHECK (result_origin_kind IN (
        'PHYSICAL_ATTEMPT', 'POLICY_NO_ATTEMPT', 'PLAN_CONTROL'
    )),
    control_plan_workflow_id text,
    control_plan_interaction_id text,
    permission_snapshot_fingerprint text CHECK (
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
    output_unavailability_reason text CHECK (output_unavailability_reason IN (
        'BODY_PUBLICATION_FAILED','BODY_PUBLICATION_UNCONFIRMED'
    )),
    public_fields json NOT NULL DEFAULT '{}'::json CHECK (json_typeof(public_fields)='object'),
    source_result_entry_id text,
    native_projection json,
    CHECK (native_projection IS NULL OR source_result_entry_id IS NOT NULL),
    FOREIGN KEY (session_id,source_result_entry_id)
        REFERENCES pulsara_v3.tool_results(session_id,result_entry_id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    result_facts jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(result_facts)='object'),
    accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (session_id, id),
    UNIQUE (session_id, tool_call_entry_id, tool_call_id),
    UNIQUE (session_id, result_entry_id),
    FOREIGN KEY (session_id,invocation_id) REFERENCES pulsara_v3.tool_invocations(session_id,id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    UNIQUE (session_id,invocation_id),
    CHECK ((result_record_kind='EXECUTED') = (invocation_id IS NOT NULL)),
    FOREIGN KEY (session_id, workspace_id)
        REFERENCES pulsara_v3.sessions (id, workspace_id) ON DELETE CASCADE,
    FOREIGN KEY (session_id, tool_call_entry_id, tool_call_id)
        REFERENCES pulsara_v3.content_parts (session_id, entry_id, tool_call_id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (session_id, attempt_id, tool_call_entry_id, tool_call_id)
        REFERENCES pulsara_v3.tool_execution_attempts (
            session_id, id, assistant_entry_id, tool_call_id
        ) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (session_id, result_entry_id)
        REFERENCES pulsara_v3.transcript_entries (session_id, id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    CHECK (
        (result_record_kind = 'IMPORTED_HISTORY'
            AND permission_snapshot_fingerprint IS NULL
            AND attempt_id IS NULL AND control_plan_workflow_id IS NULL
            AND control_plan_interaction_id IS NULL
            AND (
                (result_origin_kind = 'PHYSICAL_ATTEMPT' AND result_state IN ('SUCCESS', 'INVALID_ARGUMENTS', 'APPLICATION_ERROR', 'SYSTEM_ERROR', 'CANCELLED', 'CANCELLED_BEFORE_DISPATCH'))
                OR (result_origin_kind = 'POLICY_NO_ATTEMPT' AND result_state IN ('INVALID_ARGUMENTS', 'PERMISSION_DENIED', 'TOOL_UNAVAILABLE', 'CANCELLED_BEFORE_DISPATCH'))
                OR (result_origin_kind = 'PLAN_CONTROL' AND result_state IN ('SUCCESS', 'APPLICATION_ERROR', 'CANCELLED', 'CANCELLED_BEFORE_DISPATCH'))
            )) OR
        (result_record_kind = 'EXECUTED' AND permission_snapshot_fingerprint IS NOT NULL AND (
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
            -- An accepted attempt is admission; Steer can still win before physical dispatch.
            'SUCCESS', 'INVALID_ARGUMENTS', 'APPLICATION_ERROR', 'SYSTEM_ERROR', 'CANCELLED', 'CANCELLED_BEFORE_DISPATCH'
        )) OR
        (result_origin_kind = 'PLAN_CONTROL'
            AND attempt_id IS NULL
            AND num_nonnulls(control_plan_workflow_id, control_plan_interaction_id) = 1
            AND result_state IN ('SUCCESS', 'APPLICATION_ERROR', 'CANCELLED', 'CANCELLED_BEFORE_DISPATCH')
        )
        ))
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
    )
);

CREATE TABLE pulsara_v3.imported_tool_call_closures (
    session_id text NOT NULL,
    assistant_entry_id text NOT NULL,
    tool_call_id text NOT NULL,
    target_provider_input_through_sequence bigint NOT NULL CHECK (target_provider_input_through_sequence >= 0),
    closure_kind text NOT NULL CHECK (closure_kind IN (
        'INTERRUPTED_BEFORE_DISPATCH', 'INTERRUPTED_MAY_HAVE_PARTIALLY_EXECUTED', 'PLAN_INTERACTION_ABORTED'
    )),
    PRIMARY KEY (session_id, assistant_entry_id, tool_call_id),
    FOREIGN KEY (session_id, assistant_entry_id, tool_call_id)
        REFERENCES pulsara_v3.content_parts (session_id, entry_id, tool_call_id) ON DELETE CASCADE
);

CREATE INDEX idx_tool_results_cell_target
    ON pulsara_v3.tool_results (session_id, (public_fields->'target'->>'cell_id'))
    WHERE result_record_kind='EXECUTED' AND result_state='SUCCESS'
      AND public_fields->'target'->>'kind'='CELL';

CREATE TABLE pulsara_v3.prompt_queue_items (
    id text CHECK (id ~ '^queue:[a-z2-7]{25}[aeimquy4]$') PRIMARY KEY,
    session_id text NOT NULL,
    workspace_id text NOT NULL,
    queue_sequence bigint NOT NULL CHECK (queue_sequence >= 1),
    command_id text CHECK (command_id ~ '^command:[a-z2-7]{25}[aeimquy4]$'),
    client_submission_id text CHECK (client_submission_id ~ '^command:[a-z2-7]{25}[aeimquy4]$'),
    delivery_mode text NOT NULL CHECK (delivery_mode IN ('NEW_TURN', 'STEER_ACTIVE_TURN')),
    input_origin text NOT NULL CHECK (input_origin IN ('HUMAN_MESSAGE', 'HUMAN_STEER', 'SCHEDULED_TASK', 'AGENT_INSTRUCTION', 'AGENT_MESSAGE', 'AGENT_MATERIAL', 'CELL_OUTPUT')),
    source_session_id text,
    source_work_id text,
    source_turn_id text,
    source_tool_attempt_id text,
    source_cell_id text CHECK (source_cell_id ~ '^cell:[a-z2-7]{25}[aeimquy4]$'),
    source_record_sequence bigint CHECK (source_record_sequence >= 1),
    cell_output_kind text CHECK (cell_output_kind IN ('log','return','diagnostic','tool_result')),
    CHECK ((input_origin='CELL_OUTPUT' AND source_cell_id IS NOT NULL
            AND source_record_sequence IS NOT NULL AND cell_output_kind IS NOT NULL)
        OR (input_origin<>'CELL_OUTPUT' AND source_cell_id IS NULL
            AND source_record_sequence IS NULL AND cell_output_kind IS NULL)),
    work_inputs jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(work_inputs)='array'),
    work_title text CHECK (work_title IS NULL OR (input_origin = 'AGENT_INSTRUCTION'
        AND btrim(work_title) <> '' AND octet_length(work_title) <= 256)),
    CHECK ((input_origin IN ('AGENT_INSTRUCTION','AGENT_MESSAGE','AGENT_MATERIAL','CELL_OUTPUT')) = (command_id IS NULL AND client_submission_id IS NULL)),
    CHECK (input_origin NOT IN ('AGENT_INSTRUCTION','AGENT_MESSAGE','AGENT_MATERIAL','CELL_OUTPUT') OR source_session_id IS NOT NULL),
    scheduled_task_id text,
    scheduled_task_revision bigint,
    scheduled_due_at timestamptz,
    scheduled_trigger_source text,
    CHECK (
        (input_origin = 'SCHEDULED_TASK'
         AND scheduled_task_id IS NOT NULL AND scheduled_task_revision >= 1 AND scheduled_due_at IS NOT NULL
         AND scheduled_trigger_source IS NOT NULL AND scheduled_trigger_source IN ('TIMER', 'RUN_ONCE')) OR
        (input_origin IN ('HUMAN_MESSAGE', 'HUMAN_STEER', 'AGENT_INSTRUCTION', 'AGENT_MESSAGE', 'AGENT_MATERIAL', 'CELL_OUTPUT') AND scheduled_task_id IS NULL
         AND scheduled_task_revision IS NULL AND scheduled_due_at IS NULL AND scheduled_trigger_source IS NULL
         AND ((input_origin = 'CELL_OUTPUT') OR (input_origin = 'AGENT_INSTRUCTION' AND delivery_mode = 'NEW_TURN') OR (input_origin IN ('AGENT_MESSAGE','AGENT_MATERIAL') AND delivery_mode = 'STEER_ACTIVE_TURN') OR input_origin = CASE delivery_mode WHEN 'NEW_TURN' THEN 'HUMAN_MESSAGE' ELSE 'HUMAN_STEER' END))
    ),
    model_call_binding jsonb,
    target_turn_id text,
    permission_snapshot_id text CHECK (permission_snapshot_id ~ '^permission:[a-z2-7]{25}[aeimquy4]$'),
    requested_permission_mode text CHECK (requested_permission_mode IN (
        'read-only', 'ask-permissions', 'accept-edits', 'bypass-permissions'
    )),
    effective_permission_mode text CHECK (effective_permission_mode IN (
        'read-only', 'ask-permissions', 'accept-edits', 'bypass-permissions'
    )),
    permission_admission_source text CHECK (permission_admission_source IN (
        'USER_SUBMISSION', 'TERMINAL_OBSERVATION',
        'RUNTIME_PLAN_CONTINUATION'
    )),
    permission_overlay text CHECK (permission_overlay IN ('NONE', 'PLAN_READ_ONLY')),
    permission_plan_context_ordinal bigint CHECK (permission_plan_context_ordinal >= 0),
    permission_plan_workflow_id text,
    permission_plan_revision_at_admission bigint,
    permission_inherited_from_turn_id text,
    permission_snapshot_fingerprint text CHECK (
        permission_snapshot_fingerprint ~ '^sha256:[0-9a-f]{64}$'
    ),
    pending_plan_handoff_workflow_id text,
    pending_plan_handoff_interaction_id text,
    pending_plan_handoff_kind text CHECK (pending_plan_handoff_kind IN (
        'CANCELLED_PLAN', 'FORCE_EXITED_PLAN'
    )),
    status text NOT NULL CHECK (status IN ('PENDING', 'CONSUMED', 'CANCELLED', 'REJECTED')),
    consumed_entry_id text,
    terminal_reason text,
    accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    terminal_at timestamptz,
    UNIQUE (session_id, id),
    UNIQUE (session_id, workspace_id, id),
    UNIQUE (session_id, queue_sequence),
    FOREIGN KEY (session_id, workspace_id)
        REFERENCES pulsara_v3.sessions (id, workspace_id) ON DELETE CASCADE,
    FOREIGN KEY (session_id, command_id)
        REFERENCES pulsara_v3.session_commands (session_id, command_id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (session_id, target_turn_id)
        REFERENCES pulsara_v3.turns (session_id, id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (session_id, consumed_entry_id)
        REFERENCES pulsara_v3.transcript_entries (session_id, id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    CHECK ((delivery_mode = 'NEW_TURN') = (target_turn_id IS NULL)),
    CHECK ((status = 'PENDING') = (terminal_at IS NULL)),
    CHECK ((status = 'CONSUMED') = (consumed_entry_id IS NOT NULL)),
    CHECK ((delivery_mode = 'NEW_TURN') = (model_call_binding IS NOT NULL)),
    CHECK (
        model_call_binding IS NULL OR (
            jsonb_typeof(model_call_binding) = 'object'
            AND model_call_binding ? 'connection_id'
            AND model_call_binding ? 'reasoning'
            AND model_call_binding - ARRAY['connection_id', 'reasoning']::text[] = '{}'::jsonb
            AND jsonb_typeof(model_call_binding->'connection_id') = 'string'
        )
    ),
    CONSTRAINT ck_prompt_queue_permission_admission_exact CHECK (
        (delivery_mode = 'STEER_ACTIVE_TURN'
            AND permission_snapshot_id IS NULL
            AND requested_permission_mode IS NULL
            AND effective_permission_mode IS NULL
            AND permission_admission_source IS NULL
            AND permission_overlay IS NULL
            AND permission_plan_context_ordinal IS NULL
            AND permission_snapshot_fingerprint IS NULL) OR
        (input_origin IN ('HUMAN_MESSAGE','CELL_OUTPUT') AND delivery_mode='NEW_TURN'
            AND requested_permission_mode IS NOT NULL
            AND permission_snapshot_id IS NULL
            AND effective_permission_mode IS NULL
            AND permission_admission_source IS NULL
            AND permission_overlay IS NULL
            AND permission_plan_context_ordinal IS NULL
            AND permission_plan_workflow_id IS NULL
            AND permission_plan_revision_at_admission IS NULL
            AND permission_inherited_from_turn_id IS NULL
            AND permission_snapshot_fingerprint IS NULL) OR
        (input_origin IN ('SCHEDULED_TASK','AGENT_INSTRUCTION') AND delivery_mode='NEW_TURN'
            AND permission_snapshot_id IS NOT NULL
            AND requested_permission_mode IS NOT NULL
            AND effective_permission_mode IS NOT NULL
            AND permission_admission_source IN ('USER_SUBMISSION','RUNTIME_PLAN_CONTINUATION','TERMINAL_OBSERVATION')
            AND permission_overlay IS NOT NULL
            AND permission_plan_context_ordinal IS NOT NULL
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
            AND input_origin = 'SCHEDULED_TASK')
    )
);
CREATE UNIQUE INDEX uq_pulsara_v3_queue_plan_handoff_claim
    ON pulsara_v3.prompt_queue_items (
        session_id, pending_plan_handoff_workflow_id
    ) WHERE pending_plan_handoff_workflow_id IS NOT NULL;

ALTER TABLE pulsara_v3.content_parts ADD CONSTRAINT content_parts_queue_owner_fk
    FOREIGN KEY (session_id, workspace_id, queue_item_id)
    REFERENCES pulsara_v3.prompt_queue_items(session_id,workspace_id,id) ON DELETE CASCADE;

CREATE TABLE pulsara_v3.scheduled_tasks (
    id text CHECK (id ~ '^schedule:[a-z2-7]{25}[aeimquy4]$') PRIMARY KEY,
    session_id text NOT NULL REFERENCES pulsara_v3.sessions(id) ON DELETE CASCADE,
    name text NOT NULL CHECK (length(btrim(name)) > 0),
    prompt text NOT NULL CHECK (length(btrim(prompt)) > 0),
    schedule jsonb NOT NULL CHECK (jsonb_typeof(schedule) = 'object'),
    timezone text NOT NULL CHECK (length(timezone) > 0),
    permission_mode text NOT NULL CHECK (permission_mode IN ('read-only', 'ask-permissions', 'accept-edits', 'bypass-permissions')),
    status text NOT NULL CHECK (status IN ('ACTIVE', 'PAUSED')),
    next_run_at timestamptz,
    revision bigint NOT NULL CHECK (revision >= 1),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK ((status = 'ACTIVE') = (next_run_at IS NOT NULL))
);
CREATE INDEX scheduled_tasks_session ON pulsara_v3.scheduled_tasks(session_id, id);
-- Product rule: one session may have at most one ACTIVE plan; inactive history is unbounded.
CREATE UNIQUE INDEX scheduled_tasks_one_active_session ON pulsara_v3.scheduled_tasks(session_id) WHERE status = 'ACTIVE';
CREATE INDEX scheduled_tasks_due ON pulsara_v3.scheduled_tasks(next_run_at, id) WHERE status = 'ACTIVE';
CREATE INDEX prompt_queue_scheduled_status ON pulsara_v3.prompt_queue_items(session_id, scheduled_task_id, status);
CREATE INDEX prompt_queue_agent_source_attempt ON pulsara_v3.prompt_queue_items(source_session_id, source_tool_attempt_id, id) WHERE input_origin = 'AGENT_INSTRUCTION';

CREATE TABLE pulsara_v3.assistant_visualizations (
    session_id text NOT NULL,
    workspace_id text NOT NULL,
    assistant_entry_id text NOT NULL,
    assistant_entry_kind text NOT NULL DEFAULT 'ASSISTANT_MESSAGE'
        CHECK (assistant_entry_kind = 'ASSISTANT_MESSAGE'),
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    turn_id text,
    imported_history_group_id text,
    source_filename text NOT NULL,
    state text NOT NULL CHECK (state IN ('READY', 'FAILED')),
    blob_id text,
    failure_code text,
    failure_detail text,
    PRIMARY KEY (session_id, assistant_entry_id, ordinal),
    FOREIGN KEY (session_id, workspace_id)
        REFERENCES pulsara_v3.sessions (id, workspace_id) ON DELETE CASCADE,
    FOREIGN KEY (session_id, assistant_entry_id, assistant_entry_kind)
        REFERENCES pulsara_v3.transcript_entries (session_id, id, entry_kind)
        ON DELETE CASCADE,
    FOREIGN KEY (session_id, assistant_entry_id, turn_id)
        REFERENCES pulsara_v3.transcript_entries (session_id, id, turn_id)
        ON DELETE CASCADE,
    FOREIGN KEY (session_id, assistant_entry_id, imported_history_group_id)
        REFERENCES pulsara_v3.transcript_entries (
            session_id, id, imported_history_group_id
        ) ON DELETE CASCADE,
    FOREIGN KEY (blob_id)
        REFERENCES pulsara_v3.blobs (id) ON DELETE RESTRICT,
    CHECK (
        (turn_id IS NOT NULL AND imported_history_group_id IS NULL)
        OR
        (turn_id IS NULL AND imported_history_group_id IS NOT NULL)
    ),
    CHECK (
        (state = 'READY' AND blob_id IS NOT NULL
            AND failure_code IS NULL AND failure_detail IS NULL)
        OR
        (state = 'FAILED' AND blob_id IS NULL
            AND failure_code IS NOT NULL AND failure_detail IS NOT NULL)
    )
);
CREATE INDEX ix_pulsara_v3_assistant_visualization_blob
    ON pulsara_v3.assistant_visualizations (blob_id, workspace_id)
    WHERE blob_id IS NOT NULL;

CREATE TABLE pulsara_v3.interaction_decisions (
    id text CHECK (id ~ '^decision:[a-z2-7]{25}[aeimquy4]$') PRIMARY KEY,
    session_id text NOT NULL,
    command_id text CHECK (command_id ~ '^command:[a-z2-7]{25}[aeimquy4]$'),
    subject_kind text NOT NULL CHECK (subject_kind IN ('TOOL_CALL', 'MCP_INPUT')),
    subject_invocation_id text,
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
    FOREIGN KEY (session_id,subject_invocation_id) REFERENCES pulsara_v3.tool_invocations(session_id,id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (session_id) REFERENCES pulsara_v3.sessions (id) ON DELETE CASCADE,
    FOREIGN KEY (session_id, command_id)
        REFERENCES pulsara_v3.session_commands (session_id, command_id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (session_id, subject_tool_call_entry_id, subject_tool_call_id)
        REFERENCES pulsara_v3.content_parts (
            session_id, entry_id, tool_call_id
        ) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (session_id, subject_turn_id)
        REFERENCES pulsara_v3.turns (session_id, id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    CHECK ((subject_tool_call_entry_id IS NULL) = (subject_tool_call_id IS NULL)),
    CHECK (num_nonnulls(subject_invocation_id, subject_turn_id) = 1),
    CHECK (
        (subject_kind = 'TOOL_CALL' AND subject_invocation_id IS NOT NULL) OR
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
        session_id, subject_invocation_id
    ) WHERE subject_kind = 'TOOL_CALL' AND actor_kind = 'machine';
CREATE UNIQUE INDEX uq_pulsara_v3_tool_call_human_decision
    ON pulsara_v3.interaction_decisions (
        session_id, subject_invocation_id
    ) WHERE subject_kind = 'TOOL_CALL' AND actor_kind = 'human';

CREATE TABLE pulsara_v3.plan_workflows (
    id text CHECK (id ~ '^plan:[a-z2-7]{25}[aeimquy4]$') PRIMARY KEY,
    session_id text NOT NULL,
    workspace_id text NOT NULL,
    workflow_ordinal bigint NOT NULL CHECK (workflow_ordinal >= 1),
    status text NOT NULL CHECK (status IN (
        'ACTIVE', 'APPROVED', 'CANCELLED', 'FORCE_EXITED', 'EXITED'
    )),
    entered_by text NOT NULL CHECK (entered_by IN ('USER', 'AGENT')),
    entry_command_id text,
    entry_turn_id text,
    entry_assistant_entry_id text,
    entry_tool_call_id text,
    resume_permission_mode text NOT NULL CHECK (resume_permission_mode IN (
        'read-only', 'ask-permissions', 'accept-edits', 'bypass-permissions'
    )),
    workflow_revision bigint NOT NULL CHECK (workflow_revision >= 1),
    accepted_plan_interaction_id text,
    accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    terminal_at timestamptz,
    UNIQUE (session_id, id),
    UNIQUE (session_id, workflow_ordinal),
    FOREIGN KEY (session_id, workspace_id)
        REFERENCES pulsara_v3.sessions (id, workspace_id) ON DELETE CASCADE,
    FOREIGN KEY (session_id, entry_command_id)
        REFERENCES pulsara_v3.session_commands (session_id, command_id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (session_id, entry_turn_id)
        REFERENCES pulsara_v3.turns (session_id, id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (session_id, entry_assistant_entry_id, entry_tool_call_id)
        REFERENCES pulsara_v3.content_parts (
            session_id, entry_id, tool_call_id
        ) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
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
    id text CHECK (id ~ '^plan-interaction:[a-z2-7]{25}[aeimquy4]$') PRIMARY KEY,
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
    request_semantic_digest text NOT NULL CHECK (
        request_semantic_digest ~ '^sha256:[0-9a-f]{64}$'
    ),
    control_tool_result_id text,
    plan_file_path text CHECK (plan_file_path IS NULL OR btrim(plan_file_path)<>''),
    plan_file_error text CHECK (plan_file_error IS NULL OR btrim(plan_file_error)<>''),
    CHECK (num_nonnulls(plan_file_path,plan_file_error)<=1),
    CHECK (kind='DRAFT_REVIEW' OR num_nonnulls(plan_file_path,plan_file_error)=0),
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
        REFERENCES pulsara_v3.sessions (id, workspace_id) ON DELETE CASCADE,
    FOREIGN KEY (session_id, plan_workflow_id)
        REFERENCES pulsara_v3.plan_workflows (session_id, id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (session_id, origin_turn_id)
        REFERENCES pulsara_v3.turns (session_id, id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (session_id, assistant_entry_id, tool_call_id)
        REFERENCES pulsara_v3.content_parts (
            session_id, entry_id, tool_call_id
        ) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (session_id, control_tool_result_id)
        REFERENCES pulsara_v3.tool_results (session_id, id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (session_id, resolution_command_id)
        REFERENCES pulsara_v3.session_commands (session_id, command_id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (session_id, decision_continuation_entry_id)
        REFERENCES pulsara_v3.transcript_entries (session_id, id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    CHECK (
        (kind = 'QUESTION' AND status IN ('OPEN', 'ANSWERED', 'CANCELLED', 'ABORTED')) OR
        (kind = 'DRAFT_REVIEW' AND status IN (
            'OPEN', 'APPROVED', 'REVISION_REQUESTED', 'CANCELLED', 'ABORTED'
        ))
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
        (kind = 'QUESTION' AND status = 'CANCELLED'
            AND control_tool_result_id IS NOT NULL AND answer_kind IS NULL
            AND selected_option_ordinal IS NULL) OR
        (kind = 'QUESTION' AND status IN ('OPEN', 'ABORTED')
            AND control_tool_result_id IS NULL AND answer_kind IS NULL
            AND selected_option_ordinal IS NULL) OR
        (kind = 'DRAFT_REVIEW' AND answer_kind IS NULL AND selected_option_ordinal IS NULL
            AND ((status IN ('OPEN','ABORTED') AND control_tool_result_id IS NULL)
                OR (status NOT IN ('OPEN','ABORTED') AND control_tool_result_id IS NOT NULL)))
    ),
    CHECK (
        (status = 'APPROVED'
            AND decision_continuation_entry_id IS NOT NULL) OR
        (status <> 'APPROVED'
            AND decision_continuation_entry_id IS NULL)
    ),
    CHECK (kind = 'DRAFT_REVIEW' OR feedback_present = false),
    CHECK (status = 'REVISION_REQUESTED' OR feedback_present = false)
);
CREATE UNIQUE INDEX uq_pulsara_v3_open_plan_interaction
    ON pulsara_v3.plan_interactions (plan_workflow_id) WHERE status = 'OPEN';

ALTER TABLE pulsara_v3.plan_workflows ADD CONSTRAINT plan_workflows_accepted_interaction_fk
    FOREIGN KEY (session_id, accepted_plan_interaction_id)
    REFERENCES pulsara_v3.plan_interactions (session_id, id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE pulsara_v3.turns ADD CONSTRAINT turns_permission_plan_workflow_fk
    FOREIGN KEY (session_id, permission_plan_workflow_id)
    REFERENCES pulsara_v3.plan_workflows (session_id, id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE pulsara_v3.turns ADD CONSTRAINT turns_permission_inherited_turn_fk
    FOREIGN KEY (session_id, permission_inherited_from_turn_id)
    REFERENCES pulsara_v3.turns (session_id, id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE pulsara_v3.transcript_entries ADD CONSTRAINT transcript_entries_plan_workflow_fk
    FOREIGN KEY (session_id, source_plan_workflow_id)
    REFERENCES pulsara_v3.plan_workflows (session_id, id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE pulsara_v3.transcript_entries ADD CONSTRAINT transcript_entries_plan_interaction_fk
    FOREIGN KEY (session_id, source_plan_interaction_id)
    REFERENCES pulsara_v3.plan_interactions (session_id, id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED;
CREATE UNIQUE INDEX uq_pulsara_v3_entry_plan_terminal_handoff_claim
    ON pulsara_v3.transcript_entries (session_id, source_plan_workflow_id)
    WHERE entry_owner_kind = 'EXECUTED_TURN' AND source_plan_handoff_kind IN ('CANCELLED_PLAN', 'FORCE_EXITED_PLAN');
ALTER TABLE pulsara_v3.tool_results ADD CONSTRAINT tool_results_plan_workflow_fk
    FOREIGN KEY (session_id, control_plan_workflow_id)
    REFERENCES pulsara_v3.plan_workflows (session_id, id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE pulsara_v3.tool_results ADD CONSTRAINT tool_results_plan_interaction_fk
    FOREIGN KEY (session_id, control_plan_interaction_id)
    REFERENCES pulsara_v3.plan_interactions (session_id, id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE pulsara_v3.prompt_queue_items ADD CONSTRAINT queue_permission_plan_workflow_fk
    FOREIGN KEY (session_id, permission_plan_workflow_id)
    REFERENCES pulsara_v3.plan_workflows (session_id, id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE pulsara_v3.prompt_queue_items ADD CONSTRAINT queue_handoff_plan_workflow_fk
    FOREIGN KEY (session_id, pending_plan_handoff_workflow_id)
    REFERENCES pulsara_v3.plan_workflows (session_id, id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE pulsara_v3.prompt_queue_items ADD CONSTRAINT queue_handoff_plan_interaction_fk
    FOREIGN KEY (session_id, pending_plan_handoff_interaction_id)
    REFERENCES pulsara_v3.plan_interactions (session_id, id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED;



CREATE TABLE pulsara_v3.memory_facts (
    id text CHECK (id ~ '^memory:[a-z2-7]{25}[aeimquy4]$') PRIMARY KEY,
    memory_domain_id text NOT NULL,
    context_id text NOT NULL CHECK (
        context_id = 'ctx:global'
        OR context_id ~ '^workspace:[a-z2-7]{25}[aeimquy4]$'
    ),
    project_workspace_id text GENERATED ALWAYS AS (
        CASE WHEN context_id = 'ctx:global' THEN NULL ELSE context_id END
    ) STORED,
    fact_kind text NOT NULL CHECK (fact_kind IN (
        'USER_PROFILE', 'RESPONSE_PREFERENCE', 'FACT'
    )),
    -- A single-line title excludes the line boundaries recognized by str.splitlines().
    title text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 128
        AND title !~ U&'[\000a\000b\000c\000d\001c\001d\001e\0085\2028\2029]'),
    statement text NOT NULL,
    CHECK ((fact_kind='RESPONSE_PREFERENCE' AND statement='') OR
        (fact_kind<>'RESPONSE_PREFERENCE' AND char_length(statement) BETWEEN 1 AND 8000)),
    accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    search_text text GENERATED ALWAYS AS ('title: ' || title || E'\nstatement: ' || statement) STORED,
    statement_en text GENERATED ALWAYS AS ('title: ' || title || E'\nstatement: ' || statement) STORED,
    statement_tech text GENERATED ALWAYS AS ('title: ' || title || E'\nstatement: ' || statement) STORED,
    UNIQUE (memory_domain_id, id),
    UNIQUE (memory_domain_id, context_id, id),
    FOREIGN KEY (memory_domain_id, project_workspace_id)
        REFERENCES pulsara_v3.workspaces (memory_domain_id, id)
        ON DELETE RESTRICT
);
CREATE INDEX idx_pulsara_v3_memory_fact_project_workspace_fk
    ON pulsara_v3.memory_facts (memory_domain_id, project_workspace_id)
    WHERE project_workspace_id IS NOT NULL;
CREATE INDEX idx_pulsara_v3_memory_bm25 ON pulsara_v3.memory_facts USING bm25
    (id,search_text,statement_en,statement_tech)
    WITH (key_field='id',text_fields='{"search_text":{"tokenizer":{"type":"jieba","search_mode":true,"stopwords":["!","\"","#","$","%","&","''","(",")","*","+",",","-",".","/",":",";","<","=",">","?","@","[","\\","]","^","_","`","{","|","}","~"," ","\t","\n","\r","，","。","！","？","；","：","、","（","）","【","】","《","》","“","”","‘","’","—","…"]}},"statement_en":{"tokenizer":{"type":"default","stemmer":"English"}},"statement_tech":{"tokenizer":{"type":"regex","pattern":"[./:@+-]*[A-Za-z0-9_][A-Za-z0-9_./:@+-]*"}}}');

CREATE TABLE pulsara_v3.memory_embeddings (
    memory_domain_id text NOT NULL,
    fact_id text NOT NULL,
    fact_semantic_digest text NOT NULL CHECK (
        fact_semantic_digest ~ '^sha256:[0-9a-f]{64}$'
    ),
    embedding_contract_id text NOT NULL,
    embedding public.vector(1024) NOT NULL,
    embedded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (memory_domain_id, fact_id),
    FOREIGN KEY (memory_domain_id, fact_id)
        REFERENCES pulsara_v3.memory_facts (memory_domain_id, id) ON DELETE CASCADE
);
CREATE INDEX idx_pulsara_v3_memory_embeddings_hnsw
    ON pulsara_v3.memory_embeddings USING hnsw (embedding public.vector_cosine_ops);

CREATE FUNCTION pulsara_v3.seal_memory_fact()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    -- Match normalize_memory_text's Python str.strip(), including C0 information
    -- separators. POSIX [:space:] depends on locale and misses Unicode spaces
    -- in a C-locale database; btrim's explicit character set does not.
    trim_characters constant text := U&'\0009\000a\000b\000c\000d\001c\001d\001e\001f\0020\0085\00a0\1680\2000\2001\2002\2003\2004\2005\2006\2007\2008\2009\200a\2028\2029\202f\205f\3000';
BEGIN
    -- Normalize before text limits, immutable-title comparison and no-op
    -- detection. Discard preference bodies before ordinary body validation.
    IF NEW.fact_kind='RESPONSE_PREFERENCE' THEN
        NEW.statement := '';
    ELSE
        NEW.statement := btrim(normalize(replace(replace(NEW.statement, E'\r\n', E'\n'), E'\r', E'\n'), NFC), trim_characters);
    END IF;
    NEW.title := btrim(normalize(replace(replace(NEW.title, E'\r\n', E'\n'), E'\r', E'\n'), NFC), trim_characters);
    IF TG_OP='UPDATE' THEN
        IF ROW(NEW.id,NEW.memory_domain_id,NEW.fact_kind,NEW.title,NEW.accepted_at)
            IS DISTINCT FROM ROW(OLD.id,OLD.memory_domain_id,OLD.fact_kind,OLD.title,OLD.accepted_at) THEN
            RAISE EXCEPTION 'memory identity, title and kind are immutable' USING ERRCODE='23514';
        END IF;
        IF ROW(NEW.statement,NEW.context_id) IS DISTINCT FROM ROW(OLD.statement,OLD.context_id) THEN
            NEW.updated_at := GREATEST(clock_timestamp(), OLD.updated_at + interval '1 microsecond');
        ELSE
            NEW.updated_at := OLD.updated_at;
        END IF;
    ELSE
        NEW.updated_at := NEW.accepted_at;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER trg_pulsara_v3_memory_fact BEFORE INSERT OR UPDATE ON pulsara_v3.memory_facts
FOR EACH ROW EXECUTE FUNCTION pulsara_v3.seal_memory_fact();
REVOKE ALL ON FUNCTION pulsara_v3.seal_memory_fact() FROM PUBLIC;
CREATE TABLE pulsara_v3.agent_events (
    event_id text CHECK (event_id ~ '^event:[a-z2-7]{25}[aeimquy4]$') PRIMARY KEY,
    workspace_id text NOT NULL,
    session_id text NOT NULL,
    event_sequence bigint NOT NULL CHECK (event_sequence >= 1),
    namespace text NOT NULL CHECK (namespace = 'pulsara.core'),
    event_type text NOT NULL CHECK (event_type IN (
        'UserMessageAccepted', 'AssistantMessageAccepted', 'AssistantToolRequestAccepted',
        'ToolResultAccepted', 'TurnCompleted', 'TurnInterrupted', 'UserSteerAccepted',
        'CapabilityDecisionAccepted', 'InteractionDecisionAccepted', 'ToolAttemptAccepted',
        'ToolRemoteIdentityPublished', 'PromptQueued', 'PromptConsumed', 'PromptCancelled',
        'PromptRejected', 'CompactionAdopted',
        'TerminalObservationAccepted',
        'PlanWorkflowEntered',
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
    subject_plan_workflow_id text,
    subject_plan_interaction_id text,
    UNIQUE (session_id, event_sequence),
    FOREIGN KEY (session_id, workspace_id)
        REFERENCES pulsara_v3.sessions (id, workspace_id) ON DELETE CASCADE,
    FOREIGN KEY (session_id, subject_turn_id)
        REFERENCES pulsara_v3.turns (session_id, id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (session_id, subject_entry_id)
        REFERENCES pulsara_v3.transcript_entries (session_id, id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (session_id, subject_tool_attempt_id)
        REFERENCES pulsara_v3.tool_execution_attempts (session_id, id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (session_id, subject_queue_item_id)
        REFERENCES pulsara_v3.prompt_queue_items (session_id, id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (session_id, subject_interaction_decision_id)
        REFERENCES pulsara_v3.interaction_decisions (session_id, id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (session_id, subject_context_binding_revision_id)
        REFERENCES pulsara_v3.turn_context_binding_revisions (session_id, id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (session_id, subject_plan_workflow_id)
        REFERENCES pulsara_v3.plan_workflows (session_id, id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (session_id, subject_plan_interaction_id)
        REFERENCES pulsara_v3.plan_interactions (session_id, id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    CHECK (num_nonnulls(
        subject_turn_id, subject_entry_id, subject_tool_attempt_id,
        subject_queue_item_id, subject_interaction_decision_id,
        subject_context_binding_revision_id,
        subject_plan_workflow_id, subject_plan_interaction_id
    ) = 1),
    CHECK (
        (event_type IN ('UserMessageAccepted', 'AssistantMessageAccepted',
            'AssistantToolRequestAccepted', 'ToolResultAccepted', 'UserSteerAccepted',
            'TerminalObservationAccepted')
            AND subject_entry_id IS NOT NULL) OR
        (event_type IN ('TurnCompleted', 'TurnInterrupted') AND subject_turn_id IS NOT NULL) OR
        (event_type IN ('CapabilityDecisionAccepted', 'InteractionDecisionAccepted')
            AND subject_interaction_decision_id IS NOT NULL) OR
        (event_type IN ('ToolAttemptAccepted', 'ToolRemoteIdentityPublished')
            AND subject_tool_attempt_id IS NOT NULL) OR
        (event_type IN ('PromptQueued', 'PromptConsumed', 'PromptCancelled', 'PromptRejected')
            AND subject_queue_item_id IS NOT NULL) OR
        (event_type = 'CompactionAdopted' AND subject_context_binding_revision_id IS NOT NULL) OR
        (event_type IN ('PlanWorkflowEntered', 'PlanWorkflowExited')
            AND subject_plan_workflow_id IS NOT NULL)
        OR (event_type IN ('PlanQuestionAsked', 'PlanQuestionAnswered',
                'PlanDraftSubmitted', 'PlanDraftDecisionAccepted')
            AND subject_plan_interaction_id IS NOT NULL)
        OR (event_type = 'PlanContinuationAccepted' AND subject_entry_id IS NOT NULL)
    )
);

ALTER TABLE pulsara_v3.session_commands ADD CONSTRAINT session_commands_target_turn_fk
    FOREIGN KEY (session_id, target_turn_id) REFERENCES pulsara_v3.turns (session_id, id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE pulsara_v3.session_commands ADD CONSTRAINT session_commands_target_entry_fk
    FOREIGN KEY (session_id, target_entry_id) REFERENCES pulsara_v3.transcript_entries (session_id, id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE pulsara_v3.session_commands ADD CONSTRAINT session_commands_target_queue_fk
    FOREIGN KEY (session_id, target_queue_item_id) REFERENCES pulsara_v3.prompt_queue_items (session_id, id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE pulsara_v3.session_commands ADD CONSTRAINT session_commands_target_interaction_fk
    FOREIGN KEY (session_id, target_interaction_decision_id) REFERENCES pulsara_v3.interaction_decisions (session_id, id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE pulsara_v3.session_commands ADD CONSTRAINT session_commands_target_plan_workflow_fk
    FOREIGN KEY (session_id, target_plan_workflow_id)
    REFERENCES pulsara_v3.plan_workflows (session_id, id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE pulsara_v3.session_commands ADD CONSTRAINT session_commands_target_plan_interaction_fk
    FOREIGN KEY (session_id, target_plan_interaction_id)
    REFERENCES pulsara_v3.plan_interactions (session_id, id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED;

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
    IF TG_TABLE_NAME = 'tool_invocations' THEN
        IF NEW.source_kind='MODEL_TOOL_CALL' AND NOT EXISTS (
            SELECT 1 FROM pulsara_v3.content_parts b JOIN pulsara_v3.transcript_entries e
              ON e.session_id=b.session_id AND e.id=b.entry_id
            WHERE b.session_id=NEW.session_id AND b.entry_id=NEW.assistant_entry_id
              AND b.tool_call_id=NEW.tool_call_id AND b.type='tool_call'
              AND e.entry_owner_kind='EXECUTED_TURN' AND e.turn_id=NEW.turn_id
        ) THEN
            RAISE EXCEPTION 'model invocation requires its exact executed tool call' USING ERRCODE='23514';
        END IF;
        IF NEW.source_kind='CODE_MODE_CALL' AND NOT EXISTS (
            SELECT 1 FROM pulsara_v3.tool_execution_attempts a
            JOIN pulsara_v3.tool_invocations source ON source.session_id=a.session_id AND source.id=a.invocation_id
            JOIN pulsara_v3.content_parts b ON b.session_id=source.session_id
              AND b.entry_id=source.assistant_entry_id AND b.tool_call_id=source.tool_call_id
            WHERE a.session_id=NEW.session_id AND a.id=NEW.source_exec_attempt_id
              AND source.source_kind='MODEL_TOOL_CALL' AND source.turn_id=NEW.turn_id AND b.tool_name='exec_cell'
        ) THEN
            RAISE EXCEPTION 'code invocation requires its source exec_cell attempt' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
    END IF;

    IF TG_TABLE_NAME = 'turns' THEN
        IF NEW.status<>'RUNNING' AND EXISTS (
            SELECT 1 FROM pulsara_v3.plan_interactions
            WHERE session_id=NEW.session_id AND origin_turn_id=NEW.id AND status='OPEN'
        ) THEN
            RAISE EXCEPTION 'an open Plan interaction keeps its origin turn running' USING ERRCODE='23514';
        END IF;
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
            OLD.permission_snapshot_fingerprint IS DISTINCT FROM NEW.permission_snapshot_fingerprint
        ) THEN
            RAISE EXCEPTION 'turn permission snapshot is immutable'
                USING ERRCODE = '23514';
        END IF;
        SELECT entry_kind, turn_id INTO observed_kind, observed_turn_id
        FROM pulsara_v3.transcript_entries
        WHERE session_id = NEW.session_id AND id = NEW.initial_entry_id
          AND entry_owner_kind = 'EXECUTED_TURN';
        IF observed_turn_id IS DISTINCT FROM NEW.id THEN
            RAISE EXCEPTION 'turn initial entry must belong to its exact turn and scope'
                USING ERRCODE = '23514';
        END IF;
        IF observed_kind NOT IN ('USER_MESSAGE','TERMINAL_OBSERVATION','PLAN_CONTINUATION') THEN
            RAISE EXCEPTION 'turn initial entry kind is invalid for its scope'
                USING ERRCODE = '23514';
        END IF;
        IF NEW.final_entry_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM pulsara_v3.transcript_entries
            WHERE session_id = NEW.session_id AND id = NEW.final_entry_id
              AND entry_owner_kind = 'EXECUTED_TURN' AND turn_id = NEW.id
              AND workspace_id = NEW.workspace_id

        ) THEN
            RAISE EXCEPTION 'turn final entry must belong to its exact executed turn'
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
        RETURN NEW;
    END IF;

    IF TG_TABLE_NAME = 'transcript_entries' THEN
        IF NEW.entry_owner_kind = 'IMPORTED_HISTORY' THEN
            -- Genesis seals a dense, immutable imported prefix. All positions
            -- through the anchor are occupied; subsequent inserts cannot append
            -- imported history or interleave it with executed entries.
            SELECT a.entry_sequence INTO observed_ordinal
            FROM pulsara_v3.session_context_genesis AS g
            JOIN pulsara_v3.transcript_entries AS a
              ON a.session_id = g.session_id AND a.id = g.anchor_entry_id
            WHERE g.session_id = NEW.session_id AND g.workspace_id = NEW.workspace_id;
            IF observed_ordinal IS NULL OR NEW.entry_sequence > observed_ordinal
               OR EXISTS (SELECT 1 FROM pulsara_v3.transcript_entries
                    WHERE session_id = NEW.session_id AND entry_owner_kind = 'EXECUTED_TURN'
                      AND entry_sequence <= observed_ordinal) THEN
                RAISE EXCEPTION 'imported history must belong to the sealed genesis prefix' USING ERRCODE = '23514';
            END IF;
            PERFORM 1 FROM pulsara_v3.imported_history_groups
            WHERE session_id = NEW.session_id AND workspace_id = NEW.workspace_id
              AND id = NEW.imported_history_group_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'imported entry group does not exact-join' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END IF;
        PERFORM 1 FROM pulsara_v3.turns AS t
        WHERE t.session_id = NEW.session_id AND t.id = NEW.turn_id
          AND t.workspace_id = NEW.workspace_id
;
        IF NOT FOUND OR NEW.entry_owner_kind <> 'EXECUTED_TURN' THEN
            RAISE EXCEPTION 'executed entry must exact-join its turn scope' USING ERRCODE = '23514';
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
            ELSIF NEW.source_plan_handoff_kind = 'EXITED_PLAN'
               AND observed_status IS DISTINCT FROM 'EXITED' THEN
                RAISE EXCEPTION 'Plan exit handoff requires EXITED workflow'
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
            OLD.entry_command_id IS DISTINCT FROM NEW.entry_command_id OR
            OLD.entry_turn_id IS DISTINCT FROM NEW.entry_turn_id OR
            OLD.entry_assistant_entry_id IS DISTINCT FROM NEW.entry_assistant_entry_id OR
            OLD.entry_tool_call_id IS DISTINCT FROM NEW.entry_tool_call_id OR
            OLD.resume_permission_mode IS DISTINCT FROM NEW.resume_permission_mode OR
            NEW.workflow_revision <> OLD.workflow_revision + 1 OR
            OLD.status <> 'ACTIVE'
        ) THEN
            RAISE EXCEPTION 'Plan workflow immutable identity or lifecycle changed'
                USING ERRCODE = '23514';
        END IF;
        IF NEW.entered_by = 'AGENT' THEN
            SELECT e.turn_id, CASE WHEN (SELECT root_session_id FROM pulsara_v3.sessions WHERE id=e.session_id) IS NULL THEN 'ROOT' ELSE 'SUBAGENT' END, b.tool_name
              INTO observed_turn_id, observed_scope, observed_tool_name
            FROM pulsara_v3.content_parts AS b
            JOIN pulsara_v3.transcript_entries AS e
              ON e.session_id = b.session_id
             AND e.id = b.entry_id
            WHERE b.session_id = NEW.session_id
              AND b.entry_id = NEW.entry_assistant_entry_id
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
            OLD.request_semantic_digest IS DISTINCT FROM NEW.request_semantic_digest OR
            OLD.plan_file_path IS DISTINCT FROM NEW.plan_file_path OR
            OLD.plan_file_error IS DISTINCT FROM NEW.plan_file_error OR
            (OLD.control_tool_result_id IS DISTINCT FROM NEW.control_tool_result_id
                AND NOT (
                    OLD.status = 'OPEN'
                    AND NEW.status IN ('ANSWERED','APPROVED','REVISION_REQUESTED','CANCELLED')
                    AND OLD.control_tool_result_id IS NULL
                    AND NEW.control_tool_result_id IS NOT NULL
                )) OR
            OLD.status <> 'OPEN'
        ) THEN
            RAISE EXCEPTION 'Plan interaction immutable identity or lifecycle changed'
                USING ERRCODE = '23514';
        END IF;
        SELECT e.turn_id, CASE WHEN (SELECT root_session_id FROM pulsara_v3.sessions WHERE id=e.session_id) IS NULL THEN 'ROOT' ELSE 'SUBAGENT' END, b.tool_name, t.status
          INTO observed_turn_id, observed_scope, observed_tool_name, observed_status
        FROM pulsara_v3.content_parts AS b
        JOIN pulsara_v3.transcript_entries AS e
          ON e.session_id = b.session_id AND e.id = b.entry_id
        JOIN pulsara_v3.turns AS t
          ON t.session_id = e.session_id AND t.id = e.turn_id
        WHERE b.session_id = NEW.session_id
          AND b.entry_id = NEW.assistant_entry_id
          AND b.tool_call_id = NEW.tool_call_id;
        IF observed_turn_id IS DISTINCT FROM NEW.origin_turn_id
           OR observed_scope IS DISTINCT FROM 'ROOT'
           OR observed_tool_name IS DISTINCT FROM (
               CASE NEW.kind
                   WHEN 'QUESTION' THEN 'ask_plan_question'
                   ELSE 'exit_plan'
               END
           )
           OR (NEW.kind = 'QUESTION' AND NEW.status IN ('OPEN', 'ANSWERED', 'CANCELLED')
               AND observed_status IS DISTINCT FROM 'RUNNING')
           OR (NEW.kind = 'DRAFT_REVIEW' AND NEW.status IN ('OPEN','REVISION_REQUESTED','CANCELLED')
               AND observed_status IS DISTINCT FROM 'RUNNING') THEN
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

    IF TG_TABLE_NAME = 'tool_results' THEN
        IF NEW.result_record_kind = 'IMPORTED_HISTORY' THEN
            PERFORM 1 FROM pulsara_v3.transcript_entries AS call_entry
            JOIN pulsara_v3.transcript_entries AS result_entry
              ON result_entry.session_id = call_entry.session_id
             AND result_entry.imported_history_group_id = call_entry.imported_history_group_id
            WHERE call_entry.session_id = NEW.session_id
              AND call_entry.id = NEW.tool_call_entry_id
              AND result_entry.id = NEW.result_entry_id
              AND call_entry.workspace_id = NEW.workspace_id AND result_entry.workspace_id = NEW.workspace_id
              AND call_entry.entry_owner_kind = 'IMPORTED_HISTORY'
              AND result_entry.entry_owner_kind = 'IMPORTED_HISTORY'
              AND call_entry.entry_kind = 'ASSISTANT_TOOL_REQUEST'
              AND result_entry.entry_kind = 'TOOL_RESULT';
            IF NOT FOUND THEN
                RAISE EXCEPTION 'imported tool result must exact-join its imported call and group' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END IF;
        SELECT entry_kind, turn_id INTO observed_kind, observed_turn_id
        FROM pulsara_v3.transcript_entries
        WHERE session_id = NEW.session_id AND workspace_id = NEW.workspace_id
          AND id = NEW.result_entry_id
          AND entry_owner_kind = 'EXECUTED_TURN';
        IF observed_kind IS DISTINCT FROM 'TOOL_RESULT' THEN
            RAISE EXCEPTION 'tool result relation requires TOOL_RESULT entry'
                USING ERRCODE = '23514';
        END IF;
        PERFORM 1
        FROM pulsara_v3.tool_invocations AS invocation
        JOIN pulsara_v3.turns AS target_turn
          ON target_turn.session_id = invocation.session_id AND target_turn.id = invocation.turn_id
        WHERE invocation.session_id = NEW.session_id AND invocation.id = NEW.invocation_id
          AND invocation.turn_id = observed_turn_id
          AND invocation.assistant_entry_id IS NOT DISTINCT FROM NEW.tool_call_entry_id
          AND (invocation.source_kind='CODE_MODE_CALL' OR invocation.tool_call_id=NEW.tool_call_id)
          AND (NEW.attempt_id IS NULL OR EXISTS (SELECT 1 FROM pulsara_v3.tool_execution_attempts a
              WHERE a.session_id=NEW.session_id AND a.id=NEW.attempt_id AND a.invocation_id=NEW.invocation_id))
          AND target_turn.permission_snapshot_fingerprint
              = NEW.permission_snapshot_fingerprint;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'tool result, request, and permission must exact-join'
                USING ERRCODE = '23514';
        END IF;
        IF NEW.result_origin_kind = 'PLAN_CONTROL' THEN
            SELECT b.tool_name
              INTO observed_tool_name
            FROM pulsara_v3.content_parts AS b
            WHERE b.session_id = NEW.session_id
              AND b.entry_id = NEW.tool_call_entry_id
              AND b.tool_call_id = NEW.tool_call_id;
            IF (NEW.control_plan_workflow_id IS NOT NULL
                    AND observed_tool_name NOT IN ('enter_plan', 'exit_plan'))
               OR (NEW.control_plan_interaction_id IS NOT NULL
                    AND observed_tool_name NOT IN ('ask_plan_question', 'exit_plan')) THEN
                RAISE EXCEPTION 'Plan control result does not name a Plan tool'
                    USING ERRCODE = '23514';
            END IF;
            IF NEW.result_state = 'CANCELLED_BEFORE_DISPATCH' AND (
                observed_tool_name IS DISTINCT FROM 'ask_plan_question'
                OR NOT EXISTS (
                    SELECT 1 FROM pulsara_v3.plan_interactions
                    WHERE session_id = NEW.session_id
                      AND id = NEW.control_plan_interaction_id
                      AND kind = 'QUESTION' AND status = 'CANCELLED'
                )
            ) THEN
                RAISE EXCEPTION 'cancelled Plan control result requires a cancelled question'
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
        IF TG_OP = 'UPDATE' AND OLD.work_title IS DISTINCT FROM NEW.work_title THEN
            RAISE EXCEPTION 'accepted Work title is immutable' USING ERRCODE = '23514';
        END IF;
        IF NEW.consumed_entry_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM pulsara_v3.transcript_entries
            WHERE session_id = NEW.session_id AND id = NEW.consumed_entry_id
              AND entry_owner_kind = 'EXECUTED_TURN'
        ) THEN
            RAISE EXCEPTION 'consumed queue entry must have executed owner'
                USING ERRCODE = '23514';
        END IF;
        -- Keep the table discriminator in its own branch.  PL/pgSQL record
        -- field lookup is dynamic and an `AND NEW.target_turn_id ...` guard
        -- can still resolve that field for trigger rows from another table.
        -- Only a pending steer is an admission promise against a live ROOT
        -- target.  Terminal queue rows retain that immutable target as
        -- historical attribution after the turn itself becomes terminal.
        IF NEW.status = 'PENDING' AND NEW.target_turn_id IS NOT NULL THEN
            SELECT CASE WHEN s.root_session_id IS NULL THEN 'ROOT' ELSE 'SUBAGENT' END, t.status INTO observed_scope, observed_status
            FROM pulsara_v3.turns t JOIN pulsara_v3.sessions s ON s.id=t.session_id
            WHERE t.session_id = NEW.session_id AND t.id = NEW.target_turn_id;
            IF observed_status IS DISTINCT FROM 'RUNNING' THEN
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
               OR (NEW.pending_plan_handoff_kind = 'EXITED_PLAN'
                    AND observed_status IS DISTINCT FROM 'EXITED')
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
              AND entry_owner_kind = 'EXECUTED_TURN'
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
            WHERE session_id = NEW.session_id AND id = NEW.consumed_entry_id
              AND entry_owner_kind = 'EXECUTED_TURN';
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

    RETURN NEW;
END;
$$;
REVOKE ALL ON FUNCTION pulsara_v3.enforce_conversation_kernel_invariants() FROM PUBLIC;

CREATE FUNCTION pulsara_v3.enforce_imported_history_invariants()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    anchor_sequence bigint;
    history_count bigint;
    execution_entry text;
    execution_session text;
BEGIN
    execution_session := NULL;
    IF TG_TABLE_NAME = 'agent_events' THEN
        execution_entry := NEW.subject_entry_id;
        execution_session := NEW.session_id;
    ELSIF TG_TABLE_NAME = 'session_commands' THEN
        execution_entry := NEW.target_entry_id;
        execution_session := NEW.session_id;
    ELSIF TG_TABLE_NAME = 'interaction_decisions' THEN
        execution_entry := NEW.subject_tool_call_entry_id;
        execution_session := NEW.session_id;
    END IF;
    IF execution_entry IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM pulsara_v3.transcript_entries
        WHERE session_id = execution_session AND id = execution_entry
          AND entry_owner_kind = 'EXECUTED_TURN'
    ) THEN
        RAISE EXCEPTION 'execution occurrence cannot target imported history' USING ERRCODE = '23514';
    END IF;
    IF TG_TABLE_NAME = 'session_context_genesis' THEN
        SELECT e.entry_sequence INTO anchor_sequence
        FROM pulsara_v3.transcript_entries AS e
        JOIN pulsara_v3.imported_history_groups AS g
          ON g.session_id = e.session_id AND g.id = e.imported_history_group_id
        WHERE e.session_id = NEW.session_id AND e.id = NEW.anchor_entry_id
          AND e.workspace_id = NEW.workspace_id AND e.entry_owner_kind = 'IMPORTED_HISTORY'
          AND e.entry_kind = 'ASSISTANT_MESSAGE' AND g.final_entry_id = e.id;
        SELECT count(*) INTO history_count FROM pulsara_v3.transcript_entries
        WHERE session_id = NEW.session_id AND entry_owner_kind = 'IMPORTED_HISTORY';
        IF anchor_sequence IS NULL OR history_count <> anchor_sequence
           OR NEW.source_through_sequence >= anchor_sequence
           OR EXISTS (SELECT 1 FROM pulsara_v3.transcript_entries
                      WHERE session_id = NEW.session_id AND entry_owner_kind = 'EXECUTED_TURN') THEN
            RAISE EXCEPTION 'genesis requires one complete imported prefix and no execution' USING ERRCODE = '23514';
        END IF;
        IF NEW.base_kind = 'SNAPSHOT' AND NOT EXISTS (
            SELECT 1 FROM pulsara_v3.context_snapshots WHERE session_id = NEW.session_id
            AND id = NEW.context_snapshot_id AND workspace_id = NEW.workspace_id
            AND source_through_sequence = NEW.source_through_sequence
        ) THEN
            RAISE EXCEPTION 'genesis snapshot does not exact-join' USING ERRCODE = '23514';
        END IF;
    ELSIF TG_TABLE_NAME = 'imported_history_groups' THEN
        IF NEW.final_entry_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM pulsara_v3.transcript_entries WHERE session_id = NEW.session_id
            AND id = NEW.final_entry_id AND imported_history_group_id = NEW.id
            AND entry_owner_kind = 'IMPORTED_HISTORY'
        ) THEN
            RAISE EXCEPTION 'imported group final does not exact-join' USING ERRCODE = '23514';
        END IF;
    ELSIF TG_TABLE_NAME = 'imported_tool_call_closures' THEN
        IF NOT EXISTS (SELECT 1 FROM pulsara_v3.transcript_entries
            WHERE session_id = NEW.session_id AND id = NEW.assistant_entry_id
            AND entry_owner_kind = 'IMPORTED_HISTORY' AND entry_kind = 'ASSISTANT_TOOL_REQUEST'
            AND entry_sequence <= NEW.target_provider_input_through_sequence)
           OR EXISTS (SELECT 1 FROM pulsara_v3.tool_results AS r
                JOIN pulsara_v3.transcript_entries AS e ON e.session_id = r.session_id AND e.id = r.result_entry_id
                WHERE r.session_id = NEW.session_id AND r.tool_call_entry_id = NEW.assistant_entry_id
                AND r.tool_call_id = NEW.tool_call_id AND e.entry_sequence <= NEW.target_provider_input_through_sequence) THEN
            RAISE EXCEPTION 'imported closure requires an imported call without a visible result' USING ERRCODE = '23514';
        END IF;
    ELSIF TG_TABLE_NAME = 'tool_execution_attempts' THEN
        IF TG_OP='UPDATE' AND OLD.code_source_content_digest IS NOT NULL AND (
            ROW(OLD.code_source_inline_content,OLD.code_source_blob_id,OLD.code_source_content_digest,
                OLD.code_source_content_size,OLD.code_source_content_media_type,OLD.code_source_content_codec)
            IS DISTINCT FROM ROW(NEW.code_source_inline_content,NEW.code_source_blob_id,NEW.code_source_content_digest,
                NEW.code_source_content_size,NEW.code_source_content_media_type,NEW.code_source_content_codec)
        ) THEN
            RAISE EXCEPTION 'frozen code source is immutable' USING ERRCODE='23514';
        END IF;
        IF NEW.code_source_content_digest IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM pulsara_v3.tool_invocations i JOIN pulsara_v3.content_parts b
              ON b.session_id=i.session_id AND b.entry_id=i.assistant_entry_id AND b.tool_call_id=i.tool_call_id
            WHERE i.session_id=NEW.session_id AND i.id=NEW.invocation_id AND i.source_kind='MODEL_TOOL_CALL' AND b.tool_name='exec_cell'
        ) THEN
            RAISE EXCEPTION 'frozen code source belongs to an exec_cell attempt' USING ERRCODE='23514';
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pulsara_v3.tool_invocations AS i
            JOIN pulsara_v3.turns AS t ON t.session_id=i.session_id AND t.id=i.turn_id
            WHERE i.session_id=NEW.session_id AND i.id=NEW.invocation_id
            AND i.assistant_entry_id IS NOT DISTINCT FROM NEW.assistant_entry_id
            AND (i.source_kind='CODE_MODE_CALL' OR i.tool_call_id=NEW.tool_call_id)
            AND t.permission_snapshot_fingerprint=NEW.permission_snapshot_fingerprint) THEN
            RAISE EXCEPTION 'physical attempt requires an executed call and permission' USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE CONSTRAINT TRIGGER trg_pulsara_v3_genesis_integrity
AFTER INSERT ON pulsara_v3.session_context_genesis DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION pulsara_v3.enforce_imported_history_invariants();
REVOKE ALL ON FUNCTION pulsara_v3.enforce_imported_history_invariants() FROM PUBLIC;
CREATE CONSTRAINT TRIGGER trg_pulsara_v3_imported_group_integrity
AFTER INSERT ON pulsara_v3.imported_history_groups DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION pulsara_v3.enforce_imported_history_invariants();
CREATE CONSTRAINT TRIGGER trg_pulsara_v3_imported_closure_integrity
AFTER INSERT ON pulsara_v3.imported_tool_call_closures DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION pulsara_v3.enforce_imported_history_invariants();
CREATE CONSTRAINT TRIGGER trg_pulsara_v3_attempt_execution_owner
AFTER INSERT OR UPDATE ON pulsara_v3.tool_execution_attempts DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION pulsara_v3.enforce_imported_history_invariants();

CREATE CONSTRAINT TRIGGER trg_pulsara_v3_event_execution_owner
AFTER INSERT ON pulsara_v3.agent_events DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION pulsara_v3.enforce_imported_history_invariants();
CREATE CONSTRAINT TRIGGER trg_pulsara_v3_command_execution_owner
AFTER INSERT ON pulsara_v3.session_commands DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION pulsara_v3.enforce_imported_history_invariants();
CREATE CONSTRAINT TRIGGER trg_pulsara_v3_decision_execution_owner
AFTER INSERT ON pulsara_v3.interaction_decisions DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION pulsara_v3.enforce_imported_history_invariants();
CREATE CONSTRAINT TRIGGER trg_pulsara_v3_entry_source_integrity
AFTER INSERT ON pulsara_v3.transcript_entries
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION pulsara_v3.enforce_conversation_kernel_invariants();

CREATE CONSTRAINT TRIGGER trg_pulsara_v3_turn_initial_entry_integrity
AFTER INSERT OR UPDATE ON pulsara_v3.turns
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION pulsara_v3.enforce_conversation_kernel_invariants();

CREATE CONSTRAINT TRIGGER trg_pulsara_v3_tool_invocation_integrity
AFTER INSERT ON pulsara_v3.tool_invocations DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION pulsara_v3.enforce_conversation_kernel_invariants();

CREATE CONSTRAINT TRIGGER trg_pulsara_v3_tool_result_integrity
AFTER INSERT ON pulsara_v3.tool_results
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION pulsara_v3.enforce_conversation_kernel_invariants();

CREATE CONSTRAINT TRIGGER trg_pulsara_v3_prompt_target_integrity
AFTER INSERT OR UPDATE ON pulsara_v3.prompt_queue_items
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
    ON pulsara_v3.transcript_entries (session_id, entry_sequence);
CREATE INDEX idx_pulsara_v3_events_session_sequence
    ON pulsara_v3.agent_events (session_id, event_sequence);
CREATE INDEX idx_pulsara_v3_queue_pending
    ON pulsara_v3.prompt_queue_items (session_id, queue_sequence, id) WHERE status = 'PENDING';
REVOKE ALL ON ALL TABLES IN SCHEMA pulsara_v3 FROM PUBLIC;

-- Disposable search projection. Canonical entry/block rows alone own content.
-- QA and QAQ share one body layout and one index, selected mutually exclusively.
CREATE TABLE pulsara_v3.history_search_documents (
    id text CHECK (id ~ '^search-document:[a-z2-7]{25}[aeimquy4]$') PRIMARY KEY,
    session_id text NOT NULL REFERENCES pulsara_v3.sessions(id) ON DELETE CASCADE,
    q1_entry_id text NOT NULL,
    q2_entry_id text,
    variant text NOT NULL CHECK (variant IN ('QA','QAQ')),
    include_tools boolean NOT NULL,
    entry_ids text[] NOT NULL,
    identity jsonb NOT NULL,
    body text NOT NULL,
    body_en text GENERATED ALWAYS AS(body) STORED,
    body_tech text GENERATED ALWAYS AS(body) STORED,
    positions jsonb NOT NULL,
    messages jsonb NOT NULL,
    covered_entry_sequence bigint NOT NULL CHECK (covered_entry_sequence >= 1),
    -- Exactly the applicable subset of QA/QAQ x tools/no-tools for this group.
    -- Repeated positive-row metadata detects sparse/missing variants; zero rows
    -- remain unknown and are conservatively revisited, not durably receipted.
    expected_variants jsonb NOT NULL CHECK (
        jsonb_typeof(expected_variants) = 'array'
        AND jsonb_array_length(expected_variants) BETWEEN 1 AND 4
    ),
    UNIQUE(session_id,q1_entry_id,variant,include_tools),
    FOREIGN KEY (session_id, q1_entry_id)
        REFERENCES pulsara_v3.transcript_entries(session_id, id) ON DELETE CASCADE,
    FOREIGN KEY (session_id, q2_entry_id)
        REFERENCES pulsara_v3.transcript_entries(session_id, id) ON DELETE CASCADE
);
CREATE INDEX idx_pulsara_v3_history_session ON pulsara_v3.history_search_documents(session_id,q1_entry_id);
CREATE INDEX idx_pulsara_v3_history_bm25 ON pulsara_v3.history_search_documents USING bm25
    (id,body,body_en,body_tech)
    WITH (key_field='id',text_fields='{"body":{"tokenizer":{"type":"jieba","search_mode":true,"stopwords":["!","\"","#","$","%","&","''","(",")","*","+",",","-",".","/",":",";","<","=",">","?","@","[","\\","]","^","_","`","{","|","}","~"," ","\t","\n","\r","，","。","！","？","；","：","、","（","）","【","】","《","》","“","”","‘","’","—","…"]}},"body_en":{"tokenizer":{"type":"default","stemmer":"English"}},"body_tech":{"tokenizer":{"type":"regex","pattern":"[./:@+-]*[A-Za-z0-9_][A-Za-z0-9_./:@+-]*"}}}');

-- Disposable successful audio/video transcription blocks, independent of sessions.
CREATE TABLE pulsara_v3.asr_cache (
    id text PRIMARY KEY CHECK (id ~ '^asr-cache:[a-z2-7]{25}[aeimquy4]$'),
    memory_domain_id text NOT NULL CHECK (memory_domain_id ~ '^[a-z0-9][a-z0-9._-]{0,127}$'),
    source_path text NOT NULL CHECK (source_path <> ''),
    source_modified_ns bigint NOT NULL,
    source_size bigint NOT NULL CHECK (source_size >= 0),
    from_seconds double precision NOT NULL CHECK (from_seconds >= 0 AND from_seconds < 'Infinity'::double precision),
    to_seconds double precision NOT NULL CHECK (to_seconds > from_seconds AND to_seconds < 'Infinity'::double precision),
    provenance jsonb NOT NULL CHECK (jsonb_typeof(provenance) = 'object'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    last_used_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    CONSTRAINT uq_asr_cache_media_range UNIQUE (memory_domain_id,source_path,source_modified_ns,source_size,from_seconds,to_seconds)
);
-- PostgreSQL has no built-in float8 range. A native GiST box index preserves
-- exact floating-point endpoints without quantization or a new extension/type.
-- Reads retain explicit strict half-open comparisons to exclude touching edges.
CREATE INDEX idx_asr_cache_interval ON pulsara_v3.asr_cache USING gist
    (box(point(from_seconds, 0), point(to_seconds, 0)));
REVOKE ALL ON pulsara_v3.asr_cache FROM PUBLIC;

-- Content identity and payload are immutable; only the two owner columns are mutable.
CREATE FUNCTION pulsara_v3.enforce_content_part_invariants()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    parent_kind text;
    parent_origin text;
    asset_workspace text;
    asset_width integer;
    asset_height integer;
    asset_media text;
BEGIN
    IF TG_OP='UPDATE' THEN
        IF ROW(NEW.id,NEW.session_id,NEW.workspace_id,NEW.ordinal,NEW.type,NEW.inline_value,
                NEW.blob_id,NEW.tool_call_id,NEW.tool_name,NEW.tool_arguments,NEW.data_media_type,NEW.data_codec)
            IS DISTINCT FROM ROW(OLD.id,OLD.session_id,OLD.workspace_id,OLD.ordinal,OLD.type,OLD.inline_value,
                OLD.blob_id,OLD.tool_call_id,OLD.tool_name,OLD.tool_arguments,OLD.data_media_type,OLD.data_codec)
            OR OLD.entry_id IS NOT NULL OR OLD.queue_item_id IS NULL
            OR ROW(OLD.entry_id,OLD.queue_item_id) IS NOT DISTINCT FROM ROW(NEW.entry_id,NEW.queue_item_id) THEN
            RAISE EXCEPTION 'content payload is immutable; only queued ownership may transfer' USING ERRCODE='23514';
        END IF;
        IF TG_WHEN='AFTER' THEN
            IF NEW.entry_id IS NOT NULL THEN
                IF NOT EXISTS (
                    SELECT 1 FROM pulsara_v3.prompt_queue_items q
                    JOIN pulsara_v3.transcript_entries main ON main.session_id=q.session_id AND main.id=q.consumed_entry_id
                    JOIN pulsara_v3.transcript_entries target ON target.session_id=main.session_id
                        AND target.id=NEW.entry_id AND target.turn_id=main.turn_id
                    WHERE q.id=OLD.queue_item_id AND q.session_id=NEW.session_id AND q.status='CONSUMED'
                        AND (
                            (q.work_inputs='[]'::jsonb AND target.id=main.id)
                            OR EXISTS (
                                SELECT 1 FROM jsonb_array_elements(q.work_inputs) WITH ORDINALITY AS parts(segment,position)
                                WHERE NEW.ordinal >= (segment->>'content_start')::integer
                                    AND NEW.ordinal < (segment->>'content_start')::integer + (segment->>'content_count')::integer
                                    AND target.entry_sequence = main.entry_sequence + parts.position - 1
                                    AND ((segment->>'kind'='instruction' AND target.id=main.id)
                                        OR (segment->>'kind'<>'instruction' AND target.input_origin='AGENT_MATERIAL'))
                            )
                        )
                ) THEN
                    RAISE EXCEPTION 'content transfer must join its consumed queue input group' USING ERRCODE='23514';
                END IF;
            ELSIF NOT EXISTS (
                SELECT 1 FROM pulsara_v3.prompt_queue_items source
                JOIN pulsara_v3.prompt_queue_items target ON target.session_id=source.session_id
                JOIN pulsara_v3.session_commands command ON command.session_id=target.session_id
                    AND command.command_id=target.command_id
                    AND ((command.command_kind='STEER_QUEUED_PROMPT' AND command.target_queue_item_id=target.id)
                        OR (command.command_kind='RESOLVE_PLAN_INTERACTION' AND EXISTS (
                            SELECT 1 FROM pulsara_v3.plan_interactions interaction
                            WHERE interaction.session_id=target.session_id
                                AND interaction.id=command.target_plan_interaction_id
                                AND interaction.status='REVISION_REQUESTED'
                                AND interaction.origin_turn_id=target.target_turn_id
                        )))
                WHERE source.id=OLD.queue_item_id AND target.id=NEW.queue_item_id
                    AND source.status='CANCELLED' AND source.terminal_reason='USER_REDIRECTED_TO_STEER'
                    AND target.delivery_mode='STEER_ACTIVE_TURN'
            ) THEN
                RAISE EXCEPTION 'queue content transfer requires the actual steer redirect' USING ERRCODE='23514';
            END IF;
        END IF;
    END IF;
    IF NEW.entry_id IS NOT NULL THEN
        SELECT entry_kind,input_origin INTO parent_kind,parent_origin FROM pulsara_v3.transcript_entries
            WHERE id=NEW.entry_id AND session_id=NEW.session_id AND workspace_id=NEW.workspace_id;
    ELSE
        SELECT 'QUEUE',input_origin INTO parent_kind,parent_origin FROM pulsara_v3.prompt_queue_items
            WHERE id=NEW.queue_item_id AND session_id=NEW.session_id AND workspace_id=NEW.workspace_id;
    END IF;
    IF parent_kind IS NULL THEN
        RAISE EXCEPTION 'content owner does not exist in its exact scope' USING ERRCODE='23514';
    END IF;
    IF (NEW.type='tool_call' AND parent_kind<>'ASSISTANT_TOOL_REQUEST')
        OR (NEW.type='data' AND parent_kind NOT IN ('ASSISTANT_MESSAGE','ASSISTANT_TOOL_REQUEST'))
        OR (NEW.type='annotation' AND parent_kind NOT IN ('USER_MESSAGE','USER_STEER','QUEUE'))
        OR (NEW.type='image' AND parent_kind NOT IN ('USER_MESSAGE','USER_STEER','TOOL_RESULT','TERMINAL_OBSERVATION','QUEUE'))
        OR (NEW.type IN ('target','object') AND parent_kind NOT IN ('USER_MESSAGE','USER_STEER','TOOL_RESULT','QUEUE')) THEN
        RAISE EXCEPTION 'content type is not admitted by its source owner' USING ERRCODE='23514';
    END IF;
    IF NEW.blob_id IS NOT NULL THEN
        SELECT workspace_id,width,height,media_type INTO asset_workspace,asset_width,asset_height,asset_media
            FROM pulsara_v3.blobs WHERE id=NEW.blob_id;
        IF NEW.type='image' THEN
            IF NEW.blob_id NOT LIKE 'image:%' OR asset_workspace IS NOT NULL
                OR asset_width IS NULL OR asset_height IS NULL OR asset_media NOT LIKE 'image/%' THEN
                RAISE EXCEPTION 'image content requires a validated independent image asset' USING ERRCODE='23514';
            END IF;
        ELSIF NEW.blob_id NOT LIKE 'blob:%' OR asset_workspace IS DISTINCT FROM NEW.workspace_id THEN
            RAISE EXCEPTION 'ordinary content blob must belong to its workspace' USING ERRCODE='23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER trg_pulsara_v3_content_part_seal BEFORE INSERT OR UPDATE ON pulsara_v3.content_parts
FOR EACH ROW EXECUTE FUNCTION pulsara_v3.enforce_content_part_invariants();
CREATE CONSTRAINT TRIGGER trg_pulsara_v3_content_part_transfer AFTER UPDATE ON pulsara_v3.content_parts
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION pulsara_v3.enforce_content_part_invariants();
REVOKE ALL ON FUNCTION pulsara_v3.enforce_content_part_invariants() FROM PUBLIC;

-- Work inputs retain only the boundaries and dependency facts required by queue consumption.
CREATE FUNCTION pulsara_v3.enforce_queue_material_metadata()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    segment jsonb;
    position bigint;
    next_content bigint := 0;
    previous_kind text;
    previous_ordinal bigint := -1;
    current_rank integer;
    previous_rank integer := 0;
BEGIN
    IF NEW.work_inputs<>'[]'::jsonb AND NEW.input_origin<>'AGENT_INSTRUCTION' THEN
        RAISE EXCEPTION 'only agent instructions carry material boundaries' USING ERRCODE='23514';
    END IF;
    FOR segment,position IN SELECT value,ordinality-1 FROM jsonb_array_elements(NEW.work_inputs) WITH ORDINALITY LOOP
        IF jsonb_typeof(segment)<>'object'
            OR NOT (segment ?& ARRAY['kind','ordinal','content_start','content_count'])
            OR segment - ARRAY['kind','ordinal','content_start','content_count','source_work_id','ready'] <> '{}'::jsonb
            OR segment->>'kind' NOT IN ('instruction','context','dependency','material')
            OR jsonb_typeof(segment->'ordinal')<>'number' OR (segment->>'ordinal') !~ '^[0-9]+$'
            OR jsonb_typeof(segment->'content_start')<>'number' OR (segment->>'content_start') !~ '^[0-9]+$'
            OR jsonb_typeof(segment->'content_count')<>'number' OR (segment->>'content_count') !~ '^[0-9]+$' THEN
            RAISE EXCEPTION 'invalid queue material metadata' USING ERRCODE='23514';
        END IF;
        current_rank := CASE segment->>'kind' WHEN 'instruction' THEN 0 WHEN 'context' THEN 1 WHEN 'dependency' THEN 2 ELSE 3 END;
        IF previous_kind IS DISTINCT FROM segment->>'kind' THEN previous_ordinal := -1; END IF;
        IF (segment->>'ordinal')::bigint<=previous_ordinal OR current_rank<previous_rank
            OR (position=0 AND (segment->>'ordinal')::bigint<>0)
            OR (segment->>'content_start')::bigint<next_content
            OR ((position=0) IS DISTINCT FROM (segment->>'kind'='instruction')) THEN
            RAISE EXCEPTION 'queue material boundaries must preserve input order' USING ERRCODE='23514';
        END IF;
        IF segment->>'kind' IN ('dependency','material') THEN
            IF NOT (segment ?& ARRAY['source_work_id','ready'])
                OR jsonb_typeof(segment->'ready')<>'boolean'
                OR jsonb_typeof(segment->'source_work_id')<>'string'
                OR (segment->>'source_work_id') !~ '^queue:[a-z2-7]{25}[aeimquy4]$'
                OR (segment->>'ready'='false' AND (segment->>'content_count')::bigint<>0) THEN
                RAISE EXCEPTION 'work material requires its source and readiness' USING ERRCODE='23514';
            END IF;
        ELSIF segment ?| ARRAY['source_work_id','ready'] THEN
            RAISE EXCEPTION 'instruction and context have no work dependency facts' USING ERRCODE='23514';
        END IF;
        next_content := (segment->>'content_start')::bigint + (segment->>'content_count')::bigint;
        previous_ordinal := (segment->>'ordinal')::bigint;
        previous_kind := segment->>'kind';
        previous_rank := current_rank;
    END LOOP;
    RETURN NEW;
END;
$$;
CREATE TRIGGER trg_pulsara_v3_queue_material_metadata BEFORE INSERT OR UPDATE ON pulsara_v3.prompt_queue_items
FOR EACH ROW EXECUTE FUNCTION pulsara_v3.enforce_queue_material_metadata();
REVOKE ALL ON FUNCTION pulsara_v3.enforce_queue_material_metadata() FROM PUBLIC;

-- A result has one accepted native observation projection, without a page body.
CREATE FUNCTION pulsara_v3.seal_native_tool_projection()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    keys text[];
    point json;
    point_name text;
    item record;
    start_segment numeric;
    start_offset numeric;
    end_segment numeric;
    end_offset numeric;
BEGIN
    IF TG_OP='UPDATE' AND (OLD.native_projection IS NOT NULL
        OR NEW.native_projection IS NULL OR NEW.source_result_entry_id IS NULL
        OR (OLD.source_result_entry_id IS NOT NULL AND OLD.source_result_entry_id IS DISTINCT FROM NEW.source_result_entry_id)) THEN
        RAISE EXCEPTION 'native result projection is accepted once' USING ERRCODE='23514';
    END IF;
    IF NEW.native_projection IS NULL THEN RETURN NEW; END IF;
    IF json_typeof(NEW.native_projection)<>'object' THEN
        RAISE EXCEPTION 'native result projection must be an object' USING ERRCODE='23514';
    END IF;
    SELECT array_agg(key ORDER BY key) INTO keys FROM json_object_keys(NEW.native_projection) key;
    IF keys IS DISTINCT FROM ARRAY['end','image_errors','next','page_error','start'] THEN
        RAISE EXCEPTION 'native result projection has invalid fields' USING ERRCODE='23514';
    END IF;
    FOREACH point_name IN ARRAY ARRAY['start','end'] LOOP
        point := NEW.native_projection->point_name;
        IF json_typeof(point)<>'array' OR json_array_length(point)<>2
            OR json_typeof(point->0)<>'number' OR json_typeof(point->1)<>'number'
            OR (point->>0)!~'^[0-9]+$' OR (point->>1)!~'^[0-9]+$' THEN
            RAISE EXCEPTION 'native projection position must be two nonnegative integers' USING ERRCODE='23514';
        END IF;
    END LOOP;
    start_segment := (NEW.native_projection->'start'->>0)::numeric;
    start_offset := (NEW.native_projection->'start'->>1)::numeric;
    end_segment := (NEW.native_projection->'end'->>0)::numeric;
    end_offset := (NEW.native_projection->'end'->>1)::numeric;
    IF json_typeof(NEW.native_projection->'page_error')='null' THEN
        IF ROW(end_segment,end_offset)<=ROW(start_segment,start_offset) THEN
            RAISE EXCEPTION 'a normal native range must advance' USING ERRCODE='23514';
        END IF;
    ELSIF json_typeof(NEW.native_projection->'page_error')<>'string'
        OR ROW(end_segment,end_offset) IS DISTINCT FROM ROW(start_segment,start_offset) THEN
        RAISE EXCEPTION 'native page failure cannot mark content read' USING ERRCODE='23514';
    END IF;
    IF json_typeof(NEW.native_projection->'image_errors')<>'object' THEN
        RAISE EXCEPTION 'native image outcomes must be keyed by segment' USING ERRCODE='23514';
    END IF;
    FOR item IN SELECT * FROM json_each(NEW.native_projection->'image_errors') LOOP
        IF item.key!~'^[0-9]+$' OR json_typeof(item.value)<>'string' THEN
            RAISE EXCEPTION 'invalid native image outcome' USING ERRCODE='23514';
        END IF;
    END LOOP;
    IF json_typeof(NEW.native_projection->'next')<>'null' THEN
        IF json_typeof(NEW.native_projection->'next')<>'object'
            OR NEW.native_projection->'next'->>'kind' IS DISTINCT FROM 'NEXT'
            OR json_typeof(NEW.native_projection->'next'->'token') IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION 'invalid native NEXT target' USING ERRCODE='23514';
        END IF;
        SELECT array_agg(key ORDER BY key) INTO keys FROM json_object_keys(NEW.native_projection->'next') key;
        IF keys IS DISTINCT FROM ARRAY['kind','token'] THEN
            RAISE EXCEPTION 'invalid native NEXT fields' USING ERRCODE='23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER trg_pulsara_v3_native_tool_projection BEFORE INSERT OR UPDATE ON pulsara_v3.tool_results
FOR EACH ROW EXECUTE FUNCTION pulsara_v3.seal_native_tool_projection();
REVOKE ALL ON FUNCTION pulsara_v3.seal_native_tool_projection() FROM PUBLIC;
