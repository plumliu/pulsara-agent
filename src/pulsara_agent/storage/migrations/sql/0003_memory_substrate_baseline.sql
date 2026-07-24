CREATE FUNCTION public.pulsara_jsonb_text_array(value jsonb) RETURNS jsonb
    LANGUAGE sql IMMUTABLE
    AS $$
    SELECT CASE jsonb_typeof(value)
        WHEN 'array' THEN value
        WHEN 'string' THEN jsonb_build_array(value #>> '{}')
        ELSE '[]'::jsonb
    END
$$;

SET default_tablespace = '';

SET default_table_access_method = heap;

CREATE TABLE public.graph_documents (
    graph_id text NOT NULL,
    id text NOT NULL,
    type text,
    payload jsonb NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.memory_governance_event_outbox (
    outbox_id text NOT NULL,
    runtime_session_id text NOT NULL,
    governance_batch_id text NOT NULL,
    decision_id text NOT NULL,
    event_ids jsonb NOT NULL,
    events_payload jsonb NOT NULL,
    payload_fingerprint text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    attempt_count integer DEFAULT 0 NOT NULL,
    claim_token text,
    claimed_until timestamp with time zone,
    last_error_code text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    applied_at timestamp with time zone
);

CREATE TABLE public.memory_nodes (
    graph_id text NOT NULL,
    id text NOT NULL,
    memory_type text NOT NULL,
    scope text NOT NULL,
    status text NOT NULL,
    statement text NOT NULL,
    summary text,
    source_authority text,
    verification_status text,
    confidence_level text,
    applies_when text,
    do_not_apply_when text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    node_revision bigint DEFAULT 1 NOT NULL,
    stale_after timestamp with time zone,
    expires_at timestamp with time zone,
    fts tsvector,
    CONSTRAINT memory_nodes_node_revision_check CHECK ((node_revision >= 1))
);

CREATE TABLE public.memory_relations (
    graph_id text NOT NULL,
    source_id text NOT NULL,
    predicate text NOT NULL,
    target_id text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.memory_search_index (
    graph_id text NOT NULL,
    memory_id text NOT NULL,
    memory_type text NOT NULL,
    scope text NOT NULL,
    status text NOT NULL,
    fts tsvector NOT NULL,
    aliases text[],
    updated_at timestamp with time zone NOT NULL
);

CREATE TABLE public.memory_vector_index (
    graph_id text NOT NULL,
    memory_id text NOT NULL,
    embedding_fingerprint text NOT NULL,
    embedded_text_hash text NOT NULL,
    builder_version text NOT NULL,
    embedding public.vector(1024) NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.memory_write_outbox (
    outbox_id text NOT NULL,
    graph_id text NOT NULL,
    governance_batch_id text,
    decision_id text,
    mutation_lane text DEFAULT 'governed_memory'::text NOT NULL,
    sequence_key text DEFAULT ''::text NOT NULL,
    target_entry_key text NOT NULL,
    dirty_memory_ids jsonb DEFAULT '[]'::jsonb NOT NULL,
    payload jsonb NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    attempt_count integer DEFAULT 0 NOT NULL,
    last_error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    applied_at timestamp with time zone,
    vector_claim_token text,
    vector_claimed_until timestamp with time zone
);

CREATE TABLE public.recall_traces (
    trace_id text NOT NULL,
    graph_id text NOT NULL,
    session_id text NOT NULL,
    run_id text NOT NULL,
    turn_id text NOT NULL,
    reply_id text NOT NULL,
    query text NOT NULL,
    trigger_kind text NOT NULL,
    candidate_ids jsonb NOT NULL,
    included_ids jsonb NOT NULL,
    filtered_ids jsonb NOT NULL,
    warnings jsonb DEFAULT '[]'::jsonb NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    latency_ms integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.recall_usages (
    trace_id text NOT NULL,
    graph_id text NOT NULL,
    memory_id text NOT NULL,
    injected boolean NOT NULL,
    selected_by_tool boolean DEFAULT false NOT NULL,
    cited_by_response boolean,
    later_confirmed boolean,
    later_contradicted boolean
);

ALTER TABLE ONLY public.graph_documents
    ADD CONSTRAINT graph_documents_pkey PRIMARY KEY (graph_id, id);

ALTER TABLE ONLY public.memory_governance_event_outbox
    ADD CONSTRAINT memory_governance_event_outbo_runtime_session_id_governance_key UNIQUE (runtime_session_id, governance_batch_id, decision_id);

ALTER TABLE ONLY public.memory_governance_event_outbox
    ADD CONSTRAINT memory_governance_event_outbox_pkey PRIMARY KEY (outbox_id);

ALTER TABLE ONLY public.memory_nodes
    ADD CONSTRAINT memory_nodes_pkey PRIMARY KEY (graph_id, id);

ALTER TABLE ONLY public.memory_relations
    ADD CONSTRAINT memory_relations_pkey PRIMARY KEY (graph_id, source_id, predicate, target_id);

ALTER TABLE ONLY public.memory_search_index
    ADD CONSTRAINT memory_search_index_pkey PRIMARY KEY (graph_id, memory_id);

ALTER TABLE ONLY public.memory_vector_index
    ADD CONSTRAINT memory_vector_index_pkey PRIMARY KEY (graph_id, memory_id, embedding_fingerprint);

ALTER TABLE ONLY public.memory_write_outbox
    ADD CONSTRAINT memory_write_outbox_pkey PRIMARY KEY (outbox_id);

ALTER TABLE ONLY public.recall_traces
    ADD CONSTRAINT recall_traces_pkey PRIMARY KEY (trace_id);

ALTER TABLE ONLY public.recall_usages
    ADD CONSTRAINT recall_usages_pkey PRIMARY KEY (trace_id, memory_id);

CREATE INDEX idx_graph_documents_type ON public.graph_documents USING btree (graph_id, type);

CREATE INDEX idx_memory_governance_event_outbox_pending ON public.memory_governance_event_outbox USING btree (runtime_session_id, status, created_at);

CREATE INDEX idx_memory_nodes_status ON public.memory_nodes USING btree (graph_id, status);

CREATE INDEX idx_memory_nodes_type_status_scope ON public.memory_nodes USING btree (graph_id, memory_type, status, scope);

CREATE INDEX idx_memory_nodes_updated_at ON public.memory_nodes USING btree (graph_id, updated_at);

CREATE INDEX idx_memory_relations_source ON public.memory_relations USING btree (graph_id, source_id, predicate);

CREATE INDEX idx_memory_relations_target ON public.memory_relations USING btree (graph_id, target_id, predicate);

CREATE UNIQUE INDEX idx_memory_write_outbox_governance_decision ON public.memory_write_outbox USING btree (governance_batch_id, decision_id) WHERE ((governance_batch_id IS NOT NULL) AND (decision_id IS NOT NULL));

CREATE INDEX idx_memory_write_outbox_sequence ON public.memory_write_outbox USING btree (sequence_key, created_at, outbox_id);

CREATE INDEX idx_memory_write_outbox_status ON public.memory_write_outbox USING btree (status, created_at);

CREATE INDEX idx_msi_fts ON public.memory_search_index USING gin (fts);

CREATE INDEX idx_msi_type_scope ON public.memory_search_index USING btree (graph_id, memory_type, scope);

CREATE INDEX idx_mvi_embedding_hnsw ON public.memory_vector_index USING hnsw (embedding public.vector_cosine_ops);

CREATE INDEX idx_mvi_graph_fingerprint ON public.memory_vector_index USING btree (graph_id, embedding_fingerprint);

CREATE INDEX idx_recall_traces_scope ON public.recall_traces USING btree (graph_id, session_id, created_at);

CREATE INDEX idx_recall_usages_mem ON public.recall_usages USING btree (graph_id, memory_id);

ALTER TABLE ONLY public.memory_search_index
    ADD CONSTRAINT memory_search_index_graph_id_memory_id_fkey FOREIGN KEY (graph_id, memory_id) REFERENCES public.memory_nodes(graph_id, id) ON DELETE CASCADE;

ALTER TABLE ONLY public.memory_vector_index
    ADD CONSTRAINT memory_vector_index_graph_id_memory_id_fkey FOREIGN KEY (graph_id, memory_id) REFERENCES public.memory_nodes(graph_id, id) ON DELETE CASCADE;

ALTER TABLE ONLY public.recall_usages
    ADD CONSTRAINT recall_usages_trace_id_fkey FOREIGN KEY (trace_id) REFERENCES public.recall_traces(trace_id) ON DELETE CASCADE;
