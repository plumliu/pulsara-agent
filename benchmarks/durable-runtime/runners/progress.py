"""Low-interference operational progress reporting for long benchmarks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import re
import sys
from time import perf_counter
from typing import Callable, TextIO


_SAFE_REASON = re.compile(r"^[a-zA-Z0-9_.:-]{1,128}$")


@dataclass(frozen=True, slots=True)
class TrajectoryProgressToken:
    ordinal: int
    scenario_id: str
    case_id: str
    mode: str
    phase: str
    matrix_iteration: int
    started_at: float


class BenchmarkProgressReporter:
    """Emit bounded trajectory-level text and JSONL progress outside timers."""

    def __init__(
        self,
        *,
        total_trajectories: int,
        text_stream: TextIO | None = None,
        jsonl_path: Path | None = None,
        clock: Callable[[], float] = perf_counter,
    ) -> None:
        if total_trajectories < 1:
            raise ValueError("progress reporter requires at least one trajectory")
        self._total = total_trajectories
        self._stream = text_stream if text_stream is not None else sys.stderr
        self._jsonl_path = jsonl_path.expanduser().resolve() if jsonl_path else None
        self._clock = clock
        self._run_started_at = clock()
        self._started_count = 0
        self._completed_count = 0
        if self._jsonl_path is not None:
            self._jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            if self._jsonl_path.exists():
                raise FileExistsError(
                    f"progress log already exists: {self._jsonl_path}"
                )
            self._jsonl_path.touch()

    @property
    def completed_trajectories(self) -> int:
        return self._completed_count

    @property
    def total_trajectories(self) -> int:
        return self._total

    def start(
        self,
        *,
        scenario_id: str,
        case_id: str,
        mode: str,
        phase: str,
        matrix_iteration: int,
    ) -> TrajectoryProgressToken:
        if self._started_count >= self._total:
            raise ValueError("progress reporter exceeded its trajectory total")
        self._started_count += 1
        token = TrajectoryProgressToken(
            ordinal=self._started_count,
            scenario_id=scenario_id,
            case_id=case_id,
            mode=mode,
            phase=phase,
            matrix_iteration=matrix_iteration,
            started_at=self._clock(),
        )
        self._emit(
            event_kind="trajectory_started",
            token=token,
            trajectory_wall_seconds=None,
            eta_seconds=self._eta_seconds(),
            error_type=None,
            reason_code=None,
        )
        return token

    def passed(self, token: TrajectoryProgressToken) -> None:
        self._completed_count += 1
        if self._completed_count > self._started_count:
            raise ValueError("progress completion exceeded started trajectories")
        self._emit(
            event_kind="trajectory_passed",
            token=token,
            trajectory_wall_seconds=max(0.0, self._clock() - token.started_at),
            eta_seconds=self._eta_seconds(),
            error_type=None,
            reason_code=None,
        )

    def failed(
        self,
        token: TrajectoryProgressToken,
        error: BaseException,
    ) -> None:
        reason = getattr(error, "reason_code", None)
        if hasattr(reason, "value"):
            reason = reason.value
        reason_text = str(reason) if reason is not None else ""
        if not _SAFE_REASON.fullmatch(reason_text):
            reason_text = "benchmark_trajectory_failed"
        error_type = type(error).__name__[:128] or "BaseException"
        self._emit(
            event_kind="trajectory_failed",
            token=token,
            trajectory_wall_seconds=max(0.0, self._clock() - token.started_at),
            eta_seconds=None,
            error_type=error_type,
            reason_code=reason_text,
        )

    def _eta_seconds(self) -> float | None:
        if self._completed_count == 0:
            return None
        elapsed = max(0.0, self._clock() - self._run_started_at)
        remaining = self._total - self._completed_count
        return elapsed / self._completed_count * remaining

    def _emit(
        self,
        *,
        event_kind: str,
        token: TrajectoryProgressToken,
        trajectory_wall_seconds: float | None,
        eta_seconds: float | None,
        error_type: str | None,
        reason_code: str | None,
    ) -> None:
        elapsed = max(0.0, self._clock() - self._run_started_at)
        payload = {
            "schema_version": "pulsara.context-benchmark-progress.v1",
            "event_kind": event_kind,
            "ordinal": token.ordinal,
            "total_trajectories": self._total,
            "completed_trajectories": self._completed_count,
            "scenario_id": token.scenario_id,
            "case_id": token.case_id,
            "mode": token.mode,
            "phase": token.phase,
            "matrix_iteration": token.matrix_iteration,
            "trajectory_wall_seconds": trajectory_wall_seconds,
            "cumulative_wall_seconds": elapsed,
            "eta_seconds": eta_seconds,
            "error_type": error_type,
            "reason_code": reason_code,
            "emitted_at_utc": datetime.now(UTC).isoformat(),
        }
        label = {
            "trajectory_started": "START",
            "trajectory_passed": "PASS",
            "trajectory_failed": "FAIL",
        }[event_kind]
        fields = [
            f"[context-baseline] {label}",
            f"{token.ordinal}/{self._total}",
            f"scenario={token.scenario_id}",
            f"mode={token.mode}",
            f"phase={token.phase}",
            f"iteration={token.matrix_iteration + 1}",
        ]
        if trajectory_wall_seconds is not None:
            fields.append(f"trajectory={trajectory_wall_seconds:.2f}s")
        fields.append(f"cumulative={_format_duration(elapsed)}")
        if eta_seconds is not None:
            fields.append(f"eta={_format_duration(eta_seconds)}")
        if error_type is not None:
            fields.extend(
                (f"error_type={error_type}", f"reason={reason_code}")
            )
        print(" ".join(fields), file=self._stream, flush=True)
        if self._jsonl_path is not None:
            with self._jsonl_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        payload,
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
                handle.flush()


def _format_duration(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


__all__ = ["BenchmarkProgressReporter", "TrajectoryProgressToken"]
