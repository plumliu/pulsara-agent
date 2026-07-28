ALTER TABLE public.durable_projection_jobs
    DROP CONSTRAINT durable_projection_jobs_status_check;

ALTER TABLE public.durable_projection_jobs
    ADD COLUMN dispatch_attempt_count integer NOT NULL DEFAULT 0
        CHECK (dispatch_attempt_count >= 0),
    ADD COLUMN settlement_generation bigint NOT NULL DEFAULT 0
        CHECK (settlement_generation >= 0),
    ADD COLUMN compaction_memory_deferral jsonb,
    ADD CONSTRAINT durable_projection_jobs_status_check CHECK (
        status IN (
            'pending', 'leased', 'retry_wait', 'model_retry_wait',
            'result_ready', 'settlement_writing', 'settlement_retry_wait',
            'reconciliation_required', 'succeeded', 'superseded', 'dead_letter'
        )
    );

ALTER TABLE public.memory_candidate_projection_outbox
    DROP CONSTRAINT memory_candidate_projection_outbox_producer_kind_check;

ALTER TABLE public.memory_candidate_projection_outbox
    ADD CONSTRAINT memory_candidate_projection_outbox_producer_kind_check CHECK (
        producer_kind IN ('reflection', 'compaction_memory_extraction')
    );

ALTER TABLE public.durable_projection_result_receipts
    DROP CONSTRAINT durable_projection_result_receipts_receipt_kind_check;

ALTER TABLE public.durable_projection_result_receipts
    ADD CONSTRAINT durable_projection_result_receipts_receipt_kind_check CHECK (
        receipt_kind IN (
            'applied', 'superseded', 'compaction_memory_extraction',
            'compaction_memory_extraction_superseded'
        )
    );

CREATE TABLE public.compaction_memory_extraction_result_candidates (
    result_candidate_id text PRIMARY KEY,
    job_id text NOT NULL UNIQUE
        REFERENCES public.durable_projection_jobs(job_id) ON DELETE CASCADE,
    target_key text NOT NULL,
    completed_event_id text NOT NULL UNIQUE,
    result_semantic_fingerprint text NOT NULL,
    candidate_payload jsonb NOT NULL,
    candidate_fingerprint text NOT NULL UNIQUE,
    created_at timestamp with time zone NOT NULL DEFAULT now()
);

ALTER TABLE public.memory_candidates
    ADD COLUMN candidate_semantic_fingerprint text;

ALTER TABLE public.memory_candidate_projection_outbox
    ADD COLUMN candidate_semantic_fingerprint text;

CREATE TABLE public.background_derived_work_budget_accounts (
    runtime_session_id text PRIMARY KEY
        REFERENCES public.sessions(id) ON DELETE CASCADE,
    policy_payload jsonb NOT NULL,
    policy_fingerprint text NOT NULL,
    account_revision bigint NOT NULL CHECK (account_revision >= 0),
    account_payload jsonb NOT NULL,
    account_fingerprint text NOT NULL,
    updated_at timestamp with time zone NOT NULL DEFAULT now()
);

CREATE TABLE public.background_derived_work_budget_reservations (
    reservation_id text PRIMARY KEY,
    runtime_session_id text NOT NULL
        REFERENCES public.sessions(id) ON DELETE CASCADE,
    extraction_job_id text NOT NULL
        REFERENCES public.durable_projection_jobs(job_id) ON DELETE CASCADE,
    operation_id text NOT NULL,
    resolved_model_call_id text NOT NULL UNIQUE,
    dispatch_attempt_ordinal integer NOT NULL CHECK (dispatch_attempt_ordinal >= 1),
    reservation_payload jsonb NOT NULL,
    reservation_fingerprint text NOT NULL UNIQUE,
    status text NOT NULL CHECK (status IN ('open', 'settled', 'reconciliation_required')),
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    updated_at timestamp with time zone NOT NULL DEFAULT now(),
    UNIQUE (extraction_job_id, dispatch_attempt_ordinal)
);

CREATE TABLE public.background_derived_work_budget_settlements (
    settlement_fingerprint text PRIMARY KEY,
    reservation_id text NOT NULL UNIQUE
        REFERENCES public.background_derived_work_budget_reservations(reservation_id),
    model_call_end_event_id text NOT NULL UNIQUE,
    settlement_payload jsonb NOT NULL,
    created_at timestamp with time zone NOT NULL DEFAULT now()
);

CREATE INDEX idx_compaction_memory_extraction_result_candidates_job
    ON public.compaction_memory_extraction_result_candidates (job_id, result_candidate_id);
CREATE INDEX idx_background_derived_work_reservations_open
    ON public.background_derived_work_budget_reservations (
        runtime_session_id, status, created_at, reservation_id
    );
