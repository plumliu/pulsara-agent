from pathlib import Path
import sys


def main(root: Path) -> None:
    assert (root / "result.txt").read_text().strip() == "86"
    print("subagent-delegation verifier passed")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
