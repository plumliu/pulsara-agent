"""Read-only installed PG qualification. No settings/provider/lifecycle access."""
from pathlib import Path
from time import monotonic
import json

import psycopg
import pulsara_agent
from pulsara_agent.storage.postgres_connection_provider import PostgresRuntimeConnectionFactory

home = Path("/home/plumwsl/.pulsara")
socket = home / "desktop/socket"
admin = f"host={socket} port=5432 dbname=pulsara user=pulsara_admin"
runtime = f"host={socket} port=5432 dbname=pulsara user=pulsara_runtime"
facts = {"home": str(home), "package": pulsara_agent.__file__, "admin_dsn": admin, "runtime_dsn": runtime}
with psycopg.connect(admin) as connection:
    connection.execute("SET TRANSACTION READ ONLY")
    system_id, data, sockets, listen, version, database, role, started = connection.execute(
        "SELECT system_identifier::text,current_setting('data_directory'),current_setting('unix_socket_directories'),"
        "current_setting('listen_addresses'),current_setting('server_version_num'),current_database(),current_user,"
        "pg_postmaster_start_time() FROM pg_control_system()"
    ).fetchone()
    facts.update(system_identifier=system_id, data=data, socket=sockets, listen_addresses=listen,
                 server_version_num=version, database=database, admin_role=role, postmaster_started=str(started))
    facts["roles"] = connection.execute(
        "SELECT rolname,rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls,rolcanlogin "
        "FROM pg_roles WHERE rolname IN ('pulsara_admin','pulsara_runtime') ORDER BY rolname"
    ).fetchall()
    facts["extensions"] = connection.execute("SELECT extname,extversion FROM pg_extension ORDER BY extname").fetchall()
    facts["data_inode"] = Path(data).stat().st_ino
with psycopg.connect(runtime) as connection:
    connection.execute("SET TRANSACTION READ ONLY")
    facts["runtime"] = connection.execute("SELECT current_user,current_database(),inet_server_addr(),pg_postmaster_start_time()").fetchone()
    facts["session_count"] = connection.execute("SELECT count(*) FROM pulsara_v3.sessions").fetchone()[0]
bundle = PostgresRuntimeConnectionFactory(runtime).verify_deep(deadline_monotonic=monotonic() + 60)
facts["deep_verification"] = bundle.result.to_dict()
facts["pidfile"] = (Path(data) / "postmaster.pid").read_text().splitlines()[:8]
print(json.dumps(facts, ensure_ascii=False, indent=2, default=str))
