from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

import yaml

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"


@dataclass(frozen=True, slots=True)
class RelatednessEvalCase:
    case_id: str
    slice: str
    query: str
    canonical_memories: tuple[dict[str, str], ...]
    relevant_ids: tuple[str, ...]
    allowed_destructive_actions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RelatednessEvalReport:
    case_count: int
    recall_at_k: float
    miss_rate: float
    slice_recall_at_k: dict[str, float]
    mean_candidate_count: float
    irrelevant_candidate_count: int
    destructive_action_eval_case_count: int
    destructive_action_false_positive_count: int
    destructive_action_gate_evaluated: bool
    gate_passed: bool


def load_cases(path: Path) -> tuple[RelatednessEvalCase, ...]:
    cases: list[RelatednessEvalCase] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        cases.append(
            RelatednessEvalCase(
                case_id=payload["case_id"],
                slice=payload["slice"],
                query=payload["query"],
                canonical_memories=tuple(payload["canonical_memories"]),
                relevant_ids=tuple(payload["relevant_ids"]),
                allowed_destructive_actions=tuple(
                    payload.get("allowed_destructive_actions", ())
                ),
            )
        )
    return tuple(cases)


@dataclass(frozen=True, slots=True)
class EvalGates:
    overall_recall_at_k_min: float = 0.95
    positive_slice_recall_at_k_min: float = 0.90
    overall_miss_rate_max: float = 0.05
    destructive_action_false_positive_max: int = 0


DEFAULT_GATES = EvalGates()


@dataclass(frozen=True, slots=True)
class EvalConfig:
    gates: EvalGates
    candidate_limit: int


def load_config(path: Path = _DEFAULT_CONFIG_PATH) -> EvalConfig:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw_gates = payload.get("gates") or {}
    gates = EvalGates(
        overall_recall_at_k_min=float(
            raw_gates.get("overall_recall_at_k_min", DEFAULT_GATES.overall_recall_at_k_min)
        ),
        positive_slice_recall_at_k_min=float(
            raw_gates.get("positive_slice_recall_at_k_min", DEFAULT_GATES.positive_slice_recall_at_k_min)
        ),
        overall_miss_rate_max=float(
            raw_gates.get("overall_miss_rate_max", DEFAULT_GATES.overall_miss_rate_max)
        ),
        destructive_action_false_positive_max=int(
            raw_gates.get(
                "destructive_action_false_positive_max",
                DEFAULT_GATES.destructive_action_false_positive_max,
            )
        ),
    )
    candidate_limit = int(payload.get("candidate_limit", 5))
    return EvalConfig(gates=gates, candidate_limit=candidate_limit)


def evaluate_predictions(
    cases: Sequence[RelatednessEvalCase],
    candidate_predictions: dict[str, Sequence[str]],
    *,
    k: int,
    gates: EvalGates = DEFAULT_GATES,
    destructive_action_predictions: dict[str, Sequence[str]] | None = None,
) -> RelatednessEvalReport:
    if not cases:
        raise ValueError("relatedness eval requires at least one case")
    hits = 0
    candidate_count = 0
    irrelevant_count = 0
    slice_hits: dict[str, int] = {}
    slice_totals: dict[str, int] = {}
    destructive_false_positives = 0
    for case in cases:
        predicted = tuple(candidate_predictions.get(case.case_id, ()))[:k]
        relevant = set(case.relevant_ids)
        hit = bool(relevant.intersection(predicted))
        hits += int(hit)
        candidate_count += len(predicted)
        irrelevant_count += sum(memory_id not in relevant for memory_id in predicted)
        slice_hits[case.slice] = slice_hits.get(case.slice, 0) + int(hit)
        slice_totals[case.slice] = slice_totals.get(case.slice, 0) + 1
        if destructive_action_predictions is not None:
            allowed_actions = set(case.allowed_destructive_actions)
            destructive_false_positives += sum(
                action not in allowed_actions
                for action in destructive_action_predictions.get(case.case_id, ())
                if action in {"contradict_and_submit", "supersede_and_submit"}
            )
    recall = hits / len(cases)
    slice_recall = {
        name: slice_hits.get(name, 0) / total for name, total in slice_totals.items()
    }
    miss_rate = 1.0 - recall
    destructive_gate_evaluated = destructive_action_predictions is not None
    gate_passed = (
        recall >= gates.overall_recall_at_k_min
        and miss_rate <= gates.overall_miss_rate_max
        and destructive_gate_evaluated
        and destructive_false_positives <= gates.destructive_action_false_positive_max
        and all(
            value >= gates.positive_slice_recall_at_k_min
            for name, value in slice_recall.items()
            if name != "hard_negative"
        )
    )
    return RelatednessEvalReport(
        case_count=len(cases),
        recall_at_k=recall,
        miss_rate=miss_rate,
        slice_recall_at_k=slice_recall,
        mean_candidate_count=candidate_count / len(cases),
        irrelevant_candidate_count=irrelevant_count,
        destructive_action_eval_case_count=(
            len(cases) if destructive_gate_evaluated else 0
        ),
        destructive_action_false_positive_count=destructive_false_positives,
        destructive_action_gate_evaluated=destructive_gate_evaluated,
        gate_passed=gate_passed,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=_DEFAULT_CONFIG_PATH)
    parser.add_argument("-k", type=int, default=None)
    parser.add_argument("--gate", action="store_true")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    # Default eval k to the production candidate_limit so the gated recall
    # number is genuinely recall@candidate_limit, not recall@<arbitrary>.
    k = args.k if args.k is not None else config.candidate_limit
    cases = load_cases(args.fixture)
    prediction_payload = json.loads(args.predictions.read_text(encoding="utf-8"))
    if "candidate_predictions" in prediction_payload:
        candidate_predictions = prediction_payload["candidate_predictions"]
        destructive_predictions = prediction_payload.get("destructive_action_predictions")
    else:
        # Backward-compatible report mode. A legacy candidate-only payload can
        # still be inspected, but --gate fails closed because planner-action
        # precision was not evaluated.
        candidate_predictions = prediction_payload
        destructive_predictions = None
    report = evaluate_predictions(
        cases,
        candidate_predictions,
        k=k,
        gates=config.gates,
        destructive_action_predictions=destructive_predictions,
    )
    print(json.dumps(asdict(report), ensure_ascii=False, sort_keys=True))
    return int(args.gate and not report.gate_passed)


if __name__ == "__main__":
    raise SystemExit(main())
