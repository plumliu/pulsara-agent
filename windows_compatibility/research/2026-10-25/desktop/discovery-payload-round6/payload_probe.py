"""Finite offline resource experiment using the actual existing native recipe."""

import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
from unittest.mock import patch

REPO = Path("/home/plumwsl/workspaces/pulsara_agent")
spec = importlib.util.spec_from_file_location(
    "native_recipe", REPO / "desktop/scripts/prepare_resources.py"
)
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)
action = sys.argv[1]
root = Path(sys.argv[2])
assert (
    root.parent == Path("/home/plumwsl")
    and root.name.startswith(".g0d6-")
    and root.resolve() == root
)


def emit(event, **facts):
    print(
        json.dumps(
            dict(
                event=event,
                pid=os.getpid(),
                starttime=Path("/proc/self/stat")
                .read_text()
                .rsplit(")", 1)[1]
                .split()[19],
                uid=os.getuid(),
                **facts,
            )
        ),
        flush=True,
    )


def sentinels():
    return {
        str(p.relative_to(root)): (p.read_text() if p.exists() else None)
        for p in (
            root / "home/.pulsara/local-settings.yaml",
            root / "home/.pulsara/desktop/postgres/keep",
            root / "home/workspaces/keep",
        )
    }


if action == "pack":
    root.mkdir(mode=0o700)
    for relative, text in [
        ("home/.pulsara/local-settings.yaml", "fixture settings sentinel\n"),
        ("home/.pulsara/desktop/postgres/keep", "fixture data sentinel\n"),
        ("home/workspaces/keep", "fixture workspace sentinel\n"),
    ]:
        path = root / relative
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(text)
    assert R.target_platform() == ("linux", "x86_64")
    archive = root / "provided-payload.tar"
    with tarfile.open(archive, "w") as bundle:
        bundle.add(R.RESOURCES, arcname="payload")
        bundle.add(
            REPO / "desktop/scripts/pulsara-cli.sh", arcname="payload/bin/pulsara"
        )
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    emit(
        "packaged-local-artifact",
        archive=str(archive),
        bytes=archive.stat().st_size,
        sha256=digest,
        pins=dict(
            postgres=R.PG_VERSION, vector=R.VECTOR_VERSION, pg_search=R.SEARCH_VERSION
        ),
        target=R.target_platform(),
        sentinels=sentinels(),
        mount=subprocess.check_output(
            ["findmnt", "-T", root, "-n", "-o", "FSTYPE,SOURCE,TARGET"], text=True
        ).strip(),
    )
elif action == "receive":
    expected = sys.argv[3]
    cache = root / "cache"
    cache.mkdir(mode=0o700, exist_ok=True)
    pending = cache / "artifact.part"
    destination = cache / "artifact.tar"
    with pending.open("wb") as stream:
        while chunk := sys.stdin.buffer.read(1024 * 1024):
            stream.write(chunk)
    with pending.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    assert actual == expected
    pending.rename(destination)
    emit(
        "stdin-artifact-verified",
        archive=str(destination),
        bytes=destination.stat().st_size,
        sha256=actual,
        sentinels=sentinels(),
    )
elif action == "install":
    case, expected, fault = sys.argv[3:]
    assert case.replace("-", "").isalnum()
    before = sentinels()
    assert all(value is not None for value in before.values())
    assert os.getuid() != 0
    probe = socket.socket()
    network_error = probe.connect_ex(("192.0.2.1", 9))
    probe.close()
    assert network_error == errno.ENETUNREACH, network_error
    stage = root / "stages" / case
    stage.mkdir(mode=0o700, parents=True, exist_ok=True)
    folder = stage / "payload"
    published = root / "installed" / case / "0.1.0-alpha.6"
    archive = root / "cache" / ("missing.tar" if fault == "missing" else "artifact.tar")
    network_calls = []

    def no_download(*args, **kwargs):
        network_calls.append(str(args))
        raise AssertionError("offline recipe must not fetch")

    old_open = tarfile.bltn_open
    old_copy = tarfile.copyfileobj
    injected = []

    def write_boundary(path, mode="r", *args, **kwargs):
        if (
            fault == "enospc"
            and mode == "wb"
            and str(path).startswith(str(folder) + "/")
            and not injected
        ):
            injected.append(str(path))
            emit("native-enospc-sink-injected", target=str(path), device="/dev/full")
            return old_open("/dev/full", mode, *args, **kwargs)
        return old_open(path, mode, *args, **kwargs)

    def copied_member(source, target, *args, **kwargs):
        value = old_copy(source, target, *args, **kwargs)
        if (
            fault == "interrupt"
            and not injected
            and str(target.name).startswith(str(folder) + "/")
        ):
            injected.append(str(target.name))
            emit(
                "real-member-written-before-interruption",
                target=str(target.name),
                published=published.exists(),
            )
            signal.raise_signal(signal.SIGSTOP)
        return value

    try:
        # The release artifact must already exist; this finite deployment path
        # does not reinterpret a missing local artifact as network permission.
        if not archive.is_file():
            raise FileNotFoundError(
                errno.ENOENT, "provided offline artifact missing", str(archive)
            )
        with (
            patch.object(R.urllib.request, "urlretrieve", no_download),
            patch.object(tarfile, "bltn_open", write_boundary),
            patch.object(tarfile, "copyfileobj", copied_member),
        ):
            R.source_tree(
                "https://offline.invalid/provided-artifact", archive, expected, folder
            )
        R.RESOURCES = folder
        R.validate_resources()
        python = folder / "python/bin/python3"
        code = "import importlib.metadata,json,platform,pulsara_agent; from pulsara_agent.ripgrep import private_ripgrep; print(json.dumps(dict(python=platform.python_version(),package=pulsara_agent.__file__,version=importlib.metadata.version('pulsara-agent'),rg=str(private_ripgrep()))))"
        imported = json.loads(
            subprocess.check_output(
                [python, "-I", "-c", code],
                text=True,
                env={
                    **os.environ,
                    "HOME": str(root / "home"),
                    "PULSARA_HOME": str(root / "home/.pulsara"),
                    "UV_OFFLINE": "1",
                },
            )
        )
        assert (
            Path(imported["package"]).is_relative_to(folder)
            and imported["python"] == "3.12.15"
        )
        assert Path(imported["rg"]).is_relative_to(folder)
        published.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        folder.chmod(0o700)
        folder.rename(published)
        emit(
            "resource-version-published",
            case=case,
            published=str(published),
            mode=oct(published.stat().st_mode & 0o777),
            imported=imported,
            networkNamespace=os.readlink("/proc/self/ns/net"),
            networkConnectErrno=network_error,
            recipeDownloadCalls=network_calls,
            sentinels=sentinels(),
        )
        assert sentinels() == before
    except BaseException as error:
        emit(
            "installation-rejected",
            case=case,
            type=type(error).__name__,
            message=str(error),
            errno=getattr(error, "errno", None),
            published=published.exists(),
            stageExists=folder.exists(),
            injected=injected,
            networkConnectErrno=network_error,
            recipeDownloadCalls=network_calls,
            sentinels=sentinels(),
        )
        assert not published.exists() and sentinels() == before
        sys.exit(2)
elif action == "clear-interrupted-stage":
    stage = root / "stages" / "interrupt"
    assert stage.resolve() == stage and stage.is_relative_to(root)
    shutil.rmtree(stage)
    emit(
        "explicit-self-owned-stage-cleared",
        stage=str(stage),
        exists=stage.exists(),
        sentinels=sentinels(),
    )
elif action == "cleanup":
    assert (
        root.stat().st_uid == os.getuid() and (root / "provided-payload.tar").is_file()
    )
    facts = sentinels()
    assert all(value is not None for value in facts.values())
    shutil.rmtree(root)
    emit(
        "private-experiment-cleaned",
        root=str(root),
        exists=root.exists(),
        sentinelsBeforeCleanup=facts,
    )
else:
    raise ValueError(action)
