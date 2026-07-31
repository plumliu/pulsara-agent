import json
from pathlib import Path
import sys


def main(root: Path) -> None:
    expected = {
        "chain": [
            "data/start.txt",
            "data/node-quartz.txt",
            "data/node-ember.txt",
            "data/node-lantern.txt",
            "data/node-slate.txt",
            "data/node-harbor.txt",
            "data/node-maple.txt",
            "data/node-crown.txt",
        ],
        "values": [13, 21, 34, 55, 89, 8, 3, 144],
        "sum": 367,
        "weighted_checksum": 2043,
        "terminal_marker": "TRAIL_COMPLETE_V2",
    }
    assert json.loads((root / "child_trace.json").read_text()) == expected
    assert (root / "result.txt").read_text().strip() == "367:2043"
    print("subagent-delegation verifier passed")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
