"""Workspace cleanup serialization remains independent of memory CRUD."""

from uuid import uuid4
from pathlib import Path
from threading import Event
from concurrent.futures import ThreadPoolExecutor
from time import monotonic
import psycopg
import pytest
from pulsara_agent.memory.scope import workspace_context_id
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from pulsara_agent.conversation_kernel._repository import (
    authority as authority_repository,
)
from pulsara_agent.conversation_kernel._repository.locking import (
    lock_canonical_identities,
)
from tests.test_stage2_conversation_kernel_postgres import _name, _repository

pytestmark = pytest.mark.postgres


def test_orphan_workspace_cleanup_serializes_with_new_session_creation(
    stage2_migrated_postgres_database,
    monkeypatch: pytest.MonkeyPatch,
):
    repository = _repository(stage2_migrated_postgres_database)
    workspace_root = uuid4().hex
    workspace_id = workspace_context_id(workspace_root)
    workspace_label = Path(workspace_root).name
    runtime_dsn = stage2_migrated_postgres_database.runtime_dsn
    with psycopg.connect(runtime_dsn) as connection:
        lock_canonical_identities(
            connection,
            namespace="workspace",
            memory_domain_id="u_local",
            identities=(workspace_id,),
        )
        connection.execute(
            "INSERT INTO pulsara_v3.workspaces "
            "(memory_domain_id,id,workspace_kind,workspace_root,workspace_label) "
            "VALUES ('u_local',%s,'project',%s,%s)",
            (workspace_id, workspace_root, workspace_label),
        )

    orphan_deleted = Event()
    allow_cleanup_commit = Event()
    session_reached_lock = Event()
    real_authority_lock = authority_repository.lock_canonical_identities

    def observed_authority_lock(connection, **kwargs):
        if workspace_id in tuple(kwargs["identities"]):
            session_reached_lock.set()
        return real_authority_lock(connection, **kwargs)

    monkeypatch.setattr(
        authority_repository, "lock_canonical_identities", observed_authority_lock
    )

    def held_cleanup():
        with psycopg.connect(runtime_dsn) as connection:
            lock_canonical_identities(
                connection,
                namespace="workspace",
                memory_domain_id="u_local",
                identities=(workspace_id,),
            )
            deleted = connection.execute(
                "DELETE FROM pulsara_v3.workspaces AS w "
                "WHERE w.memory_domain_id='u_local' AND w.id=%s "
                "AND NOT EXISTS (SELECT 1 FROM pulsara_v3.sessions s "
                "WHERE s.memory_domain_id=w.memory_domain_id AND s.workspace_id=w.id) "
                "AND NOT EXISTS (SELECT 1 FROM pulsara_v3.memory_facts f "
                "WHERE f.memory_domain_id=w.memory_domain_id "
                "AND f.project_workspace_id=w.id) RETURNING id",
                (workspace_id,),
            ).fetchone()
            assert deleted == (workspace_id,)
            orphan_deleted.set()
            assert allow_cleanup_commit.wait(timeout=10)

    def create_session():
        return repository.acquire_host_writer(
            intent="NEW",
            session_id=_name("session"),
            workspace_id=workspace_id,
            workspace_root=workspace_root,
            writer_owner_id=_name("host"),
            lease_seconds=120,
            deadline_monotonic=monotonic() + 30,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        cleanup_future = executor.submit(held_cleanup)
        assert orphan_deleted.wait(timeout=10)
        session_future = executor.submit(create_session)
        assert session_reached_lock.wait(timeout=10)
        assert not session_future.done()
        allow_cleanup_commit.set()
        cleanup_future.result(timeout=10)
        lease = session_future.result(timeout=10)
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        row = connection.execute(
            "SELECT w.workspace_root, s.workspace_id FROM pulsara_v3.workspaces w "
            "JOIN pulsara_v3.sessions s ON s.memory_domain_id=w.memory_domain_id "
            "AND s.workspace_id=w.id WHERE s.id=%s",
            (lease.guard.session_id,),
        ).fetchone()
    assert row == (workspace_root, workspace_id)


def test_new_session_commit_prevents_stale_orphan_workspace_cleanup(
    stage2_migrated_postgres_database,
    monkeypatch: pytest.MonkeyPatch,
):
    repository = _repository(stage2_migrated_postgres_database)
    workspace_root = uuid4().hex
    workspace_id = workspace_context_id(workspace_root)
    workspace_label = Path(workspace_root).name
    runtime_dsn = stage2_migrated_postgres_database.runtime_dsn
    with psycopg.connect(runtime_dsn) as connection:
        lock_canonical_identities(
            connection,
            namespace="workspace",
            memory_domain_id="u_local",
            identities=(workspace_id,),
        )
        connection.execute(
            "INSERT INTO pulsara_v3.workspaces "
            "(memory_domain_id,id,workspace_kind,workspace_root,workspace_label) "
            "VALUES ('u_local',%s,'project',%s,%s)",
            (workspace_id, workspace_root, workspace_label),
        )

    cleanup_planned = Event()
    allow_cleanup_lock = Event()
    cleanup_reached_lock = Event()
    session_has_lock = Event()
    allow_session_finish = Event()
    real_authority_lock = authority_repository.lock_canonical_identities

    def held_authority_lock(connection, **kwargs):
        result = real_authority_lock(connection, **kwargs)
        if workspace_id in tuple(kwargs["identities"]):
            session_has_lock.set()
            assert allow_session_finish.wait(timeout=10)
        return result

    monkeypatch.setattr(
        authority_repository, "lock_canonical_identities", held_authority_lock
    )

    def cleanup_after_initial_plan():
        with psycopg.connect(runtime_dsn) as connection:
            assert connection.execute(
                "SELECT 1 FROM pulsara_v3.workspaces w "
                "WHERE w.memory_domain_id='u_local' AND w.id=%s "
                "AND NOT EXISTS (SELECT 1 FROM pulsara_v3.sessions s "
                "WHERE s.memory_domain_id=w.memory_domain_id "
                "AND s.workspace_id=w.id)",
                (workspace_id,),
            ).fetchone() == (1,)
            cleanup_planned.set()
            assert allow_cleanup_lock.wait(timeout=10)
            cleanup_reached_lock.set()
            lock_canonical_identities(
                connection,
                namespace="workspace",
                memory_domain_id="u_local",
                identities=(workspace_id,),
            )
            return connection.execute(
                "DELETE FROM pulsara_v3.workspaces AS w "
                "WHERE w.memory_domain_id='u_local' AND w.id=%s "
                "AND NOT EXISTS (SELECT 1 FROM pulsara_v3.sessions s "
                "WHERE s.memory_domain_id=w.memory_domain_id "
                "AND s.workspace_id=w.id) "
                "AND NOT EXISTS (SELECT 1 FROM pulsara_v3.memory_facts f "
                "WHERE f.memory_domain_id=w.memory_domain_id "
                "AND f.project_workspace_id=w.id) RETURNING id",
                (workspace_id,),
            ).fetchone()

    def create_session():
        return repository.acquire_host_writer(
            intent="NEW",
            session_id=_name("session"),
            workspace_id=workspace_id,
            workspace_root=workspace_root,
            writer_owner_id=_name("host"),
            lease_seconds=120,
            deadline_monotonic=monotonic() + 30,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        cleanup_future = executor.submit(cleanup_after_initial_plan)
        assert cleanup_planned.wait(timeout=10)
        session_future = executor.submit(create_session)
        assert session_has_lock.wait(timeout=10)
        allow_cleanup_lock.set()
        assert cleanup_reached_lock.wait(timeout=10)
        assert not cleanup_future.done()
        allow_session_finish.set()
        lease = session_future.result(timeout=10)
        assert cleanup_future.result(timeout=10) is None

    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        assert connection.execute(
            "SELECT w.workspace_root, s.workspace_id FROM pulsara_v3.workspaces w "
            "JOIN pulsara_v3.sessions s ON s.memory_domain_id=w.memory_domain_id "
            "AND s.workspace_id=w.id WHERE s.id=%s",
            (lease.guard.session_id,),
        ).fetchone() == (workspace_root, workspace_id)
