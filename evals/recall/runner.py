"""Golden-set runner for memory recall.

Phase 0/1 keeps this intentionally small: it exercises the recall pipeline over
versioned fixtures and provides a gate skeleton before the frozen v1 floor is
available.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pulsara_agent.memory.explain import explain_memory
from pulsara_agent.memory.query import CanonicalNodeView
from pulsara_agent.memory.recall import LexicalMemoryRecallService, RecallQuery
from pulsara_agent.ontology import memory


DEFAULT_FIXTURE = Path(__file__).with_name("fixtures") / "v1_golden.jsonl"
DEFAULT_FLOOR = Path(__file__).with_name("baseline") / "v1_floor.json"


@dataclass(frozen=True, slots=True)
class RecallEvalCase:
    case_id: str
    seed_memory: tuple[dict[str, Any], ...]
    query: str
    expected_included: tuple[str, ...]
    expected_excluded: tuple[str, ...]
    latency_budget_ms: int
    projection_char_budget: int
    must_have_warning: bool = False


@dataclass(frozen=True, slots=True)
class RecallEvalReport:
    case_count: int
    included_hit_rate: float
    excluded_leak_count: int
    rejected_leak_count: int
    superseded_leak_count: int
    confabulation_count: int
    p95_latency_ms: float
    projection_over_budget_count: int
    failures: tuple[str, ...]
    informational: tuple[str, ...]

    @property
    def gate_passed(self) -> bool:
        return not self.failures


class FixtureMemoryQuery:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.views = {_view_from_fixture(row): row for row in rows}
        self.by_id = {view.id: view for view in self.views}

    def fetch_nodes(self, ids, *, graph_id: str | None = None) -> list[CanonicalNodeView]:
        return [self.by_id[node_id] for node_id in ids if node_id in self.by_id]

    def lexical_candidates(
        self,
        *,
        terms,
        scopes,
        types,
        limit,
        graph_id: str | None = None,
    ) -> list[tuple[str, float]]:
        return self._candidates(terms=terms, scopes=scopes, types=types, limit=limit)

    def fts_candidates(
        self,
        *,
        query_text,
        scopes,
        types,
        limit,
        graph_id: str | None = None,
    ) -> list[tuple[str, float]]:
        terms = tuple(term for term in query_text.casefold().split() if len(term) >= 2)
        return self._candidates(terms=terms, scopes=scopes, types=types, limit=limit)

    def _candidates(self, *, terms, scopes, types, limit) -> list[tuple[str, float]]:
        scored: list[tuple[str, float]] = []
        scope_set = set(scopes or ())
        type_set = set(types or ())
        for view in self.by_id.values():
            if scope_set and view.scope not in scope_set:
                continue
            if type_set and view.memory_type not in type_set:
                continue
            haystack = " ".join(
                [
                    view.id,
                    view.memory_type,
                    view.scope,
                    view.statement,
                    view.summary or "",
                    view.applies_when or "",
                    view.do_not_apply_when or "",
                ]
            ).casefold()
            score = sum(1 for term in terms if term.casefold() in haystack)
            if score:
                scored.append((view.id, float(score)))
        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored[:limit]


def run_eval(cases: list[RecallEvalCase], *, floor: dict[str, Any] | None = None) -> RecallEvalReport:
    included_hits = 0
    included_total = 0
    excluded_leaks = 0
    rejected_leaks = 0
    superseded_leaks = 0
    confabulations = 0
    projection_over_budget = 0
    latencies: list[float] = []
    failures: list[str] = []
    informational: list[str] = []

    for case in cases:
        service = LexicalMemoryRecallService(FixtureMemoryQuery(list(case.seed_memory)))
        started = time.perf_counter()
        result = asyncio.run(
            service.recall(
                RecallQuery(
                    text=case.query,
                    scopes=tuple(
                        sorted(
                            {
                                str(row["scope"])
                                for row in case.seed_memory
                                if isinstance(row.get("scope"), str)
                            }
                        )
                    ),
                    limit=5,
                )
            )
        )
        latency_ms = (time.perf_counter() - started) * 1000
        latencies.append(latency_ms)
        included_ids = {item.memory_id for item in result.items}
        included_total += len(case.expected_included)
        included_hits += sum(1 for memory_id in case.expected_included if memory_id in included_ids)
        leaked = sorted(memory_id for memory_id in case.expected_excluded if memory_id in included_ids)
        excluded_leaks += len(leaked)
        if leaked:
            failures.append(f"{case.case_id}: expected excluded ids leaked: {leaked}")
        missing = sorted(memory_id for memory_id in case.expected_included if memory_id not in included_ids)
        if missing:
            failures.append(f"{case.case_id}: expected included ids missing: {missing}")
        rejected = [
            item.memory_id
            for item in result.items
            if item.status is memory.NodeStatus.REJECTED
        ]
        rejected_leaks += len(rejected)
        if rejected:
            failures.append(f"{case.case_id}: rejected ids leaked: {rejected}")
        superseded = [
            item.memory_id
            for item in result.items
            if item.status is memory.NodeStatus.SUPERSEDED
        ]
        superseded_leaks += len(superseded)
        if superseded:
            failures.append(f"{case.case_id}: superseded ids leaked: {superseded}")
        views = {view.id: view for view in service.memory_query.fetch_nodes([item.memory_id for item in result.items])}
        for item in result.items:
            try:
                explain_memory(views[item.memory_id], signals=item.why)
            except Exception as exc:
                confabulations += 1
                failures.append(f"{case.case_id}: ungrounded explanation for {item.memory_id}: {exc}")
        if latency_ms > case.latency_budget_ms:
            failures.append(
                f"{case.case_id}: latency {latency_ms:.2f}ms exceeded budget {case.latency_budget_ms}ms"
            )
        projection_chars = sum(len(item.snippet) for item in result.items)
        if projection_chars > case.projection_char_budget:
            projection_over_budget += 1
            failures.append(
                f"{case.case_id}: projection chars {projection_chars} exceeded budget {case.projection_char_budget}"
            )

    included_hit_rate = (included_hits / included_total) if included_total else 1.0
    if floor is None:
        informational.append("v1_floor.json not loaded; quality floor check is informational.")
    else:
        floor_metrics = floor.get("metrics", {})
        floor_hit_rate = float(floor_metrics.get("included_hit_rate", 0.0))
        if included_hit_rate < floor_hit_rate:
            failures.append(
                f"included_hit_rate {included_hit_rate:.4f} fell below frozen floor {floor_hit_rate:.4f}"
            )

    return RecallEvalReport(
        case_count=len(cases),
        included_hit_rate=included_hit_rate,
        excluded_leak_count=excluded_leaks,
        rejected_leak_count=rejected_leaks,
        superseded_leak_count=superseded_leaks,
        confabulation_count=confabulations,
        p95_latency_ms=_p95(latencies),
        projection_over_budget_count=projection_over_budget,
        failures=tuple(failures),
        informational=tuple(informational),
    )


def load_cases(path: Path) -> list[RecallEvalCase]:
    cases: list[RecallEvalCase] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        payload = json.loads(stripped)
        try:
            cases.append(
                RecallEvalCase(
                    case_id=str(payload["case_id"]),
                    seed_memory=tuple(payload["seed_memory"]),
                    query=str(payload["query"]),
                    expected_included=tuple(payload.get("expected_included", ())),
                    expected_excluded=tuple(payload.get("expected_excluded", ())),
                    latency_budget_ms=int(payload.get("latency_budget_ms", 500)),
                    projection_char_budget=int(payload.get("projection_char_budget", 1200)),
                    must_have_warning=bool(payload.get("must_have_warning", False)),
                )
            )
        except KeyError as exc:
            raise ValueError(f"{path}:{line_number}: missing required field {exc}") from exc
    return cases


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run memory recall golden-set evals.")
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--floor", type=Path, default=DEFAULT_FLOOR)
    parser.add_argument("--gate", action="store_true", help="Exit non-zero when blocking gates fail.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args(argv)

    report = run_eval(load_cases(args.fixture), floor=_load_floor(args.floor))
    payload = {
        "case_count": report.case_count,
        "included_hit_rate": report.included_hit_rate,
        "excluded_leak_count": report.excluded_leak_count,
        "rejected_leak_count": report.rejected_leak_count,
        "superseded_leak_count": report.superseded_leak_count,
        "confabulation_count": report.confabulation_count,
        "p95_latency_ms": report.p95_latency_ms,
        "projection_over_budget_count": report.projection_over_budget_count,
        "gate_passed": report.gate_passed,
        "failures": list(report.failures),
        "informational": list(report.informational),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("Memory recall eval")
        for key, value in payload.items():
            print(f"{key}: {value}")
    return 1 if args.gate and not report.gate_passed else 0


def _load_floor(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _view_from_fixture(row: dict[str, Any]) -> CanonicalNodeView:
    now = _parse_datetime(row.get("updated_at")) or datetime.now(UTC)
    status = memory.NodeStatus(str(row.get("status", memory.NodeStatus.ACTIVE.value)))
    return CanonicalNodeView(
        id=str(row["id"]),
        memory_type=str(row["memory_type"]),
        scope=str(row["scope"]),
        status=status,
        statement=str(row["statement"]),
        summary=_optional_str(row.get("summary")),
        source_authority=_optional_enum(memory.SourceAuthority, row.get("source_authority")),
        verification_status=_optional_enum(memory.VerificationStatus, row.get("verification_status")),
        confidence_level=_optional_enum(memory.ConfidenceLevel, row.get("confidence_level")),
        applies_when=_optional_str(row.get("applies_when")),
        do_not_apply_when=_optional_str(row.get("do_not_apply_when")),
        created_at=_parse_datetime(row.get("created_at")) or now,
        updated_at=now,
        evidence_ids=tuple(row.get("evidence_ids", ())),
        outgoing=tuple(tuple(item) for item in row.get("outgoing", ())),
        incoming=tuple(tuple(item) for item in row.get("incoming", ())),
    )


def _optional_enum(enum_type, value):
    if value is None:
        return None
    return enum_type(str(value))


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[94]


if __name__ == "__main__":
    raise SystemExit(main())
