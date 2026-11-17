"""Exercise incompatible baseline reset using a disposable bundled PostgreSQL cluster."""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
import psycopg
from pulsara_agent.desktop_runtime import prepare_database
from pulsara_agent.desktop_maintenance import Maintenance
from pulsara_agent.settings import LocalPostgresConfig, LocalSettingsStore
from pulsara_agent.storage.migrations.errors import (
    PostgresSchemaError,
    PostgresSchemaFailureCode,
)

resources = Path(__file__).resolve().parents[1] / "resources"
binary = resources / "postgres/bin"
home = Path(tempfile.mkdtemp(prefix="pr-", dir="/tmp")).resolve()
os.environ["PULSARA_HOME"] = str(home)
parent = home / "desktop"
parent.mkdir(mode=0o700)
data = parent / "postgres"
socket = parent / "socket"
socket.mkdir(mode=0o700)
config = LocalPostgresConfig(
    f"host={socket} port=5432 dbname=pulsara user=pulsara_runtime",
    f"host={socket} port=5432 dbname=pulsara user=pulsara_admin",
)


def run(binary_name, *args):
    return subprocess.run(
        [str(binary / binary_name), *map(str, args)],
        check=True,
        capture_output=True,
        text=True,
    )


def start():
    socket.mkdir(mode=0o700, exist_ok=True)
    run(
        "initdb",
        "-D",
        data,
        "-U",
        "pulsara_admin",
        "--auth-local=trust",
        "--auth-host=reject",
        "--encoding=UTF8",
        "--locale=C",
    )
    run(
        "pg_ctl",
        "start",
        "-w",
        "-D",
        data,
        "-l",
        parent / "postgres.log",
        "-o",
        f"-c listen_addresses='' -k {socket} -c unix_socket_permissions=0700 -c shared_preload_libraries=pg_search",
    )


try:
    start()
    prepare_database(config, initialize=True)
    with psycopg.connect(config.admin_dsn, autocommit=True) as db:
        db.execute(
            "UPDATE public.pulsara_schema_migrations SET resource_sha256 = %s",
            ("0" * 64,),
        )
    try:
        prepare_database(config, initialize=False)
        raise AssertionError("incompatible baseline accepted")
    except PostgresSchemaError as error:
        assert error.code is PostgresSchemaFailureCode.MIGRATION_UNIVERSE_RESET_REQUIRED
        print("PASS: actual incompatible baseline classified")
    (home / "local-settings.yaml").write_text("invalid old settings with fixture-key")
    with Maintenance(home, resources, binary) as maintenance:
        maintenance.clear()
    assert not data.exists() and not (home / "local-settings.yaml").exists()
    start()
    prepare_database(config, initialize=True)
    assert LocalSettingsStore(postgres=config).read().model_api_keys == ()
    print(
        "PASS: live dedicated PostgreSQL stopped, home reset, fresh current baseline initialized; no credentials retained"
    )
finally:
    if data.exists():
        subprocess.run(
            [str(binary / "pg_ctl"), "stop", "-w", "-D", str(data), "-m", "immediate"],
            capture_output=True,
        )
    shutil.rmtree(home)
