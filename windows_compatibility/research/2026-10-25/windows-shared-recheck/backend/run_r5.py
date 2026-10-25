"""Create one private PG, invoke the installed context harness, then settle it.

Only execute after root has confirmed alpha10 installed/ready and the upgrade
after snapshot is complete. This never starts/stops the user's database.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import traceback


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--installed-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--connection-id", required=True)
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    import psycopg
    from psycopg.conninfo import make_conninfo

    assert sys.platform == "linux" and os.getuid() == 1000
    repo, installed = args.repo.resolve(strict=True), args.installed_root.resolve(strict=True)
    assert repo == Path("/home/plumwsl/workspaces/pulsara_agent")
    assert installed == Path("/home/plumwsl/.local/share/pulsara/runtime/0.1.0-alpha.10")
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    root = Path(tempfile.mkdtemp(prefix=".wsr-provider-", dir="/home/plumwsl"))
    os.chmod(root, 0o700)
    inode = root.stat().st_ino
    data, socket = root / "data", root / "s"
    socket.mkdir(mode=0o700)
    assert subprocess.check_output(["findmnt", "-no", "FSTYPE", "-T", str(root)], text=True).strip() == "ext4"
    binaries = installed / "postgres/bin"
    python = installed / "python/bin/python3"
    helper = Path("/mnt/c/Users/plumlocal/AppData/Local/Programs/pulsara/resources/windows-interop/pulsara-interop.exe")
    assert helper.is_file()
    admin = make_conninfo(host=str(socket), port=5432, dbname="postgres", user="pulsara_admin")
    runtime = make_conninfo(host=str(socket), port=5432, dbname="postgres", user="pulsara_runtime")
    report = {"root": str(root), "data": str(data), "socket": str(socket), "formal_cluster_used": False,
              "installed_root": str(installed), "helper": str(helper), "context_process_invoked": False}
    attempted, started = False, False

    def command(name, argv):
        completed = subprocess.run(argv, cwd=root, text=True, capture_output=True)
        (out / f"{name}.log").write_text(completed.stdout + completed.stderr, encoding="utf-8")
        assert completed.returncode == 0, f"{name}: exit {completed.returncode}"

    def identity():
        with psycopg.connect(admin) as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            row = connection.execute(
                "SELECT system_identifier::text,current_setting('data_directory'),current_setting('unix_socket_directories'),"
                "current_setting('listen_addresses'),inet_server_addr(),current_setting('server_version_num')::integer FROM pg_control_system()"
            ).fetchone()
        assert row[1:] == (str(data), str(socket), "", None, 170009)
        return row

    try:
        wrapper = Path(__file__).resolve().parent / "run_context.py"
        bootstrap = [str(python), "-I", str(wrapper), "--repo", str(repo), "--installed-root", str(installed)]
        command("installed-import-check", [*bootstrap, "--check-imports"])
        command("initdb", [str(binaries / "initdb"), "-D", str(data), "-U", "pulsara_admin",
                           "--auth-local=trust", "--auth-host=reject", "--encoding=UTF8", "--locale=C"])
        attempted = True
        command("start", [str(binaries / "pg_ctl"), "start", "-w", "-D", str(data), "-l", str(out / "postgres.log"),
                          "-o", f"-c listen_addresses='' -k {socket} -c unix_socket_permissions=0700 -c shared_preload_libraries=pg_search"])
        started = True
        report["identity"] = identity()
        with psycopg.connect(admin, autocommit=True) as connection:
            connection.execute("CREATE ROLE pulsara_runtime LOGIN")
        argv = [*bootstrap, "--execute", "--saved-home", "/home/plumwsl/.pulsara",
                "--connection-id", args.connection_id, "--model", args.model,
                "--distribution", "Ubuntu", "--user", "plumwsl", "--expected-uid", "1000",
                "--interop-helper", str(helper), "--socket", str(socket), "--port", "5432",
                "--admin-role", "pulsara_admin", "--runtime-role", "pulsara_runtime", "--root-database", "postgres",
                "--expected-data", str(data), "--expected-system-id", report["identity"][0],
                "--scratch-root", str(root), "--output", str(out / "context/result.json")]
        report["argv"] = argv
        report["context_process_invoked"] = True
        completed = subprocess.run(argv, cwd=root, text=True, capture_output=True)
        (out / "context.log").write_text(completed.stdout + completed.stderr, encoding="utf-8")
        report["context_exit"] = completed.returncode
        assert completed.returncode == 0
        result = json.loads((out / "context/result.json").read_text(encoding="utf-8"))
        assert result["status"] == "completed" and result["undropped_databases"] == []
        report["passed"] = True
    except BaseException:
        report["error"] = traceback.format_exc()
        raise
    finally:
        try:
            if attempted and not started:
                report["cleanup_unresolved"] = "PG start unconfirmed; preserve private home"
                raise RuntimeError(report["cleanup_unresolved"])
            if started:
                assert identity() == tuple(report["identity"])
                with psycopg.connect(admin) as connection:
                    report["remaining_databases"] = [row[0] for row in connection.execute(
                        "SELECT datname FROM pg_database WHERE datname NOT IN ('postgres','template0','template1') ORDER BY datname")]
                command("stop", [str(binaries / "pg_ctl"), "stop", "-w", "-D", str(data), "-m", "fast"])
                status = subprocess.run([str(binaries / "pg_ctl"), "status", "-D", str(data)], capture_output=True, text=True)
                report["final_pg_status"] = status.returncode
                assert status.returncode == 3 and not report["remaining_databases"]
            info = root.lstat()
            assert root.parent == Path("/home/plumwsl") and root.name.startswith(".wsr-provider-")
            assert info.st_ino == inode and info.st_uid == os.getuid() and stat.S_ISDIR(info.st_mode)
            shutil.rmtree(root)
            report["private_root_removed"] = not root.exists()
        finally:
            (out / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
