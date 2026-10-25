from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

REPO = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
PRIVATE = json.loads((REPO / "output/macos-regression-20261025/root/private-pg.json").read_text())

NO_DATABASE = [
    "tests/test_terminal_startup_cleanup.py",
    "tests/test_terminal_output_settlement.py",
    "tests/test_round2_terminal_output.py",
    "tests/test_round2_terminal_environment.py",
    "tests/test_pr03_process_termination.py",
    "tests/test_terminal_cognitive_subtraction.py",
    "tests/test_round2_terminal_monitor.py",
    "tests/test_round2_terminal_safe_point.py",
    "tests/test_round2_terminal_architecture.py",
    "tests/test_stage2_terminal_host_lifetime.py",
    "tests/test_round9_2_hook_subsystem.py",
    "tests/test_mcp_transport_lifecycle.py",
    "tests/test_mcp_credentials.py",
    "tests/test_windows_interop_failures.py",
]
DATABASE = [
    "tests/test_round2_terminal_host.py",
    "tests/test_round2_terminal_postgres.py",
]


def main():
    import psycopg
    from psycopg.conninfo import conninfo_to_dict
    import pulsara_agent

    fields = conninfo_to_dict(PRIVATE["admin_dsn"])
    assert fields["host"] == PRIVATE["socket"]
    assert fields["dbname"] == "pulsara"
    assert PRIVATE["data"].startswith("/private/tmp/pm-")
    with psycopg.connect(PRIVATE["admin_dsn"], autocommit=True) as connection:
        data, address, identifier = connection.execute(
            "SELECT current_setting('data_directory'), inet_server_addr(), system_identifier::text FROM pg_control_system()"
        ).fetchone()
    assert data == PRIVATE["data"] and address is None
    assert identifier == PRIVATE["identity"].rsplit("|", 1)[-1]

    results = {"git": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip(),
               "python": sys.executable, "module": pulsara_agent.__file__, "platform": sys.platform,
               "private_pg_identity": {"data": data, "address": address, "system_identifier": identifier}, "runs": []}
    with tempfile.TemporaryDirectory(prefix="mac-terminal-regression-") as temporary:
        home = Path(temporary).resolve() / "home"
        home.mkdir(mode=0o700)
        env = dict(os.environ)
        env.update(PULSARA_HOME=str(home), CI="true", PULSARA_POSTGRES_ADMIN_DSN=PRIVATE["admin_dsn"], PULSARA_POSTGRES_DSN=PRIVATE["runtime_dsn"])
        for name, tests in [("posix-contracts", NO_DATABASE), ("private-pg-terminal", DATABASE)]:
            command = [sys.executable, "-m", "pytest", "-q", "-ra", *tests]
            record = {"name": name, "command": command, "started": datetime.now(timezone.utc).isoformat(), "home": str(home)}
            (OUT / f"{name}-command.json").write_text(json.dumps(record, indent=2) + "\n")
            with (OUT / f"{name}.log").open("w") as log:
                completed = subprocess.run(command, cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT)
            record.update(exit_code=completed.returncode, finished=datetime.now(timezone.utc).isoformat())
            results["runs"].append(record)
            (OUT / "suites-results.json").write_text(json.dumps(results, indent=2) + "\n")
            print(json.dumps(record), flush=True)
        results["temporary_home_removed"] = True
    (OUT / "suites-results.json").write_text(json.dumps(results, indent=2) + "\n")
    return int(any(run["exit_code"] for run in results["runs"]))


if __name__ == "__main__":
    raise SystemExit(main())
