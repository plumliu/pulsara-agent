"""Architecture gates for the boundary-only fingerprint hard-cut."""

from __future__ import annotations

from dataclasses import fields
import json
from pathlib import Path

from pulsara_agent.capability.contracts import (
    FrozenToolCapabilityExposureSelection,
)
from pulsara_agent.capability.tool_action import ToolActionClassifierBinding
from pulsara_agent.conversation_kernel.assistant_settlement import (
    PreparedAssistantMessageSettlement,
)
from pulsara_agent.conversation_kernel.capability_composition import (
    PreparedLocalSkillCatalogSourceSnapshot,
)
from pulsara_agent.conversation_kernel.cold_epoch import (
    SelectedDurableReplayHydrationRequest,
    SubagentInitialSeed,
)
from pulsara_agent.conversation_kernel.compaction.contracts import (
    RecentHumanMessageProof,
)
from pulsara_agent.conversation_kernel.compaction.runtime_handoff import (
    FrozenRootSubagentTaskBoardHandoffFact,
)
from pulsara_agent.conversation_kernel.mcp.contracts import McpInstallationCandidate
from pulsara_agent.conversation_kernel.memory.reflection import (
    PreparedCheapHintReflectionCandidateBatch,
)
from pulsara_agent.conversation_kernel.subagents import contracts as subagent_contracts
from pulsara_agent.conversation_kernel.tool_runtime import (
    ProductionBuiltinExecutorBinding,
)
from pulsara_agent.model_input.continuity import (
    PreparedProviderInputAppendCandidate,
    ProcessLocalProviderInputInstallPermit,
)
from pulsara_agent.tools.builtins.todo import FrozenTodoItem


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = (
    ROOT
    / "benchmarks/suites/core/v1/fingerprint_subtraction_hard_cut_activation.json"
)


def _field_names(value: type[object]) -> set[str]:
    return {item.name for item in fields(value)}


def test_hard_cut_mandatory_process_local_fingerprint_fields_are_absent() -> None:
    forbidden_by_type = {
        ToolActionClassifierBinding: {"implementation_build_fingerprint"},
        FrozenToolCapabilityExposureSelection: {"selection_fingerprint"},
        McpInstallationCandidate: {"candidate_fingerprint"},
        PreparedLocalSkillCatalogSourceSnapshot: {"root_policy_fingerprint"},
        SubagentInitialSeed: {"objective_item_fingerprint", "seed_fingerprint"},
        SelectedDurableReplayHydrationRequest: {
            "source_dispatch_read_fingerprint",
            "request_fingerprint",
        },
        RecentHumanMessageProof: {"item_fingerprint"},
        FrozenTodoItem: {"item_fingerprint"},
        PreparedCheapHintReflectionCandidateBatch: {"batch_fingerprint"},
        FrozenRootSubagentTaskBoardHandoffFact: {"fact_fingerprint"},
        PreparedAssistantMessageSettlement: {"candidate_fingerprint"},
        PreparedProviderInputAppendCandidate: {"candidate_fingerprint"},
        ProcessLocalProviderInputInstallPermit: {
            "candidate_fingerprint",
            "execution_fingerprint",
        },
    }
    for value, forbidden in forbidden_by_type.items():
        assert _field_names(value).isdisjoint(forbidden), value.__name__


def test_hard_cut_subagent_process_local_aggregate_fields_are_absent() -> None:
    forbidden = {
        "eligible_context_units_fingerprint",
        "parent_context_selection_fingerprint",
        "parent_context_source_fingerprint",
        "dependency_item_fingerprint",
        "dependency_context_fingerprint",
        "task_draft_candidate_fingerprint",
        "batch_candidate_fingerprint",
        "settlement_candidate_fingerprint",
        "mailbox_item_fingerprint",
        "mailbox_batch_candidate_fingerprint",
    }
    for name in dir(subagent_contracts):
        value = getattr(subagent_contracts, name)
        if isinstance(value, type) and value.__module__ == subagent_contracts.__name__:
            try:
                names = _field_names(value)
            except TypeError:
                continue
            assert names.isdisjoint(forbidden), name


def test_builtin_binding_keeps_exact_catalog_entry_not_duplicate_schema_fields() -> None:
    assert _field_names(ProductionBuiltinExecutorBinding) == {
        "catalog_entry",
        "executor_identity",
    }


def test_new_activation_evidence_contains_no_file_or_report_hash_inventory() -> None:
    payload = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "fingerprint-subtraction-activation-v1"
    forbidden_keys = {
        "source_documents",
        "document_sha256",
        "post_activation_code_sha256",
        "post_review_code_sha256",
        "production_code_sha256",
        "code_sha256",
        "final_modules",
        "activation_report_sha256",
        "evidence_sha256",
        "file_digest",
        "source_fingerprint",
        "implementation_tree_hash",
        "report_integrity_hash",
    }

    def visit(value: object) -> None:
        if isinstance(value, dict):
            assert forbidden_keys.isdisjoint(value)
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(payload)


def test_historical_activation_reports_are_not_current_hash_gates() -> None:
    source = (ROOT / "tests/test_repository_modularization_architecture.py").read_text(
        encoding="utf-8"
    )
    assert "_ROUND8_ROUND10_REPOSITORY_DELTA_SHA256" not in source
    assert "post_activation_code_sha256" not in source
    assert "source_documents" not in source
