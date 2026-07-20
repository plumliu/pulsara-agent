from pathlib import Path
import sys


def main(root: Path) -> None:
    assert (root / "answer.txt").read_text().strip() == "Asterford-Veylan"
    assert len(tuple((root / "story").glob("chapter-*.md"))) == 6
    print("manual-compaction-trail verifier passed")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
