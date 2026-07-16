"""Crash-visible journals for long context benchmark runs."""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping


_SAFE_REASON = re.compile(r"^[a-zA-Z0-9_.:-]{1,128}$")


def _json_line(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        indent=2,
    ).encode("utf-8")
    with temporary.open("wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _bounded_failure(error: BaseException) -> tuple[str, str]:
    error_type = type(error).__name__[:128] or "BaseException"
    reason = getattr(error, "reason_code", None)
    if hasattr(reason, "value"):
        reason = reason.value
    reason_text = str(reason) if reason is not None else ""
    if not _SAFE_REASON.fullmatch(reason_text):
        reason_text = "context_benchmark_failed"
    return error_type, reason_text


class ContextResultJournal:
    """Append accepted measured rows before publishing the final result."""

    def __init__(
        self,
        *,
        output_path: Path,
        benchmark_run_id: str,
        scenario_id: str,
        expected_rows: int,
        git_commit: str,
        replace_incomplete: bool = False,
    ) -> None:
        if expected_rows < 1:
            raise ValueError("context journal requires measured rows")
        self.output_path = output_path.expanduser().resolve()
        self.summary_path = self.output_path.with_suffix(
            self.output_path.suffix + ".summary.json"
        )
        self.inprogress_path = self.output_path.with_name(
            f"{self.output_path.stem}.inprogress{self.output_path.suffix}"
        )
        self.progress_path = self.output_path.with_name(
            f"{self.output_path.stem}.progress.json"
        )
        self._benchmark_run_id = benchmark_run_id
        self._scenario_id = scenario_id
        self._expected_rows = expected_rows
        self._git_commit = git_commit
        self._row_count = 0
        self._digest = sha256()
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        if self.output_path.exists() or self.summary_path.exists():
            raise FileExistsError(
                f"completed context benchmark output already exists: {self.output_path}"
            )
        incomplete = (self.inprogress_path, self.progress_path)
        if any(path.exists() for path in incomplete):
            if not replace_incomplete:
                raise FileExistsError(
                    "incomplete context benchmark output exists; use a new "
                    "output path or --replace-incomplete"
                )
            for path in incomplete:
                path.unlink(missing_ok=True)
        with self.inprogress_path.open("xb"):
            pass
        self._write_progress(status="in_progress")

    @property
    def row_count(self) -> int:
        return self._row_count

    @property
    def raw_sample_vector_sha256(self) -> str:
        return f"sha256:{self._digest.hexdigest()}"

    def append(self, payload: Mapping[str, Any]) -> None:
        sample_ordinal = payload.get("sample_ordinal")
        if sample_ordinal != self._row_count:
            raise ValueError("context journal sample ordinal is not contiguous")
        encoded = _json_line(payload)
        with self.inprogress_path.open("ab") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        self._digest.update(encoded)
        self._row_count += 1
        self._write_progress(status="in_progress")

    def finalize(self, summary_payload: Mapping[str, Any]) -> None:
        if self._row_count != self._expected_rows:
            raise ValueError("context journal measured row count is incomplete")
        if (
            summary_payload.get("raw_sample_vector_sha256")
            != self.raw_sample_vector_sha256
        ):
            raise ValueError("context journal raw vector fingerprint drifted")
        temporary_summary = self.summary_path.with_name(
            f".{self.summary_path.name}.tmp"
        )
        with temporary_summary.open("wb") as handle:
            handle.write(
                json.dumps(
                    summary_payload,
                    ensure_ascii=True,
                    sort_keys=True,
                    indent=2,
                ).encode("utf-8")
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(self.inprogress_path, self.output_path)
        os.replace(temporary_summary, self.summary_path)
        _fsync_directory(self.output_path.parent)
        self.progress_path.unlink(missing_ok=True)
        _fsync_directory(self.output_path.parent)

    def mark_failed(self, error: BaseException) -> None:
        error_type, reason_code = _bounded_failure(error)
        self._write_progress(
            status="failed",
            error_type=error_type,
            reason_code=reason_code,
        )

    def _write_progress(
        self,
        *,
        status: str,
        error_type: str | None = None,
        reason_code: str | None = None,
    ) -> None:
        _atomic_write_json(
            self.progress_path,
            {
                "schema_version": "pulsara.context-result-journal.v1",
                "status": status,
                "benchmark_run_id": self._benchmark_run_id,
                "scenario_id": self._scenario_id,
                "git_commit": self._git_commit,
                "expected_rows": self._expected_rows,
                "completed_rows": self._row_count,
                "raw_sample_vector_sha256": self.raw_sample_vector_sha256,
                "inprogress_file": self.inprogress_path.name,
                "output_file": self.output_path.name,
                "error_type": error_type,
                "reason_code": reason_code,
            },
        )


class ContextSuiteJournal:
    """Record scenario-level completion without authorizing suite resume."""

    def __init__(
        self,
        *,
        output_directory: Path,
        dataset_id: str,
        manifest_contract_fingerprint: str,
        git_commit: str,
        expected_scenario_ids: tuple[str, ...],
        total_trajectories: int,
    ) -> None:
        self.output_directory = output_directory.expanduser().resolve()
        self.output_directory.mkdir(parents=True, exist_ok=True)
        self.inprogress_path = (
            self.output_directory / "context-suite.inprogress.json"
        )
        self.summary_path = self.output_directory / "context-suite.summary.json"
        if self.inprogress_path.exists() or self.summary_path.exists():
            raise FileExistsError(
                "context suite output already contains a suite journal"
            )
        self._dataset_id = dataset_id
        self._manifest_contract_fingerprint = manifest_contract_fingerprint
        self._git_commit = git_commit
        self._expected = expected_scenario_ids
        self._total = total_trajectories
        self._completed: list[dict[str, Any]] = []
        self._write(status="in_progress")

    def scenario_completed(
        self,
        *,
        scenario_id: str,
        output_file: str,
        summary_file: str,
        summary_file_sha256: str,
        benchmark_run_id: str,
        sample_count: int,
        raw_sample_vector_sha256: str,
        measurement_contract_adhered: bool,
        production_acceptance_passed: bool,
    ) -> None:
        expected = self._expected[len(self._completed)]
        if scenario_id != expected:
            raise ValueError("context suite scenario completion order drifted")
        self._completed.append(
            {
                "scenario_id": scenario_id,
                "output_file": output_file,
                "summary_file": summary_file,
                "summary_file_sha256": summary_file_sha256,
                "benchmark_run_id": benchmark_run_id,
                "sample_count": sample_count,
                "raw_sample_vector_sha256": raw_sample_vector_sha256,
                "measurement_contract_adhered": (
                    measurement_contract_adhered
                ),
                "production_acceptance_passed": production_acceptance_passed,
            }
        )
        self._write(status="in_progress")

    def finalize(self) -> None:
        if tuple(item["scenario_id"] for item in self._completed) != self._expected:
            raise ValueError("context suite scenario set is incomplete")
        payload = self._payload(status="completed")
        payload["production_acceptance_passed"] = all(
            item["production_acceptance_passed"] for item in self._completed
        )
        _atomic_write_json(self.summary_path, payload)
        self.inprogress_path.unlink(missing_ok=True)

    def mark_failed(self, error: BaseException) -> None:
        error_type, reason_code = _bounded_failure(error)
        self._write(
            status="failed",
            error_type=error_type,
            reason_code=reason_code,
        )

    def _write(
        self,
        *,
        status: str,
        error_type: str | None = None,
        reason_code: str | None = None,
    ) -> None:
        _atomic_write_json(
            self.inprogress_path,
            self._payload(
                status=status,
                error_type=error_type,
                reason_code=reason_code,
            ),
        )

    def _payload(
        self,
        *,
        status: str,
        error_type: str | None = None,
        reason_code: str | None = None,
    ) -> dict[str, Any]:
        return {
            "schema_version": "pulsara.context-suite-journal.v1",
            "status": status,
            "dataset_id": self._dataset_id,
            "manifest_contract_fingerprint": (
                self._manifest_contract_fingerprint
            ),
            "git_commit": self._git_commit,
            "expected_scenario_ids": self._expected,
            "completed_scenarios": tuple(self._completed),
            "total_trajectories": self._total,
            "error_type": error_type,
            "reason_code": reason_code,
        }


__all__ = ["ContextResultJournal", "ContextSuiteJournal"]
