from __future__ import annotations

import asyncio
from collections import OrderedDict
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from benchmarks.suites.contracts import (
    DogfoodContractError,
    HiddenVerifierResultFact,
    load_suite,
    runner_build_fingerprint,
)
from benchmarks.suites.graders import grade_durable_evidence, run_hidden_verifier
from benchmarks.suites.run_core_dogfood import DEFAULT_SUITE_ROOT, main
from benchmarks.suites.runner import CoreDogfoodRunner, _prepare_fixture, _record_result
from pulsara_agent.ports.run_execution import RunSuspendedOutcome


EXPECTED_SCENARIOS = (
    "cache-continuity",
    "durable-resume",
    "manual-compaction-trail",
    "plan-workflow",
    "subagent-delegation",
    "workspace-patch",
)


def test_cme5_dod_evidence_matches_frozen_suite() -> None:
    evidence = json.loads(
        (DEFAULT_SUITE_ROOT / "cme5_dod_evidence.json").read_text(encoding="utf-8")
    )
    suite = load_suite(DEFAULT_SUITE_ROOT)
    scenario = next(
        item
        for item in suite.scenarios
        if item.contract.scenario_id == "manual-compaction-trail"
    )

    assert evidence["schema_version"] == "pulsara.cme5-dod-evidence.v1"
    assert evidence["hard_cut"] == "CME0-CME5"
    assert evidence["debt_item"] == "D5"
    assert evidence["status"] == "passed"
    assert tuple(item["gate_id"] for item in evidence["gates"]) == tuple(
        f"CME{index}" for index in range(6)
    )
    assert all(item["status"] == "passed" for item in evidence["gates"])

    assert evidence["real_llm_dogfood"]["status"] == "passed"
    dogfood = evidence["latest_real_llm_revalidation"]
    assert dogfood["status"] == "passed"
    assert dogfood["candidate_count"] >= 1
    assert dogfood["terminal_status"] in {"governed_no_write", "governed_write"}
    assert dogfood["isolated_postgres_database_dropped"] is True
    rebase = evidence["latest_suite_contract_rebase"]
    assert rebase["suite_contract_fingerprint"] == suite.suite_contract_fingerprint
    assert (
        rebase["manual_compaction_scenario_contract_fingerprint"]
        == scenario.scenario_contract_fingerprint
    )
    assert rebase["manual_compaction_behavior_changed"] is False

    repository_root = DEFAULT_SUITE_ROOT.parents[3]
    implementation_spec = (
        repository_root
        / "PULSARA_POST_COMPACTION_MEMORY_EXTRACTION_HARD_CUT_IMPLEMENTATION.zh.md"
    ).read_text(encoding="utf-8")
    dod = implementation_spec.split("## 22. Definition of Done", 1)[1].split(
        "## 23. 最终不变量", 1
    )[0]
    assert "- [ ]" not in dod
    assert "D5 CLOSED" in implementation_spec.splitlines()[2]

    debt = (
        repository_root / "PULSARA_RUNTIME_ARCHITECTURE_DEBT_REBASE.zh.md"
    ).read_text(encoding="utf-8")
    assert "### D5：Compaction-memory extension（`CLOSED`）" in debt


def test_d6_dod_evidence_matches_ownership_hard_cut() -> None:
    evidence = json.loads(
        (DEFAULT_SUITE_ROOT / "d6_dod_evidence.json").read_text(encoding="utf-8")
    )
    suite = load_suite(DEFAULT_SUITE_ROOT)

    assert evidence["schema_version"] == "pulsara.d6-dod-evidence.v1"
    assert evidence["hard_cut"] == "D6-0-D6-5"
    assert evidence["debt_item"] == "D6"
    assert evidence["status"] == "passed"
    assert tuple(item["gate_id"] for item in evidence["gates"]) == tuple(
        f"D6-{index}" for index in range(6)
    )
    assert all(item["status"] == "passed" for item in evidence["gates"])
    pytest_evidence = evidence["pytest"]
    assert (
        pytest_evidence["baseline_full_collection"]
        + pytest_evidence["post_review_added_nodes"]["collected"]
        == pytest_evidence["collected"]
    )
    assert (
        pytest_evidence["post_review_added_nodes"]["passed"]
        == pytest_evidence["post_review_added_nodes"]["collected"]
    )
    assert pytest_evidence["post_review_added_nodes"]["failed"] == 0
    assert evidence["static_validation"]["forbidden_d4_observation_count"] == 0
    assert (
        evidence["static_validation"][
            "residual_cross_package_scc_observation_count"
        ]
        == 0
    )

    assert evidence["real_llm_dogfood"]["status"] == "passed"
    dogfood = evidence["latest_real_llm_revalidation"]
    assert dogfood["status"] == "passed"
    assert dogfood["passed_scenarios"] == len(suite.scenarios)
    assert dogfood["failed_scenarios"] == 0
    assert dogfood["isolated_postgres_databases_dropped"] is True
    subagent = evidence["latest_subagent_difficulty_revalidation"]
    scenario = next(
        item
        for item in suite.scenarios
        if item.contract.scenario_id == "subagent-delegation"
    )
    assert subagent["suite_contract_fingerprint"] == suite.suite_contract_fingerprint
    assert (
        subagent["scenario_contract_fingerprint"]
        == scenario.scenario_contract_fingerprint
    )
    assert subagent["runner_build_fingerprint"] == runner_build_fingerprint(
        DEFAULT_SUITE_ROOT.parents[1]
    )
    assert subagent["status"] == "passed"
    assert (
        subagent["difficulty_contract"]["observed_child_tool_calls"]
        >= subagent["difficulty_contract"]["minimum_child_tool_calls"]
    )
    assert subagent["initial_attempt"]["child_tool_gate_passed"] is True
    assert subagent["successful_rerun"]["hidden_verifier_passed"] is True
    assert subagent["isolated_postgres_databases_dropped"] is True

    remediation = evidence["post_review_remediation"]
    assert remediation["status"] == "passed"
    assert tuple(remediation["findings"]) == (
        "activation_driver_physical_exit_ownership",
        "child_timeout_child_run_terminalization_order",
        "canonical_bounded_final_output_materialization",
        "pending_interaction_exact_source_authority",
        "confirmed_run_end_finalization_snapshot",
    )
    assert all(item["failed"] == 0 for item in remediation["targeted_pytest"])
    real_llm_union = remediation["real_llm_union"]
    assert real_llm_union["status"] == "passed"
    assert real_llm_union["union_passed_scenarios"] == len(suite.scenarios)
    assert real_llm_union["union_failed_scenarios"] == 0
    assert real_llm_union["failed_scenario_rerun"]["status"] == "passed"

    repository_root = DEFAULT_SUITE_ROOT.parents[3]
    implementation_spec = (
        repository_root
        / "PULSARA_AGENT_RUNTIME_AND_HOST_SESSION_OWNERSHIP_HARD_CUT_IMPLEMENTATION.zh.md"
    ).read_text(encoding="utf-8")
    dod = implementation_spec.split("## 22. Definition of Done", 1)[1].split(
        "## 23. 最终裁决", 1
    )[0]
    assert "- [ ]" not in dod
    assert "D6 CLOSED" in implementation_spec.splitlines()[2]

    debt = (
        repository_root / "PULSARA_RUNTIME_ARCHITECTURE_DEBT_REBASE.zh.md"
    ).read_text(encoding="utf-8")
    assert "### D6：AgentRuntime/HostSession ownership 拆分（`CLOSED`）" in debt


def test_core_dogfood_suite_is_frozen_and_complete() -> None:
    suite = load_suite(DEFAULT_SUITE_ROOT)

    assert (
        tuple(item.contract.scenario_id for item in suite.scenarios)
        == EXPECTED_SCENARIOS
    )
    assert len(suite.suite_contract_fingerprint) == 64
    for scenario in suite.scenarios:
        assert len(scenario.scenario_contract_fingerprint) == 64
        assert scenario.file_inventory
        assert all(
            "verify.py" != item.path or item.size_bytes > 0
            for item in scenario.file_inventory
        )


def test_core_dogfood_runner_consumes_opaque_run_result() -> None:
    runner_source = (
        DEFAULT_SUITE_ROOT.parents[1] / "runner.py"
    ).read_text(encoding="utf-8")
    assert "result.state" not in runner_source
    assert "result.run_id" in runner_source


def test_core_dogfood_runner_accepts_plan_suspension_without_final_text() -> None:
    suspended = RunSuspendedOutcome.model_construct(
        owner_identity=SimpleNamespace(run_id="run:plan"),
        pending_interaction=SimpleNamespace(interaction_kind="plan"),
    )
    recorded: OrderedDict[str, str] = OrderedDict()
    progress: list[str] = []
    runner = object.__new__(CoreDogfoodRunner)
    runner.progress = progress.append

    class _SuspendingSession:
        async def run_turn(self, prompt: str):
            assert prompt == "plan"
            return suspended

    result = asyncio.run(
        runner._run_turn(
            _SuspendingSession(),
            "plan",
            recorded,
            "plan-workflow:plan",
            allow_suspension=True,
        )
    )

    assert result is suspended
    assert recorded == {}
    assert progress[-1] == "plan-workflow:plan: run SUSPENDED run_id=run:plan"
    _record_result(suspended, recorded)
    assert recorded == {}


def test_core_dogfood_runner_rejects_unexpected_suspension() -> None:
    suspended = RunSuspendedOutcome.model_construct(
        owner_identity=SimpleNamespace(run_id="run:workspace"),
        pending_interaction=SimpleNamespace(interaction_kind="approval"),
    )
    runner = object.__new__(CoreDogfoodRunner)
    runner.progress = lambda _message: None

    class _SuspendingSession:
        async def run_turn(self, prompt: str):
            return suspended

    with pytest.raises(RuntimeError, match="unexpectedly suspended"):
        asyncio.run(
            runner._run_turn(
                _SuspendingSession(),
                "workspace task",
                OrderedDict(),
                "workspace-patch",
            )
        )


def test_suite_detects_fixture_or_verifier_drift(tmp_path: Path) -> None:
    copied = tmp_path / "v1"
    shutil.copytree(DEFAULT_SUITE_ROOT, copied)
    target = copied / "scenarios" / "durable-resume" / "workdir" / "resume_source.txt"
    target.write_text("RESUME_TOKEN=DRIFTED\n", encoding="utf-8")

    with pytest.raises(DogfoodContractError, match="fingerprint drift"):
        load_suite(copied)


def test_hidden_verifiers_are_not_copied_into_model_workspaces(tmp_path: Path) -> None:
    suite = load_suite(DEFAULT_SUITE_ROOT)
    for scenario in suite.scenarios:
        workspace = tmp_path / scenario.contract.scenario_id
        _prepare_fixture(scenario, workspace)
        assert not (workspace / "verify.py").exists()


def test_all_hidden_verifiers_accept_known_good_outputs(tmp_path: Path) -> None:
    suite = load_suite(DEFAULT_SUITE_ROOT)
    for scenario in suite.scenarios:
        workspace = tmp_path / scenario.contract.scenario_id
        _prepare_fixture(scenario, workspace)
        _install_known_good_solution(scenario.contract.scenario_id, workspace)
        result = run_hidden_verifier(
            scenario_root=scenario.scenario_root,
            verifier_path=scenario.contract.verifier.path,
            workspace=workspace,
            timeout_seconds=scenario.contract.verifier.timeout_seconds,
        )
        assert result.passed, (scenario.contract.scenario_id, result.stderr)


def test_seed_workspaces_do_not_already_pass_hidden_verifiers(tmp_path: Path) -> None:
    suite = load_suite(DEFAULT_SUITE_ROOT)
    for scenario in suite.scenarios:
        workspace = tmp_path / scenario.contract.scenario_id
        _prepare_fixture(scenario, workspace)
        result = run_hidden_verifier(
            scenario_root=scenario.scenario_root,
            verifier_path=scenario.contract.verifier.path,
            workspace=workspace,
            timeout_seconds=scenario.contract.verifier.timeout_seconds,
        )
        assert not result.passed, scenario.contract.scenario_id


def test_cache_scenario_grader_requires_real_cache_and_balanced_lifecycle() -> None:
    suite = load_suite(DEFAULT_SUITE_ROOT)
    scenario = next(
        item.contract
        for item in suite.scenarios
        if item.contract.scenario_id == "cache-continuity"
    )
    counts = {
        "RUN_START": 3,
        "RUN_END": 3,
        "MODEL_CALL_START": 3,
        "MODEL_CALL_END": 3,
        "TOOL_CALL_START": 4,
        "TOOL_RESULT_END": 4,
        "PROVIDER_INPUT_GENERATION_STARTED": 1,
        "PROVIDER_INPUT_APPEND_COMMITTED": 3,
        "PROVIDER_INPUT_GENERATION_CLOSED": 1,
    }
    session_report = {
        "runs": [{"id": f"run:{index}", "status": "finished"} for index in range(3)],
        "event_counts": counts,
        "event_count": sum(counts.values()),
        "diagnostics": [],
        "model_usage_by_run": [
            {
                "run_id": f"run:{index}",
                "total_tokens": 100,
                "cached_input_tokens": 0 if index == 0 else 40,
                "reported_call_count": 1,
                "missing_usage_call_count": 0,
            }
            for index in range(3)
        ],
        "provider_input_generations": [
            {
                "generation_id": "generation:1",
                "rollover": None,
                "model_calls": [
                    {"cached_input_tokens": 0},
                    {"cached_input_tokens": 40},
                    {"cached_input_tokens": 40},
                ],
            }
        ],
    }
    root_reports = tuple(
        {
            "run": {"id": f"run:{index}", "status": "finished"},
            "timeline": {
                "items": [
                    {
                        "kind": "tool_call",
                        "metadata": {"tool_name": "write_file"},
                    }
                ]
                if index == 2
                else []
            },
        }
        for index in range(3)
    )
    verifier = HiddenVerifierResultFact(
        passed=True,
        exit_code=0,
        elapsed_seconds=0,
        stdout="ok",
        stderr="",
    )

    grade = grade_durable_evidence(
        scenario=scenario,
        session_report=session_report,
        root_run_reports=root_reports,
        final_texts=("one", "two", "three"),
        verifier=verifier,
    )
    assert grade.passed
    assert grade.total_tokens == 300
    assert grade.cached_input_tokens == 80
    assert all(
        item.assertion_id != "total_token_budget" for item in grade.assertions
    )

    session_report["model_usage_by_run"] = [
        {**item, "total_tokens": 10_000_000}
        for item in session_report["model_usage_by_run"]
    ]
    gross_usage_is_telemetry = grade_durable_evidence(
        scenario=scenario,
        session_report=session_report,
        root_run_reports=root_reports,
        final_texts=("one", "two", "three"),
        verifier=verifier,
    )
    assert gross_usage_is_telemetry.passed
    assert gross_usage_is_telemetry.total_tokens == 30_000_000

    session_report["provider_input_generations"] = [
        {
            "generation_id": "generation:1",
            "rollover": None,
            "model_calls": [
                {"cached_input_tokens": 80},
                {"cached_input_tokens": 0},
                {"cached_input_tokens": 0},
            ],
        }
    ]
    first_call_only = grade_durable_evidence(
        scenario=scenario,
        session_report=session_report,
        root_run_reports=root_reports,
        final_texts=("one", "two", "three"),
        verifier=verifier,
    )
    assert not first_call_only.passed

    session_report["model_usage_by_run"] = [
        {**item, "cached_input_tokens": 0}
        for item in session_report["model_usage_by_run"]
    ]
    missed = grade_durable_evidence(
        scenario=scenario,
        session_report=session_report,
        root_run_reports=root_reports,
        final_texts=("one", "two", "three"),
        verifier=verifier,
    )
    assert not missed.passed
    assert not next(
        item
        for item in missed.assertions
        if item.assertion_id == "provider_reported_positive_continuation_cache_hit"
    ).passed


def test_manual_compaction_grader_requires_exact_memory_extraction_chain() -> None:
    suite = load_suite(DEFAULT_SUITE_ROOT)
    scenario = next(
        item.contract
        for item in suite.scenarios
        if item.contract.scenario_id == "manual-compaction-trail"
    )
    counts = {
        "CONTEXT_COMPACTION_COMPLETED": 1,
        "CONTEXT_COMPACTION_MEMORY_EXTRACTION_COMPLETED": 1,
        "CONTEXT_COMPACTION_MEMORY_EXTRACTION_REQUESTED": 1,
        "MODEL_CALL_START": 4,
        "MODEL_CALL_END": 4,
        "RUN_START": 2,
        "RUN_END": 2,
        "TOOL_CALL_START": 7,
        "TOOL_RESULT_END": 7,
    }
    session_report = {
        "runs": [
            {"id": "run:discovery", "status": "finished"},
            {"id": "run:continuation", "status": "finished"},
        ],
        "event_counts": counts,
        "event_count": sum(counts.values()),
        "diagnostics": [],
        "model_usage_by_run": [
            {
                "run_id": "run:discovery",
                "total_tokens": 300,
                "cached_input_tokens": 0,
                "reported_call_count": 2,
                "missing_usage_call_count": 0,
            },
            {
                "run_id": "run:continuation",
                "total_tokens": 200,
                "cached_input_tokens": 0,
                "reported_call_count": 2,
                "missing_usage_call_count": 0,
            },
        ],
        "provider_input_generations": [
            {
                "generation_id": "generation:manual",
                "rollover": None,
                "model_calls": [
                    {"cached_input_tokens": 0},
                    {"cached_input_tokens": 0},
                    {"cached_input_tokens": 0},
                    {"cached_input_tokens": 0},
                ],
            }
        ],
        "compaction_memory_extraction_durable_status": [
            {
                "status": "governed_write",
                "completed_sequence": 50,
                "request": {"event_id": "request:1", "sequence": 51},
                "job": {"job_id": "job:1"},
                "model_lifecycle": [
                    {
                        "start": {"sequence": 65},
                        "end": {"sequence": 80},
                        "input": {
                            "status": "full",
                            "all_sources_direct_human": True,
                            "nodes": [
                                {
                                    "projection_kind": "full",
                                    "source_event_type": "RUN_START",
                                    "source_ingress_kind": "human",
                                }
                            ],
                        },
                    }
                ],
                "result": {"event_id": "result:1", "sequence": 90},
                "result_candidate": {
                    "job_id": "job:1",
                    "completed_event_id": "result:1",
                },
                "outbox": [
                    {"producer_event_id": "result:1", "status": "applied"}
                ],
                "candidates": [{"entry_id": "candidate:1"}],
            }
        ],
    }
    root_reports = (
        {
            "run": {"id": "run:discovery", "status": "finished"},
            "timeline": {
                "items": [
                    {"kind": "tool_call", "metadata": {"tool_name": "read_file"}}
                    for _ in range(6)
                ]
            },
            "events": [{"type": "RUN_START", "sequence": 1}],
        },
        {
            "run": {"id": "run:continuation", "status": "finished"},
            "timeline": {
                "items": [
                    {"kind": "tool_call", "metadata": {"tool_name": "write_file"}}
                ]
            },
            "events": [{"type": "RUN_START", "sequence": 70}],
        },
    )
    verifier = HiddenVerifierResultFact(
        passed=True,
        exit_code=0,
        elapsed_seconds=0,
        stdout="ok",
        stderr="",
    )

    grade = grade_durable_evidence(
        scenario=scenario,
        session_report=session_report,
        root_run_reports=root_reports,
        final_texts=("discovery", "continuation"),
        verifier=verifier,
    )
    assert grade.passed, tuple(
        (item.assertion_id, item.detail)
        for item in grade.assertions
        if not item.passed
    )


def test_subagent_grader_requires_durable_child_tool_count() -> None:
    suite = load_suite(DEFAULT_SUITE_ROOT)
    scenario = next(
        item.contract
        for item in suite.scenarios
        if item.contract.scenario_id == "subagent-delegation"
    )
    counts = {
        "MODEL_CALL_START": 2,
        "MODEL_CALL_END": 2,
        "RUN_START": 1,
        "RUN_END": 1,
        "SUBAGENT_RESULT_CONSUMED": 1,
        "SUBAGENT_RUN_COMPLETED": 1,
        "SUBAGENT_RUN_STARTED": 1,
        "SUBAGENT_TASK_COMPLETED": 1,
        "SUBAGENT_TASK_CREATED": 1,
        "TOOL_CALL_START": 3,
        "TOOL_RESULT_END": 3,
    }
    session_report = {
        "runs": [{"id": "run:parent", "status": "finished"}],
        "event_counts": counts,
        "event_count": sum(counts.values()),
        "events": [
            {
                "type": "SUBAGENT_RUN_COMPLETED",
                "subagent_run_id": "subagent:1",
                "tool_call_count": 10,
            }
        ],
        "diagnostics": [],
        "model_usage_by_run": [
            {
                "run_id": "run:parent",
                "total_tokens": 200,
                "cached_input_tokens": 0,
                "reported_call_count": 2,
                "missing_usage_call_count": 0,
            }
        ],
        "provider_input_generations": [
            {
                "generation_id": "generation:subagent",
                "rollover": None,
                "model_calls": [
                    {"cached_input_tokens": 0},
                    {"cached_input_tokens": 0},
                ],
            }
        ],
    }
    root_reports = (
        {
            "run": {"id": "run:parent", "status": "finished"},
            "timeline": {
                "items": [
                    {
                        "kind": "tool_call",
                        "metadata": {"tool_name": tool_name},
                    }
                    for tool_name in (
                        "create_agent_tasks",
                        "wait_agent_tasks",
                        "write_file",
                    )
                ]
            },
        },
    )
    verifier = HiddenVerifierResultFact(
        passed=True,
        exit_code=0,
        elapsed_seconds=0,
        stdout="ok",
        stderr="",
    )

    grade = grade_durable_evidence(
        scenario=scenario,
        session_report=session_report,
        root_run_reports=root_reports,
        final_texts=("done",),
        verifier=verifier,
    )
    assert grade.passed, tuple(
        (item.assertion_id, item.detail)
        for item in grade.assertions
        if not item.passed
    )

    session_report["events"][0]["tool_call_count"] = 9
    below_minimum = grade_durable_evidence(
        scenario=scenario,
        session_report=session_report,
        root_run_reports=root_reports,
        final_texts=("done",),
        verifier=verifier,
    )
    assert not next(
        item
        for item in below_minimum.assertions
        if item.assertion_id == "subagent_child_tool_call_minimum"
    ).passed


def test_core_dogfood_cli_validate_and_list_are_offline(capsys) -> None:
    assert main(["validate"]) == 0
    validate_output = capsys.readouterr().out
    assert "PASS suite=pulsara-core-dogfood-v1" in validate_output

    assert main(["list"]) == 0
    listed = capsys.readouterr().out
    for scenario_id in EXPECTED_SCENARIOS:
        assert scenario_id in listed


def test_core_dogfood_cli_requires_explicit_network_opt_in(monkeypatch, capsys) -> None:
    monkeypatch.delenv("PULSARA_RUN_CORE_DOGFOOD", raising=False)
    assert main(["run", "--confirm-network"]) == 2
    assert "set PULSARA_RUN_CORE_DOGFOOD=1" in capsys.readouterr().err


def _install_known_good_solution(scenario_id: str, root: Path) -> None:
    if scenario_id == "cache-continuity":
        for name, value in {
            "cache_round1.txt": "BLUE-EMBER-731|phase-one",
            "cache_round2.txt": "BLUE-EMBER-731|phase-two",
            "cache_final.txt": "BLUE-EMBER-731|phase-three",
        }.items():
            (root / name).write_text(value, encoding="utf-8")
        return
    if scenario_id == "durable-resume":
        (root / "before_resume.txt").write_text("ORCHID-RESUME-4421|before")
        (root / "after_resume.txt").write_text("ORCHID-RESUME-4421|after")
        return
    if scenario_id == "manual-compaction-trail":
        (root / "answer.txt").write_text("Asterford-Veylan")
        return
    if scenario_id == "subagent-delegation":
        (root / "child_trace.json").write_text(
            json.dumps(
                {
                    "chain": [
                        "data/start.txt",
                        "data/node-quartz.txt",
                        "data/node-ember.txt",
                        "data/node-lantern.txt",
                        "data/node-slate.txt",
                        "data/node-harbor.txt",
                        "data/node-maple.txt",
                        "data/node-crown.txt",
                    ],
                    "values": [13, 21, 34, 55, 89, 8, 3, 144],
                    "sum": 367,
                    "weighted_checksum": 2043,
                    "terminal_marker": "TRAIL_COMPLETE_V2",
                }
            )
        )
        (root / "result.txt").write_text("367:2043")
        return
    if scenario_id == "workspace-patch":
        (root / "retry_queue.py").write_text(
            "from dataclasses import dataclass\n"
            "from typing import Callable, Iterable\n"
            "@dataclass(frozen=True)\n"
            "class Result:\n"
            "    item: str\n"
            "    attempts: int\n"
            "    succeeded: bool\n"
            "def drain_with_retries(items: Iterable[str], *, max_retries: int, worker: Callable[[str], bool]):\n"
            "    if max_retries < 0: raise ValueError('max_retries')\n"
            "    output = []\n"
            "    for item in items:\n"
            "        succeeded = False\n"
            "        for attempt in range(1, max_retries + 2):\n"
            "            succeeded = worker(item)\n"
            "            if succeeded: break\n"
            "        output.append(Result(item, attempt, succeeded))\n"
            "    return output\n",
            encoding="utf-8",
        )
        (root / "PATCH_NOTES.md").write_text("RETRY_QUEUE_FIXED_V1")
        return
    if scenario_id == "plan-workflow":
        (root / "limiter.py").write_text(
            "class RateLimiter:\n"
            "    def __init__(self, limit):\n"
            "        if limit <= 0: raise ValueError('limit')\n"
            "        self.limit = limit\n"
            "        self.counts = {}\n"
            "    def allow(self, key):\n"
            "        used = self.counts.get(key, 0)\n"
            "        if used >= self.limit: return False\n"
            "        self.counts[key] = used + 1\n"
            "        return True\n"
            "    def reset(self, key):\n"
            "        self.counts.pop(key, None)\n",
            encoding="utf-8",
        )
        (root / "PLAN_DONE.md").write_text("PLAN_WORKFLOW_FIXED_V1")
        return
    raise AssertionError(f"missing known-good fixture for {scenario_id}")
