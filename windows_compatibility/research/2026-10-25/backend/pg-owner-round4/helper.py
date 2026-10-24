"""One physical PostgresOperations call; no lifecycle owner or saved settings."""

import fcntl
import json
import os
from pathlib import Path
import select
import shutil
import signal
import stat
import subprocess
import sys

REPO = Path(sys.argv[1])
HOME = Path(sys.argv[2])
METHOD = sys.argv[3]
FAULT = sys.argv[4]
assert HOME.parent == Path("/home/plumwsl") and HOME.name.startswith(".g0p4-")
assert HOME.resolve() == HOME and HOME.stat().st_uid == os.getuid()
assert stat.S_IMODE(HOME.stat().st_mode) == 0o700
os.environ["PULSARA_HOME"] = str(HOME)
os.environ["G0_PG4_HOME"] = str(HOME)
from pulsara_agent.desktop_runtime import acquire_kernel_lock  # noqa: E402 -- preserve executed research snapshot

DATA = HOME / "desktop/postgres"
SOCKET = HOME / "desktop/socket"
BIN = REPO / "desktop/resources/postgres/bin"
kernel_fd = None
pg_fd = None
reply_lost = False


def identity(pid=None):
    pid = pid or os.getpid()
    return {
        "pid": pid,
        "start_ticks": Path(f"/proc/{pid}/stat").read_text().split()[21],
    }


def emit(value):
    global reply_lost
    try:
        print(
            json.dumps(value),
            end=""
            if FAULT == "unterminated-result"
            and METHOD == "run:pg_ctl:start"
            and value.get("type") == "result"
            else "\n",
            flush=True,
        )
    except BrokenPipeError:
        reply_lost = True


def gate(name):
    (HOME / "helper-gate").write_text(json.dumps({**identity(), "stage": name}))
    emit({"type": "gate", "stage": name, **identity()})
    signal.raise_signal(signal.SIGSTOP)


def pg_command(binary, args, inherited):
    environment = {**os.environ, "LC_ALL": "C"}
    if (
        FAULT in ("helper-death", "carrier-death", "node-death")
        and METHOD == "run:pg_ctl:start"
    ):
        hook = Path(__file__).parent / "fault.so"
        environment.update(
            LD_PRELOAD=str(hook), G0_HOOK_HOME=str(HOME), G0_HOOK_STAGE="postgres-exec"
        )
        args = list(args)
        option = args.index("-o") + 1
        args[option] = args[option].replace(
            "shared_preload_libraries=pg_search",
            f"shared_preload_libraries='pg_search,{hook}'",
        )
    child = subprocess.Popen(
        [str(BIN / binary), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
        close_fds=True,
        pass_fds=tuple(inherited),
    )
    emit({"type": "launched", "method": METHOD, "child": identity(child.pid)})
    stdout, stderr = child.communicate()
    return {"exit": child.returncode, "stdout": stdout, "stderr": stderr}


def validate_data():
    assert DATA.resolve() == DATA and DATA.parent.resolve() == DATA.parent
    metadata = DATA.stat()
    assert metadata.st_uid == os.getuid() and stat.S_ISDIR(metadata.st_mode)
    assert (DATA / "PG_VERSION").read_text().strip().isdigit()


def perform(frame):
    assert frame["operation"] == METHOD
    if METHOD == "prepareLayout":
        SOCKET.mkdir(mode=0o700, parents=True, exist_ok=True)
        SOCKET.chmod(0o700)
        return None
    if METHOD == "readVersion":
        return (DATA / "PG_VERSION").read_text()
    if METHOD == "readInstance":
        lines = (DATA / "postmaster.pid").read_text().splitlines()
        directory = (
            str(Path(lines[1]).resolve()) if frame["form"] == "resolved" else lines[1]
        )
        return {"directory": directory, "socket": lines[4] if len(lines) > 4 else None}
    if METHOD == "socketMode":
        return SOCKET.stat().st_mode
    if METHOD == "resetPathsMatch":
        return DATA.resolve() == DATA and DATA.parent.resolve() == DATA.parent
    if METHOD == "dataDirectoryInfo":
        metadata = DATA.stat()
        return {
            "uid": metadata.st_uid,
            "currentUserUid": os.getuid(),
            "isDirectory": stat.S_ISDIR(metadata.st_mode),
        }
    if METHOD == "removeDataDirectory":
        validate_data()
        status = pg_command("pg_ctl", ["status", "-D", str(DATA)], [kernel_fd])
        if status["exit"] != 3:
            raise RuntimeError(f"fresh stopped state required: {status}")
        inode = os.fstat(pg_fd).st_ino
        shutil.rmtree(DATA)
        assert (HOME / "desktop/pg-lifetime.lock").stat().st_ino == inode
        emit(
            {
                "type": "deleted",
                "kernel_held": True,
                "pg_held": True,
                "lock_inode": inode,
            }
        )
        return None
    assert METHOD in (
        "run:initdb",
        "run:pg_ctl:start",
        "run:pg_ctl:stop",
        "run:pg_ctl:status",
    )
    binary = "initdb" if METHOD == "run:initdb" else "pg_ctl"
    args = frame["args"]
    assert isinstance(args, list) and all(
        isinstance(argument, str) for argument in args
    )
    assert args[args.index("-D") + 1] == str(DATA)
    if binary == "pg_ctl":
        assert args[0] == METHOD.rsplit(":", 1)[1]
    if METHOD == "run:pg_ctl:stop":
        lines = (DATA / "postmaster.pid").read_text().splitlines()
        assert lines[1] == str(DATA) and lines[4] == str(SOCKET)
    inherited = [pg_fd] if METHOD == "run:pg_ctl:start" else [kernel_fd]
    if METHOD == "run:initdb":
        inherited.append(pg_fd)
    result = pg_command(binary, args, inherited)
    readback = pg_command("pg_ctl", ["status", "-D", str(DATA)], [kernel_fd])
    emit({"type": "settled", "method": METHOD, "result": result, "readback": readback})
    if FAULT == "reply-loss" and METHOD == "run:pg_ctl:start":
        gate("before-result")
    if result["exit"] != 0:
        return {
            "pg_error": {
                "code": result["exit"],
                "message": result["stderr"] or result["stdout"],
                "stdout": result["stdout"],
                "stderr": result["stderr"],
            }
        }
    return {"stdout": result["stdout"], "stderr": result["stderr"]}


try:
    if FAULT == "before-ready-eof" and METHOD == "run:pg_ctl:start":
        gate("before-ready")
    if select.select([sys.stdin], [], [], 0)[0]:
        assert sys.stdin.readline() == "", "operation arrived before ready"
        emit({"type": "no-operation", "stage": "before-ready"})
    else:
        kernel_fd = acquire_kernel_lock()
        if METHOD in ("run:initdb", "run:pg_ctl:start", "removeDataDirectory"):
            pg_fd = os.open(
                HOME / "desktop/pg-lifetime.lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
            )
            metadata = os.fstat(pg_fd)
            assert stat.S_ISREG(metadata.st_mode) and metadata.st_uid == os.getuid()
            assert stat.S_IMODE(metadata.st_mode) == 0o600 and metadata.st_nlink == 1
            try:
                fcntl.flock(pg_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("PG_LIFETIME_BUSY")
        emit({"type": "ready", "method": METHOD, **identity()})
        line = sys.stdin.readline()
        if not line:
            emit({"type": "no-operation", "stage": "after-ready"})
        else:
            value = perform(json.loads(line))
            if isinstance(value, dict) and "pg_error" in value:
                emit({"type": "result", "ok": False, "error": value["pg_error"]})
            else:
                emit({"type": "result", "ok": True, "value": value})
except BaseException as error:
    code = (
        "ENOENT"
        if isinstance(error, FileNotFoundError)
        else "PG_KERNEL_BUSY"
        if isinstance(error, BlockingIOError)
        else "PG_LIFETIME_BUSY"
        if str(error) == "PG_LIFETIME_BUSY"
        else type(error).__name__
    )
    emit(
        {"type": "result", "ok": False, "error": {"code": code, "message": str(error)}}
    )
finally:
    for descriptor in (pg_fd, kernel_fd):
        if descriptor is not None:
            os.close(descriptor)
    if reply_lost:
        os._exit(0)
