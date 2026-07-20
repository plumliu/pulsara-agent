import pytest

from limiter import RateLimiter


def test_exact_limit() -> None:
    limiter = RateLimiter(2)
    assert [limiter.allow("alpha") for _ in range(3)] == [True, True, False]


def test_keys_are_independent() -> None:
    limiter = RateLimiter(1)
    assert limiter.allow("alpha") is True
    assert limiter.allow("beta") is True


def test_non_positive_limit_is_rejected() -> None:
    with pytest.raises(ValueError):
        RateLimiter(0)
