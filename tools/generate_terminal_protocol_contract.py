#!/usr/bin/env python3
"""Generate or verify the sole Protocol v3 cross-language contract."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "src/pulsara_agent/terminal_protocol/schema/terminal_kernel_v3.proto"
PY_PROTO = (
    ROOT
    / "src/pulsara_agent/terminal_protocol/generated_v3/terminal_kernel_v3_pb2.py"
)
GO_PROTO = ROOT / "clients/terminal/internal/protocolv3/terminal_kernel_v3.pb.go"
GO_BUILD = ROOT / "clients/terminal/internal/buildinfo/buildinfo.go"
GATEWAY = ROOT / "src/pulsara_agent/terminal_protocol/v3_gateway.py"
GO_CLIENT = ROOT / "clients/terminal/internal/kernelclient/client.go"
FIXTURE = ROOT / "tests/fixtures/stage2_protocol_v3_cross_language.json"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not args.check:
        subprocess.run(
            [str(ROOT / "clients/terminal/scripts/generate_protocol.sh")],
            cwd=ROOT,
            check=True,
        )
    _verify()


def _verify() -> None:
    identity = "sha256:" + sha256(SCHEMA.read_bytes()).hexdigest()
    for path in (GO_BUILD, GATEWAY, GO_CLIENT):
        if re.search(re.escape(identity), path.read_text(encoding="utf-8")) is None:
            raise SystemExit(f"Protocol v3 schema identity is stale in {path}")
    _verify_generated()
    _verify_fixture(identity)


def _verify_generated() -> None:
    with tempfile.TemporaryDirectory(prefix="pulsara-terminal-v3-") as raw:
        temporary = Path(raw)
        environment = os.environ.copy()
        tools = ROOT / "clients/terminal/spikes/s0/.tools"
        environment["PATH"] = str(tools) + os.pathsep + environment.get("PATH", "")
        subprocess.run(
            [
                "protoc",
                "-I",
                str(SCHEMA.parent),
                f"--python_out={temporary}",
                str(SCHEMA),
            ],
            check=True,
            env=environment,
        )
        python_generated = temporary / "terminal_kernel_v3_pb2.py"
        if python_generated.read_bytes() != PY_PROTO.read_bytes():
            raise SystemExit("generated Protocol v3 Python binding is stale")
        subprocess.run(
            [
                "protoc",
                "-I",
                str(SCHEMA.parent),
                f"--go_out={temporary}",
                "--go_opt=module=github.com/plumliu/pulsara-agent/clients/terminal",
                str(SCHEMA),
            ],
            check=True,
            env=environment,
        )
        go_generated = temporary / "internal/protocolv3/terminal_kernel_v3.pb.go"
        if go_generated.read_bytes() != GO_PROTO.read_bytes():
            raise SystemExit("generated Protocol v3 Go binding is stale")


def _verify_fixture(identity: str) -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    if fixture.get("schema_fingerprint") != identity:
        raise SystemExit("Protocol v3 cross-language fixture schema is stale")
    sys.path.insert(0, str(ROOT / "src"))
    from pulsara_agent.terminal_protocol.generated_v3 import (
        terminal_kernel_v3_pb2 as wire,
    )

    snapshot = wire.CanonicalSessionSnapshot()
    snapshot.ParseFromString(bytes.fromhex(fixture["snapshot_protobuf_hex"]))
    observed = snapshot.snapshot_fingerprint
    snapshot.snapshot_fingerprint = ""
    expected = "sha256:" + sha256(
        b"terminal-canonical-snapshot:v3\0"
        + snapshot.SerializeToString(deterministic=True)
    ).hexdigest()
    if observed != expected or fixture.get("snapshot_fingerprint") != expected:
        raise SystemExit("Protocol v3 snapshot golden drifted")


if __name__ == "__main__":
    main()
