from __future__ import annotations

from collections import Counter
from pathlib import Path

from tests.support.event_log_mutation_guard import (
    EVENT_ROW_MUTATION_METHODS,
    MAINTENANCE_METHODS,
    scan_production_event_log_mutations,
    scan_source_event_log_mutations,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "src" / "pulsara_agent"

DIRECT_EVENT_LOG_MUTATION_ALLOWLIST = {
    (
        "runtime/session.py",
        "RuntimeSession",
        "_commit_reduce_enqueue",
        "self.event_log",
        "extend",
    ),
    (
        "runtime/session.py",
        "RuntimeSession",
        "_persist_runtime_projection_checkpoint",
        "self.event_log",
        "write_runtime_projection_checkpoint",
    ),
    (
        "runtime/authority_materialization/account.py",
        "LedgerMaterializationCoordinator",
        "_commit_atomic",
        "self.event_log",
        "extend_with_materialization_state",
    ),
    (
        "runtime/long_horizon/checkpoint_doctor.py",
        None,
        "verify_or_rebuild_subagent_graph_checkpoint",
        "event_log",
        "append",
    ),
}

DIRECT_EVENT_LOG_MAINTENANCE_INVENTORY = Counter(
    {
        (
            "runtime/session.py",
            "RuntimeSession",
            "__post_init__",
            "self.event_log",
            "ensure_runtime_session_owner",
        ): 1,
        (
            "runtime/subagent/runtime.py",
            "SubagentRuntime",
            "_create_child_runtime_session",
            "event_log",
            "ensure_runtime_session_owner",
        ): 1,
        (
            "host/resume.py",
            None,
            "repair_dangling_runs_for_resume",
            "log",
            "repair_run_projection",
        ): 2,
        (
            "runtime/session.py",
            "RuntimeSession",
            "_adopt_unbootstrapped_in_memory_account_for_test",
            "self.event_log",
            "adopt_materialization_account_state_for_test",
        ): 1,
    }
)


def test_production_event_log_mutation_inventory_is_exact() -> None:
    observations = scan_production_event_log_mutations(PACKAGE_ROOT)
    escapes = tuple(
        item for item in observations if item.syntax_kind != "direct_call"
    )
    assert escapes == ()

    event_rows = {
        item.allowlist_key
        for item in observations
        if item.method in EVENT_ROW_MUTATION_METHODS
    }
    assert event_rows == DIRECT_EVENT_LOG_MUTATION_ALLOWLIST

    maintenance = Counter(
        item.allowlist_key
        for item in observations
        if item.method in MAINTENANCE_METHODS
    )
    assert maintenance == DIRECT_EVENT_LOG_MAINTENANCE_INVENTORY


def test_event_log_mutation_guard_rejects_wrong_scope_receiver_and_method() -> None:
    observations = scan_source_event_log_mutations(
        """
class RuntimeSession:
    def wrong_owner(self):
        self.event_log.extend(())

def repair_dangling_runs_for_resume(event_log):
    event_log.extend(())
""",
        relative_path="runtime/session.py",
    )
    assert {item.allowlist_key for item in observations}.isdisjoint(
        DIRECT_EVENT_LOG_MUTATION_ALLOWLIST
    )


def test_event_log_mutation_guard_detects_alias_bound_method_and_getattr() -> None:
    observations = scan_source_event_log_mutations(
        """
def mutate(event_log):
    alias = event_log
    alias.extend(())
    writer = event_log.extend
    writer(())
    getattr(event_log, "extend")(())
""",
    )
    assert {item.syntax_kind for item in observations} == {
        "direct_call",
        "bound_method_escape",
        "bound_method_call",
        "getattr_call",
        "getattr_escape",
    }
    assert all(item.receiver == "event_log" for item in observations)


def test_event_log_mutation_guard_detects_nested_aliases_but_skips_nested_scopes() -> None:
    observations = scan_source_event_log_mutations(
        """
def mutate(event_log, enabled):
    if enabled:
        alias = event_log
        alias.extend(())
    try:
        writer = event_log.append
        writer(object())
    finally:
        getattr(event_log, "extend")(())

    def unrelated_nested_scope():
        event_log.extend(())
""",
    )
    assert {item.syntax_kind for item in observations} == {
        "direct_call",
        "bound_method_escape",
        "bound_method_call",
        "getattr_call",
        "getattr_escape",
    }
    assert all(item.enclosing_function == "mutate" for item in observations)
    assert all(item.receiver == "event_log" for item in observations)
