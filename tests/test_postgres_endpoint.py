from __future__ import annotations

from typing import Any

from pulsara_agent.storage import postgres_endpoint


class _RecordingConnection:
    def __init__(self) -> None:
        self.autocommit = True
        self.closed = False
        self.executions: list[tuple[str, tuple[object, ...]]] = []

    def execute(
        self, statement: str, parameters: tuple[object, ...]
    ) -> None:
        self.executions.append((statement, parameters))


def test_large_finite_deadline_does_not_overflow_postgres_timeout(
    monkeypatch: Any,
) -> None:
    connection = _RecordingConnection()
    monkeypatch.setattr(postgres_endpoint, "monotonic", lambda: 27_134.303)

    postgres_endpoint.apply_connection_deadline(  # type: ignore[arg-type]
        connection,
        1_000_000_000.0,
    )

    assert [parameters for _, parameters in connection.executions] == [
        ("0",),
        ("0",),
    ]


def test_representable_deadline_keeps_exact_millisecond_timeout(
    monkeypatch: Any,
) -> None:
    connection = _RecordingConnection()
    monkeypatch.setattr(postgres_endpoint, "monotonic", lambda: 100.0)

    postgres_endpoint.apply_connection_deadline(  # type: ignore[arg-type]
        connection,
        130.0,
    )

    assert [parameters for _, parameters in connection.executions] == [
        ("30000ms",),
        ("30000ms",),
    ]
