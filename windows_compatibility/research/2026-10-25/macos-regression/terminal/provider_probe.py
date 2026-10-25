from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

OUT = Path(__file__).resolve().parent
REPO = OUT.parents[2]
import pulsara_agent
PACKAGE_SOURCE = Path(pulsara_agent.__file__).resolve()
assert PACKAGE_SOURCE.is_relative_to(REPO / "desktop/resources/python")
# Import production from the freshly prepared installed wheel first; this path
# append exposes only tests helpers, never repo/src or PYTHONPATH.
sys.path.append(str(REPO))

from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.settings import LocalSettingsStore
from tests.dogfood import run_terminal_cognitive_dogfood as cognitive
from tests.dogfood.run_model_switch_handover_dogfood import _RecordingModelRuntime, _scrub
from tests.support.postgres_database import create_migrated_postgres_test_database, drop_postgres_test_database

PRIVATE = json.loads((REPO / "output/macos-regression-20261025/root/private-pg.json").read_text())


def main():
    # Read the saved production snapshot before the ordinary runner switches to
    # its private temporary home. Never write the saved store or use its PG.
    saved_home = require_pulsara_home()
    saved = LocalSettingsStore(saved_home / "local-settings.yaml").read()
    secrets = tuple(value.decode("utf-8") for value in saved.credential_scrub_set().values)
    os.environ["PULSARA_POSTGRES_ADMIN_DSN"] = PRIVATE["admin_dsn"]
    os.environ["PULSARA_POSTGRES_DSN"] = PRIVATE["runtime_dsn"]
    os.environ["CI"] = "true"
    report = {
        "phase": "macos-a57ce3b1-native-terminal",
        "model": "openai/gpt-6-luna",
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "python": sys.executable, "module": str(PACKAGE_SOURCE),
        "cases": [], "trace": [], "provider_records": [],
        "database_cleanup": False,
    }
    created = None

    def create_database(_saved):
        nonlocal created
        created = create_migrated_postgres_test_database()
        report["database"] = {"name": created.database_name, "admin_dsn": created.admin_dsn, "runtime_dsn": created.runtime_dsn, "private_cluster_data": PRIVATE["data"]}
        return created.database_name, PRIVATE["admin_dsn"], created.admin_dsn, created.runtime_dsn

    def drop_database(_saved, name):
        assert created is not None and name == created.database_name
        drop_postgres_test_database(PRIVATE["admin_dsn"], name)
        report["database_cleanup"] = True

    class RecordingRuntimeFactory:
        @staticmethod
        def production(**kwargs):
            delegate = ModelRuntime.production(**kwargs)
            return _RecordingModelRuntime(delegate, report["provider_records"], None)

    def save(path, value, _saved):
        encoded = json.dumps(_scrub(value, secrets), ensure_ascii=False, indent=2)
        if any(secret and secret in encoded for secret in secrets):
            raise RuntimeError("configured credential remained in report")
        path.write_text(encoded + "\n")

    # Adapt only this offline runner's test DB factory and read-only transport
    # observation. Production owners, tools, prompts and provider are unchanged.
    cognitive._create_database = create_database
    cognitive._drop_database = drop_database
    cognitive.ModelRuntime = RecordingRuntimeFactory
    cognitive.save = save
    args = SimpleNamespace(model=report["model"], cases="directories,interaction,pty,monitor_wake", phase=report["phase"], output=OUT / "provider-results.json")
    (OUT / "provider-command.json").write_text(json.dumps({"command": [sys.executable, "-I", str(Path(__file__).resolve())], "cases": args.cases, "model": args.model, "saved_home": str(saved_home), "private_cluster": PRIVATE["data"]}, indent=2) + "\n")
    try:
        asyncio.run(cognitive.run(args, report))
    except BaseException as error:
        report.update(status="failed", failure_type=type(error).__name__, failure_message=str(error))
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    save(args.output, report, saved)
    print(json.dumps({"status": report["status"], "cases": [case["case"] for case in report["cases"]], "database_cleanup": report["database_cleanup"]}), flush=True)
    return int(report["status"] != "completed")


if __name__ == "__main__":
    raise SystemExit(main())
