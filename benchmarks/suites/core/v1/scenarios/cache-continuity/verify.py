from pathlib import Path
import sys


def main(root: Path) -> None:
    expected = {
        "cache_round1.txt": "BLUE-EMBER-731|phase-one",
        "cache_round2.txt": "BLUE-EMBER-731|phase-two",
        "cache_final.txt": "BLUE-EMBER-731|phase-three",
    }
    for name, content in expected.items():
        assert (root / name).read_text().strip() == content, name
    print("cache-continuity verifier passed")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
