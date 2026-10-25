"""Copy a finite evidence selection, retaining complete selected provider records."""

import json
from pathlib import Path
import shutil

repo = Path(__file__).resolve().parents[4]
source = Path(__file__).resolve().parent
destination = (
    repo
    / "windows_compatibility/research/2026-10-25/terminal/phase-b/provider-acceptance"
)
assert not destination.exists(), destination
destination.mkdir(parents=True)
selected = {
    "20261025T090220Z-capability-mcp-hook-4ee8ead9": "alpha6-capability",
    "20261025T095813Z-subagent-capacity-4d97321d": "alpha7-capacity",
    "20261025T095915Z-subagent-context-41466d85": "alpha7-context",
    "20261025T100647Z-subagent-process-compaction-a07e29e7": "alpha7-process-compaction",
}
matrix = []
for result in sorted(source.glob("*/result.json")):
    value = json.loads(result.read_text(encoding="utf-8"))
    matrix.append(
        dict(
            run=result.parent.name,
            status=value["status"],
            provider_calls=len(value.get("provider_calls", [])),
            prefix_checks=len(value.get("prefix_checks", [])),
            compared=sum(item["compared"] for item in value.get("prefix_checks", [])),
            failure=value.get("failure_type"),
            undropped=value["undropped_databases"],
            selected=selected.get(result.parent.name),
            full_raw_archived=result.parent.name in selected,
        )
    )
(source / "final-matrix.json").write_text(
    json.dumps(matrix, ensure_ascii=False, indent=2), encoding="utf-8"
)


def copy(relative, target):
    item = source / relative
    if not item.exists():
        return
    output = destination / target
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(item, output)


for name in (
    "run_remaining_wsl.py",
    "execute.py",
    "prepare.py",
    "pg_terminal_adapter.py",
    "interrupt_owned.py",
    "cleanup_retained.py",
    "cleanup_private.py",
    "audit_final.py",
    "inspect_owned_run.py",
    "archive.py",
):
    copy(name, "scripts/" + name)
for name in (
    "report.zh.md",
    "final-matrix.json",
    "cleanup-final.json",
    "cleanup-checkpoint.json",
    "cleanup-histories-final.json",
    "cleanup-private-final.json",
    "prepared.json",
):
    copy(name, name)
for name in selected:
    for item in (source / name).iterdir():
        if item.is_file():
            copy(name + "/" + item.name, "evidence/" + name + "/" + item.name)

# One complete original ROOT hang, original context semantic failure, and the
# two final process-probe failures; omit repeated complete provider payloads.
negative = {
    "20261025T092127Z-subagent-capacity-a850ec70": [
        "result.json",
        "stages.jsonl",
        "command.json",
        "interrupt-control.json",
        "process-result.json",
    ],
    "20261025T090646Z-subagent-context-b10adc18": [
        "result.json",
        "command.json",
        "process-result.json",
    ],
    "20261025T092702Z-subagent-context-d513398d": [
        "stages.jsonl",
        "interrupt-control.json",
    ],
    "20261025T091434Z-subagent-capacity-e3ddb48a": [
        "readonly-admission-state.json",
        "stages.jsonl",
        "interrupt-control.json",
    ],
    "20261025T100252Z-subagent-process-compaction-5d3eea50": [
        "result.json",
        "command.json",
        "process-result.json",
    ],
    "20261025T100315Z-subagent-process-compaction-3de77a03": [
        "result.json",
        "command.json",
        "process-result.json",
        "probe-source.py",
    ],
    "20261025T085818Z-subagent-capacity-f36438f0": ["result.json"],
    "20261025T092814Z-subagent-root-regression-34130612": [
        "result.json",
        "command.json",
        "process-result.json",
    ],
}
for name, items in negative.items():
    for item in items:
        copy(name + "/" + item, "evidence/" + name + "/" + item)
for item in matrix:
    artifact = "evidence/" + item["run"] + "/result.json"
    item["full_raw_archived"] = (destination / artifact).exists()
    item["raw_artifact"] = artifact if item["full_raw_archived"] else None
encoded = json.dumps(matrix, ensure_ascii=False, indent=2)
(source / "final-matrix.json").write_text(encoded, encoding="utf-8")
(destination / "final-matrix.json").write_text(encoded, encoding="utf-8")
copy("archive.py", "scripts/archive.py")
tracked = [
    "src/pulsara_agent/conversation_kernel/repository_errors.py",
    "src/pulsara_agent/conversation_kernel/_repository/conversation.py",
    "src/pulsara_agent/conversation_kernel/turn_admission.py",
    "src/pulsara_agent/conversation_kernel/runner.py",
    "tests/test_round5_long_horizon_postgres.py",
    "tests/dogfood/run_round10_subagent_dogfood.py",
    "tests/dogfood/run_model_switch_handover_dogfood.py",
    "tests/dogfood/run_capability_change_observation_dogfood.py",
    "tests/dogfood/run_content_revision_line_edit_dogfood.py",
]
files = sorted(
    str(path.relative_to(repo)).replace("\\", "/")
    for path in destination.rglob("*")
    if path.is_file()
)
manifest = "\n".join(tracked + files) + "\n"
(destination / "archive-files.txt").write_text(manifest, encoding="utf-8")
(source / "archive-files.txt").write_text(manifest, encoding="utf-8")
print(
    json.dumps(
        dict(
            directory=str(destination),
            files=len(files),
            bytes=sum(
                path.stat().st_size for path in destination.rglob("*") if path.is_file()
            ),
        )
    )
)
