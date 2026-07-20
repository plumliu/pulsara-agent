from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


def main(root: Path) -> None:
    spec = importlib.util.spec_from_file_location(
        "retry_queue", root / "retry_queue.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["retry_queue"] = module
    spec.loader.exec_module(module)

    calls: dict[str, int] = {}

    def worker(item: str) -> bool:
        calls[item] = calls.get(item, 0) + 1
        return (item == "alpha" and calls[item] == 2) or item == "gamma"

    results = module.drain_with_retries(
        ["alpha", "beta", "gamma"], max_retries=2, worker=worker
    )
    assert [item.item for item in results] == ["alpha", "beta", "gamma"]
    assert [(item.attempts, item.succeeded) for item in results] == [
        (2, True),
        (3, False),
        (1, True),
    ]
    try:
        module.drain_with_retries([], max_retries=-1, worker=worker)
    except ValueError:
        pass
    else:
        raise AssertionError("negative max_retries must raise ValueError")
    assert "RETRY_QUEUE_FIXED_V1" in (root / "PATCH_NOTES.md").read_text()
    print("workspace-patch verifier passed")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
