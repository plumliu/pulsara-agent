from __future__ import annotations

import json
import sys

import pytest

from pulsara_agent import cli
from pulsara_agent.llm import ModelRole
from pulsara_agent.llm.config import (
    DEFAULT_MODEL_CONTEXT_LIMITS,
    DEFAULT_OPENAI_API,
    LLMConfig,
    ModelSlotConfig,
)
from pulsara_agent.llm.factory import build_llm_runtime
from pulsara_agent.llm.provider import ProviderProfile
from pulsara_agent.primitives.model_call import ModelContextLimits
from pulsara_agent.runtime.long_horizon.feasibility import (
    PRODUCTION_SUBAGENT_PROFILE_IDS,
    ProductionRolloutBudgetConfigurationError,
    ProductionRolloutBudgetFeasibilityDrift,
    check_production_rollout_budget_configuration,
    evaluate_production_rollout_budget_feasibility,
    require_production_rollout_budget_configuration,
    require_prevalidated_production_rollout_pair,
    resolve_production_model_targets,
)
from pulsara_agent.settings import PulsaraSettings, StorageConfig


def _llm_config(
    *,
    pro_limits: ModelContextLimits = DEFAULT_MODEL_CONTEXT_LIMITS,
    flash_limits: ModelContextLimits = DEFAULT_MODEL_CONTEXT_LIMITS,
    pro_model_id: str = "model-pro",
) -> LLMConfig:
    return LLMConfig(
        api_key="super-secret-api-key",
        base_url="https://models.example.test/private-secret/v1",
        pro=ModelSlotConfig(model_id=pro_model_id, limits=pro_limits),
        flash=ModelSlotConfig(model_id="model-flash", limits=flash_limits),
        api=DEFAULT_OPENAI_API,
        provider="test-provider",
        provider_profile=ProviderProfile(
            id="test-provider",
            wire_api=DEFAULT_OPENAI_API,
        ),
    )


def _small_limits() -> ModelContextLimits:
    return ModelContextLimits(
        total_context_tokens=4_096,
        max_input_tokens=4_096,
        max_output_tokens=2_048,
        default_output_tokens=2_048,
        input_safety_margin_tokens=256,
    )


def test_production_rollout_matrix_enumerates_only_reachable_slot_pairs() -> None:
    runtime = build_llm_runtime(_llm_config())

    report = evaluate_production_rollout_budget_feasibility(
        targets_by_role=resolve_production_model_targets(runtime)
    )

    assert report.feasible is True
    assert report.infeasible_pairs == ()
    assert len(report.matrix) == 2 + 2 * len(PRODUCTION_SUBAGENT_PROFILE_IDS)
    assert {
        (row.execution_profile_kind, row.execution_profile_id, row.primary_target_slot)
        for row in report.matrix
    } == {
        ("host_root", "host_pro", "pro"),
        ("host_root", "host_flash", "flash"),
        *{
            ("subagent_child", profile_id, role.value)
            for profile_id in PRODUCTION_SUBAGENT_PROFILE_IDS
            for role in (ModelRole.PRO, ModelRole.FLASH)
        },
    }
    assert {row.summarizer_target_slot for row in report.matrix} == {"flash"}
    assert all(row.total_rollout_budget_milliunits > 0 for row in report.matrix)
    assert all(row.finalization_reserve_milliunits > 0 for row in report.matrix)
    assert all(row.exploration_allowance_milliunits > 0 for row in report.matrix)


def test_infeasible_primary_slot_reports_every_reachable_affected_pair() -> None:
    report = check_production_rollout_budget_configuration(
        _llm_config(pro_limits=_small_limits())
    )

    assert report.feasible is False
    assert len(report.infeasible_pairs) == 1 + len(PRODUCTION_SUBAGENT_PROFILE_IDS)
    assert {row.primary_target_slot for row in report.infeasible_pairs} == {"pro"}
    assert {
        row.reason_code for row in report.infeasible_pairs
    } == {"exploration_allowance_non_positive"}
    assert all(
        row.exploration_allowance_milliunits <= 0
        for row in report.infeasible_pairs
    )

    with pytest.raises(ProductionRolloutBudgetConfigurationError) as exc_info:
        require_production_rollout_budget_configuration(
            _llm_config(pro_limits=_small_limits())
        )
    assert exc_info.value.report == report


def test_config_check_outputs_full_redacted_matrix_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = PulsaraSettings(
        llm=_llm_config(pro_limits=_small_limits()),
        storage=StorageConfig(),
    )
    monkeypatch.setattr(
        PulsaraSettings,
        "from_env",
        classmethod(lambda cls, prefix="PULSARA": settings),
    )
    monkeypatch.setattr(sys, "argv", ["pulsara", "config-check"])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 2
    stdout = capsys.readouterr().out
    payload = json.loads(stdout)
    matrix = payload["rollout_budget_feasibility"]
    assert matrix["feasible"] is False
    assert len(matrix["matrix"]) == 10
    assert len(matrix["infeasible_pairs"]) == 5
    assert "super-secret-api-key" not in stdout
    assert "private-secret" not in stdout


def test_config_check_accepts_feasible_production_matrix(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = PulsaraSettings(
        llm=_llm_config(),
        storage=StorageConfig(),
    )
    monkeypatch.setattr(
        PulsaraSettings,
        "from_env",
        classmethod(lambda cls, prefix="PULSARA": settings),
    )
    monkeypatch.setattr(sys, "argv", ["pulsara", "config-check"])

    cli.main()

    payload = json.loads(capsys.readouterr().out)
    matrix = payload["rollout_budget_feasibility"]
    assert matrix["feasible"] is True
    assert matrix["infeasible_pairs"] == []
    assert len(matrix["matrix"]) == 10


def test_pre_run_pair_rechecks_same_policy_and_model_fingerprints() -> None:
    runtime = build_llm_runtime(_llm_config())
    targets = resolve_production_model_targets(runtime)
    report = evaluate_production_rollout_budget_feasibility(
        targets_by_role=targets
    )

    actual = require_prevalidated_production_rollout_pair(
        report=report,
        execution_profile_kind="host_root",
        execution_profile_id="host_pro",
        primary_target_slot="pro",
        primary_target=targets[ModelRole.PRO],
        summarizer_target_slot="flash",
        summarizer_target=targets[ModelRole.FLASH],
    )

    assert actual.result_fingerprint == next(
        row.result_fingerprint
        for row in report.matrix
        if row.execution_profile_kind == "host_root"
        and row.execution_profile_id == "host_pro"
    )


def test_pre_run_rejects_pair_that_drifted_since_configuration_validation() -> None:
    runtime = build_llm_runtime(_llm_config())
    targets = resolve_production_model_targets(runtime)
    report = evaluate_production_rollout_budget_feasibility(
        targets_by_role=targets
    )
    drifted_targets = resolve_production_model_targets(
        build_llm_runtime(_llm_config(pro_model_id="model-pro-rebound"))
    )

    with pytest.raises(
        ProductionRolloutBudgetFeasibilityDrift,
        match="rollout_budget_feasibility_drift",
    ):
        require_prevalidated_production_rollout_pair(
            report=report,
            execution_profile_kind="host_root",
            execution_profile_id="host_pro",
            primary_target_slot="pro",
            primary_target=drifted_targets[ModelRole.PRO],
            summarizer_target_slot="flash",
            summarizer_target=drifted_targets[ModelRole.FLASH],
        )
