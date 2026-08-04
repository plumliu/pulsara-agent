#!/usr/bin/env python3
"""Verify/generate the cross-language Terminal Protocol 2.0 contract identity."""

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
SCHEMA_ROOT = ROOT / "src/pulsara_agent/terminal_protocol/schema"
PROTO = SCHEMA_ROOT / "terminal_client.proto"
MANIFEST = SCHEMA_ROOT / "terminal_client_fingerprint_contract.v1.json"
GOLDEN = SCHEMA_ROOT / "terminal_client_fingerprint_golden.v1.json"
CODEC = ROOT / "src/pulsara_agent/terminal_protocol/codec.py"
GO_VALUES = ROOT / "clients/terminal/internal/protocolvalue/vocabulary_gen.go"
GO_BUILD = ROOT / "clients/terminal/internal/buildinfo/buildinfo.go"
GO_PROTO = ROOT / "clients/terminal/internal/protocol/terminal_client.pb.go"
PY_PROTO = ROOT / "src/pulsara_agent/terminal_protocol/generated/terminal_client_pb2.py"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check", action="store_true", help="verify generated files are current"
    )
    parser.add_argument(
        "--sync-opaque-fields",
        action="store_true",
        help="rewrite the descriptor-derived opaque fingerprint inventory",
    )
    args = parser.parse_args()
    if args.sync_opaque_fields:
        _sync_opaque_fingerprint_fields()
        _verify_contract()
        return
    if not args.check:
        subprocess.run(
            [str(ROOT / "clients/terminal/scripts/generate_protocol.sh")],
            cwd=ROOT,
            check=True,
        )
    _verify_contract()


def _verify_contract() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    proto_hex = sha256(PROTO.read_bytes()).hexdigest()
    if manifest["proto_sha256"] != proto_hex:
        raise SystemExit("terminal fingerprint manifest has a stale proto SHA-256")
    expected = "sha256:" + proto_hex
    if golden["proto_schema_fingerprint"] != expected:
        raise SystemExit("terminal fingerprint golden has a stale schema identity")
    contracts = manifest["fingerprint_contracts"]
    namespaces = [item["namespace"] for item in contracts]
    messages = [item["message"] for item in contracts]
    if len(namespaces) != len(set(namespaces)) or len(messages) != len(set(messages)):
        raise SystemExit("terminal fingerprint contracts are not one-to-one")
    _verify_fingerprint_field_inventory(manifest)
    _verify_cross_language_constants(manifest)
    _require_literal(CODEC, expected)
    _require_literal(GO_VALUES, expected)
    _require_literal(GO_BUILD, expected)
    _verify_generated_protobuf()
    for relative, expected_digest in manifest.get(
        "generated_contract_files_sha256", {}
    ).items():
        path = ROOT / relative
        observed = sha256(path.read_bytes()).hexdigest()
        if observed != expected_digest:
            raise SystemExit(f"generated terminal contract drifted: {relative}")
    sys.path.insert(0, str(ROOT / "src"))
    from pulsara_agent.terminal_protocol.generated import terminal_client_pb2 as wire
    from pulsara_agent.terminal_protocol.generated.terminal_client_fingerprint import (
        attachment_challenge_commitment,
        install_protobuf_fingerprint,
    )

    candidate = wire.HandshakeRecoveryCandidateIdentity(
        candidate_version=1,
        client_instance_id="client:golden",
        attachment_attempt_generation=1,
        host_session_id="host:golden",
        requested_runtime_session_id="runtime:golden",
        requested_attachment_role=wire.ATTACHMENT_ROLE_OBSERVER,
        minimum_protocol_major=2,
        minimum_protocol_minor=0,
        maximum_protocol_major=2,
        maximum_protocol_minor=0,
        client_build_identity="pulsara-tui:golden",
        supported_capabilities=tuple(range(1, 12)),
        required_capabilities=tuple(range(1, 12)),
        schema_contract_fingerprint=expected,
    )
    install_protobuf_fingerprint(
        "terminal-handshake-recovery-candidate:v1",
        candidate,
        own_field="candidate_fingerprint",
        clear_fields=("candidate_id",),
    )
    candidate.candidate_id = (
        "handshake:" + candidate.candidate_fingerprint.removeprefix("sha256:")
    )
    if candidate.candidate_fingerprint != golden["candidate"]["fingerprint"]:
        raise SystemExit("terminal handshake candidate golden fingerprint drifted")
    if (
        candidate.SerializeToString(deterministic=True).hex()
        != golden["candidate"]["deterministic_protobuf_hex"]
    ):
        raise SystemExit("terminal handshake candidate protobuf golden drifted")
    challenge = golden["attachment_challenge"]
    observed = attachment_challenge_commitment(
        auth_attempt_id="auth:golden",
        candidate_fingerprint=candidate.candidate_fingerprint,
        candidate_id=candidate.candidate_id,
        connection_id="connection:golden",
        negotiation_winner_fingerprint="sha256:" + "a" * 64,
        request_id="request:golden",
        challenge=bytes.fromhex(challenge["challenge_hex"]),
    )
    if observed != challenge["commitment"]:
        raise SystemExit("terminal attachment challenge golden drifted")


def _require_literal(path: Path, value: str) -> None:
    text = path.read_text(encoding="utf-8")
    if re.search(re.escape(value), text) is None:
        raise SystemExit(f"terminal schema identity is stale in {path}")


def _integer_constant(path: Path, name: str) -> int:
    text = path.read_text(encoding="utf-8")
    match = re.search(
        rf"(?m)^\s*{re.escape(name)}(?:\s+uint32)?\s*=\s*(\d+)\s*$",
        text,
    )
    if match is None:
        raise SystemExit(f"terminal fixed protocol constant is missing: {path}:{name}")
    return int(match.group(1))


def _verify_cross_language_constants(manifest: dict[str, object]) -> None:
    fixed = manifest.get("fixed_cross_language_constants")
    if not isinstance(fixed, dict):
        raise SystemExit("terminal fixed cross-language constant manifest is missing")
    expected = fixed.get("maximum_pinned_history_roots")
    if not isinstance(expected, int) or expected < 1:
        raise SystemExit("terminal pinned-root protocol constant is invalid")
    python_value = _integer_constant(CODEC, "MAXIMUM_PINNED_HISTORY_ROOTS")
    go_value = _integer_constant(GO_VALUES, "MaximumPinnedHistoryRoots")
    if python_value != expected or go_value != expected:
        raise SystemExit(
            "terminal pinned-root protocol constant drifted: "
            f"manifest={expected} python={python_value} go={go_value}"
        )


def _descriptor_fingerprint_fields() -> set[tuple[str, str]]:
    sys.path.insert(0, str(ROOT / "src"))
    from google.protobuf.descriptor import Descriptor, FieldDescriptor
    from pulsara_agent.terminal_protocol.generated import terminal_client_pb2 as wire

    result: set[tuple[str, str]] = set()

    def visit(message: Descriptor) -> None:
        for field in message.fields:
            if "fingerprint" not in field.name:
                continue
            if field.type != FieldDescriptor.TYPE_STRING:
                raise SystemExit(
                    f"terminal fingerprint field is not a string: {message.name}.{field.name}"
                )
            result.add((message.name, field.name))
        for nested in message.nested_types:
            visit(nested)

    for message in wire.DESCRIPTOR.message_types_by_name.values():
        visit(message)
    return result


def _verify_fingerprint_field_inventory(manifest: dict[str, object]) -> None:
    contracts = manifest["fingerprint_contracts"]
    assert isinstance(contracts, list)
    recomputable = {
        (str(item["message"]), str(item["own_field"]))
        for item in contracts
        if isinstance(item, dict)
    }
    opaque_raw = manifest["opaque_domain_fingerprint_fields"]
    if not isinstance(opaque_raw, list) or any(
        not isinstance(item, dict) or set(item) != {"message", "field"}
        for item in opaque_raw
    ):
        raise SystemExit(
            "terminal opaque fingerprint inventory must use exact message/field entries"
        )
    opaque = {(str(item["message"]), str(item["field"])) for item in opaque_raw}
    if len(opaque) != len(opaque_raw):
        raise SystemExit("terminal opaque fingerprint inventory contains duplicates")
    overlap = recomputable & opaque
    if overlap:
        raise SystemExit(
            f"terminal fingerprint fields have dual classification: {sorted(overlap)!r}"
        )
    observed = _descriptor_fingerprint_fields()
    classified = recomputable | opaque
    if observed != classified:
        missing = sorted(observed - classified)
        foreign = sorted(classified - observed)
        raise SystemExit(
            f"terminal fingerprint inventory is not exhaustive: missing={missing!r} foreign={foreign!r}"
        )


def _sync_opaque_fingerprint_fields() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    recomputable = {
        (str(item["message"]), str(item["own_field"]))
        for item in manifest["fingerprint_contracts"]
    }
    manifest["opaque_domain_fingerprint_fields"] = [
        {"message": message, "field": field}
        for message, field in sorted(_descriptor_fingerprint_fields() - recomputable)
    ]
    MANIFEST.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _verify_generated_protobuf() -> None:
    with tempfile.TemporaryDirectory(prefix="pulsara-terminal-proto-") as raw:
        temporary = Path(raw)
        environment = dict(os.environ)
        tools_root = ROOT / "clients/terminal/spikes/s0/.tools"
        environment["PATH"] = f"{tools_root}:{environment.get('PATH', '')}"
        subprocess.run(
            [
                "protoc",
                "-I",
                str(SCHEMA_ROOT),
                f"--go_out={temporary}",
                "--go_opt=module=github.com/plumliu/pulsara-agent/clients/terminal",
                str(PROTO),
            ],
            check=True,
            env=environment,
        )
        subprocess.run(
            [
                "protoc",
                "-I",
                str(SCHEMA_ROOT),
                f"--python_out={temporary}",
                str(PROTO),
            ],
            check=True,
            env=environment,
        )
        generated_go = temporary / "internal/protocol/terminal_client.pb.go"
        generated_python = temporary / "terminal_client_pb2.py"
        if generated_go.read_bytes() != GO_PROTO.read_bytes():
            raise SystemExit("generated Go Terminal Protocol binding is stale")
        if generated_python.read_bytes() != PY_PROTO.read_bytes():
            raise SystemExit("generated Python Terminal Protocol binding is stale")


if __name__ == "__main__":
    main()
