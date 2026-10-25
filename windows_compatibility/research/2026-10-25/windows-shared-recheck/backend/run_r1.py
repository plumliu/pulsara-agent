"""Run the exact R1 selection on one newly created, private ext4 PostgreSQL.

Prepare only until root announces fresh resources. Never uses saved settings.
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
import xml.etree.ElementTree as ET


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--pg-bin", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    import psycopg
    from psycopg.conninfo import make_conninfo

    assert sys.platform == "linux" and os.getuid() == 1000
    repo, binaries = args.repo.resolve(strict=True), args.pg_bin.resolve(strict=True)
    assert repo == Path("/home/plumwsl/workspaces/pulsara_agent")
    assert binaries == repo / "desktop/resources/postgres/bin"
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    root = Path(tempfile.mkdtemp(prefix=".wsr-", dir="/home/plumwsl"))
    os.chmod(root, 0o700)
    inode = root.stat().st_ino
    data, socket = root / "data", root / "s"
    socket.mkdir(mode=0o700)
    assert subprocess.check_output(["findmnt", "-no", "FSTYPE", "-T", str(root)], text=True).strip() == "ext4"
    assert len(os.fsencode(str(socket / ".s.PGSQL.5432"))) < 104
    admin = make_conninfo(host=str(socket), port=5432, dbname="postgres", user="pulsara_admin")
    runtime = make_conninfo(host=str(socket), port=5432, dbname="postgres", user="pulsara_runtime")
    report = {"root": str(root), "data": str(data), "socket": str(socket), "formal_cluster_used": False}
    started = False
    start_attempted = False

    def command(name, argv):
        completed = subprocess.run(argv, cwd=root, text=True, capture_output=True)
        (out / f"{name}.log").write_text(completed.stdout + completed.stderr, encoding="utf-8")
        if completed.returncode:
            raise RuntimeError(f"{name} exited {completed.returncode}; see its log")

    def identity():
        with psycopg.connect(admin) as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            row = connection.execute(
                "SELECT system_identifier::text,current_setting('data_directory'),"
                "current_setting('unix_socket_directories'),current_setting('listen_addresses'),"
                "inet_server_addr(),current_setting('server_version_num')::integer FROM pg_control_system()"
            ).fetchone()
        assert row[1:] == (str(data), str(socket), "", None, 170009)
        return row

    try:
        command("initdb", [str(binaries / "initdb"), "-D", str(data), "-U", "pulsara_admin",
                           "--auth-local=trust", "--auth-host=reject", "--encoding=UTF8", "--locale=C"])
        start_attempted = True
        command("start", [str(binaries / "pg_ctl"), "start", "-w", "-D", str(data), "-l", str(out / "postgres.log"),
                          "-o", f"-c listen_addresses='' -k {socket} -c unix_socket_permissions=0700 -c shared_preload_libraries=pg_search"])
        started = True
        report["identity"] = identity()
        with psycopg.connect(admin, autocommit=True) as connection:
            connection.execute("CREATE ROLE pulsara_runtime LOGIN")
        environment = os.environ.copy()
        environment.update(CI="true", PULSARA_POSTGRES_ADMIN_DSN=admin, PULSARA_POSTGRES_DSN=runtime)
        selection = ["tests/test_root_prospective_head_reprepare.py",
                     "tests/test_round3_1_provider_input_prefix_continuity.py",
                     "tests/test_round5_long_horizon_postgres.py::test_root_child_admission_after_root_accept_reprepares_only_the_accepted_turn"]
        argv = [str(repo / ".venv/bin/python"), "-m", "pytest", "-vv", "--junitxml", str(out / "r1.xml"),
                *[str(repo / item) for item in selection]]
        report["pytest_argv"] = argv
        # The original fixture looks for cwd/.env. This private cwd has none.
        assert not root.joinpath(".env").exists()
        completed = subprocess.run(argv, cwd=root, env=environment, text=True, capture_output=True)
        (out / "pytest.log").write_text(completed.stdout + completed.stderr, encoding="utf-8")
        report["pytest_exit"] = completed.returncode
        suites = ET.parse(out / "r1.xml").getroot().iter("testsuite")
        counts = {key: 0 for key in ("tests", "failures", "errors", "skipped")}
        for suite in suites:
            for key in counts:
                counts[key] += int(suite.attrib.get(key, 0))
        report["counts"] = counts
        assert completed.returncode == 0 and counts == {"tests": 27, "failures": 0, "errors": 0, "skipped": 0}
        report["passed"] = True
    except BaseException:
        report["error"] = traceback.format_exc()
        raise
    finally:
        try:
            if start_attempted and not started:
                report["cleanup_unresolved"] = "pg_ctl start did not settle successfully; preserve private home for exact observation"
                raise RuntimeError(report["cleanup_unresolved"])
            if started:
                assert identity() == tuple(report["identity"])
                with psycopg.connect(admin) as connection:
                    report["remaining_test_databases"] = [row[0] for row in connection.execute(
                        "SELECT datname FROM pg_database WHERE datname NOT IN ('postgres','template0','template1') ORDER BY datname")]
                command("stop", [str(binaries / "pg_ctl"), "stop", "-w", "-D", str(data), "-m", "fast"])
                status = subprocess.run([str(binaries / "pg_ctl"), "status", "-D", str(data)], capture_output=True, text=True)
                report["final_pg_status"] = status.returncode
                assert status.returncode == 3
                assert not report["remaining_test_databases"], "original UUID database teardown did not complete"
            assert root.parent == Path("/home/plumwsl") and root.name.startswith(".wsr-")
            info = root.lstat()
            assert info.st_ino == inode and info.st_uid == os.getuid() and stat.S_ISDIR(info.st_mode)
            shutil.rmtree(root)
            report["private_home_removed"] = not root.exists()
        finally:
            (out / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
