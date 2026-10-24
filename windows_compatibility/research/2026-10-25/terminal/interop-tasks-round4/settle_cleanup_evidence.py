"""Separate an inaccessible current PID from the exact old HANDLE already joined."""

import json

import windows_probe as W

raw_path = W.BASE / "cleanup-current-query-failure.json"
if not raw_path.exists():
    raw_path.write_bytes((W.BASE / "cleanup-final.json").read_bytes())
raw = json.loads(raw_path.read_text(encoding="utf-8"))
assert (
    not raw["remainingWindows"]
    and not raw["remainingLinux"]
    and not raw["remainingDirectories"]
)
unknown = raw["unknownWindows"]
assert len(unknown) == 1 and unknown[0]["pid"] == 33468 and unknown[0]["error"] == 5
diagnostic = W.J.rows(W.BASE / "tail-cancel-c584858c-A.diagnostic.jsonl")[0]
assert diagnostic["pid"] == 33468 and diagnostic["creation"] == 134373340069445234
events = W.J.rows(W.BASE / "tail-cancel-c584858c-A.windows.jsonl")
opened = next(
    row for row in events if row["event"] == "host-verified-transferred-fd-job-binding"
)
assert (
    opened["sameCreation"]
    and opened["creation"] == diagnostic["creation"]
    and opened["waitResult"] == 258
)
joined = next(row for row in events if row["event"] == "windows-scope-joined")
held = next(
    row
    for row in joined["facts"]["held"]
    if row["pid"] == diagnostic["pid"] and row["creation"] == diagnostic["creation"]
)
assert held["waitResult"] == 0 and joined["facts"]["accounting"]["ActiveProcesses"] == 0
result = dict(
    raw,
    remainingExactOwned=[],
    currentPidUnobservable=unknown,
    historicallyExactJoined=[
        dict(
            **held,
            source="tail-cancel-c584858c-A.windows.jsonl",
            event="windows-scope-joined",
            time=joined["time"],
            currentPidState="unobservable; no conclusion of absence or reuse",
        )
    ],
    qualification="Historical exact owned identities settled; current PID 33468 remains inaccessible. No current PID action performed.",
)
(W.BASE / "cleanup-final.json").write_text(
    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(
    json.dumps(
        {
            key: result[key]
            for key in (
                "windowsIdentityCount",
                "linuxIdentityCount",
                "privateDirectoryCount",
                "remainingExactOwned",
                "remainingLinux",
                "remainingDirectories",
                "currentPidUnobservable",
                "historicallyExactJoined",
            )
        }
    )
)
