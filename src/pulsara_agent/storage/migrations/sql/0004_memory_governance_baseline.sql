CREATE TABLE public.memory_candidate_evidence_rejections (
    runtime_session_id text NOT NULL,
    candidate_entry_id text NOT NULL,
    claim_generation integer NOT NULL,
    governance_batch_id text NOT NULL,
    rejection_event_id text NOT NULL,
    rejection_payload jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT memory_candidate_evidence_rejections_claim_generation_check CHECK ((claim_generation >= 1))
);

CREATE TABLE public.memory_candidate_projection_outbox (
    runtime_session_id text NOT NULL,
    producer_kind text NOT NULL,
    producer_event_id text NOT NULL,
    candidate_entry_id text NOT NULL,
    candidate_index integer NOT NULL,
    outbox_item_fingerprint text NOT NULL,
    producer_payload_fingerprint text NOT NULL,
    producer_event_identity jsonb NOT NULL,
    candidate_payload_fingerprint text NOT NULL,
    candidate_attribution_fingerprint text NOT NULL,
    candidate_payload jsonb NOT NULL,
    status text NOT NULL,
    last_stable_failure_code text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    applied_at timestamp with time zone,
    CONSTRAINT memory_candidate_projection_outbox_candidate_index_check CHECK ((candidate_index >= 0)),
    CONSTRAINT memory_candidate_projection_outbox_producer_kind_check CHECK ((producer_kind = ANY (ARRAY['reflection'::text, 'compaction'::text]))),
    CONSTRAINT memory_candidate_projection_outbox_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'applying'::text, 'applied'::text, 'failed'::text])))
);

CREATE TABLE public.memory_candidates (
    entry_id text NOT NULL,
    payload jsonb NOT NULL,
    origin text NOT NULL,
    source_session_id text NOT NULL,
    source_run_id text NOT NULL,
    source_turn_id text NOT NULL,
    source_reply_id text NOT NULL,
    source_tool_call_id text,
    user_quote text,
    quoted_evidence_locator jsonb,
    source_event_id text,
    source_artifact_id text,
    intent_fingerprint text,
    evidence_rejection_event_id text,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.memory_governance_batch_inputs (
    runtime_session_id text NOT NULL,
    governance_batch_id text NOT NULL,
    batch_input_reference jsonb NOT NULL,
    preparing_claims_fingerprint text NOT NULL,
    source_ledger_through_sequence bigint NOT NULL,
    resolved_model_call_id text NOT NULL,
    status text NOT NULL,
    prepared_event_id text,
    terminal_event_id text,
    record_fingerprint text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT memory_governance_batch_inpu_source_ledger_through_sequen_check CHECK ((source_ledger_through_sequence >= 0)),
    CONSTRAINT memory_governance_batch_inputs_status_check CHECK ((status = ANY (ARRAY['staged'::text, 'prepared'::text, 'terminal'::text])))
);

CREATE TABLE public.memory_governance_candidate_claims (
    candidate_entry_id text NOT NULL,
    runtime_session_id text NOT NULL,
    candidate_row_fingerprint text NOT NULL,
    governance_batch_id text NOT NULL,
    claim_generation integer NOT NULL,
    status text NOT NULL,
    prepared_event_id text,
    terminal_record_id text,
    previous_claim_fingerprint text,
    claim_fingerprint text NOT NULL,
    claim_payload jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT memory_governance_candidate_claims_claim_generation_check CHECK ((claim_generation >= 1)),
    CONSTRAINT memory_governance_candidate_claims_status_check CHECK ((status = ANY (ARRAY['preparing'::text, 'prepared'::text, 'terminal'::text, 'released'::text])))
);

CREATE TABLE public.memory_governance_decisions (
    decision_id text NOT NULL,
    governance_batch_id text NOT NULL,
    batch_input_fingerprint text NOT NULL,
    batch_input_reference_fingerprint text NOT NULL,
    governance_model_call_id text NOT NULL,
    decision_index integer NOT NULL,
    requested_decision_payload_fingerprint text NOT NULL,
    decision_payload_fingerprint text NOT NULL,
    decision jsonb NOT NULL,
    write_outcome jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.memory_candidate_evidence_rejections
    ADD CONSTRAINT memory_candidate_evidence_rejections_pkey PRIMARY KEY (candidate_entry_id, claim_generation);

ALTER TABLE ONLY public.memory_candidate_projection_outbox
    ADD CONSTRAINT memory_candidate_projection_outbox_pkey PRIMARY KEY (runtime_session_id, producer_kind, producer_event_id, candidate_entry_id);

ALTER TABLE ONLY public.memory_candidates
    ADD CONSTRAINT memory_candidates_pkey PRIMARY KEY (entry_id);

ALTER TABLE ONLY public.memory_governance_batch_inputs
    ADD CONSTRAINT memory_governance_batch_inputs_pkey PRIMARY KEY (runtime_session_id, governance_batch_id);

ALTER TABLE ONLY public.memory_governance_candidate_claims
    ADD CONSTRAINT memory_governance_candidate_claims_pkey PRIMARY KEY (candidate_entry_id);

ALTER TABLE ONLY public.memory_governance_decisions
    ADD CONSTRAINT memory_governance_decisions_pkey PRIMARY KEY (decision_id);

CREATE INDEX idx_memory_candidate_evidence_rejections_session ON public.memory_candidate_evidence_rejections USING btree (runtime_session_id, created_at);

CREATE INDEX idx_memory_candidate_projection_outbox_pending ON public.memory_candidate_projection_outbox USING btree (runtime_session_id, status, created_at);

CREATE INDEX idx_memory_candidates_origin ON public.memory_candidates USING btree (origin);

CREATE INDEX idx_memory_candidates_session_origin_fingerprint ON public.memory_candidates USING btree (source_session_id, origin, intent_fingerprint) WHERE (intent_fingerprint IS NOT NULL);

CREATE INDEX idx_memory_candidates_source_run ON public.memory_candidates USING btree (source_run_id, created_at);

CREATE INDEX idx_memory_governance_claims_batch ON public.memory_governance_candidate_claims USING btree (runtime_session_id, governance_batch_id, status);

CREATE INDEX idx_memory_governance_decisions_batch ON public.memory_governance_decisions USING btree (governance_batch_id, created_at);

CREATE UNIQUE INDEX idx_memory_governance_decisions_input_index ON public.memory_governance_decisions USING btree (batch_input_fingerprint, decision_index);

ALTER TABLE ONLY public.memory_candidate_evidence_rejections
    ADD CONSTRAINT memory_candidate_evidence_rejections_candidate_entry_id_fkey FOREIGN KEY (candidate_entry_id) REFERENCES public.memory_candidates(entry_id);

ALTER TABLE ONLY public.memory_candidate_evidence_rejections
    ADD CONSTRAINT memory_candidate_evidence_rejections_runtime_session_id_fkey FOREIGN KEY (runtime_session_id) REFERENCES public.sessions(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.memory_candidates
    ADD CONSTRAINT memory_candidates_source_run_id_fkey FOREIGN KEY (source_run_id) REFERENCES public.runs(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.memory_candidates
    ADD CONSTRAINT memory_candidates_source_session_id_fkey FOREIGN KEY (source_session_id) REFERENCES public.sessions(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.memory_candidates
    ADD CONSTRAINT memory_candidates_source_turn_id_fkey FOREIGN KEY (source_turn_id) REFERENCES public.turns(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.memory_governance_batch_inputs
    ADD CONSTRAINT memory_governance_batch_inputs_runtime_session_id_fkey FOREIGN KEY (runtime_session_id) REFERENCES public.sessions(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.memory_governance_candidate_claims
    ADD CONSTRAINT memory_governance_candidate_claims_candidate_entry_id_fkey FOREIGN KEY (candidate_entry_id) REFERENCES public.memory_candidates(entry_id);

ALTER TABLE ONLY public.memory_governance_candidate_claims
    ADD CONSTRAINT memory_governance_candidate_claims_runtime_session_id_fkey FOREIGN KEY (runtime_session_id) REFERENCES public.sessions(id) ON DELETE CASCADE;
