#!/usr/bin/env python3
"""Run the deterministic append-only provider-input benchmark."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
import tempfile

RUNNER_DIR = Path(__file__).resolve().parent
BENCHMARK_ROOT = RUNNER_DIR.parent
REPO_ROOT = BENCHMARK_ROOT.parents[1]
for path in (REPO_ROOT, BENCHMARK_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from generators.provider_input_prefix import (  # noqa: E402
    run_provider_input_prefix_benchmark,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pulsara deterministic provider-input prefix benchmark"
    )
    parser.add_argument("--model-calls", type=int, default=5)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    workspace = args.workspace or Path(
        tempfile.mkdtemp(prefix="pulsara-provider-prefix-")
    )
    result = asyncio.run(
        run_provider_input_prefix_benchmark(
            workspace_root=workspace,
            model_calls=args.model_calls,
        )
    )
    payload = result.to_dict()
    rendered = json.dumps(payload, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
