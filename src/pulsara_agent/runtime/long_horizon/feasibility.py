"""Static production model-pair rollout-budget feasibility checks."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, model_validator

from pulsara_agent.llm import ModelRole
from pulsara_agent.llm.config import LLMConfig
from pulsara_agent.llm.factory import build_llm_runtime
from pulsara_agent.llm.resolution import ResolvedModelTarget
from pulsara_agent.primitives.long_horizon import (
    RolloutBudgetFeasibilityResult,
    RolloutBudgetPolicyFact,
    default_rollout_budget_policy,
    evaluate_rollout_budget_feasibility,
)
from pulsara_agent.primitives.model_call import ResolvedModelTargetFact


PRODUCTION_SUBAGENT_PROFILE_IDS = (
    "general_worker",
    "research_worker",
    "review_worker",
    "verification_worker",
)


class ProductionModelTargetResolver(Protocol):
    def resolve_target(self, *, role: ModelRole) -> ResolvedModelTarget: ...


@dataclass(frozen=True, slots=True)
class ProductionRolloutTargetPair:
    execution_profile_kind: Literal["host_root", "subagent_child"]
    execution_profile_id: str
    primary_role: ModelRole
    summarizer_role: ModelRole = ModelRole.FLASH


class ProductionRolloutBudgetFeasibilityReport(BaseModel):
    """Redacted, deterministic configuration-doctor output."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["production_rollout_budget_feasibility.v1"] = (
        "production_rollout_budget_feasibility.v1"
    )
    policy_fingerprint: str
    matrix: tuple[RolloutBudgetFeasibilityResult, ...]
    infeasible_pairs: tuple[RolloutBudgetFeasibilityResult, ...]
    feasible: bool

    @model_validator(mode="after")
    def _matrix_consistency(self) -> "ProductionRolloutBudgetFeasibilityReport":
        expected = tuple(row for row in self.matrix if not row.feasible)
        if self.infeasible_pairs != expected:
            raise ValueError("rollout feasibility report has inconsistent failed pairs")
        if self.feasible != (not expected):
            raise ValueError("rollout feasibility report has inconsistent status")
        if any(row.policy_fingerprint != self.policy_fingerprint for row in self.matrix):
            raise ValueError("rollout feasibility matrix mixes policy identities")
        return self


class ProductionRolloutBudgetConfigurationError(ValueError):
    """At least one enabled production model pair cannot preserve finalization."""

    def __init__(self, report: ProductionRolloutBudgetFeasibilityReport) -> None:
        self.report = report
        pairs = ", ".join(
            (
                f"{row.execution_profile_kind}:{row.execution_profile_id} "
                f"{row.primary_target_slot}->{row.summarizer_target_slot} "
                f"({row.reason_code})"
            )
            for row in report.infeasible_pairs
        )
        super().__init__(f"production rollout budget configuration is infeasible: {pairs}")


class ProductionRolloutBudgetFeasibilityDrift(RuntimeError):
    """The actual PRE_RUN pair differs from the configuration-validated pair."""

    reason_code = "rollout_budget_feasibility_drift"


def production_rollout_target_pairs() -> tuple[ProductionRolloutTargetPair, ...]:
    """Enumerate model pairs reachable through current production selection.

    Host runs may select either configured role. Child runtimes inherit that
    primary role, while same-run window compaction always resolves the flash
    slot. There is no production fallback route or profile-specific model slot
    today, so a Cartesian product of all slots would overstate the surface.
    """

    root_pairs = tuple(
        ProductionRolloutTargetPair(
            execution_profile_kind="host_root",
            execution_profile_id=f"host_{role.value}",
            primary_role=role,
        )
        for role in (ModelRole.PRO, ModelRole.FLASH)
    )
    child_pairs = tuple(
        ProductionRolloutTargetPair(
            execution_profile_kind="subagent_child",
            execution_profile_id=profile_id,
            primary_role=role,
        )
        for profile_id in PRODUCTION_SUBAGENT_PROFILE_IDS
        for role in (ModelRole.PRO, ModelRole.FLASH)
    )
    return (*root_pairs, *child_pairs)


def resolve_production_model_targets(
    resolver: ProductionModelTargetResolver,
) -> dict[ModelRole, ResolvedModelTargetFact]:
    """Resolve each production slot once through the canonical runtime path."""

    return {
        role: resolver.resolve_target(role=role).fact
        for role in (ModelRole.PRO, ModelRole.FLASH)
    }


def evaluate_production_rollout_budget_feasibility(
    *,
    targets_by_role: Mapping[ModelRole, ResolvedModelTargetFact],
    policy: RolloutBudgetPolicyFact | None = None,
) -> ProductionRolloutBudgetFeasibilityReport:
    """Evaluate every reachable production pair without duplicating formulas."""

    effective_policy = policy or default_rollout_budget_policy()
    missing = tuple(role.value for role in ModelRole if role not in targets_by_role)
    if missing:
        raise ValueError(
            "production rollout feasibility is missing model slots: "
            + ", ".join(missing)
        )
    matrix = tuple(
        evaluate_rollout_budget_feasibility(
            execution_profile_kind=pair.execution_profile_kind,
            execution_profile_id=pair.execution_profile_id,
            primary_target_slot=pair.primary_role.value,
            primary_target=targets_by_role[pair.primary_role],
            summarizer_target_slot=pair.summarizer_role.value,
            summarizer_target=targets_by_role[pair.summarizer_role],
            policy=effective_policy,
        )
        for pair in production_rollout_target_pairs()
    )
    infeasible = tuple(row for row in matrix if not row.feasible)
    return ProductionRolloutBudgetFeasibilityReport(
        policy_fingerprint=effective_policy.policy_fingerprint,
        matrix=matrix,
        infeasible_pairs=infeasible,
        feasible=not infeasible,
    )


def check_production_rollout_budget_configuration(
    config: LLMConfig,
    *,
    policy: RolloutBudgetPolicyFact | None = None,
    resolver: ProductionModelTargetResolver | None = None,
) -> ProductionRolloutBudgetFeasibilityReport:
    """Resolve configured slots and return the complete doctor matrix."""

    runtime = resolver or build_llm_runtime(config)
    return evaluate_production_rollout_budget_feasibility(
        targets_by_role=resolve_production_model_targets(runtime),
        policy=policy,
    )


def require_production_rollout_budget_configuration(
    config: LLMConfig,
    *,
    policy: RolloutBudgetPolicyFact | None = None,
    resolver: ProductionModelTargetResolver | None = None,
) -> ProductionRolloutBudgetFeasibilityReport:
    """Fail closed before accepting production work when any pair is invalid."""

    report = check_production_rollout_budget_configuration(
        config,
        policy=policy,
        resolver=resolver,
    )
    if not report.feasible:
        raise ProductionRolloutBudgetConfigurationError(report)
    return report


def require_prevalidated_production_rollout_pair(
    *,
    report: ProductionRolloutBudgetFeasibilityReport,
    execution_profile_kind: Literal["host_root", "subagent_child"],
    execution_profile_id: str,
    primary_target_slot: str,
    primary_target: ResolvedModelTargetFact,
    summarizer_target_slot: str,
    summarizer_target: ResolvedModelTargetFact,
    policy: RolloutBudgetPolicyFact | None = None,
) -> RolloutBudgetFeasibilityResult:
    """Re-evaluate one actual pair and require its configuration-time identity."""

    effective_policy = policy or default_rollout_budget_policy()
    actual = evaluate_rollout_budget_feasibility(
        execution_profile_kind=execution_profile_kind,
        execution_profile_id=execution_profile_id,
        primary_target_slot=primary_target_slot,
        primary_target=primary_target,
        summarizer_target_slot=summarizer_target_slot,
        summarizer_target=summarizer_target,
        policy=effective_policy,
    )
    expected = tuple(
        row
        for row in report.matrix
        if row.execution_profile_kind == execution_profile_kind
        and row.execution_profile_id == execution_profile_id
        and row.primary_target_slot == primary_target_slot
        and row.summarizer_target_slot == summarizer_target_slot
    )
    if (
        len(expected) != 1
        or report.policy_fingerprint != effective_policy.policy_fingerprint
        or expected[0].result_fingerprint != actual.result_fingerprint
    ):
        raise ProductionRolloutBudgetFeasibilityDrift(
            "rollout_budget_feasibility_drift: actual model pair or policy "
            "differs from the configuration-validated matrix"
        )
    if not actual.feasible:
        raise ProductionRolloutBudgetConfigurationError(
            ProductionRolloutBudgetFeasibilityReport(
                policy_fingerprint=effective_policy.policy_fingerprint,
                matrix=(actual,),
                infeasible_pairs=(actual,),
                feasible=False,
            )
        )
    return actual


__all__ = [
    "PRODUCTION_SUBAGENT_PROFILE_IDS",
    "ProductionRolloutBudgetConfigurationError",
    "ProductionRolloutBudgetFeasibilityDrift",
    "ProductionRolloutBudgetFeasibilityReport",
    "ProductionRolloutTargetPair",
    "check_production_rollout_budget_configuration",
    "evaluate_production_rollout_budget_feasibility",
    "production_rollout_target_pairs",
    "require_production_rollout_budget_configuration",
    "require_prevalidated_production_rollout_pair",
    "resolve_production_model_targets",
]
