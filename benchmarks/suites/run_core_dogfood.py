"""CLI for validating and running the frozen Pulsara core dogfood suite."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import time

from pulsara_agent.settings import PulsaraSettings
from pulsara_agent.storage.schema_verification_service import (
    acquire_verified_postgres_access_sync,
)

from benchmarks.suites.contracts import DogfoodContractError, load_suite
from benchmarks.suites.runner import CoreDogfoodRunner, write_suite_summary


DEFAULT_SUITE_ROOT = Path(__file__).parent / "core" / "v1"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate or run the frozen Pulsara core real-LLM dogfood suite."
    )
    parser.add_argument(
        "--suite-root",
        type=Path,
        default=DEFAULT_SUITE_ROOT,
        help="Suite root containing manifest.json.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "validate", help="Validate contracts and fingerprints offline."
    )
    subparsers.add_parser("list", help="List frozen scenarios without network access.")

    run = subparsers.add_parser("run", help="Run selected scenarios serially.")
    run.add_argument("--scenario", action="append", default=[])
    run.add_argument("--env-file", type=Path, default=Path(".env"))
    run.add_argument("--override-env", action="store_true")
    run.add_argument("--results-dir", type=Path)
    run.add_argument("--keep-workspaces", action="store_true")
    run.add_argument("--fail-fast", action="store_true")
    run.add_argument(
        "--confirm-network",
        action="store_true",
        help="Acknowledge that this command spends real provider tokens.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        suite = load_suite(args.suite_root)
    except DogfoodContractError as exc:
        print(f"contract error: {exc}", file=sys.stderr)
        return 2

    if args.command == "validate":
        print(
            f"PASS suite={suite.manifest.suite_id} "
            f"scenarios={len(suite.scenarios)} "
            f"fingerprint={suite.suite_contract_fingerprint}"
        )
        for scenario in suite.scenarios:
            print(
                f"  {scenario.contract.scenario_id} "
                f"{scenario.scenario_contract_fingerprint}"
            )
        return 0

    if args.command == "list":
        for scenario in suite.scenarios:
            workflow = scenario.contract.workflow.workflow_kind
            print(
                f"{scenario.contract.scenario_id:28} "
                f"{workflow:22} {scenario.contract.model_role:5} "
                f"{scenario.contract.timeout_seconds:4}s  "
                f"{scenario.contract.description}"
            )
        return 0

    if os.getenv("PULSARA_RUN_CORE_DOGFOOD") != "1":
        print(
            "refusing real-provider execution: set PULSARA_RUN_CORE_DOGFOOD=1",
            file=sys.stderr,
        )
        return 2
    if not args.confirm_network:
        print(
            "refusing real-provider execution: pass --confirm-network",
            file=sys.stderr,
        )
        return 2
    selected = suite.select(frozenset(args.scenario))
    settings = _load_settings(args.env_file, override=args.override_env)
    try:
        postgres_lease = acquire_verified_postgres_access_sync(
            settings.storage.postgres_dsn,
            deadline_monotonic=time.monotonic() + 30.0,
        )
    except Exception as exc:
        print(
            f"PostgreSQL schema preflight failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2
    else:
        postgres_lease.release()

    started_at = datetime.now(timezone.utc)
    started_monotonic = time.monotonic()
    results_dir = args.results_dir or Path(
        f"/tmp/pulsara-core-dogfood-{started_at.strftime('%Y%m%dT%H%M%SZ')}"
    )
    runner = CoreDogfoodRunner(
        suite=suite,
        settings=settings,
        results_root=results_dir,
        keep_workspaces=args.keep_workspaces,
    )
    print(f"suite={suite.manifest.suite_id}")
    print(f"suite_fingerprint={suite.suite_contract_fingerprint}")
    print(f"runner_fingerprint={runner.runner_fingerprint}")
    print(f"results={results_dir.resolve()}")
    results = asyncio.run(runner.run_selected(selected, fail_fast=args.fail_fast))
    summary = write_suite_summary(
        suite=suite,
        runner_fingerprint=runner.runner_fingerprint,
        results_root=results_dir,
        selected_ids=tuple(item.contract.scenario_id for item in selected),
        results=results,
        started_at=started_at,
        elapsed_seconds=time.monotonic() - started_monotonic,
    )
    print(
        f"SUMMARY passed={len(summary.passed_scenario_ids)} "
        f"failed={len(summary.failed_scenario_ids)} "
        f"elapsed={summary.elapsed_seconds:.1f}s"
    )
    return 1 if summary.failed_scenario_ids else 0


def _load_settings(path: Path, *, override: bool) -> PulsaraSettings:
    if path.exists():
        return PulsaraSettings.from_env_file(path, override=override)
    return PulsaraSettings.from_env()


if __name__ == "__main__":
    raise SystemExit(main())
