from pathlib import Path
import sys


def main(root: Path) -> None:
    sys.path.insert(0, str(root))
    from limiter import RateLimiter

    limiter = RateLimiter(2)
    assert [limiter.allow("alpha") for _ in range(3)] == [True, True, False]
    assert [limiter.allow("beta") for _ in range(3)] == [True, True, False]
    limiter.reset("alpha")
    assert limiter.allow("alpha") is True
    assert limiter.allow("beta") is False
    limiter.reset("missing")
    for invalid in (0, -1):
        try:
            RateLimiter(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError("non-positive limit must raise ValueError")
    assert "PLAN_WORKFLOW_FIXED_V1" in (root / "PLAN_DONE.md").read_text()
    print("plan-workflow verifier passed")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
