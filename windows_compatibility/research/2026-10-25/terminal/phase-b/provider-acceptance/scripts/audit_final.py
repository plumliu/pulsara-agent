"""Read-only exact database/path audit after all original owners have closed."""

import json
from pathlib import Path

import psycopg
from psycopg.conninfo import make_conninfo

base = Path(
    "/mnt/c/python_workspace/pulsara-agent/output/phase-b/terminal/provider-acceptance"
)
databases = set()
paths = set()
for result in base.glob("*/result.json"):
    report = json.loads(result.read_text())
    assert not report.get("undropped_databases"), result
    if report.get("created_database"):
        databases.add(report["created_database"])
    databases.update(report.get("dropped_databases", []))
    paths.update(report[key] for key in ("scratch", "test_home") if report.get(key))
prepared = json.loads((base / "prepared.json").read_text())
paths.update(prepared[key] for key in ("directory", "scratch_root"))
assert all(name.startswith("pulsara_wsl_dogfood_") for name in databases)
with psycopg.connect(
    make_conninfo(
        host="/home/plumwsl/.pulsara/desktop/socket",
        port=5432,
        user="pulsara_admin",
        dbname="pulsara",
    )
) as connection:
    identity = connection.execute(
        "SELECT system_identifier::text FROM pg_control_system()"
    ).fetchone()[0]
    assert identity == "7700523439330484737"
    remaining = connection.execute(
        "SELECT datname FROM pg_database WHERE datname=ANY(%s)", (list(databases),)
    ).fetchall()
assert not remaining, remaining
remaining_paths = [value for value in sorted(paths) if Path(value).exists()]
assert not remaining_paths, remaining_paths
print(
    json.dumps(
        dict(
            system_identifier=identity,
            exactDatabases=sorted(databases),
            remainingDatabases=remaining,
            checkedPathCount=len(paths),
            remainingPaths=remaining_paths,
            productionClusterMutation=False,
        )
    )
)
