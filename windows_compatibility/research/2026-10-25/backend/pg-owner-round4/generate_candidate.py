"""Mechanical two-line guard against the actual owner, output-only research."""

import difflib
from pathlib import Path

out = Path(__file__).resolve().parent
root = out.parents[3]
source = (root / "desktop/src/postgres.ts").read_text(encoding="utf-8")
anchor = "    } catch (error) {\n      // pg_ctl's readiness wait may expire while the server it launched is alive."
replacement = """    } catch (error) {
      const execution = (error as {postgresExecution?: unknown} | null)?.postgresExecution;
      if (execution === 'not-submitted' || execution === 'unknown') throw error;
      // pg_ctl's readiness wait may expire while the server it launched is alive."""
assert source.count(anchor) == 1
candidate = source.replace(anchor, replacement)
directory = out / "candidate-source"
directory.mkdir(exist_ok=True)
(directory / "postgres.ts").write_text(candidate, encoding="utf-8")
for name in ("postgres-operations.ts", "postgres-posix.ts", "postgres.test.ts"):
    (directory / name).write_bytes((root / "desktop/src" / name).read_bytes())
(out / "candidate.diff").write_text(
    "".join(
        difflib.unified_diff(
            source.splitlines(keepends=True),
            candidate.splitlines(keepends=True),
            fromfile="desktop/src/postgres.ts",
            tofile="output-only/candidate-source/postgres.ts",
        )
    ),
    encoding="utf-8",
)
