from retry_queue import drain_with_retries


def test_success_stops_retrying() -> None:
    calls = []

    def worker(item: str) -> bool:
        calls.append(item)
        return len(calls) == 2

    assert drain_with_retries(["a"], max_retries=3, worker=worker)[0].attempts == 2


def test_zero_retries_still_means_one_attempt() -> None:
    result = drain_with_retries(["a"], max_retries=0, worker=lambda _: False)
    assert result[0].attempts == 1
