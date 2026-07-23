CREATE TABLE public.agent_events (
    id text NOT NULL,
    session_id text NOT NULL,
    run_id text NOT NULL,
    turn_id text NOT NULL,
    reply_id text NOT NULL,
    sequence bigint NOT NULL,
    event_type text NOT NULL,
    event_schema_version text NOT NULL,
    event_schema_fingerprint text NOT NULL,
    event_domain_contract_fingerprint text NOT NULL,
    transcript_event_domain text NOT NULL,
    transcript_semantic_prefix_count bigint NOT NULL,
    transcript_semantic_prefix_accumulator text NOT NULL,
    ledger_continuity_accumulator text NOT NULL,
    ledger_payload_prefix_bytes bigint NOT NULL,
    created_at timestamp with time zone NOT NULL,
    payload jsonb NOT NULL,
    CONSTRAINT agent_events_ledger_payload_prefix_bytes_check CHECK ((ledger_payload_prefix_bytes >= 0)),
    CONSTRAINT agent_events_transcript_event_domain_check CHECK ((transcript_event_domain = ANY (ARRAY['transcript_semantic'::text, 'transcript_acceleration'::text, 'non_transcript'::text]))),
    CONSTRAINT agent_events_transcript_semantic_prefix_count_check CHECK ((transcript_semantic_prefix_count >= 0))
);

CREATE TABLE public.artifacts (
    id text NOT NULL,
    session_id text,
    run_id text,
    media_type text DEFAULT 'text/plain'::text NOT NULL,
    text_body text,
    binary_body bytea,
    digest text NOT NULL,
    size_bytes bigint NOT NULL,
    stored_at text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT artifacts_check CHECK (((text_body IS NOT NULL) OR (binary_body IS NOT NULL)))
);

CREATE TABLE public.ledger_materialization_accounts (
    session_id text NOT NULL,
    account_state_fingerprint text NOT NULL,
    ledger_materialization_generation bigint NOT NULL,
    consumer_horizon_revision bigint NOT NULL,
    ledger_through_sequence bigint NOT NULL,
    state_payload jsonb NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ledger_materialization_accou_ledger_materialization_gener_check CHECK ((ledger_materialization_generation >= 0)),
    CONSTRAINT ledger_materialization_accounts_consumer_horizon_revision_check CHECK ((consumer_horizon_revision >= 0)),
    CONSTRAINT ledger_materialization_accounts_ledger_through_sequence_check CHECK ((ledger_through_sequence >= 0))
);

CREATE TABLE public.runs (
    id text NOT NULL,
    session_id text NOT NULL,
    status text DEFAULT 'running'::text NOT NULL,
    stop_reason text,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);

CREATE TABLE public.runtime_projection_checkpoints (
    session_id text NOT NULL,
    projection_kind text NOT NULL,
    through_sequence bigint NOT NULL,
    projection_schema_version text NOT NULL,
    ledger_prefix jsonb NOT NULL,
    validation_base_through_sequence bigint NOT NULL,
    validation_base_state_payload jsonb NOT NULL,
    payload_fingerprint text NOT NULL,
    state_payload jsonb NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT runtime_projection_checkpoints_check CHECK (((validation_base_through_sequence >= 0) AND (validation_base_through_sequence <= through_sequence))),
    CONSTRAINT runtime_projection_checkpoints_through_sequence_check CHECK ((through_sequence >= 0))
);

CREATE TABLE public.sessions (
    id text NOT NULL,
    workspace_root text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);

CREATE TABLE public.tool_execution_records (
    id text NOT NULL,
    session_id text NOT NULL,
    run_id text NOT NULL,
    turn_id text NOT NULL,
    tool_call_id text NOT NULL,
    tool_name text NOT NULL,
    status text NOT NULL,
    input_summary text DEFAULT ''::text NOT NULL,
    output_summary text DEFAULT ''::text NOT NULL,
    artifact_id text,
    event_start_sequence bigint,
    event_end_sequence bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);

CREATE TABLE public.tool_result_artifacts (
    id text NOT NULL,
    session_id text NOT NULL,
    run_id text NOT NULL,
    turn_id text NOT NULL,
    reply_id text NOT NULL,
    tool_call_id text NOT NULL,
    tool_name text NOT NULL,
    artifact_id text NOT NULL,
    role text DEFAULT 'output'::text NOT NULL,
    ordinal integer DEFAULT 0 NOT NULL,
    media_type text NOT NULL,
    size_bytes bigint NOT NULL,
    stored_complete boolean DEFAULT true NOT NULL,
    loss_reason text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);

CREATE TABLE public.turns (
    id text NOT NULL,
    session_id text NOT NULL,
    run_id text NOT NULL,
    turn_index integer NOT NULL,
    status text DEFAULT 'running'::text NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);

CREATE TABLE public.working_context_summaries (
    summary_id text NOT NULL,
    memory_domain_id text NOT NULL,
    summary text NOT NULL,
    workspace_label text,
    workspace_key text,
    source_session_id text NOT NULL,
    source_run_id text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);

ALTER TABLE ONLY public.agent_events
    ADD CONSTRAINT agent_events_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.agent_events
    ADD CONSTRAINT agent_events_session_id_sequence_key UNIQUE (session_id, sequence);

ALTER TABLE ONLY public.artifacts
    ADD CONSTRAINT artifacts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.ledger_materialization_accounts
    ADD CONSTRAINT ledger_materialization_accounts_pkey PRIMARY KEY (session_id);

ALTER TABLE ONLY public.runs
    ADD CONSTRAINT runs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.runtime_projection_checkpoints
    ADD CONSTRAINT runtime_projection_checkpoints_pkey PRIMARY KEY (session_id, projection_kind);

ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.tool_execution_records
    ADD CONSTRAINT tool_execution_records_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.tool_execution_records
    ADD CONSTRAINT tool_execution_records_run_id_tool_call_id_key UNIQUE (run_id, tool_call_id);

ALTER TABLE ONLY public.tool_result_artifacts
    ADD CONSTRAINT tool_result_artifacts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.tool_result_artifacts
    ADD CONSTRAINT tool_result_artifacts_run_id_tool_call_id_role_ordinal_key UNIQUE (run_id, tool_call_id, role, ordinal);

ALTER TABLE ONLY public.turns
    ADD CONSTRAINT turns_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.turns
    ADD CONSTRAINT turns_run_id_turn_index_key UNIQUE (run_id, turn_index);

ALTER TABLE ONLY public.working_context_summaries
    ADD CONSTRAINT working_context_summaries_memory_domain_id_key UNIQUE (memory_domain_id);

ALTER TABLE ONLY public.working_context_summaries
    ADD CONSTRAINT working_context_summaries_pkey PRIMARY KEY (summary_id);

CREATE INDEX idx_agent_events_reply_sequence ON public.agent_events USING btree (reply_id, sequence);

CREATE INDEX idx_agent_events_run_sequence ON public.agent_events USING btree (run_id, sequence);

CREATE INDEX idx_agent_events_session_model_call_sequence ON public.agent_events USING btree (session_id, COALESCE((payload #>> '{resolved_call,resolved_model_call_id}'::text[]), (payload #>> '{resolved_model_call_id}'::text[]), (payload #>> '{model_stream_attribution,resolved_model_call_id}'::text[])), sequence);

CREATE INDEX idx_agent_events_session_transcript_domain_sequence ON public.agent_events USING btree (session_id, transcript_event_domain, sequence);

CREATE INDEX idx_agent_events_session_type_sequence ON public.agent_events USING btree (session_id, event_type, sequence);

CREATE INDEX idx_agent_events_type ON public.agent_events USING btree (event_type);

CREATE INDEX idx_artifacts_run_id ON public.artifacts USING btree (run_id);

CREATE INDEX idx_artifacts_session_id ON public.artifacts USING btree (session_id);

CREATE INDEX idx_runs_session_id ON public.runs USING btree (session_id);

CREATE INDEX idx_tool_execution_records_artifact_id ON public.tool_execution_records USING btree (artifact_id);

CREATE INDEX idx_tool_execution_records_run_id ON public.tool_execution_records USING btree (run_id);

CREATE INDEX idx_tool_result_artifacts_artifact_id ON public.tool_result_artifacts USING btree (artifact_id);

CREATE INDEX idx_tool_result_artifacts_session_id ON public.tool_result_artifacts USING btree (session_id);

CREATE INDEX idx_turns_run_id ON public.turns USING btree (run_id);

CREATE INDEX idx_working_context_domain ON public.working_context_summaries USING btree (memory_domain_id, updated_at);

ALTER TABLE ONLY public.agent_events
    ADD CONSTRAINT agent_events_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.runs(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.agent_events
    ADD CONSTRAINT agent_events_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.agent_events
    ADD CONSTRAINT agent_events_turn_id_fkey FOREIGN KEY (turn_id) REFERENCES public.turns(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.artifacts
    ADD CONSTRAINT artifacts_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.ledger_materialization_accounts
    ADD CONSTRAINT ledger_materialization_accounts_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.runs
    ADD CONSTRAINT runs_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.runtime_projection_checkpoints
    ADD CONSTRAINT runtime_projection_checkpoints_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.tool_execution_records
    ADD CONSTRAINT tool_execution_records_artifact_id_fkey FOREIGN KEY (artifact_id) REFERENCES public.artifacts(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.tool_execution_records
    ADD CONSTRAINT tool_execution_records_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.runs(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.tool_execution_records
    ADD CONSTRAINT tool_execution_records_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.tool_execution_records
    ADD CONSTRAINT tool_execution_records_turn_id_fkey FOREIGN KEY (turn_id) REFERENCES public.turns(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.tool_result_artifacts
    ADD CONSTRAINT tool_result_artifacts_artifact_id_fkey FOREIGN KEY (artifact_id) REFERENCES public.artifacts(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.tool_result_artifacts
    ADD CONSTRAINT tool_result_artifacts_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.runs(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.tool_result_artifacts
    ADD CONSTRAINT tool_result_artifacts_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.tool_result_artifacts
    ADD CONSTRAINT tool_result_artifacts_turn_id_fkey FOREIGN KEY (turn_id) REFERENCES public.turns(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.turns
    ADD CONSTRAINT turns_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.runs(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.turns
    ADD CONSTRAINT turns_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id) ON DELETE CASCADE;

--
