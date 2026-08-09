from __future__ import annotations

import ast
from hashlib import sha256
from pathlib import Path
import re
import subprocess
import sys

from pulsara_agent.terminal_protocol.codec import PROTOCOL_SCHEMA_FINGERPRINT


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "pulsara_agent"
FOUNDATION_SPEC = ROOT / "PULSARA_TERMINAL_PRESENTATION_FOUNDATION_IMPLEMENTATION.zh.md"
PROTOCOL_SPEC = ROOT / "PULSARA_TERMINAL_CLIENT_PROTOCOL_CONTRACT.zh.md"
PROTO = SRC / "terminal_protocol" / "schema" / "terminal_client.proto"


def test_host_close_drains_command_owners_before_queue_delivery_owners() -> None:
    source = (SRC / "host" / "session.py").read_text(encoding="utf-8")
    close_start = source.index("    async def aclose(")
    command_drain = source.index(
        "stop_and_drain_commands(",
        close_start,
    )
    delivery_drain = source.index(
        "stop_and_drain_queue_deliveries(",
        close_start,
    )
    assert command_drain < delivery_drain


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
    assert len(requirements) == len(set(requirements)) == 86
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


def test_production_terminal_renderer_dependency_matches_s0_compatibility_pin() -> None:
    production_go_mod = (ROOT / "clients" / "terminal" / "go.mod").read_text(
        encoding="utf-8"
    )
    spike_go_mod = (
        ROOT / "clients" / "terminal" / "spikes" / "s0" / "go.mod"
    ).read_text(encoding="utf-8")
    expected = (
        "github.com/charmbracelet/ultraviolet "
        "v0.0.0-20260416155717-489999b90468 // indirect"
    )
    assert production_go_mod.count(expected) == 1
    assert spike_go_mod.count(expected) == 1


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


def test_protocol_schema_fingerprint_and_cross_language_goldens_are_stable() -> None:
    digest = f"sha256:{sha256(PROTO.read_bytes()).hexdigest()}"
    assert digest == PROTOCOL_SCHEMA_FINGERPRINT
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/generate_terminal_protocol_contract.py"),
            "--check",
        ],
        cwd=ROOT,
        check=True,
    )


def test_go_renderer_is_confined_to_the_terminal_client_boundary() -> None:
    go_files = tuple(ROOT.rglob("*.go"))
    assert go_files
    assert all(path.is_relative_to(ROOT / "clients/terminal") for path in go_files)
    assert set(ROOT.rglob("go.mod")) == {
        ROOT / "clients/terminal/go.mod",
        ROOT / "clients/terminal/spikes/s0/go.mod",
    }
    schema = PROTO.read_text(encoding="utf-8").lower()
    for renderer_term in ("layout", "color", "key_binding", "animation"):
        assert renderer_term not in schema


def test_s1_go_client_respects_protocol_and_runtime_package_boundaries() -> None:
    client = ROOT / "clients/terminal"
    production_go = tuple(
        path
        for path in (client / "internal").rglob("*.go")
        if not path.name.endswith("_test.go")
    )
    assert production_go
    forbidden_authority_tokens = (
        "pulsara_agent/event",
        "pulsara_agent/runtime",
        "AgentEvent",
        "RawStoredEventEnvelope",
    )
    violations = {
        path.relative_to(ROOT).as_posix(): token
        for path in production_go
        for token in forbidden_authority_tokens
        if token in path.read_text(encoding="utf-8")
    }
    assert violations == {}

    app_sources = tuple(
        path
        for path in (client / "internal/app").glob("*.go")
        if not path.name.endswith("_test.go")
    )
    assert all(
        'clients/terminal/internal/protocol"' not in path.read_text(encoding="utf-8")
        for path in app_sources
    )
    protocol_values = client / "internal/protocolvalue"
    assert {
        path.name
        for path in protocol_values.glob("*.go")
        if not path.name.endswith("_test.go")
    } == {
        "vocabulary_gen.go",
        "carriers_gen.go",
        "s2_carriers_gen.go",
        "s3_carriers_gen.go",
    }
    assert all(
        path.read_text(encoding="utf-8").startswith(
            "// Code generated by generate_terminal_protocol_contract.py. DO NOT EDIT."
        )
        for path in protocol_values.glob("*_gen.go")
    )


def test_s3_command_candidates_and_wire_lowering_have_single_owners() -> None:
    internal = ROOT / "clients" / "terminal" / "internal"
    production_go = tuple(
        path for path in internal.rglob("*.go") if not path.name.endswith("_test.go")
    )

    candidate_calls: dict[str, list[str]] = {
        "NewSubmitCandidate(": [],
        "NewStopCandidate(": [],
    }
    for path in production_go:
        source = path.read_text(encoding="utf-8")
        relative = path.relative_to(ROOT).as_posix()
        for token in candidate_calls:
            if token in source and path.name != "candidate.go":
                candidate_calls[token].append(relative)
    assert candidate_calls == {
        "NewSubmitCandidate(": ["clients/terminal/internal/app/keymap.go"],
        "NewStopCandidate(": ["clients/terminal/internal/app/keymap.go"],
    }

    wire_calls: dict[str, list[str]] = {"MutationFrame(": [], "QueryCommandFrame(": []}
    for path in production_go:
        source = path.read_text(encoding="utf-8")
        relative = path.relative_to(ROOT).as_posix()
        for token in wire_calls:
            if token in source and path.name != "encode.go":
                wire_calls[token].append(relative)
    assert wire_calls == {
        "MutationFrame(": ["clients/terminal/internal/client/mutation.go"],
        "QueryCommandFrame(": ["clients/terminal/internal/client/mutation.go"],
    }


def test_s3_mutation_payload_bound_has_one_protocol_accounting_seam() -> None:
    internal = ROOT / "clients" / "terminal" / "internal"
    production_go = tuple(
        path for path in internal.rglob("*.go") if not path.name.endswith("_test.go")
    )
    call_sites = sorted(
        path.relative_to(ROOT).as_posix()
        for path in production_go
        if "MarshalBoundedDeterministicPayload(" in path.read_text(encoding="utf-8")
    )
    assert call_sites == [
        "clients/terminal/internal/commandstate/candidate.go",
        "clients/terminal/internal/protocol/payload.go",
        "clients/terminal/internal/wire/framing.go",
    ]
    joined = "\n".join(path.read_text(encoding="utf-8") for path in production_go)
    assert "request:frame-size-probe" not in joined
    assert "FramedMutationBytes" not in joined


def test_s1_production_update_signal_and_foreground_owners_are_exact() -> None:
    app_root = ROOT / "clients" / "terminal" / "internal" / "app"
    deterministic_sources = (
        app_root / "update.go",
        app_root / "view.go",
    )
    for path in deterministic_sources:
        source = path.read_text(encoding="utf-8")
        assert "time.Now(" not in source
        assert "time.Until(" not in source

    update = (app_root / "update.go").read_text(encoding="utf-8")
    assert "tea.Tick(" not in update
    assert "tea.Quit" not in update
    assert "ScheduleTickEffect" in update
    assert "QuitProgramEffect" in update

    program = (app_root / "program.go").read_text(encoding="utf-8")
    assert "tea.WithoutSignalHandler()" in program
    assert "tea.WithoutCatchPanics()" not in program
    assert "tea.WithFilter(" not in program
    assert "productionInputBoundary" in program

    launcher = (
        ROOT / "src" / "pulsara_agent" / "terminal_client" / "launcher.py"
    ).read_text(encoding="utf-8")
    assert "process_group=0" in launcher
    assert "start_new_session=True" not in launcher


def test_s1_layout_viewport_and_scrollback_owners_are_exact() -> None:
    client_root = ROOT / "clients" / "terminal"
    transcript_root = client_root / "internal" / "components" / "transcript"
    kernel_viewport_owner = client_root / "internal" / "kernelapp" / "model.go"
    viewport_tokens = ("scrollOffset", "followTail", "viewportAnchor", "WrapCache")
    violations: list[str] = []
    for path in (client_root / "internal").rglob("*.go"):
        if (
            path.name.endswith("_test.go")
            or path.is_relative_to(transcript_root)
            or path == kernel_viewport_owner
        ):
            continue
        source = path.read_text(encoding="utf-8")
        for token in viewport_tokens:
            if token in source:
                violations.append(f"{path.relative_to(ROOT).as_posix()}:{token}")
    assert violations == []
    production_binary = (client_root / "cmd" / "pulsara-tui" / "main.go").read_text(
        encoding="utf-8"
    )
    assert "kernelbootstrap.Run" in production_binary
    assert "internal/bootstrap" not in production_binary
    assert "followTail" in kernel_viewport_owner.read_text(encoding="utf-8")

    presentation = (client_root / "internal" / "presentation" / "state.go").read_text(
        encoding="utf-8"
    )
    for legacy_owner in (
        "TranscriptViewportState",
        "func (s State) Resize",
        "func (s State) Scroll",
        "scrollOffset",
    ):
        assert legacy_owner not in presentation

    app_state = (client_root / "internal" / "app" / "state.go").read_text(
        encoding="utf-8"
    )
    assert "transcript          transcript.Model" in app_state
    assert "TranscriptViewportState" not in app_state

    view = (client_root / "internal" / "app" / "view.go").read_text(encoding="utf-8")
    for io_token in ("os.", "net.", "Read(", "RequestSnapshot", "Effect{"):
        assert io_token not in view
    assert "NewLayoutPlan(" not in view

    clear_owners = []
    for path in tuple(SRC.rglob("*.py")) + tuple(client_root.rglob("*.go")):
        source = path.read_text(encoding="utf-8")
        if "\\x1b[3J" in source or "ESC[3J" in source:
            clear_owners.append(path.relative_to(ROOT).as_posix())
    assert clear_owners == ["src/pulsara_agent/terminal_client/launcher.py"]


def test_s1_physical_terminalization_owners_are_confined_and_drained() -> None:
    client_root = ROOT / "clients" / "terminal" / "internal" / "client"
    registry = (client_root / "operation_registry.go").read_text(encoding="utf-8")
    connection = (client_root / "connection.go").read_text(encoding="utf-8")
    service = (client_root / "service.go").read_text(encoding="utf-8")

    for type_name in (
        "ConnectionTerminalizationAttemptIdentity",
        "ConnectionTerminalizationAttemptHandle",
        "PreparedConnectionTerminalization",
        "OperationSettlementCapability",
        "PostJoinOperationSettlementCapability",
        "PhysicalOperationFailureReceipt",
    ):
        assert registry.count(f"type {type_name} ") == 1
        assert f"type {type_name} " not in connection
    for type_name in (
        "PhysicalConnectionDrainHandle",
        "PhysicalConnectionDrainLaunchPermit",
        "PhysicalConnectionDrainRunnerLease",
        "PhysicalConnectionTerminalReceipt",
    ):
        assert connection.count(f"type {type_name} ") == 1
        assert f"type {type_name} " not in registry

    assert "drainConnectionTerminalizations(deadline)" in service
    assert "waitPhysicalDrain(start.handle)" in registry
    assert "receipt.validate(handle)" in connection

    failure_classifier_calls: list[str] = []
    for path in (ROOT / "clients" / "terminal").rglob("*.go"):
        if path.name.endswith("_test.go"):
            continue
        source = path.read_text(encoding="utf-8")
        if "app.ClassifyPublicFailure(" in source:
            failure_classifier_calls.append(path.relative_to(ROOT).as_posix())
        assert "NewPhysicalPublicFailure(" not in source
        assert "FailureReconnectUnavailable" not in source
    assert failure_classifier_calls == [
        "clients/terminal/internal/client/operation_registry.go"
    ]

    app_state = (
        ROOT / "clients" / "terminal" / "internal" / "app" / "state.go"
    ).read_text(encoding="utf-8")
    assert "serverNotifications []" not in app_state
    presentation_state = (
        ROOT / "clients" / "terminal" / "internal" / "presentation" / "state.go"
    ).read_text(encoding="utf-8")
    assert "value.Control = protocolvalue.ControlProjection{}" in presentation_state


def test_terminal_observation_wait_cannot_starve_attachment_heartbeat() -> None:
    from pulsara_agent.terminal_protocol.codec import (
        HEARTBEAT_INTERVAL_MS,
        MAXIMUM_OBSERVATION_WAIT_MS,
    )

    assert 0 < MAXIMUM_OBSERVATION_WAIT_MS < HEARTBEAT_INTERVAL_MS
