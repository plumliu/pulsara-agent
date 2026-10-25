"""Read-only diagnosis of the explicitly selected installed first-start PG."""
import json
from time import monotonic
import psycopg
from pulsara_agent.storage.postgres_connection_provider import PostgresRuntimeConnectionFactory

dsn = "host=/home/plumwsl/.pulsara/desktop/socket port=5432 dbname=pulsara user=pulsara_runtime"
with psycopg.connect(dsn) as connection:
    connection.execute("SET TRANSACTION READ ONLY")
    print(json.dumps({
        "runtime": connection.execute("SELECT current_user,current_database(),current_setting('search_path')").fetchone(),
        "extensions": connection.execute("SELECT extname,extversion FROM pg_extension ORDER BY extname").fetchall(),
        "schemas": connection.execute("SELECT nspname FROM pg_namespace WHERE nspname LIKE 'pulsara%'").fetchall(),
        "session_count": connection.execute("SELECT count(*) FROM pulsara_v3.sessions").fetchone()[0],
    }, default=str))
with psycopg.connect(dsn.replace("user=pulsara_runtime", "user=pulsara_admin")) as connection:
    connection.execute("SET TRANSACTION READ ONLY")
    print(json.dumps({"identity": connection.execute(
        "SELECT system_identifier::text,current_setting('data_directory'),current_setting('unix_socket_directories'),"
        "current_setting('listen_addresses'),current_user,current_database(),pg_postmaster_start_time() "
        "FROM pg_control_system()"
    ).fetchone()}, default=str))
try:
    PostgresRuntimeConnectionFactory(dsn).verify(deadline_monotonic=monotonic() + 30)
except Exception as error:
    print(json.dumps({"type": type(error).__name__, "message": str(error),
                      "code": str(getattr(error, "code", None)), "detail": str(getattr(error, "detail", None))}))
else:
    print("VERIFY_OK")

# Inspect the existing observation values without changing SQL or the verifier.
from pulsara_agent.storage.migrations import verifier, registry
from pulsara_agent.storage.migrations.manifest import build_postgres_schema_manifest
original = verifier.postgres_schema_fingerprint
observations = []
def observe(domain, value):
    observations.append(value)
    return original(domain, value)
verifier.postgres_schema_fingerprint = observe
with PostgresRuntimeConnectionFactory(dsn).connect(deadline_monotonic=monotonic() + 30, autocommit=False) as connection:
    connection.execute("SET TRANSACTION READ ONLY")
    actual = verifier._observed_catalog_fingerprint(connection)
verifier.postgres_schema_fingerprint = original
def normalize(value):
    if isinstance(value, str):
        return value.replace("\r\n", "\n")
    if isinstance(value, (tuple, list)):
        return tuple(normalize(item) for item in value)
    if isinstance(value, dict):
        return {key: normalize(item) for key, item in value.items()}
    return value
payload = observations[0]
print(json.dumps({
    "expected": build_postgres_schema_manifest().observed_catalog_fingerprint,
    "actual": actual,
    "crlf_normalized": original("pulsara:postgres-observed-catalog:v2", normalize(payload)),
    "function_body_crlf": [(row[0], row[-1].count("\r\n")) for row in payload["functions"] if "\r\n" in row[-1]],
    "installed_baseline_crlf": registry.POSTGRES_MIGRATION_REGISTRY.definition(0).resource_bytes().count(b"\r\n"),
}))
