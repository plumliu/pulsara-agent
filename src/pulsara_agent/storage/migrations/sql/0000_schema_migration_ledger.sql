CREATE TABLE public.pulsara_schema_migrations (
    version bigint PRIMARY KEY CHECK (version >= 0),
    name text NOT NULL UNIQUE,
    checksum text NOT NULL CHECK (checksum ~ '^[0-9a-f]{64}$'),
    migration_contract_fingerprint text NOT NULL
        CHECK (migration_contract_fingerprint ~ '^sha256:[0-9a-f]{64}$'),
    registry_prefix_fingerprint text NOT NULL
        CHECK (registry_prefix_fingerprint ~ '^sha256:[0-9a-f]{64}$'),
    applied_at timestamp with time zone NOT NULL DEFAULT now(),
    application_version text NOT NULL
);
