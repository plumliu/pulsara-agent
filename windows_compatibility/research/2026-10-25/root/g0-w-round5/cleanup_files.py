"""Remove only the ten observed root-owned experiment homes after process audit."""

import json
import os
from pathlib import Path
import shutil

here = Path(__file__).parent
names = [
    ".g0-http-4ep2i_ga",
    ".g0-http-5jet9x6e",
    ".g0-files-yjev4s5f",
    ".g0-http-xji2tyl1",
    ".g0-files-s2wiqd62",
    ".g0-http-70ycg166",
    ".g0-http-b4shi0t6",
    ".g0-http-f57bq6k1",
    ".g0-http-fgvcxh3y",
    ".g0-files-7gizwyeo",
]
roots = [Path("/home/plumwsl") / n for n in names]
assert os.getuid() == 1000
live = []
markers = [
    b"G0-only finite directory GUI fixture",
    b"Real production HTTP/import/preview owners",
]
for entry in Path("/proc").iterdir():
    if not entry.name.isdigit() or int(entry.name) == os.getpid():
        continue
    try:
        args = (entry / "cmdline").read_bytes().split(b"\0")
        # The finite test server was passed as python -c code, not a pattern in
        # a shell's source or another observer's arguments.
        if (
            len(args) > 2
            and Path(os.fsdecode(args[0])).name.startswith("python")
            and b"-c" in args
        ):
            code = args[args.index(b"-c") + 1]
            if any(code.startswith(b'"""' + m) for m in markers):
                live.append(
                    {"pid": int(entry.name), "commandPrefix": os.fsdecode(args[0])}
                )
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        pass
assert not live, live
known = {}
for source in here.glob("*.json"):
    try:
        value = json.loads(source.read_text(encoding="utf-8-sig"))
        if isinstance(value, dict):
            for event in value.get("events", []):
                if event.get("event") == "ready" and event.get("home") in map(
                    str, roots
                ):
                    known.setdefault(event["home"], []).append(source.name)
    except (ValueError, UnicodeError):
        pass
inventory = []
for root in roots:
    assert root.parent == Path("/home/plumwsl") and not root.is_symlink()
    assert root.stat().st_uid == 1000
    if root.name.startswith(".g0-http-"):
        assert set(p.name for p in root.iterdir()) <= {"static", "workspace", "imports"}
        assert "G0 production HTTP owners" in (root / "static/index.html").read_text()
        assert (root / "workspace/sample.pdf").read_bytes().startswith(b"%PDF-1.4")
    else:
        assert str(root) in known
        assert set(p.name for p in root.iterdir()) <= {"imports"}
    inventory.append(
        {
            "path": str(root),
            "readyRecords": known.get(str(root), []),
            "fixtureTemplateChecked": root.name.startswith(".g0-http-"),
        }
    )
    for directory, _, _ in os.walk(root, followlinks=False):
        os.chmod(directory, 0o700)
    shutil.rmtree(root)
workspace = Path("/home/plumwsl/workspaces/中文 工程 G0-R5")
assert not workspace.is_symlink()
assert set(p.name for p in workspace.iterdir()) == {
    "symlink-ext4",
    "symlink-drvfs",
    "no-access",
    "duplicate",
    "lost-reply",
}
os.chmod(workspace / "no-access", 0o700)
shutil.rmtree(workspace)
result = {
    "liveMatchingServers": live,
    "homes": inventory,
    "remainingHomes": [str(p) for p in roots if p.exists()],
    "workspaceRemoved": not workspace.exists(),
    "scope": "No other prefix-matched directories were removed; ten exact observed paths only.",
}
(here / "cleanup-files.json").write_text(
    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(
    json.dumps(
        {
            "removedHomes": len(roots),
            "remainingHomes": result["remainingHomes"],
            "workspaceRemoved": result["workspaceRemoved"],
        }
    )
)
