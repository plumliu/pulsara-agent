from __future__ import annotations

import asyncio
from pathlib import Path
import sys


BENCHMARK_ROOT = Path(__file__).resolve().parents[1] / "benchmarks" / "durable-runtime"
if str(BENCHMARK_ROOT) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_ROOT))

from generators.provider_input_prefix import (  # noqa: E402
    run_provider_input_prefix_benchmark,
)


def test_provider_input_prefix_benchmark_uses_committed_generation_boundaries(
    tmp_path: Path,
) -> None:
    result = asyncio.run(
        run_provider_input_prefix_benchmark(
            workspace_root=tmp_path,
            model_calls=5,
        )
    )

    assert result.observed_model_calls == 5
    assert sum(result.calls_per_generation.values()) == 5
    assert result.same_generation_prefix_comparison_count >= 1
    assert result.prefix_invariant_holds is True
    assert result.old_transcript_unit_rerender_count == 0
    assert result.rollover_count == len(result.rollover_reasons)
    assert result.exact_restore_unit_count > 0
    assert result.max_model_start_bytes > 0
    assert result.max_horizon_root_bytes > 0
