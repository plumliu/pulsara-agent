CREATE TABLE public.prompt_queue_accounts (
    session_id text PRIMARY KEY
        REFERENCES public.sessions(id) ON DELETE CASCADE,
    next_accepted_ordinal bigint NOT NULL DEFAULT 1
        CHECK (next_accepted_ordinal >= 1),
    queue_chain_head_event_id text,
    queue_chain_head_sequence bigint NOT NULL DEFAULT 0
        CHECK (queue_chain_head_sequence >= 0),
    queue_chain_head_payload_fingerprint text,
    account_revision bigint NOT NULL DEFAULT 0 CHECK (account_revision >= 0),
    checkpoint_generation bigint NOT NULL DEFAULT 0
        CHECK (checkpoint_generation >= 0),
    checkpoint_through_sequence bigint NOT NULL DEFAULT 0
        CHECK (checkpoint_through_sequence >= 0),
    checkpoint_fingerprint text NOT NULL,
    transition_count bigint NOT NULL DEFAULT 0 CHECK (transition_count >= 0),
    transition_accumulator text NOT NULL,
    bounded_tail_first_sequence bigint NOT NULL DEFAULT 0
        CHECK (bounded_tail_first_sequence >= 0),
    bounded_tail_count integer NOT NULL DEFAULT 0
        CHECK (bounded_tail_count BETWEEN 0 AND 256),
    bounded_tail_payload_bytes bigint NOT NULL DEFAULT 0
        CHECK (bounded_tail_payload_bytes BETWEEN 0 AND 8388608),
    bounded_tail_accumulator text NOT NULL,
    pending_item_count integer NOT NULL DEFAULT 0 CHECK (pending_item_count >= 0),
    reserved_item_count integer NOT NULL DEFAULT 0 CHECK (reserved_item_count >= 0),
    artifact_bytes bigint NOT NULL DEFAULT 0 CHECK (artifact_bytes >= 0),
    pending_item_head_set_accumulator text NOT NULL,
    row_set_accumulator text NOT NULL,
    reducer_contract_fingerprint text NOT NULL,
    event_registry_fingerprint text NOT NULL,
    account_fingerprint text NOT NULL,
    updated_at timestamp with time zone NOT NULL DEFAULT now(),
    CHECK (
        (queue_chain_head_event_id IS NULL
            AND queue_chain_head_sequence = 0
            AND queue_chain_head_payload_fingerprint IS NULL
            AND account_revision = 0)
        OR
        (queue_chain_head_event_id IS NOT NULL
            AND queue_chain_head_sequence >= 1
            AND queue_chain_head_payload_fingerprint IS NOT NULL
            AND account_revision >= 1)
    )
);

CREATE TABLE public.prompt_queue_items (
    session_id text NOT NULL
        REFERENCES public.sessions(id) ON DELETE CASCADE,
    queue_item_id text NOT NULL,
    accepted_ordinal bigint NOT NULL CHECK (accepted_ordinal >= 1),
    delivery_state text NOT NULL CHECK (
        delivery_state IN (
            'accepted_pending', 'steer_reserved', 'follow_up_reserved',
            'committed_to_active_run', 'committed_to_new_run', 'cancelled',
            'delivery_rejected', 'reconciliation_required'
        )
    ),
    content_retention_state text NOT NULL CHECK (
        content_retention_state IN ('active', 'retired')
    ),
    row_revision bigint NOT NULL CHECK (row_revision >= 1),
    head_transition_event_id text NOT NULL,
    head_transition_event_type text NOT NULL,
    head_transition_sequence bigint NOT NULL CHECK (head_transition_sequence >= 1),
    head_candidate_payload_fingerprint text NOT NULL,
    requested_delivery_mode text NOT NULL CHECK (
        requested_delivery_mode IN ('auto', 'steer', 'follow_up')
    ),
    resolved_delivery_mode text NOT NULL CHECK (
        resolved_delivery_mode IN ('pending', 'steer', 'follow_up')
    ),
    state_payload jsonb NOT NULL,
    reducer_contract_fingerprint text NOT NULL,
    event_registry_fingerprint text NOT NULL,
    row_fingerprint text NOT NULL,
    updated_at timestamp with time zone NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, queue_item_id),
    UNIQUE (session_id, accepted_ordinal),
    UNIQUE (session_id, head_transition_event_id)
);

CREATE TABLE public.prompt_queue_content_references (
    session_id text NOT NULL
        REFERENCES public.sessions(id) ON DELETE CASCADE,
    queue_item_id text NOT NULL,
    content_kind text NOT NULL CHECK (
        content_kind IN ('inline', 'confirmed_artifact')
    ),
    content_semantic_fingerprint text NOT NULL,
    content_attribution_fingerprint text NOT NULL,
    content_fact_fingerprint text NOT NULL,
    artifact_id text REFERENCES public.artifacts(id) ON DELETE RESTRICT,
    preparation_id text,
    hold_revision bigint CHECK (hold_revision IS NULL OR hold_revision >= 0),
    reference_payload jsonb NOT NULL,
    reference_fingerprint text NOT NULL,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, queue_item_id),
    FOREIGN KEY (session_id, queue_item_id)
        REFERENCES public.prompt_queue_items(session_id, queue_item_id)
        ON DELETE RESTRICT,
    CHECK (
        (content_kind = 'inline'
            AND artifact_id IS NULL
            AND preparation_id IS NULL
            AND hold_revision IS NULL)
        OR
        (content_kind = 'confirmed_artifact'
            AND artifact_id IS NOT NULL
            AND preparation_id IS NOT NULL
            AND hold_revision IS NOT NULL)
    )
);

CREATE TABLE public.prompt_queue_artifact_preparation_holds (
    preparation_id text PRIMARY KEY,
    session_id text NOT NULL
        REFERENCES public.sessions(id) ON DELETE CASCADE,
    owner_client_submission_identity text NOT NULL,
    artifact_id text NOT NULL
        REFERENCES public.artifacts(id) ON DELETE RESTRICT,
    artifact_identity_fingerprint text NOT NULL,
    content_fingerprint text NOT NULL,
    state text NOT NULL CHECK (state IN ('PREPARED', 'CONSUMED', 'RELEASED')),
    consuming_queue_item_id text,
    hold_revision bigint NOT NULL CHECK (hold_revision >= 0),
    created_at_utc timestamp with time zone NOT NULL,
    expires_at_utc timestamp with time zone NOT NULL,
    hold_payload jsonb NOT NULL,
    preparation_fingerprint text NOT NULL UNIQUE,
    hold_row_fingerprint text NOT NULL,
    updated_at timestamp with time zone NOT NULL DEFAULT now(),
    CHECK (expires_at_utc > created_at_utc),
    CHECK (
        (state = 'PREPARED' AND consuming_queue_item_id IS NULL)
        OR (state = 'CONSUMED' AND consuming_queue_item_id IS NOT NULL)
        OR state = 'RELEASED'
    )
);

CREATE TABLE public.terminal_command_receipts (
    session_id text NOT NULL
        REFERENCES public.sessions(id) ON DELETE CASCADE,
    client_instance_id text NOT NULL,
    command_id text NOT NULL,
    command_kind text NOT NULL,
    request_semantic_fingerprint text NOT NULL,
    target_id text NOT NULL,
    target_generation bigint NOT NULL CHECK (target_generation >= 1),
    receipt_revision bigint NOT NULL CHECK (receipt_revision >= 1),
    outcome_payload jsonb NOT NULL,
    receipt_fingerprint text NOT NULL,
    updated_at timestamp with time zone NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, client_instance_id, command_id)
);

CREATE INDEX idx_prompt_queue_items_session_state_ordinal
    ON public.prompt_queue_items (session_id, delivery_state, accepted_ordinal);
CREATE INDEX idx_prompt_queue_items_session_head
    ON public.prompt_queue_items (session_id, head_transition_sequence);
CREATE INDEX idx_prompt_queue_content_references_artifact
    ON public.prompt_queue_content_references (artifact_id, session_id, queue_item_id)
    WHERE artifact_id IS NOT NULL;
CREATE INDEX idx_prompt_queue_artifact_holds_session_state_expiry
    ON public.prompt_queue_artifact_preparation_holds (
        session_id, state, expires_at_utc, preparation_id
    );
CREATE INDEX idx_terminal_command_receipts_session_updated
    ON public.terminal_command_receipts (session_id, updated_at, command_id);
