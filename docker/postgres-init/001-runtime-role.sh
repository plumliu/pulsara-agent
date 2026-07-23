#!/usr/bin/env bash
set -Eeuo pipefail

runtime_user="${PULSARA_RUNTIME_DB_USER:-pulsara_runtime}"
runtime_password="${PULSARA_RUNTIME_DB_PASSWORD:-pulsara_runtime}"

psql \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set=ON_ERROR_STOP=1 \
  --set=runtime_user="$runtime_user" \
  --set=runtime_password="$runtime_password" <<'SQL'
SELECT format(
    'CREATE ROLE %I LOGIN PASSWORD %L',
    :'runtime_user',
    :'runtime_password'
)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = :'runtime_user'
) \gexec

SELECT format(
    'GRANT CONNECT ON DATABASE %I TO %I',
    current_database(),
    :'runtime_user'
) \gexec
SQL
