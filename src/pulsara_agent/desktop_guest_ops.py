"""One private PostgreSQL fact/command per process; BundledPostgres owns policy.

The host sends exactly one JSONL operation and EOF after our locked ready frame.
No settings, schema owner, background worker or execution replay is involved.
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
from pathlib import Path
import select
import selectors
import shutil
import signal
import stat
import subprocess
import sys

from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.desktop_runtime import acquire_kernel_lock
from pulsara_agent.desktop_app.linux_storage import require_native_ext4

# The existing finite exec boundary applies independently to stdout and stderr.
OUTPUT_LIMIT = 4 * 1024 * 1024
METHODS = {
    "prepareLayout", "readVersion", "readInstance", "socketMode",
    "resetPathsMatch", "dataDirectoryInfo", "removeDataDirectory",
    "maintenanceCheck",
    "run:initdb", "run:pg_ctl:status", "run:pg_ctl:start", "run:pg_ctl:stop",
}


class OperationError(Exception):
    def __init__(self, message: str, *, code: str | int, execution: str,
                 stdout: str = "", stderr: str = ""):
        super().__init__(message)
        self.code, self.execution = code, execution
        self.stdout, self.stderr = stdout, stderr


def _private_directory(directory: Path) -> None:
    info = directory.stat()
    if (directory.resolve() != directory or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
        raise ValueError(f"directory must be canonical and private to this user: {directory}")


def _empty_home_parent(home: Path) -> None:
    info = home.parent.stat()
    if (not home.is_absolute() or home.parent.resolve() != home.parent or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022):
        raise ValueError("empty application home must have a trusted user-owned parent")
    require_native_ext4(home.parent)


def _lock_file(descriptor: int) -> None:
    info = os.fstat(descriptor)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise ValueError("maintenance lock must be a private, single-link regular file")


def _read_private(path: Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077):
            raise ValueError(f"not a private regular PostgreSQL file: {path}")
        with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(descriptor)


class PostgresFacts:
    def __init__(self, binary_directory: Path, method: str):
        if os.getuid() == 0:
            raise ValueError("desktop PostgreSQL operations must not run as root")
        self.home = require_pulsara_home()
        self.empty_home = method == "maintenanceCheck" and not self.home.exists() and not self.home.is_symlink()
        if self.empty_home:
            _empty_home_parent(self.home)
        else:
            _private_directory(self.home)
            require_native_ext4(self.home)
        if (not binary_directory.is_absolute() or binary_directory.resolve() != binary_directory
                or not binary_directory.is_dir()):
            raise ValueError("PostgreSQL bundle directory must be canonical and absolute")
        self.binary_directory, self.method = binary_directory, method
        self.parent = self.home / "desktop"
        self.data, self.socket = self.parent / "postgres", self.parent / "socket"
        self.log = self.parent / "postgres.log"
        self.kernel_fd: int | None = None
        self.pg_fd: int | None = None
        self.mutating = False

    def acquire(self) -> None:
        if self.empty_home:
            return
        self.kernel_fd = acquire_kernel_lock()
        _lock_file(self.kernel_fd)
        if self.method in {"run:initdb", "run:pg_ctl:start", "removeDataDirectory", "maintenanceCheck"}:
            self.pg_fd = os.open(
                self.parent / "pg-lifetime.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600,
            )
            _lock_file(self.pg_fd)
            try:
                fcntl.flock(self.pg_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise OperationError("PostgreSQL operation is still active", code="PG_LIFETIME_BUSY", execution="not-submitted") from error

    def close(self) -> None:
        # Do not LOCK_UN: an inherited open-file description may still protect a child.
        for descriptor in (self.pg_fd, self.kernel_fd):
            if descriptor is not None:
                os.close(descriptor)

    def validate_data(self) -> None:
        _private_directory(self.data)
        if not _read_private(self.data / "PG_VERSION").strip().isdigit():
            raise ValueError("cannot verify dedicated PostgreSQL data directory")

    def read_instance(self, form: str) -> dict:
        lines = _read_private(self.data / "postmaster.pid").splitlines()
        directory = lines[1] if len(lines) > 1 else None
        if form == "resolved" and directory is not None:
            directory = str(Path(directory).resolve())
        return {"directory": directory, "socket": lines[4] if len(lines) > 4 else None}

    def verify_instance(self) -> None:
        if self.read_instance("raw") != {"directory": str(self.data), "socket": str(self.socket)}:
            raise ValueError("running PostgreSQL instance differs from the dedicated data/socket")

    def command(self, binary: str, args: list[str], inherited: tuple[int, ...]) -> dict:
        try:
            child = subprocess.Popen(
                [str(self.binary_directory / binary), *args], stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True,
                pass_fds=inherited, env={**os.environ, "LC_ALL": "C"},
            )
        except OSError as error:
            raise OperationError(str(error), code=errno.errorcode.get(error.errno, "PG_SPAWN_FAILED"), execution="not-submitted") from error
        self.mutating = self.mutating or binary == "initdb" or args[0] in {"start", "stop"}
        output = {"stdout": bytearray(), "stderr": bytearray()}
        overflow = False
        with selectors.DefaultSelector() as selector:
            for name, stream in (("stdout", child.stdout), ("stderr", child.stderr)):
                selector.register(stream, selectors.EVENT_READ, name)
            while selector.get_map():
                for key, _ in selector.select():
                    data = os.read(key.fileobj.fileno(), 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        continue
                    value = output[key.data]
                    remaining = OUTPUT_LIMIT - len(value)
                    value.extend(data[:remaining])
                    overflow = overflow or len(data) > remaining
        code = child.wait()
        result = {name: bytes(value).decode("utf-8", errors="replace") for name, value in output.items()}
        if overflow:
            raise OperationError("PostgreSQL command exceeded the finite output boundary", code="ERR_CHILD_PROCESS_STDIO_MAXBUFFER", execution="unknown", **result)
        if code < 0:
            raise OperationError(f"PostgreSQL command ended by {signal.Signals(-code).name}", code="PG_COMMAND_SIGNAL", execution="unknown", **result)
        return {"code": code, **result}

    def perform(self, request: dict):
        method = self.method
        if method == "maintenanceCheck":
            if self.empty_home:
                if self.home.exists() or self.home.is_symlink():
                    raise OperationError("maintenance layout changed", code="PG_STATE_UNKNOWN", execution="unknown")
                _empty_home_parent(self.home)
                return None
            if not self.data.exists() and not self.data.is_symlink():
                return None
            self.validate_data()
            status = self.command("pg_ctl", ["status", "-D", str(self.data)], (self.kernel_fd,))
            if status["code"] != 3:
                raise OperationError("PostgreSQL is active or its state cannot be verified", code="PG_STATE_UNKNOWN", execution="unknown", stdout=status["stdout"], stderr=status["stderr"])
            return None
        if method == "prepareLayout":
            self.mutating = True
            if self.socket.exists() or self.socket.is_symlink():
                _private_directory(self.socket)
            else:
                self.socket.mkdir(mode=0o700)
            return None
        if method == "readVersion":
            if self.data.exists() or self.data.is_symlink():
                _private_directory(self.data)
            return _read_private(self.data / "PG_VERSION")
        if method == "readInstance":
            self.validate_data()
            return self.read_instance(request["form"])
        if method == "socketMode":
            _private_directory(self.socket)
            return self.socket.stat().st_mode
        if method == "resetPathsMatch":
            return self.data.resolve() == self.data and self.parent.resolve() == self.parent
        if method == "dataDirectoryInfo":
            info = self.data.stat()
            return {"uid": info.st_uid, "currentUserUid": os.getuid(), "isDirectory": stat.S_ISDIR(info.st_mode)}
        if method == "removeDataDirectory":
            self.validate_data()
            status = self.command("pg_ctl", ["status", "-D", str(self.data)], (self.kernel_fd,))
            if status["code"] != 3:
                raise OperationError("fresh stopped PostgreSQL state is required for deletion", code="PG_NOT_STOPPED", execution="not-submitted", stdout=status["stdout"], stderr=status["stderr"])
            self.mutating = True
            shutil.rmtree(self.data)
            return None
        binary = "initdb" if method == "run:initdb" else "pg_ctl"
        args = request["args"]
        self.validate_arguments(binary, args)
        _private_directory(self.socket)
        if binary == "initdb":
            inherited = (self.kernel_fd, self.pg_fd)
        else:
            self.validate_data()
            if _read_private(self.data / "PG_VERSION").strip() != "17":
                raise ValueError("bundled PostgreSQL requires major version 17")
            inherited = (self.pg_fd,) if args[0] == "start" else (self.kernel_fd,)
            if args[0] in {"start", "stop"}:
                status = self.command("pg_ctl", ["status", "-D", str(self.data)], (self.kernel_fd,))
                if status["code"] == 0:
                    self.verify_instance()
                    if args[0] == "start":
                        raise OperationError("PostgreSQL was started by another caller", code="PG_ALREADY_RUNNING", execution="not-submitted")
                elif status["code"] != 3:
                    raise OperationError("cannot verify current PostgreSQL state", code="PG_STATE_UNKNOWN", execution="unknown", stdout=status["stdout"], stderr=status["stderr"])
        result = self.command(binary, args, inherited)
        if result["code"]:
            raise OperationError(result["stderr"] or result["stdout"] or "PostgreSQL command failed", code=result["code"], execution="failed", stdout=result["stdout"], stderr=result["stderr"])
        return {"stdout": result["stdout"], "stderr": result["stderr"]}

    def validate_arguments(self, binary: str, args: list[str]) -> None:
        socket = "'" + str(self.socket).replace("'", "'\\''") + "'"
        options = f"-c listen_addresses='' -k {socket} -c unix_socket_permissions=0700 -c port=5432 -c shared_preload_libraries=pg_search"
        expected = {
            "run:initdb": ["-D", str(self.data), "-U", "pulsara_admin", "--auth-local=trust", "--auth-host=reject", "--encoding=UTF8", "--locale=C"],
            "run:pg_ctl:status": ["status", "-D", str(self.data)],
            "run:pg_ctl:stop": ["stop", "-w", "-D", str(self.data), "-m", "fast"],
            "run:pg_ctl:start": ["start", "-w", "-D", str(self.data), "-l", str(self.log), "-o", options],
        }
        if binary not in {"initdb", "pg_ctl"} or args != expected[self.method]:
            raise ValueError("command does not match the dedicated PostgreSQL operation")


def _emit(value: dict) -> bool:
    data = (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8")
    if len(data) > OUTPUT_LIMIT:
        data = (json.dumps({"type": "result", "ok": False, "error": {
            "code": "PG_REPLY_TOO_LARGE", "message": "PostgreSQL result exceeded the finite output boundary",
            "execution": "unknown", "stdout": "", "stderr": "",
        }}) + "\n").encode()
    try:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        return True
    except BrokenPipeError:
        return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--postgres-bin", required=True, type=Path)
    parser.add_argument("--method", required=True, choices=sorted(METHODS))
    arguments = parser.parse_args()
    facts = None
    try:
        # Never buffer an operation before acquiring its actual admission locks.
        if select.select([sys.stdin.buffer], [], [], 0)[0]:
            if sys.stdin.buffer.read(1):
                raise ValueError("operation arrived before ready")
            return
        facts = PostgresFacts(arguments.postgres_bin, arguments.method)
        facts.acquire()
        if select.select([sys.stdin.buffer], [], [], 0)[0]:
            if sys.stdin.buffer.read(1):
                raise ValueError("operation arrived before locked ready")
            return
        if not _emit({"type": "ready", "method": arguments.method}):
            return
        raw = sys.stdin.buffer.read()
        if not raw:
            return
        if raw.count(b"\n") != 1 or not raw.endswith(b"\n"):
            raise ValueError("one complete LF-terminated operation followed by EOF is required")
        request = json.loads(raw.decode("utf-8", errors="strict"))
        required = {"operation", "args"} if arguments.method.startswith("run:") else {"operation", "form"} if arguments.method == "readInstance" else {"operation"}
        if not isinstance(request, dict) or set(request) != required or request.get("operation") != arguments.method:
            raise ValueError("invalid PostgreSQL operation frame")
        if arguments.method == "readInstance" and request["form"] not in {"raw", "resolved"}:
            raise ValueError("invalid PostgreSQL directory form")
        if arguments.method.startswith("run:") and (not isinstance(request["args"], list) or not all(isinstance(arg, str) for arg in request["args"])):
            raise ValueError("invalid PostgreSQL command arguments")
        value = facts.perform(request)
        _emit({"type": "result", "ok": True, "value": value})
    except Exception as error:
        execution = error.execution if isinstance(error, OperationError) else "unknown" if facts is not None and facts.mutating else "not-submitted"
        code = error.code if isinstance(error, OperationError) else "PG_KERNEL_BUSY" if isinstance(error, BlockingIOError) else errno.errorcode.get(error.errno, type(error).__name__) if isinstance(error, OSError) else type(error).__name__
        _emit({"type": "result", "ok": False, "error": {
            "code": code, "message": str(error), "execution": execution,
            "stdout": error.stdout if isinstance(error, OperationError) else "",
            "stderr": error.stderr if isinstance(error, OperationError) else "",
        }})
    finally:
        if facts is not None:
            facts.close()


if __name__ == "__main__":
    main()
