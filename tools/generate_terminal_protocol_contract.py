#!/usr/bin/env python3
"""Generate or verify the renderer-neutral Protocol v3 Python contract."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
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
GATEWAY = ROOT / "src/pulsara_agent/terminal_protocol/v3_gateway.py"
FIXTURE = ROOT / "tests/fixtures/stage2_protocol_v3_wire.json"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not args.check:
        _generate_python(PY_PROTO.parent)
    _verify()


def _verify() -> None:
    identity = "sha256:" + sha256(SCHEMA.read_bytes()).hexdigest()
    if re.search(re.escape(identity), GATEWAY.read_text(encoding="utf-8")) is None:
        raise SystemExit(f"Protocol v3 schema identity is stale in {GATEWAY}")
    _verify_generated()
    _verify_fixture(identity)


def _generate_python(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "protoc",
            "-I",
            str(SCHEMA.parent),
            f"--python_out={output}",
            str(SCHEMA),
        ],
        cwd=ROOT,
        check=True,
    )


def _verify_generated() -> None:
    with tempfile.TemporaryDirectory(prefix="pulsara-terminal-v3-") as raw:
        temporary = Path(raw)
        _generate_python(temporary)
        python_generated = temporary / "terminal_kernel_v3_pb2.py"
        if python_generated.read_bytes() != PY_PROTO.read_bytes():
            raise SystemExit("generated Protocol v3 Python binding is stale")


def _verify_fixture(identity: str) -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    if fixture.get("schema_fingerprint") != identity:
        raise SystemExit("Protocol v3 wire fixture schema is stale")
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
