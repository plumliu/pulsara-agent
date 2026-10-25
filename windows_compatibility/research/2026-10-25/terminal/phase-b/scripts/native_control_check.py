import json
from pathlib import Path
import subprocess
import sys

base = Path(__file__).resolve().parent
helper = (
    Path(sys.argv[1]) if len(sys.argv) > 1 else base / "helper" / "pulsara-interop.exe"
)
rows = []
for mode, payload in [
    ("--version", None),
    ("--scope", b'{"distribution":"unsubmitted"}'),
    ("--probe", b'{"job":"unsubmitted"}'),
]:
    done = subprocess.run(
        [str(helper), mode], input=payload, capture_output=True, timeout=10
    )
    rows.append(
        dict(
            mode=mode,
            input=None if payload is None else payload.decode(),
            exit=done.returncode,
            stdout=done.stdout.decode("utf-8"),
            stderr=done.stderr.decode("utf-8"),
        )
    )
assert rows[0]["exit"] == 0 and rows[0]["stdout"].strip() == "pulsara-interop 1"
assert all(
    row["exit"] == 1 and "Partial interop control frame" in row["stderr"]
    for row in rows[1:]
)
(base / "native-control-results.json").write_text(
    json.dumps(rows, indent=2), encoding="utf-8"
)
print(json.dumps(rows))
