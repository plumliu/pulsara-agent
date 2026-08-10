from pathlib import Path
import sys


def main(root: Path) -> None:
    assert (root / "queue_first.txt").read_text().strip() == "QUEUE_FIRST_ACCEPTED_V2"
    assert (root / "queue_second.txt").read_text().strip() == "QUEUE_SECOND_ACCEPTED_V2"
    print("prompt-queue-fifo verifier passed")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
