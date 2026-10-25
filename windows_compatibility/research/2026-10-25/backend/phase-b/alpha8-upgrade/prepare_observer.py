"""Copy the existing read-only observer to its independent alpha8 evidence folder."""
import ast
from pathlib import Path

directory = Path(__file__).resolve().parent
source = (directory.parent / "alpha7-upgrade/observe_upgrade.py").read_text(encoding="utf-8")
source = source.replace("authorized alpha7 build", "authorized alpha8 build")
source = source.replace('choices=("before", "after", "reinstalled")', 'choices=("before", "after")')
source = source.replace(
    'expected_runtime = "0.1.0-alpha.6" if args.stage == "before" else "0.1.0-alpha.7"',
    'expected_runtime = "0.1.0-alpha.7" if args.stage == "before" else "0.1.0-alpha.8"',
).replace("alpha7-upgrade", "alpha8-upgrade")
lines = []
skipping = False
indent = 0
for line in source.splitlines(keepends=True):
    if line.strip() == 'if args.stage == "reinstalled":':
        skipping = True
        indent = len(line) - len(line.lstrip())
        continue
    if skipping and line.strip() and len(line) - len(line.lstrip()) <= indent:
        skipping = False
    if not skipping:
        lines.append(line)
source = "".join(lines)
ast.parse(source)
target = directory / "observe_upgrade.py"
assert not target.exists()
target.write_text(source, encoding="utf-8")
print("alpha8 observer created; syntax OK")
