from __future__ import annotations

from typing import Any
from time import monotonic

import pytest

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
        ("0", "0"),
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
        ("30000ms", "30000ms"),
    ]


def test_deadline_restores_transaction_mode_after_setting_failure(monkeypatch):
    connection = _RecordingConnection()
    connection.autocommit = False
    monkeypatch.setattr(postgres_endpoint, "monotonic", lambda: 100.0)

    def failed(*_args):
        assert connection.autocommit is True
        raise RuntimeError("setting failed")

    monkeypatch.setattr(connection, "execute", failed)
    with pytest.raises(RuntimeError, match="setting failed"):
        postgres_endpoint.apply_connection_deadline(connection, 130.0)
    assert connection.autocommit is False


@pytest.mark.postgres
@pytest.mark.parametrize("deadline, expected", [(130.0, "30s"), (1_000_000_000.0, "0")])
def test_both_server_timeouts_are_installed_before_transaction_work(
    stage2_migrated_postgres_database, monkeypatch, deadline, expected,
):
    factory = postgres_endpoint.ResolvedPostgresConnectionFactory(
        stage2_migrated_postgres_database.runtime_dsn, application_name="test-timeouts",
    )
    with factory.connect(deadline_monotonic=monotonic() + 30) as connection:
        assert connection.autocommit is False
        monkeypatch.setattr(postgres_endpoint, "monotonic", lambda: 100.0)
        postgres_endpoint.apply_connection_deadline(connection, deadline)
        assert connection.autocommit is False
        assert connection.execute(
            "SELECT current_setting('statement_timeout'), current_setting('lock_timeout'), "
            "current_setting('search_path')"
        ).fetchone() == (expected, expected, "public")
