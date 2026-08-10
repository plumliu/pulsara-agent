"""Canonical conversation-kernel test fixtures."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.support.postgres_database import (
    admin_root_dsn,
    create_migrated_postgres_test_database,
    drop_postgres_test_database,
)


_POSTGRES_KERNEL_MODULES = frozenset(
    {
        "test_stage2_canonical_reader.py",
        "test_stage2_conversation_kernel_postgres.py",
        "test_stage2_conversation_runner.py",
        "test_stage2_kernel_host_dogfood.py",
        "test_stage2_tui_cross_language.py",
    }
)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    postgres_marker = pytest.mark.postgres
    for item in items:
        if Path(str(item.fspath)).name in _POSTGRES_KERNEL_MODULES:
            item.add_marker(postgres_marker)


@pytest.fixture(scope="session")
def stage2_migrated_postgres_database():
    try:
        database = create_migrated_postgres_test_database()
    except Exception as exc:
        if os.environ.get("CI"):
            pytest.fail(f"Kernel PostgreSQL test database unavailable in CI: {exc}")
        pytest.skip(f"Kernel PostgreSQL test database unavailable: {exc}")
    try:
        yield database
    finally:
        drop_postgres_test_database(admin_root_dsn(), database.database_name)
