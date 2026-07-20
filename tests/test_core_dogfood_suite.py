from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from benchmarks.suites.contracts import (
    DogfoodContractError,
    HiddenVerifierResultFact,
    load_suite,
)
from benchmarks.suites.graders import grade_durable_evidence, run_hidden_verifier
from benchmarks.suites.run_core_dogfood import DEFAULT_SUITE_ROOT, main
from benchmarks.suites.runner import _prepare_fixture


EXPECTED_SCENARIOS = (
    "cache-continuity",
    "durable-resume",
    "manual-compaction-trail",
    "plan-workflow",
    "subagent-delegation",
    "workspace-patch",
)


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
    assert grade.cached_input_tokens == 80

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
        (root / "result.txt").write_text("86")
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
