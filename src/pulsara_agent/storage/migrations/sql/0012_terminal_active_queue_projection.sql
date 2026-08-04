ALTER TABLE public.prompt_queue_accounts
    ADD COLUMN active_client_item_count integer NOT NULL DEFAULT 0,
    ADD COLUMN active_client_item_accumulator text NOT NULL
        DEFAULT 'sha256:b61032e1a717e15a5c8980408f8ae848d063c6db6e6742d83b9b64b9bdb56c18';

ALTER TABLE public.prompt_queue_accounts
    ADD CONSTRAINT prompt_queue_accounts_active_client_item_count_check
    CHECK (active_client_item_count BETWEEN 0 AND 64);

CREATE INDEX idx_prompt_queue_items_session_active_ordinal
    ON public.prompt_queue_items (session_id, accepted_ordinal, queue_item_id)
    WHERE delivery_state IN (
        'accepted_pending', 'steer_reserved', 'follow_up_reserved',
        'reconciliation_required'
    ) AND content_retention_state = 'active';

CREATE FUNCTION pg_temp.pulsara_canonical_jsonb(value jsonb)
RETURNS text
LANGUAGE plpgsql
IMMUTABLE
STRICT
AS $$
DECLARE
    kind text;
    rendered text;
BEGIN
    kind := jsonb_typeof(value);
    IF kind = 'object' THEN
        SELECT '{' || coalesce(
            string_agg(
                to_json(key)::text || ':' || pg_temp.pulsara_canonical_jsonb(item),
                ',' ORDER BY key
            ),
            ''
        ) || '}'
        INTO rendered
        FROM jsonb_each(value) AS entry(key, item);
        RETURN rendered;
    ELSIF kind = 'array' THEN
        SELECT '[' || coalesce(
            string_agg(
                pg_temp.pulsara_canonical_jsonb(item),
                ',' ORDER BY ordinal
            ),
            ''
        ) || ']'
        INTO rendered
        FROM jsonb_array_elements(value) WITH ORDINALITY AS entry(item, ordinal);
        RETURN rendered;
    ELSIF kind = 'string' THEN
        RETURN to_json(value #>> '{}')::text;
    END IF;
    RETURN value::text;
END;
$$;

CREATE FUNCTION pg_temp.pulsara_context_fingerprint(namespace text, value jsonb)
RETURNS text
LANGUAGE sql
IMMUTABLE
STRICT
AS $$
    SELECT 'sha256:' || encode(
        digest(
            convert_to(namespace, 'UTF8')
                || decode('00', 'hex')
                || convert_to(pg_temp.pulsara_canonical_jsonb(value), 'UTF8'),
            'sha256'
        ),
        'hex'
    )
$$;

DO $$
DECLARE
    account_row record;
    checkpoint_payload jsonb;
    new_checkpoint_fingerprint text;
    active_count integer;
    active_fingerprints jsonb;
    active_accumulator text;
    checkpoint_active_count integer;
    checkpoint_active_fingerprints jsonb;
    checkpoint_active_accumulator text;
    state_payload_v2 jsonb;
    raw_payload jsonb;
    raw_fingerprint text;
    account_payload jsonb;
    new_account_fingerprint text;
BEGIN
    FOR account_row IN
        SELECT account.*, checkpoint.projection_kind,
               checkpoint.through_sequence AS raw_through_sequence,
               checkpoint.ledger_prefix,
               checkpoint.validation_base_through_sequence,
               checkpoint.validation_base_state_payload,
               checkpoint.state_payload,
               checkpoint.payload_fingerprint AS old_raw_fingerprint
        FROM public.prompt_queue_accounts AS account
        JOIN public.runtime_projection_checkpoints AS checkpoint
          ON checkpoint.session_id = account.session_id
         AND checkpoint.projection_kind = 'prompt_queue.v1'
        ORDER BY account.session_id
        FOR UPDATE OF account, checkpoint
    LOOP
        SELECT count(*)::integer,
               coalesce(
                   jsonb_agg(view_fingerprint ORDER BY accepted_ordinal, queue_item_id),
                   '[]'::jsonb
               )
        INTO active_count, active_fingerprints
        FROM (
            SELECT item.accepted_ordinal,
                   item.queue_item_id,
                   pg_temp.pulsara_context_fingerprint(
                       'prompt-queue-item-view:v1',
                       jsonb_build_object(
                           'queue_item_id', item.queue_item_id,
                           'accepted_ordinal', item.accepted_ordinal,
                           'delivery_state', item.delivery_state,
                           'content_retention_state', item.content_retention_state,
                           'requested_delivery_mode', item.requested_delivery_mode,
                           'resolved_delivery_mode', item.resolved_delivery_mode,
                           'public_preview', CASE
                               WHEN item.state_payload->'prepared_content'->>'content_kind' = 'inline'
                               THEN left(
                                   item.state_payload->'prepared_content'->>'canonical_utf8_text',
                                   512
                               )
                               ELSE '[confirmed artifact content]'
                           END,
                           'head_event_id', item.head_transition_event_id,
                           'item_revision', item.row_revision
                       )
                   ) AS view_fingerprint
            FROM public.prompt_queue_items AS item
            WHERE item.session_id = account_row.session_id
              AND item.delivery_state IN (
                  'accepted_pending', 'steer_reserved', 'follow_up_reserved',
                  'reconciliation_required'
              )
              AND item.content_retention_state = 'active'
        ) AS active_items;

        IF active_count > 64 THEN
            RAISE EXCEPTION 'prompt queue active projection exceeds 64 for session %',
                account_row.session_id;
        END IF;
        active_accumulator := pg_temp.pulsara_context_fingerprint(
            'terminal-active-prompt-queue-items:v1', active_fingerprints
        );

        SELECT count(*)::integer,
               coalesce(
                   jsonb_agg(view_fingerprint ORDER BY accepted_ordinal, queue_item_id),
                   '[]'::jsonb
               )
        INTO checkpoint_active_count, checkpoint_active_fingerprints
        FROM (
            SELECT (item->>'accepted_ordinal')::bigint AS accepted_ordinal,
                   item->>'queue_item_id' AS queue_item_id,
                   pg_temp.pulsara_context_fingerprint(
                       'prompt-queue-item-view:v1',
                       jsonb_build_object(
                           'queue_item_id', item->>'queue_item_id',
                           'accepted_ordinal', (item->>'accepted_ordinal')::bigint,
                           'delivery_state', item->>'delivery_state',
                           'content_retention_state', item->>'content_retention_state',
                           'requested_delivery_mode', item->>'requested_delivery_mode',
                           'resolved_delivery_mode', item->>'resolved_delivery_mode',
                           'public_preview', CASE
                               WHEN item->'prepared_content'->>'content_kind' = 'inline'
                               THEN left(
                                   item->'prepared_content'->>'canonical_utf8_text',
                                   512
                               )
                               ELSE '[confirmed artifact content]'
                           END,
                           'head_event_id', item->>'head_event_id',
                           'item_revision', (item->>'item_revision')::bigint
                       )
                   ) AS view_fingerprint
            FROM jsonb_array_elements(account_row.state_payload->'items') AS item
            WHERE item->>'delivery_state' IN (
                      'accepted_pending', 'steer_reserved', 'follow_up_reserved',
                      'reconciliation_required'
                  )
              AND item->>'content_retention_state' = 'active'
        ) AS checkpoint_active_items;
        IF checkpoint_active_count > 64 THEN
            RAISE EXCEPTION 'prompt queue checkpoint active projection exceeds 64 for session %',
                account_row.session_id;
        END IF;
        checkpoint_active_accumulator := pg_temp.pulsara_context_fingerprint(
            'terminal-active-prompt-queue-items:v1', checkpoint_active_fingerprints
        );

        checkpoint_payload := jsonb_build_object(
            'schema_version', 'prompt_queue_domain_checkpoint.v2',
            'runtime_session_id', account_row.session_id,
            'reducer_id', 'pulsara.prompt_queue.reducer',
            'reducer_version', '2',
            'reducer_contract_fingerprint',
                'sha256:0117f98f96af947eb0e729b52c42aa42cc8276306ce5796c1d66a0b2022cb341',
            'event_registry_id', 'pulsara.prompt_queue.event_registry',
            'event_registry_version', '1',
            'event_registry_fingerprint', account_row.event_registry_fingerprint,
            'checkpoint_generation', account_row.checkpoint_generation,
            'through_sequence', account_row.checkpoint_through_sequence,
            'transition_count', account_row.state_payload->'checkpoint'->'transition_count',
            'transition_accumulator', account_row.state_payload->'checkpoint'->'transition_accumulator',
            'account_revision', account_row.state_payload->'checkpoint'->'account_revision',
            'next_accepted_ordinal', account_row.state_payload->'checkpoint'->'next_accepted_ordinal',
            'pending_item_head_set_accumulator',
                account_row.state_payload->'checkpoint'->'pending_item_head_set_accumulator',
            'active_client_item_count', checkpoint_active_count,
            'active_client_item_accumulator', checkpoint_active_accumulator,
            'queue_row_set_accumulator',
                account_row.state_payload->'checkpoint'->'queue_row_set_accumulator',
            'resulting_queue_head_event_id',
                account_row.state_payload->'checkpoint'->'resulting_queue_head_event_id',
            'resulting_queue_head_payload_fingerprint',
                account_row.state_payload->'checkpoint'->'resulting_queue_head_payload_fingerprint'
        );
        new_checkpoint_fingerprint := pg_temp.pulsara_context_fingerprint(
            'prompt-queue-domain-checkpoint:v2', checkpoint_payload
        );
        checkpoint_payload := checkpoint_payload || jsonb_build_object(
            'checkpoint_fingerprint', new_checkpoint_fingerprint
        );
        state_payload_v2 := jsonb_set(
            account_row.state_payload,
            '{checkpoint}',
            checkpoint_payload,
            false
        );
        raw_payload := jsonb_build_object(
            'projection_kind', account_row.projection_kind,
            'through_sequence', account_row.raw_through_sequence,
            'projection_schema_version', 'prompt_queue_domain_checkpoint.v2',
            'ledger_prefix', account_row.ledger_prefix,
            'validation_base_through_sequence',
                account_row.validation_base_through_sequence,
            'validation_base_state_payload',
                account_row.validation_base_state_payload,
            'state_payload', state_payload_v2
        );
        raw_fingerprint := pg_temp.pulsara_context_fingerprint(
            'prompt-queue-runtime-checkpoint-row:v2', raw_payload
        );

        account_payload := jsonb_build_object(
            'schema_version', 'prompt_queue_account_projection.v2',
            'runtime_session_id', account_row.session_id,
            'next_accepted_ordinal', account_row.next_accepted_ordinal,
            'queue_chain_head_event_id', account_row.queue_chain_head_event_id,
            'queue_chain_head_sequence', account_row.queue_chain_head_sequence,
            'queue_chain_head_payload_fingerprint',
                account_row.queue_chain_head_payload_fingerprint,
            'account_revision', account_row.account_revision,
            'checkpoint_generation', account_row.checkpoint_generation,
            'checkpoint_through_sequence', account_row.checkpoint_through_sequence,
            'checkpoint_fingerprint', new_checkpoint_fingerprint,
            'transition_count', account_row.transition_count,
            'transition_accumulator', account_row.transition_accumulator,
            'bounded_tail_first_sequence', account_row.bounded_tail_first_sequence,
            'bounded_tail_count', account_row.bounded_tail_count,
            'bounded_tail_payload_bytes', account_row.bounded_tail_payload_bytes,
            'bounded_tail_accumulator', account_row.bounded_tail_accumulator,
            'pending_item_count', account_row.pending_item_count,
            'reserved_item_count', account_row.reserved_item_count,
            'artifact_bytes', account_row.artifact_bytes,
            'pending_item_head_set_accumulator',
                account_row.pending_item_head_set_accumulator,
            'active_client_item_count', active_count,
            'active_client_item_accumulator', active_accumulator,
            'row_set_accumulator', account_row.row_set_accumulator,
            'reducer_contract_fingerprint',
                'sha256:0117f98f96af947eb0e729b52c42aa42cc8276306ce5796c1d66a0b2022cb341',
            'event_registry_fingerprint', account_row.event_registry_fingerprint
        );
        new_account_fingerprint := pg_temp.pulsara_context_fingerprint(
            'prompt-queue-account-row:v2', account_payload
        );

        UPDATE public.runtime_projection_checkpoints
        SET projection_schema_version = 'prompt_queue_domain_checkpoint.v2',
            state_payload = state_payload_v2,
            payload_fingerprint = raw_fingerprint,
            updated_at = now()
        WHERE session_id = account_row.session_id
          AND projection_kind = account_row.projection_kind
          AND payload_fingerprint = account_row.old_raw_fingerprint;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'prompt queue checkpoint changed during migration for session %',
                account_row.session_id;
        END IF;

        UPDATE public.prompt_queue_items
        SET reducer_contract_fingerprint =
                'sha256:0117f98f96af947eb0e729b52c42aa42cc8276306ce5796c1d66a0b2022cb341',
            updated_at = now()
        WHERE session_id = account_row.session_id;

        UPDATE public.prompt_queue_accounts
        SET checkpoint_fingerprint = new_checkpoint_fingerprint,
            active_client_item_count = active_count,
            active_client_item_accumulator = active_accumulator,
            reducer_contract_fingerprint =
                'sha256:0117f98f96af947eb0e729b52c42aa42cc8276306ce5796c1d66a0b2022cb341',
            account_fingerprint = new_account_fingerprint,
            updated_at = now()
        WHERE session_id = account_row.session_id;
    END LOOP;
END;
$$;

ALTER TABLE public.prompt_queue_accounts
    ALTER COLUMN active_client_item_count DROP DEFAULT,
    ALTER COLUMN active_client_item_accumulator DROP DEFAULT;
