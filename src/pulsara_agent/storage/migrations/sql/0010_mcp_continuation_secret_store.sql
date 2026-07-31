CREATE TABLE public.mcp_continuation_secret_carriers (
    continuation_carrier_id text PRIMARY KEY,
    runtime_session_id text NOT NULL
        REFERENCES public.sessions(id) ON DELETE CASCADE,
    interaction_id text NOT NULL,
    round_ordinal integer NOT NULL CHECK (round_ordinal BETWEEN 1 AND 10),
    carrier_kind text NOT NULL CHECK (
        carrier_kind IN ('awaiting_client_input', 'replay_ready')
    ),
    algorithm text NOT NULL CHECK (algorithm = 'AES-256-GCM'),
    key_id text NOT NULL,
    nonce_bytes bytea NOT NULL CHECK (octet_length(nonce_bytes) = 12),
    ciphertext_bytes bytea NOT NULL CHECK (
        octet_length(ciphertext_bytes) BETWEEN 17 AND 524304
    ),
    aad_fingerprint text NOT NULL,
    carrier_plaintext_commitment text NOT NULL,
    stored_envelope_fingerprint text NOT NULL UNIQUE,
    carrier_state text NOT NULL CHECK (
        carrier_state IN (
            'awaiting_client_input', 'replay_ready', 'dispatch_reserved'
        )
    ),
    control_revision bigint NOT NULL CHECK (control_revision >= 1),
    source_event_id text NOT NULL,
    control_fingerprint text NOT NULL,
    created_at_utc timestamp with time zone NOT NULL,
    operation_expires_at_utc timestamp with time zone NOT NULL,
    expiry_fingerprint text NOT NULL,
    CHECK (operation_expires_at_utc > created_at_utc),
    CHECK (
        (carrier_kind = 'awaiting_client_input'
            AND carrier_state = 'awaiting_client_input')
        OR carrier_kind = 'replay_ready'
    )
);

CREATE INDEX idx_mcp_continuation_secret_carriers_session_state
    ON public.mcp_continuation_secret_carriers (
        runtime_session_id, carrier_state, operation_expires_at_utc,
        continuation_carrier_id
    );

CREATE INDEX idx_mcp_continuation_secret_carriers_expiry
    ON public.mcp_continuation_secret_carriers (
        operation_expires_at_utc, continuation_carrier_id
    );

ALTER TABLE public.ledger_materialization_accounts
    ADD COLUMN companion_payload_reserved_bytes_total bigint NOT NULL DEFAULT 0
        CHECK (companion_payload_reserved_bytes_total >= 0),
    ADD COLUMN companion_payload_charged_bytes_lifetime bigint NOT NULL DEFAULT 0
        CHECK (companion_payload_charged_bytes_lifetime >= 0),
    ADD COLUMN companion_charge_contract_fingerprint text;

ALTER TABLE public.ledger_materialization_accounts
    ADD CONSTRAINT ledger_materialization_accounts_companion_balance_check CHECK (
        companion_payload_charged_bytes_lifetime
            <= companion_payload_reserved_bytes_total
    );
