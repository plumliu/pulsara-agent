from __future__ import annotations

import importlib.util
import json
import hashlib
import sys
from pathlib import Path


_RUNNER_PATH = Path(__file__).resolve().parents[1] / "evals" / "recall" / "runner.py"
_SPEC = importlib.util.spec_from_file_location("recall_eval_runner", _RUNNER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
runner = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = runner
_SPEC.loader.exec_module(runner)


def test_recall_eval_runner_loads_fixture_cases() -> None:
    cases = runner.load_cases(Path("evals/recall/fixtures/v1_golden.jsonl"))

    assert len(cases) == 1
    assert cases[0].case_id == "pref-concise-recall"
    assert cases[0].expected_included == ("preference:fixture-concise",)


def test_recall_eval_runner_reports_expected_hit_rate_for_fixture() -> None:
    cases = runner.load_cases(Path("evals/recall/fixtures/v1_golden.jsonl"))
    report = runner.run_eval(cases)

    assert report.case_count == 1
    assert report.included_hit_rate == 1.0
    assert report.excluded_leak_count == 0
    assert report.superseded_leak_count == 0
    assert report.confabulation_count == 0
    assert report.gate_passed


def test_recall_eval_runner_gate_entrypoint_exits_cleanly_without_blocking_floor() -> None:
    exit_code = runner.main(["--fixture", "evals/recall/fixtures/v1_golden.jsonl", "--gate", "--json"])

    assert exit_code == 0


def test_recall_eval_floor_matches_fixture_hash() -> None:
    fixture = Path("evals/recall/fixtures/v1_golden.jsonl")
    floor = json.loads(Path("evals/recall/baseline/v1_floor.json").read_text(encoding="utf-8"))
    config = Path("evals/recall/config.yaml").read_text(encoding="utf-8")
    digest = hashlib.sha256(fixture.read_bytes()).hexdigest()

    assert floor["golden_set_sha"] == f"sha256:{digest}"
    assert f"golden_set_sha: \"sha256:{digest}\"" in config
