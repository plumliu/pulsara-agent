"""Existing migration suite on one private snapshot/PG, no saved settings."""
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

repository = Path("/mnt/c/python_workspace/pulsara-agent")
output = repository / "output/windows-g0/pg_admission_r3/phase-b-pg"
directory = Path(tempfile.mkdtemp(prefix=".pg-reg-", dir="/home/plumwsl"))
source = directory / "src"
shutil.copytree(repository / "src/pulsara_agent", source / "pulsara_agent", ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "static", "ripgrep"))
home = directory / "home"
socket = home / "desktop/socket"
socket.mkdir(mode=0o700, parents=True)
home.chmod(0o700)
socket.parent.chmod(0o700)
data = home / "desktop/postgres"
binary = Path("/home/plumwsl/workspaces/pulsara_agent/desktop/resources/postgres/bin")
admin = f"host={socket} port=5432 dbname=pulsara user=pulsara_admin"
runtime = f"host={socket} port=5432 dbname=pulsara user=pulsara_runtime"
environment = {**os.environ, "PULSARA_HOME": str(home), "PULSARA_POSTGRES_ADMIN_DSN": admin,
               "PULSARA_POSTGRES_DSN": runtime, "PYTHONPATH": str(source) + ":" + str(repository)}
environment.pop("PULSARA_BENCHMARK_POSTGRES_ADMIN_DSN", None)
facts = {"directory": str(directory), "admin_dsn": admin, "runtime_dsn": runtime}

def execute(name, command):
    result = subprocess.run(command, cwd=directory, env=environment, capture_output=True, text=True)
    facts[name] = {"exit": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
    return result

try:
    assert execute("initdb", [str(binary / "initdb"), "-D", str(data), "-U", "pulsara_admin", "--auth-local=trust", "--auth-host=reject", "--locale=C", "--encoding=UTF8"]).returncode == 0
    assert execute("start", [str(binary / "pg_ctl"), "start", "-w", "-D", str(data), "-l", str(home / "postgres.log"),
                             "-o", f"-c listen_addresses='' -k {socket} -c unix_socket_permissions=0700 -c shared_preload_libraries=pg_search"]).returncode == 0
    prepare = "from pulsara_agent.desktop_runtime import prepare_database;from pulsara_agent.settings import LocalPostgresConfig;import os;prepare_database(LocalPostgresConfig(os.environ['PULSARA_POSTGRES_DSN'],os.environ['PULSARA_POSTGRES_ADMIN_DSN']),initialize=True)"
    assert execute("prepare", [sys.executable, "-c", prepare]).returncode == 0
    execute("pytest", [sys.executable, "-m", "pytest", str(repository / "tests/test_stage5_clean_migration.py"),
                       str(repository / "tests/test_migration_resource_portability.py"), "-q", "-p", "no:cacheprovider"])
finally:
    status = execute("status_before_cleanup", [str(binary / "pg_ctl"), "status", "-D", str(data)])
    if status.returncode == 0:
        stopped = execute("stop", [str(binary / "pg_ctl"), "stop", "-w", "-D", str(data), "-m", "fast"])
        assert stopped.returncode == 0
    status = execute("final_status", [str(binary / "pg_ctl"), "status", "-D", str(data)])
    assert status.returncode == 3
    from pulsara_agent.desktop_runtime import acquire_kernel_lock
    # Bind this cleanup check to this script's private home, not the caller's.
    os.environ["PULSARA_HOME"] = str(home)
    kernel = acquire_kernel_lock()
    lifetime = os.open(home / "desktop/pg-lifetime.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    fcntl.flock(lifetime, fcntl.LOCK_EX | fcntl.LOCK_NB)
    facts["both_locks_available"] = True
    assert directory.resolve().parent == Path("/home/plumwsl") and directory.name.startswith(".pg-reg-")
    shutil.rmtree(directory)
    os.close(lifetime)
    os.close(kernel)
    facts["removed"] = not directory.exists()
    (output / "existing-migration-regression.json").write_text(json.dumps(facts, indent=2))
print(json.dumps({"pytest_exit": facts.get("pytest", {}).get("exit"), "removed": facts["removed"]}))
assert facts.get("pytest", {}).get("exit") == 0
