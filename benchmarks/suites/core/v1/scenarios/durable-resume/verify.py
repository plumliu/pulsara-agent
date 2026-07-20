from pathlib import Path
import sys


def main(root: Path) -> None:
    assert (
        root / "before_resume.txt"
    ).read_text().strip() == "ORCHID-RESUME-4421|before"
    assert (root / "after_resume.txt").read_text().strip() == "ORCHID-RESUME-4421|after"
    print("durable-resume verifier passed")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
