CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE public.runtime_write_guard_secrets (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    guard_secret bytea NOT NULL CHECK (octet_length(guard_secret) = 32),
    authorized_runtime_role text NOT NULL,
    created_at timestamp with time zone NOT NULL DEFAULT now()
);

CREATE TABLE public.runtime_write_admission_epochs (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    epoch_number bigint NOT NULL CHECK (epoch_number >= 1),
    mode text NOT NULL CHECK (mode IN ('normal', 'maintenance')),
    authorized_runtime_role text NOT NULL,
    active_migration_registry_prefix_fingerprint text NOT NULL,
    protected_relation_registry_fingerprint text NOT NULL,
    maintenance_operation_id text,
    target_migration_version integer,
    state_revision bigint NOT NULL CHECK (state_revision >= 1),
    epoch_payload jsonb NOT NULL,
    epoch_fingerprint text NOT NULL,
    updated_at timestamp with time zone NOT NULL DEFAULT now(),
    CHECK (
        (mode = 'normal' AND maintenance_operation_id IS NULL AND target_migration_version IS NULL)
        OR
        (mode = 'maintenance' AND maintenance_operation_id IS NOT NULL AND target_migration_version IS NOT NULL)
    )
);

CREATE TABLE public.runtime_write_protected_relations (
    schema_name text NOT NULL,
    relation_name text NOT NULL,
    relation_payload jsonb NOT NULL,
    relation_fingerprint text NOT NULL,
    registry_fingerprint text NOT NULL,
    PRIMARY KEY (schema_name, relation_name)
);

CREATE TABLE public.durable_projection_kind_activations (
    projection_kind text PRIMARY KEY,
    activation_payload jsonb NOT NULL,
    activation_fingerprint text NOT NULL UNIQUE,
    created_at timestamp with time zone NOT NULL DEFAULT now()
);

CREATE TABLE public.durable_projection_pre_activation_contracts (
    projection_kind text PRIMARY KEY,
    contract_payload jsonb NOT NULL,
    contract_fingerprint text NOT NULL UNIQUE,
    created_at timestamp with time zone NOT NULL DEFAULT now()
);

CREATE TABLE public.durable_projection_pre_activation_session_cutovers (
    runtime_session_id text NOT NULL REFERENCES public.sessions(id) ON DELETE CASCADE,
    projection_kind text NOT NULL,
    cutover_payload jsonb NOT NULL,
    cutover_fingerprint text NOT NULL,
    PRIMARY KEY (runtime_session_id, projection_kind)
);

CREATE TABLE public.durable_projection_pre_activation_coverage_pages (
    page_fingerprint text PRIMARY KEY,
    runtime_session_id text NOT NULL REFERENCES public.sessions(id) ON DELETE CASCADE,
    projection_kind text NOT NULL,
    page_index integer NOT NULL CHECK (page_index >= 0),
    page_payload jsonb NOT NULL,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    UNIQUE (runtime_session_id, projection_kind, page_index, page_fingerprint)
);

CREATE TABLE public.durable_projection_pre_activation_coverage_receipts (
    coverage_receipt_id text PRIMARY KEY,
    runtime_session_id text NOT NULL REFERENCES public.sessions(id) ON DELETE CASCADE,
    projection_kind text NOT NULL,
    frozen_through_sequence bigint NOT NULL CHECK (frozen_through_sequence >= 0),
    receipt_payload jsonb NOT NULL,
    receipt_fingerprint text NOT NULL UNIQUE,
    created_at timestamp with time zone NOT NULL DEFAULT now()
);

CREATE TABLE public.durable_projection_session_cutovers (
    runtime_session_id text NOT NULL REFERENCES public.sessions(id) ON DELETE CASCADE,
    projection_kind text NOT NULL,
    cutover_through_sequence bigint NOT NULL CHECK (cutover_through_sequence >= 0),
    cutover_payload jsonb NOT NULL,
    cutover_fingerprint text NOT NULL,
    PRIMARY KEY (runtime_session_id, projection_kind)
);

CREATE TABLE public.durable_projection_seed_failures (
    failure_id text PRIMARY KEY,
    runtime_session_id text NOT NULL REFERENCES public.sessions(id) ON DELETE CASCADE,
    projection_kind text NOT NULL,
    blocked_from_sequence bigint NOT NULL CHECK (blocked_from_sequence >= 0),
    blocked_through_sequence bigint NOT NULL CHECK (blocked_through_sequence >= blocked_from_sequence),
    failure_payload jsonb NOT NULL,
    failure_fingerprint text NOT NULL UNIQUE,
    created_at timestamp with time zone NOT NULL DEFAULT now()
);

CREATE TABLE public.durable_projection_seed_failure_resolutions (
    resolution_fingerprint text PRIMARY KEY,
    failure_id text NOT NULL REFERENCES public.durable_projection_seed_failures(failure_id),
    resolution_payload jsonb NOT NULL,
    created_at timestamp with time zone NOT NULL DEFAULT now()
);

CREATE TABLE public.durable_projection_jobs (
    job_id text PRIMARY KEY,
    projection_kind text NOT NULL,
    target_key text NOT NULL,
    runtime_session_id text NOT NULL REFERENCES public.sessions(id) ON DELETE CASCADE,
    run_id text NOT NULL,
    source_event_id text NOT NULL,
    source_sequence bigint NOT NULL CHECK (source_sequence >= 1),
    source_event_type text NOT NULL,
    source_reference jsonb NOT NULL,
    trigger_horizon jsonb NOT NULL,
    handler_contract jsonb NOT NULL,
    handler_contract_fingerprint text NOT NULL,
    activation_fingerprint text NOT NULL,
    seed_contract_fingerprint text NOT NULL,
    delivery_policy jsonb NOT NULL,
    delivery_policy_fingerprint text NOT NULL,
    canonical_mutation_surface_plan jsonb NOT NULL,
    canonical_mutation_surface_plan_fingerprint text NOT NULL,
    job_semantic_fingerprint text NOT NULL,
    job_candidate_fingerprint text NOT NULL,
    status text NOT NULL CHECK (
        status IN ('pending', 'leased', 'retry_wait', 'succeeded', 'superseded', 'dead_letter')
    ),
    state_revision bigint NOT NULL CHECK (state_revision >= 0),
    repair_generation bigint NOT NULL CHECK (repair_generation >= 0),
    attempt_count integer NOT NULL CHECK (attempt_count >= 0),
    lease_generation bigint NOT NULL CHECK (lease_generation >= 0),
    lease_owner_id text,
    lease_expires_at timestamp with time zone,
    next_attempt_at timestamp with time zone,
    last_failure jsonb,
    result_receipt_reference jsonb,
    state_fingerprint text NOT NULL,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    updated_at timestamp with time zone NOT NULL DEFAULT now(),
    UNIQUE (projection_kind, source_event_id, target_key)
);

CREATE INDEX idx_durable_projection_jobs_claim
    ON public.durable_projection_jobs (status, next_attempt_at, created_at, job_id);
CREATE INDEX idx_durable_projection_jobs_source
    ON public.durable_projection_jobs (runtime_session_id, source_sequence);
CREATE INDEX idx_durable_projection_jobs_target
    ON public.durable_projection_jobs (projection_kind, target_key, source_sequence);
CREATE INDEX idx_durable_projection_jobs_lease_expiry
    ON public.durable_projection_jobs (lease_expires_at, job_id)
    WHERE status = 'leased';

CREATE TABLE public.durable_projection_result_receipts (
    receipt_id text PRIMARY KEY,
    receipt_kind text NOT NULL CHECK (receipt_kind IN ('applied', 'superseded')),
    projection_kind text NOT NULL,
    target_key text NOT NULL,
    candidate_source_sequence bigint NOT NULL CHECK (candidate_source_sequence >= 1),
    effective_source_sequence bigint NOT NULL CHECK (effective_source_sequence >= 1),
    result_semantic_fingerprint text NOT NULL,
    receipt_payload jsonb NOT NULL,
    receipt_fingerprint text NOT NULL UNIQUE,
    created_at timestamp with time zone NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX idx_durable_projection_receipts_applied_source
    ON public.durable_projection_result_receipts (
        projection_kind, target_key, effective_source_sequence
    )
    WHERE receipt_kind = 'applied';
CREATE INDEX idx_durable_projection_receipts_candidate
    ON public.durable_projection_result_receipts (
        projection_kind, target_key, candidate_source_sequence
    );

CREATE TABLE public.durable_projection_target_heads (
    projection_kind text NOT NULL,
    target_key text NOT NULL,
    source_sequence bigint NOT NULL CHECK (source_sequence >= 1),
    head_payload jsonb NOT NULL,
    head_fingerprint text NOT NULL,
    updated_at timestamp with time zone NOT NULL DEFAULT now(),
    PRIMARY KEY (projection_kind, target_key)
);

CREATE TABLE public.durable_projection_target_authority_conflicts (
    conflict_id text PRIMARY KEY,
    projection_kind text NOT NULL,
    target_key text NOT NULL,
    candidate_source_sequence bigint NOT NULL CHECK (candidate_source_sequence >= 1),
    existing_target_head_fingerprint text NOT NULL,
    conflict_payload jsonb NOT NULL,
    conflict_fingerprint text NOT NULL UNIQUE,
    created_at timestamp with time zone NOT NULL DEFAULT now()
);
CREATE INDEX idx_durable_projection_target_conflicts
    ON public.durable_projection_target_authority_conflicts (
        projection_kind, target_key, created_at, conflict_id
    );

CREATE TABLE public.durable_projection_target_execution_leases (
    projection_kind text NOT NULL,
    target_key text NOT NULL,
    owner_job_id text NOT NULL REFERENCES public.durable_projection_jobs(job_id) ON DELETE CASCADE,
    source_sequence bigint NOT NULL CHECK (source_sequence >= 1),
    lease_generation bigint NOT NULL CHECK (lease_generation >= 1),
    lease_owner_id text NOT NULL,
    lease_expires_at timestamp with time zone NOT NULL,
    lease_payload jsonb NOT NULL,
    lease_fingerprint text NOT NULL,
    PRIMARY KEY (projection_kind, target_key)
);

CREATE TABLE public.graph_relation_facts (
    graph_id text NOT NULL,
    relation_id text NOT NULL,
    source_document_id text NOT NULL,
    predicate_iri text NOT NULL,
    target_document_id text NOT NULL,
    relation_payload jsonb NOT NULL,
    relation_fingerprint text NOT NULL,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    PRIMARY KEY (graph_id, relation_id),
    UNIQUE (graph_id, source_document_id, predicate_iri, target_document_id)
);
CREATE INDEX idx_graph_relation_facts_source
    ON public.graph_relation_facts (graph_id, source_document_id, predicate_iri, relation_id);

CREATE TABLE public.canonical_mutations_v2 (
    mutation_id text PRIMARY KEY,
    mutation_kind text NOT NULL,
    graph_id text NOT NULL,
    sequence_key text NOT NULL,
    mutation_sequence_number bigint NOT NULL CHECK (mutation_sequence_number >= 1),
    mutation_payload jsonb NOT NULL,
    mutation_semantic_fingerprint text NOT NULL,
    mutation_fact_fingerprint text NOT NULL UNIQUE,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    UNIQUE (sequence_key, mutation_sequence_number)
);

CREATE TABLE public.canonical_mutation_sequence_heads (
    sequence_key text PRIMARY KEY,
    head_payload jsonb NOT NULL,
    head_fingerprint text NOT NULL,
    updated_at timestamp with time zone NOT NULL DEFAULT now()
);

CREATE TABLE public.canonical_mutation_surface_deliveries (
    mutation_id text NOT NULL REFERENCES public.canonical_mutations_v2(mutation_id) ON DELETE CASCADE,
    surface text NOT NULL,
    sequence_key text NOT NULL,
    surface_sequence_number bigint NOT NULL CHECK (surface_sequence_number >= 1),
    delivery_identity jsonb NOT NULL,
    delivery_identity_fingerprint text NOT NULL,
    delivery_policy jsonb NOT NULL,
    status text NOT NULL CHECK (
        status IN ('pending', 'leased', 'retry_wait', 'applied', 'decommissioned', 'dead_letter')
    ),
    state_revision bigint NOT NULL CHECK (state_revision >= 0),
    repair_generation bigint NOT NULL CHECK (repair_generation >= 0),
    attempt_count integer NOT NULL CHECK (attempt_count >= 0),
    lease_generation bigint NOT NULL CHECK (lease_generation >= 0),
    lease_owner_id text,
    lease_expires_at timestamp with time zone,
    next_attempt_at timestamp with time zone,
    terminal_receipt jsonb,
    last_failure jsonb,
    state_fingerprint text NOT NULL,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    updated_at timestamp with time zone NOT NULL DEFAULT now(),
    PRIMARY KEY (mutation_id, surface),
    UNIQUE (surface, sequence_key, surface_sequence_number)
);
CREATE INDEX idx_canonical_mutation_surface_claim
    ON public.canonical_mutation_surface_deliveries (
        surface, status, next_attempt_at, created_at, mutation_id
    );

CREATE TABLE public.canonical_mutation_surface_sequence_heads (
    surface text NOT NULL,
    sequence_key text NOT NULL,
    head_payload jsonb NOT NULL,
    head_fingerprint text NOT NULL,
    updated_at timestamp with time zone NOT NULL DEFAULT now(),
    PRIMARY KEY (surface, sequence_key)
);

CREATE TABLE public.canonical_mutation_surface_target_heads (
    surface text NOT NULL,
    sequence_key text NOT NULL,
    head_payload jsonb NOT NULL,
    head_fingerprint text NOT NULL,
    updated_at timestamp with time zone NOT NULL DEFAULT now(),
    PRIMARY KEY (surface, sequence_key)
);

CREATE TABLE public.canonical_mutation_v2_migration_binding_plan_pages (
    page_fingerprint text PRIMARY KEY,
    maintenance_operation_id text NOT NULL,
    page_index integer NOT NULL CHECK (page_index >= 0),
    page_payload jsonb NOT NULL,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    UNIQUE (maintenance_operation_id, page_index)
);

CREATE TABLE public.canonical_mutation_v2_migration_binding_plans (
    plan_id text PRIMARY KEY,
    maintenance_operation_id text NOT NULL UNIQUE,
    plan_payload jsonb NOT NULL,
    plan_fingerprint text NOT NULL UNIQUE,
    created_at timestamp with time zone NOT NULL DEFAULT now()
);

CREATE TABLE public.canonical_mutation_v2_migration_binding_receipts (
    receipt_fingerprint text PRIMARY KEY,
    plan_id text NOT NULL REFERENCES public.canonical_mutation_v2_migration_binding_plans(plan_id),
    receipt_payload jsonb NOT NULL,
    created_at timestamp with time zone NOT NULL DEFAULT now()
);

CREATE TABLE public.durable_projection_repair_actions (
    repair_action_id text PRIMARY KEY,
    owner_kind text NOT NULL,
    owner_id text NOT NULL,
    repair_generation bigint NOT NULL CHECK (repair_generation >= 1),
    action_payload jsonb NOT NULL,
    action_fingerprint text NOT NULL UNIQUE,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    UNIQUE (owner_kind, owner_id, repair_generation)
);

CREATE OR REPLACE FUNCTION public.pulsara_runtime_write_lock_key(
    lock_domain text,
    epoch_payload jsonb
) RETURNS bigint
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT (
        ('x' || substr(
            encode(
                public.hmac(
                    convert_to(lock_domain || ':' || epoch_payload::text, 'UTF8'),
                    (SELECT guard_secret FROM public.runtime_write_guard_secrets WHERE singleton),
                    'sha256'
                ),
                'hex'
            ),
            1,
            16
        ))::bit(64)::bigint
    )
$$;

CREATE OR REPLACE FUNCTION public.pulsara_acquire_normal_runtime_write_guard(
    expected_epoch_fingerprint text,
    expected_registry_prefix text
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    current_epoch public.runtime_write_admission_epochs%ROWTYPE;
    barrier_key bigint;
    token_key bigint;
BEGIN
    SELECT * INTO STRICT current_epoch
    FROM public.runtime_write_admission_epochs
    WHERE singleton;
    IF session_user <> current_epoch.authorized_runtime_role THEN
        RAISE EXCEPTION 'runtime write role mismatch';
    END IF;
    IF current_epoch.mode <> 'normal'
       OR current_epoch.epoch_fingerprint <> expected_epoch_fingerprint
       OR current_epoch.active_migration_registry_prefix_fingerprint <> expected_registry_prefix THEN
        RAISE EXCEPTION 'runtime write epoch mismatch';
    END IF;
    barrier_key := public.pulsara_runtime_write_lock_key(
        'runtime-write-barrier',
        jsonb_build_object('database_oid', (SELECT oid FROM pg_database WHERE datname = current_database()))
    );
    PERFORM pg_advisory_xact_lock_shared(barrier_key);
    token_key := public.pulsara_runtime_write_lock_key(
        'runtime-write-token',
        current_epoch.epoch_payload
    );
    PERFORM pg_advisory_xact_lock_shared(token_key);
    RETURN current_epoch.epoch_payload;
END
$$;

CREATE OR REPLACE FUNCTION public.pulsara_read_runtime_write_admission_epoch()
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
STABLE
SET search_path = pg_catalog, public
AS $$
DECLARE
    current_epoch public.runtime_write_admission_epochs%ROWTYPE;
BEGIN
    SELECT * INTO STRICT current_epoch
    FROM public.runtime_write_admission_epochs
    WHERE singleton;
    IF session_user <> current_epoch.authorized_runtime_role THEN
        RAISE EXCEPTION 'runtime write role mismatch';
    END IF;
    RETURN current_epoch.epoch_payload;
END
$$;

CREATE OR REPLACE FUNCTION public.pulsara_acquire_maintenance_runtime_write_guard(
    expected_maintenance_operation_id text,
    expected_epoch_fingerprint text
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    current_epoch public.runtime_write_admission_epochs%ROWTYPE;
    barrier_key bigint;
    token_key bigint;
BEGIN
    SELECT * INTO STRICT current_epoch
    FROM public.runtime_write_admission_epochs
    WHERE singleton;
    IF current_epoch.mode <> 'maintenance'
       OR current_epoch.maintenance_operation_id <> expected_maintenance_operation_id
       OR current_epoch.epoch_fingerprint <> expected_epoch_fingerprint THEN
        RAISE EXCEPTION 'maintenance write epoch mismatch';
    END IF;
    barrier_key := public.pulsara_runtime_write_lock_key(
        'runtime-write-barrier',
        jsonb_build_object('database_oid', (SELECT oid FROM pg_database WHERE datname = current_database()))
    );
    PERFORM pg_advisory_xact_lock_shared(barrier_key);
    token_key := public.pulsara_runtime_write_lock_key(
        'runtime-write-token',
        current_epoch.epoch_payload
    );
    PERFORM pg_advisory_xact_lock_shared(token_key);
    RETURN current_epoch.epoch_payload;
END
$$;

CREATE OR REPLACE FUNCTION public.pulsara_enter_runtime_write_maintenance(
    expected_normal_epoch_fingerprint text,
    expected_registry_prefix text,
    expected_protected_relation_registry_fingerprint text,
    requested_maintenance_operation_id text,
    requested_target_migration_version integer,
    resulting_epoch_payload jsonb,
    resulting_epoch_fingerprint text
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    current_epoch public.runtime_write_admission_epochs%ROWTYPE;
    barrier_key bigint;
BEGIN
    IF requested_maintenance_operation_id IS NULL
       OR requested_maintenance_operation_id = ''
       OR requested_target_migration_version < 1 THEN
        RAISE EXCEPTION 'maintenance transition identity is invalid';
    END IF;
    barrier_key := public.pulsara_runtime_write_lock_key(
        'runtime-write-barrier',
        jsonb_build_object('database_oid', (SELECT oid FROM pg_database WHERE datname = current_database()))
    );
    PERFORM pg_advisory_xact_lock(barrier_key);
    SELECT * INTO STRICT current_epoch
    FROM public.runtime_write_admission_epochs
    WHERE singleton
    FOR UPDATE;
    IF current_epoch.mode <> 'normal'
       OR current_epoch.epoch_fingerprint <> expected_normal_epoch_fingerprint
       OR current_epoch.active_migration_registry_prefix_fingerprint <> expected_registry_prefix
       OR current_epoch.protected_relation_registry_fingerprint <> expected_protected_relation_registry_fingerprint THEN
        RAISE EXCEPTION 'normal epoch transition compare-and-set failed';
    END IF;
    IF (resulting_epoch_payload ->> 'epoch_number')::bigint <> current_epoch.epoch_number + 1
       OR resulting_epoch_payload ->> 'mode' <> 'maintenance'
       OR resulting_epoch_payload ->> 'maintenance_operation_id' <> requested_maintenance_operation_id
       OR (resulting_epoch_payload ->> 'target_migration_version')::integer <> requested_target_migration_version
       OR resulting_epoch_payload ->> 'epoch_fingerprint' <> resulting_epoch_fingerprint THEN
        RAISE EXCEPTION 'maintenance epoch payload mismatch';
    END IF;
    UPDATE public.runtime_write_admission_epochs
    SET epoch_number = current_epoch.epoch_number + 1,
        mode = 'maintenance',
        maintenance_operation_id = requested_maintenance_operation_id,
        target_migration_version = requested_target_migration_version,
        state_revision = current_epoch.state_revision + 1,
        epoch_payload = resulting_epoch_payload,
        epoch_fingerprint = resulting_epoch_fingerprint,
        updated_at = now()
    WHERE singleton;
    RETURN resulting_epoch_payload;
END
$$;

CREATE OR REPLACE FUNCTION public.pulsara_install_runtime_write_normal_epoch(
    expected_maintenance_operation_id text,
    expected_maintenance_epoch_fingerprint text,
    resulting_registry_prefix text,
    resulting_protected_relation_registry_fingerprint text,
    resulting_epoch_payload jsonb,
    resulting_epoch_fingerprint text
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    current_epoch public.runtime_write_admission_epochs%ROWTYPE;
    barrier_key bigint;
BEGIN
    barrier_key := public.pulsara_runtime_write_lock_key(
        'runtime-write-barrier',
        jsonb_build_object('database_oid', (SELECT oid FROM pg_database WHERE datname = current_database()))
    );
    PERFORM pg_advisory_xact_lock(barrier_key);
    SELECT * INTO STRICT current_epoch
    FROM public.runtime_write_admission_epochs
    WHERE singleton
    FOR UPDATE;
    IF current_epoch.mode <> 'maintenance'
       OR current_epoch.maintenance_operation_id <> expected_maintenance_operation_id
       OR current_epoch.epoch_fingerprint <> expected_maintenance_epoch_fingerprint THEN
        RAISE EXCEPTION 'maintenance epoch finalization compare-and-set failed';
    END IF;
    IF (resulting_epoch_payload ->> 'epoch_number')::bigint <> current_epoch.epoch_number + 1
       OR resulting_epoch_payload ->> 'mode' <> 'normal'
       OR resulting_epoch_payload ->> 'maintenance_operation_id' IS NOT NULL
       OR resulting_epoch_payload ->> 'target_migration_version' IS NOT NULL
       OR resulting_epoch_payload ->> 'active_migration_registry_prefix_fingerprint' <> resulting_registry_prefix
       OR resulting_epoch_payload ->> 'protected_relation_registry_fingerprint' <> resulting_protected_relation_registry_fingerprint
       OR resulting_epoch_payload ->> 'epoch_fingerprint' <> resulting_epoch_fingerprint THEN
        RAISE EXCEPTION 'normal epoch payload mismatch';
    END IF;
    UPDATE public.runtime_write_admission_epochs
    SET epoch_number = current_epoch.epoch_number + 1,
        mode = 'normal',
        active_migration_registry_prefix_fingerprint = resulting_registry_prefix,
        protected_relation_registry_fingerprint = resulting_protected_relation_registry_fingerprint,
        maintenance_operation_id = NULL,
        target_migration_version = NULL,
        state_revision = current_epoch.state_revision + 1,
        epoch_payload = resulting_epoch_payload,
        epoch_fingerprint = resulting_epoch_fingerprint,
        updated_at = now()
    WHERE singleton;
    RETURN resulting_epoch_payload;
END
$$;

CREATE OR REPLACE FUNCTION public.pulsara_abort_runtime_write_maintenance(
    expected_maintenance_operation_id text,
    expected_maintenance_epoch_fingerprint text,
    resulting_epoch_payload jsonb,
    resulting_epoch_fingerprint text
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    current_epoch public.runtime_write_admission_epochs%ROWTYPE;
    target_already_applied boolean;
BEGIN
    SELECT * INTO STRICT current_epoch
    FROM public.runtime_write_admission_epochs
    WHERE singleton;
    IF current_epoch.mode <> 'maintenance'
       OR current_epoch.maintenance_operation_id <> expected_maintenance_operation_id
       OR current_epoch.epoch_fingerprint <> expected_maintenance_epoch_fingerprint THEN
        RAISE EXCEPTION 'maintenance abort compare-and-set failed';
    END IF;
    SELECT EXISTS (
        SELECT 1
        FROM public.pulsara_schema_migrations
        WHERE version = current_epoch.target_migration_version
    ) INTO target_already_applied;
    IF target_already_applied THEN
        RAISE EXCEPTION 'maintenance target migration is already applied';
    END IF;
    RETURN public.pulsara_install_runtime_write_normal_epoch(
        expected_maintenance_operation_id,
        expected_maintenance_epoch_fingerprint,
        current_epoch.active_migration_registry_prefix_fingerprint,
        current_epoch.protected_relation_registry_fingerprint,
        resulting_epoch_payload,
        resulting_epoch_fingerprint
    );
END
$$;

CREATE OR REPLACE FUNCTION public.pulsara_assert_runtime_write_guard()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    current_epoch public.runtime_write_admission_epochs%ROWTYPE;
    relation_rule jsonb;
    barrier_key bigint;
    token_key bigint;
    operation_name text;
BEGIN
    SELECT * INTO STRICT current_epoch
    FROM public.runtime_write_admission_epochs
    WHERE singleton;
    SELECT relation_payload INTO STRICT relation_rule
    FROM public.runtime_write_protected_relations
    WHERE schema_name = TG_TABLE_SCHEMA AND relation_name = TG_TABLE_NAME;
    operation_name := lower(TG_OP);
    IF current_epoch.mode = 'normal' THEN
        IF session_user <> current_epoch.authorized_runtime_role THEN
            RAISE EXCEPTION 'runtime write role mismatch';
        END IF;
        IF NOT (relation_rule -> 'allowed_normal_operations' ? operation_name) THEN
            RAISE EXCEPTION 'runtime write operation is not allowed';
        END IF;
    ELSE
        IF NOT (relation_rule -> 'allowed_maintenance_operations' ? operation_name) THEN
            RAISE EXCEPTION 'maintenance write operation is not allowed';
        END IF;
    END IF;
    barrier_key := public.pulsara_runtime_write_lock_key(
        'runtime-write-barrier',
        jsonb_build_object('database_oid', (SELECT oid FROM pg_database WHERE datname = current_database()))
    );
    token_key := public.pulsara_runtime_write_lock_key(
        'runtime-write-token',
        current_epoch.epoch_payload
    );
    IF NOT EXISTS (
        SELECT 1
        FROM pg_locks
        WHERE pid = pg_backend_pid()
          AND locktype = 'advisory'
          AND granted
          AND classid = (((barrier_key >> 32) & 4294967295)::bigint)::oid
          AND objid = (barrier_key & 4294967295)::oid
          AND objsubid = 1
    ) OR NOT EXISTS (
        SELECT 1
        FROM pg_locks
        WHERE pid = pg_backend_pid()
          AND locktype = 'advisory'
          AND granted
          AND classid = (((token_key >> 32) & 4294967295)::bigint)::oid
          AND objid = (token_key & 4294967295)::oid
          AND objsubid = 1
    ) THEN
        RAISE EXCEPTION 'runtime write admission guard is absent';
    END IF;
    RETURN COALESCE(NEW, OLD);
END
$$;

REVOKE ALL ON FUNCTION public.pulsara_runtime_write_lock_key(text, jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.pulsara_acquire_normal_runtime_write_guard(text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.pulsara_read_runtime_write_admission_epoch() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.pulsara_acquire_maintenance_runtime_write_guard(text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.pulsara_enter_runtime_write_maintenance(text, text, text, text, integer, jsonb, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.pulsara_install_runtime_write_normal_epoch(text, text, text, text, jsonb, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.pulsara_abort_runtime_write_maintenance(text, text, jsonb, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.pulsara_assert_runtime_write_guard() FROM PUBLIC;
