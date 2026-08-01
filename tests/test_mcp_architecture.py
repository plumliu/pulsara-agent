from __future__ import annotations

import ast
import inspect
from pathlib import Path

from pulsara_agent.ports.mcp import McpPreparedCompanionIdentity
from pulsara_agent.primitives.frozen import FrozenFactBase
from pulsara_agent.primitives.mcp_continuation import (
    McpContinuationDispatchReservationFact,
)
from pulsara_agent.primitives.mcp_continuation_storage import (
    McpContinuationCarrierControlFact,
    McpStoredContinuationEnvelopeFact,
)
from pulsara_agent.primitives.storage_frozen import FrozenStorageFactBase
from pulsara_agent.runtime.mcp.recovery import (
    terminalize_reopened_mcp_input_required,
)


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "pulsara_agent"


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return modules


def test_mcp_low_level_contracts_do_not_import_host_or_agent_runtime() -> None:
    for relative in (
        "primitives/mcp.py",
        "runtime/mcp/types.py",
    ):
        modules = _imported_modules(SRC / relative)
        assert not any(module.startswith("pulsara_agent.host") for module in modules)
        assert "pulsara_agent.runtime.agent" not in modules


def test_mcp_sdk_and_worker_do_not_import_capability_or_host_mutation_layers() -> None:
    sdk_modules = _imported_modules(SRC / "runtime/mcp/sdk.py")
    worker_modules = _imported_modules(SRC / "runtime/mcp/supervisor.py")
    assert not any(
        module.startswith("pulsara_agent.capability") for module in sdk_modules
    )
    assert not any(module.startswith("pulsara_agent.host") for module in worker_modules)


def test_mcp_removed_production_symbols_do_not_reappear() -> None:
    source_text = "\n".join(path.read_text() for path in SRC.rglob("*.py"))
    removed_symbols = (
        "McpCapabilityBindingBundle",
        "build_mcp_bundle",
        "runtime_wiring.mcp_manager",
        "runtime_wiring.mcp_bundle",
        "SdkMcpClientManager.start",
        "startup_timeout_ms",
    )
    for symbol in removed_symbols:
        assert symbol not in source_text


def test_official_mcp_sdk_and_httpx2_are_owned_only_by_stable_facade() -> None:
    allowed = SRC / "runtime" / "mcp" / "sdk.py"
    observations: list[tuple[str, str]] = []
    for path in SRC.rglob("*.py"):
        for module in _imported_modules(path):
            if module == "mcp" or module.startswith("mcp.") or module == "httpx2":
                if path != allowed:
                    observations.append((str(path.relative_to(SRC)), module))
    assert observations == []


def test_mcp_v2_has_no_beta_or_year_named_behavior_era() -> None:
    guarded = (
        ROOT / "pyproject.toml",
        SRC / "primitives" / "mcp_protocol.py",
        SRC / "runtime" / "mcp" / "sdk.py",
    )
    text = "\n".join(path.read_text() for path in guarded)
    for forbidden in ("2.0.0b", "2.0.0rc", "LEGACY_2025", "MODERN_2026"):
        assert forbidden not in text


def test_mcp_v2_product_boundary_does_not_advertise_unowned_capabilities() -> None:
    protocol = (SRC / "primitives" / "mcp_protocol.py").read_text()
    sdk = (SRC / "runtime" / "mcp" / "sdk.py").read_text()
    assert "sampling_advertised=False" in sdk
    assert "roots_advertised=False" in sdk
    assert "logging_advertised=False" in sdk
    assert "MCP2 V1 does not advertise sampling, roots, or logging" in protocol
    assert "CreateTask" not in sdk
    assert "McpApp" not in sdk


def test_mcp_sdk_facade_has_no_private_or_high_level_execution_escape_hatch() -> None:
    sdk = (SRC / "runtime" / "mcp" / "sdk.py").read_text()
    for forbidden in (
        "cache=False",
        "._exit_stack",
        "._exit_callbacks",
        ".session.call_tool(",
        ".client.call_tool(",
    ):
        assert forbidden not in sdk
    assert ".session.send_request(" in sdk
    assert ".session.send_discover(" in sdk


def test_mcp_schema_authority_is_rejected_instead_of_repaired() -> None:
    schema = (SRC / "runtime" / "mcp" / "schema.py").read_text()
    assert 'setdefault("type", "object")' not in schema
    assert "setdefault('type', 'object')" not in schema
    assert 'setdefault("properties", {})' not in schema
    assert "setdefault('properties', {})" not in schema


def test_mcp_storage_vocabulary_is_not_event_safe_vocabulary() -> None:
    assert not issubclass(FrozenStorageFactBase, FrozenFactBase)
    for storage_type in (
        McpContinuationCarrierControlFact,
        McpStoredContinuationEnvelopeFact,
    ):
        assert issubclass(storage_type, FrozenStorageFactBase)
        assert not issubclass(storage_type, FrozenFactBase)


def test_mcp_dispatch_and_companion_contracts_keep_guards_out_of_durable_payload() -> (
    None
):
    dispatch_fields = set(McpContinuationDispatchReservationFact.model_fields)
    assert "expected_materialization_account_revision" not in dispatch_fields
    assert "expected_materialization_account_fingerprint" not in dispatch_fields
    companion_fields = set(McpPreparedCompanionIdentity.__dataclass_fields__)
    assert {
        "ordered_candidate_event_ids",
        "ordered_candidate_schema_binding_fingerprints",
        "ordered_candidate_payload_fingerprints",
        "exact_ordered_batch_fingerprint",
    } <= companion_fields


def test_generic_durable_and_diagnostic_sinks_install_mcp_secret_guard() -> None:
    guarded_sources = (
        SRC / "event_log" / "serialization.py",
        SRC / "memory" / "artifacts" / "archive.py",
        SRC / "memory" / "artifacts" / "postgres_archive.py",
        SRC / "inspector" / "service.py",
    )
    for path in guarded_sources:
        source = path.read_text()
        assert "assert_not_mcp_secret" in source, path


def test_mcp_ready_and_child_recovery_use_closed_production_owners() -> None:
    sdk = (SRC / "runtime" / "mcp" / "sdk.py").read_text()
    child = (SRC / "runtime" / "subagent" / "runtime.py").read_text()
    host = (SRC / "host" / "core.py").read_text()

    assert "McpSdkConformedClientGeneration(" in sdk
    assert "McpSdkNegotiatedProtocolBinding(" in sdk
    assert "complete_listing_accumulator=None" not in sdk
    assert "generation.sdk_protocol_binding is not protocol" in sdk
    assert sdk.count("_mcp_sdk_concurrency_mode(connection)") == 2
    assert "refreshed if item.server_id == refreshed.server_id" not in sdk
    assert ".terminalize_reopened_input_required(" in child
    assert "terminalize_reopened_mcp_input_required(" not in child
    assert "build_full_mcp_elicitation_capability(" in host

    repository_parameter = inspect.signature(
        terminalize_reopened_mcp_input_required
    ).parameters["continuation_repository"]
    assert repository_parameter.default is inspect.Signature.empty
