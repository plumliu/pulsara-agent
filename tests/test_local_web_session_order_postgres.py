from __future__ import annotations

from datetime import datetime, timedelta, timezone
from time import monotonic
from uuid import uuid4

import psycopg
import pytest

from pulsara_agent.conversation_kernel.host import (
    _list_resumable_session_rows_across_workspaces,
)
from pulsara_agent.conversation_kernel.repository import ConversationKernelRepository
from pulsara_agent.workspace_identity import HostWorkspaceInput, resolve_workspace
from tests.support.postgres import verified_postgres_provider


pytestmark = pytest.mark.postgres


def test_session_catalog_order_ignores_writer_lease_maintenance(
    stage2_migrated_postgres_database,
    tmp_path,
) -> None:
    repository = ConversationKernelRepository(
        verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    )
    suffix = uuid4().hex
    memory_domain_id = f"session-order-{suffix}"
    older_id = f"session:older-{suffix}"
    newer_id = f"session:newer-{suffix}"
    for session_id in (older_id, newer_id):
        workspace = resolve_workspace(
            HostWorkspaceInput(
                workspace_kind="transient",
                workspace_root=tmp_path / session_id,
                display_label="快速开始",
                memory_domain_id=memory_domain_id,
            )
        )
        repository.acquire_host_writer(
            session_id=session_id,
            workspace_id=workspace.workspace_key,
            workspace_kind="transient",
            workspace_root=str(workspace.workspace_root),
            workspace_label=workspace.display_label,
            memory_domain_id=memory_domain_id,
            writer_owner_id=f"host:{session_id}",
            lease_seconds=60,
            deadline_monotonic=monotonic() + 30,
        )

    now = datetime.now(timezone.utc)
    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as connection:
        connection.execute(
            """
            UPDATE pulsara_v3.sessions
            SET created_at = CASE id WHEN %s THEN %s ELSE %s END,
                updated_at = CASE id WHEN %s THEN %s ELSE %s END
            WHERE id IN (%s, %s)
            """,
            (
                older_id,
                now - timedelta(hours=2),
                now - timedelta(hours=1),
                older_id,
                now,
                now - timedelta(minutes=30),
                older_id,
                newer_id,
            ),
        )

    rows = _list_resumable_session_rows_across_workspaces(
        repository,
        memory_domain_id,
        False,
        monotonic() + 30,
    )

    assert [str(row["id"]) for row in rows] == [newer_id, older_id]
    assert rows[0]["updated_at"] == now - timedelta(hours=1)
    assert rows[1]["updated_at"] == now - timedelta(hours=2)
