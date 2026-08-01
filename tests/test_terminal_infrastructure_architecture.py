from __future__ import annotations

import ast
from hashlib import sha256
from pathlib import Path
import re

from pulsara_agent.terminal_protocol.codec import PROTOCOL_SCHEMA_FINGERPRINT
from pulsara_agent.terminal_protocol.generated import terminal_client_pb2 as wire


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "pulsara_agent"
FOUNDATION_SPEC = ROOT / "PULSARA_TERMINAL_PRESENTATION_FOUNDATION_IMPLEMENTATION.zh.md"
PROTOCOL_SPEC = ROOT / "PULSARA_TERMINAL_CLIENT_PROTOCOL_CONTRACT.zh.md"
PROTO = SRC / "terminal_protocol" / "schema" / "terminal_client.proto"


TRACEABILITY: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "TUI-FND-EVT": (
        (
            "src/pulsara_agent/primitives/stored_event.py",
            "src/pulsara_agent/ports/stored_event.py",
            "src/pulsara_agent/event_log/serialization.py",
        ),
        ("tests/test_runtime_committed_writer.py", "tests/test_event_log_contract.py"),
    ),
    "TUI-FND-OBS": (
        (
            "src/pulsara_agent/runtime/terminal_presentation/observation.py",
            "src/pulsara_agent/runtime/authority_materialization/transcript_reducer.py",
            "src/pulsara_agent/runtime/session.py",
        ),
        (
            "tests/test_terminal_presentation_foundation.py",
            "tests/test_runtime_committed_writer.py",
        ),
    ),
    "TUI-FND-PROJ": (
        (
            "src/pulsara_agent/primitives/presentation_history.py",
            "src/pulsara_agent/runtime/terminal_presentation/projection.py",
            "src/pulsara_agent/runtime/terminal_presentation/history_checkpoint.py",
            "src/pulsara_agent/runtime/terminal_presentation/history_tree.py",
        ),
        ("tests/test_terminal_presentation_foundation.py",),
    ),
    "TUI-FND-VIEW": (
        (
            "src/pulsara_agent/ports/terminal_presentation.py",
            "src/pulsara_agent/runtime/terminal_presentation/viewport.py",
            "src/pulsara_agent/runtime/terminal_presentation/service.py",
        ),
        ("tests/test_terminal_protocol.py",),
    ),
    "TUI-FND-CMD": (
        (
            "src/pulsara_agent/ports/terminal_application.py",
            "src/pulsara_agent/runtime/terminal_application/services.py",
            "src/pulsara_agent/runtime/terminal_application/command_receipt.py",
        ),
        ("tests/test_terminal_protocol.py",),
    ),
    "TUI-FND-QUEUE": (
        (
            "src/pulsara_agent/primitives/prompt_queue.py",
            "src/pulsara_agent/runtime/terminal_application/prompt_queue.py",
            "src/pulsara_agent/runtime/terminal_application/prompt_queue_checkpoint.py",
            "src/pulsara_agent/runtime/terminal_application/artifact_hold.py",
        ),
        (
            "tests/test_host_core.py",
            "tests/test_terminal_presentation_foundation.py",
            "tests/test_schema_migrations.py",
        ),
    ),
    "TUI-FND-INT": (
        (
            "src/pulsara_agent/runtime/terminal_application/secret.py",
            "src/pulsara_agent/runtime/terminal_application/services.py",
        ),
        ("tests/test_terminal_protocol.py", "tests/test_mcp_host_lifecycle.py"),
    ),
    "TUI-FND-LIFE": (
        (
            "src/pulsara_agent/host/core.py",
            "src/pulsara_agent/host/session.py",
            "src/pulsara_agent/runtime/session.py",
        ),
        ("tests/test_host_lifecycle_contract.py", "tests/test_terminal_protocol.py"),
    ),
    "TUI-FND-GATE": (
        ("tests/test_terminal_infrastructure_architecture.py",),
        (
            "tests/test_terminal_infrastructure_architecture.py",
            "tests/test_schema_migrations.py",
        ),
    ),
    "TUI-PROTO-TRANSPORT": (
        (
            "src/pulsara_agent/terminal_protocol/gateway.py",
            "src/pulsara_agent/terminal_protocol/schema/terminal_client.proto",
        ),
        ("tests/test_terminal_protocol.py",),
    ),
    "TUI-PROTO-HELLO": (
        (
            "src/pulsara_agent/terminal_protocol/codec.py",
            "src/pulsara_agent/terminal_protocol/gateway.py",
        ),
        ("tests/test_terminal_protocol.py",),
    ),
    "TUI-PROTO-ATTACH": (
        (
            "src/pulsara_agent/runtime/terminal_application/attachment.py",
            "src/pulsara_agent/terminal_protocol/gateway.py",
        ),
        ("tests/test_terminal_protocol.py",),
    ),
    "TUI-PROTO-CURSOR": (
        (
            "src/pulsara_agent/primitives/presentation_history.py",
            "src/pulsara_agent/runtime/terminal_presentation/viewport.py",
            "src/pulsara_agent/terminal_protocol/codec.py",
        ),
        ("tests/test_terminal_protocol.py",),
    ),
    "TUI-PROTO-OBS": (
        (
            "src/pulsara_agent/terminal_protocol/codec.py",
            "src/pulsara_agent/terminal_protocol/gateway.py",
            "src/pulsara_agent/terminal_protocol/schema/terminal_client.proto",
        ),
        ("tests/test_terminal_protocol.py",),
    ),
    "TUI-PROTO-BP": (
        (
            "src/pulsara_agent/runtime/terminal_presentation/observation.py",
            "src/pulsara_agent/terminal_protocol/gateway.py",
        ),
        (
            "tests/test_terminal_presentation_foundation.py",
            "tests/test_terminal_protocol.py",
        ),
    ),
    "TUI-PROTO-CMD": (
        (
            "src/pulsara_agent/ports/terminal_application.py",
            "src/pulsara_agent/runtime/terminal_application/command_receipt.py",
            "src/pulsara_agent/terminal_protocol/gateway.py",
        ),
        ("tests/test_terminal_protocol.py",),
    ),
    "TUI-PROTO-SECRET": (
        (
            "src/pulsara_agent/runtime/terminal_application/secret.py",
            "src/pulsara_agent/terminal_protocol/gateway.py",
        ),
        ("tests/test_terminal_protocol.py", "tests/test_mcp_host_lifecycle.py"),
    ),
    "TUI-PROTO-LIFE": (
        (
            "src/pulsara_agent/runtime/terminal_application/services.py",
            "src/pulsara_agent/host/core.py",
            "src/pulsara_agent/terminal_protocol/gateway.py",
        ),
        ("tests/test_host_lifecycle_contract.py", "tests/test_terminal_protocol.py"),
    ),
    "TUI-PROTO-SCHEMA": (
        (
            "src/pulsara_agent/terminal_protocol/schema/terminal_client.proto",
            "src/pulsara_agent/terminal_protocol/generated/terminal_client_pb2.py",
            "src/pulsara_agent/terminal_protocol/codec.py",
        ),
        ("tests/test_terminal_infrastructure_architecture.py",),
    ),
    "TUI-PROTO-GATE": (
        ("tests/test_terminal_infrastructure_architecture.py",),
        (
            "tests/test_terminal_infrastructure_architecture.py",
            "tests/test_terminal_protocol.py",
        ),
    ),
}


def _requirement_ids(path: Path, namespace: str) -> tuple[str, ...]:
    result = []
    pattern = re.compile(rf"({re.escape(namespace)}-[A-Z0-9-]+)")
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.lstrip().startswith("#"):
            continue
        match = pattern.search(line)
        if match is not None:
            result.append(match.group(1))
    return tuple(result)


def test_every_renderer_neutral_requirement_has_owner_and_test_traceability() -> None:
    requirements = _requirement_ids(FOUNDATION_SPEC, "TUI-FND") + _requirement_ids(
        PROTOCOL_SPEC, "TUI-PROTO"
    )
    assert len(requirements) == len(set(requirements)) == 78
    uncovered = []
    for requirement in requirements:
        matches = tuple(
            value
            for prefix, value in TRACEABILITY.items()
            if requirement.startswith(prefix)
        )
        if len(matches) != 1:
            uncovered.append(requirement)
            continue
        owners, tests = matches[0]
        assert owners and tests
        assert all((ROOT / path).is_file() for path in owners)
        assert all((ROOT / path).is_file() for path in tests)
    assert uncovered == []


def test_production_terminal_foundation_has_no_renderer_or_test_imports() -> None:
    violations: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                modules = (node.module,)
            else:
                continue
            for module in modules:
                if module.split(".", 1)[0] == "tests":
                    violations.append(
                        f"{path.relative_to(ROOT).as_posix()}:{node.lineno}:{module}"
                    )
    renderer_neutral_roots = (
        SRC / "runtime" / "terminal_presentation",
        SRC / "runtime" / "terminal_application",
        SRC / "terminal_protocol",
        SRC / "ports",
        SRC / "primitives",
    )
    forbidden_frameworks = {
        "prompt_toolkit",
        "textual",
        "bubbletea",
        "bubbles",
        "lipgloss",
    }
    for root in renderer_neutral_roots:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = tuple(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module is not None:
                    modules = (node.module,)
                else:
                    continue
                for module in modules:
                    if module.split(".", 1)[0] in forbidden_frameworks:
                        violations.append(
                            f"{path.relative_to(ROOT).as_posix()}:{node.lineno}:{module}"
                        )
    assert violations == []


def test_stored_event_carrier_and_receipt_have_single_definition_owners() -> None:
    expected = {
        "RawStoredEventEnvelope": "src/pulsara_agent/primitives/stored_event.py",
        "EncoderBuiltStoredEventPair": "src/pulsara_agent/ports/stored_event.py",
        "DecoderHydratedStoredEventPair": "src/pulsara_agent/ports/stored_event.py",
        "StoredEventBatchCommitReceipt": "src/pulsara_agent/ports/stored_event.py",
        "JoinedRawStoredEventRangeProof": "src/pulsara_agent/ports/stored_event.py",
    }
    observed: dict[str, list[str]] = {name: [] for name in expected}
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name in observed:
                observed[node.name].append(path.relative_to(ROOT).as_posix())
    assert observed == {name: [owner] for name, owner in expected.items()}


def test_presentation_hot_path_cannot_scan_eventlog_or_subscribe_publisher() -> None:
    roots = (
        SRC / "runtime" / "terminal_presentation",
        SRC / "runtime" / "terminal_application",
        SRC / "terminal_protocol",
    )
    violations: list[str] = []
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(
                    node.func, ast.Attribute
                ):
                    continue
                if node.func.attr == "iter":
                    violations.append(
                        f"{path.relative_to(ROOT).as_posix()}:{node.lineno}:iter"
                    )
                if (
                    node.func.attr == "subscribe"
                    and isinstance(node.func.value, ast.Attribute)
                    and node.func.value.attr == "publisher"
                ):
                    violations.append(
                        f"{path.relative_to(ROOT).as_posix()}:{node.lineno}:publisher"
                    )
    assert violations == []


def test_protocol_schema_fingerprint_and_golden_hello_are_stable() -> None:
    digest = f"sha256:{sha256(PROTO.read_bytes()).hexdigest()}"
    assert digest == PROTOCOL_SCHEMA_FINGERPRINT
    golden = wire.ClientFrame(
        hello=wire.HelloRequest(
            request_id="golden:hello",
            supported_version_range=wire.ProtocolVersionRange(
                major=1,
                minimum_minor=0,
                maximum_minor=0,
                schema_contract_fingerprint=PROTOCOL_SCHEMA_FINGERPRINT,
            ),
            client_instance_id="headless:golden",
            client_build_identity="python-headless-conformance:v1",
            supported_capabilities=(
                "history_page_v1",
                "presentation_root_advance_v1",
            ),
            requested_attachment_mode=wire.ATTACHMENT_ROLE_OBSERVER,
            launch_capability=bytes.fromhex("00112233445566778899aabbccddeeff"),
        )
    )
    assert golden.SerializeToString(deterministic=True).hex() == (
        "0acf010a0c676f6c64656e3a68656c6c6f124b080122477368613235363a"
        "38616664653335616230323162633036373535306139393833616562343930"
        "37326133323037353432346263343632633563343137646665336562373130"
        "61331a0f686561646c6573733a676f6c64656e221e707974686f6e2d686561"
        "646c6573732d636f6e666f726d616e63653a76312a0f686973746f72795f70"
        "6167655f76312a1c70726573656e746174696f6e5f726f6f745f616476616e"
        "63655f763130013a1000112233445566778899aabbccddeeff"
    )


def test_renderer_and_go_artifacts_are_deferred_and_absent() -> None:
    assert not tuple(ROOT.rglob("*.go"))
    assert not tuple(ROOT.rglob("go.mod"))
    schema = PROTO.read_text(encoding="utf-8").lower()
    for renderer_term in ("layout", "color", "key_binding", "animation"):
        assert renderer_term not in schema


def test_terminal_observation_wait_cannot_starve_attachment_heartbeat() -> None:
    from pulsara_agent.terminal_protocol.codec import (
        HEARTBEAT_INTERVAL_MS,
        MAXIMUM_OBSERVATION_WAIT_MS,
    )

    assert 0 < MAXIMUM_OBSERVATION_WAIT_MS < HEARTBEAT_INTERVAL_MS
