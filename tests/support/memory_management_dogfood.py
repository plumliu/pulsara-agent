"""Disposable PostgreSQL + real local Web surface for manual memory UI dogfood.

No model calls, credentials, persistent user settings, or user database resets.
Ctrl-C closes the app and drops only the uniquely created test database.
"""

import asyncio
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from unittest.mock import patch
from uuid import uuid4

from pulsara_agent.tool_permission import default_permission_policy
from pulsara_agent.web_app.application import LocalWebApplication
from pulsara_agent.workspace_identity import HostWorkspaceInput, resolve_workspace
from tests.support.model_config import test_model_runtime, acquire_bound_test_writer
from tests.support.postgres_database import (
    create_migrated_postgres_test_database,
    drop_postgres_test_database,
    admin_root_dsn,
)
from tests.test_memory_management_postgres import memory_graph


async def main():
    database = create_migrated_postgres_test_database()
    app = None
    resources = ExitStack()
    try:
        directory = resources.enter_context(
            TemporaryDirectory(prefix="pulsara-memory-ui-")
        )
        root = Path(directory)
        project_root = root / "周末旅行"
        project_root.mkdir()
        project = resolve_workspace(HostWorkspaceInput("project", project_root))
        quick = resolve_workspace(HostWorkspaceInput("transient", root / "quick"))

        # Repository fixtures deliberately use synthetic/removed directory IDs.
        # Browser dogfood instead needs real canonical workspace identities so
        # the unmodified session-resume boundary can reopen source conversations.
        def browser_writer(repository, **kwargs):
            workspace = (
                quick if kwargs.get("workspace_kind") == "transient" else project
            )
            kwargs.update(
                workspace_id=workspace.workspace_key,
                workspace_kind=workspace.workspace_kind,
                workspace_root=str(workspace.workspace_root),
                workspace_label=workspace.display_label,
            )
            return acquire_bound_test_writer(repository, **kwargs)

        def project_writer(repository, *, workspace_id, domain):
            assert workspace_id == project.workspace_key
            return browser_writer(
                repository,
                session_id=str(uuid4()),
                memory_domain_id=domain,
                writer_owner_id=str(uuid4()),
                lease_seconds=30,
                deadline_monotonic=monotonic() + 30,
            )

        resources.enter_context(
            patch(
                "tests.test_memory_management_postgres.acquire_bound_test_writer",
                browser_writer,
            )
        )
        resources.enter_context(
            patch("tests.test_memory_management_postgres._lease", project_writer)
        )
        repo, domain, accept = memory_graph.__wrapped__(database)
        root = accept(
            "用户喜欢在安静的环境中阅读，周末有空时也喜欢散步。", kind="USER_PROFILE"
        )
        accept(
            "介绍旅行路线时，可以参考用户喜欢安静的背景。",
            kind="FACT",
            based_on=(root,),
        )
        accept(
            "这个项目优先安排安静的周末路线。",
            kind="DECISION",
            context=project.workspace_key,
            based_on=(root,),
        )
        old = accept("回答先给简短结论。", kind="RESPONSE_PREFERENCE")
        accept(
            "回答先给结论，再按需要解释细节。",
            kind="RESPONSE_PREFERENCE",
            relation="SUPERSEDE",
            target=old,
        )
        conflict = accept("用户目前通常在周六有空。", kind="FACT")
        accept(
            "用户目前通常在周日有空。",
            kind="FACT",
            relation="CONTRADICT",
            target=conflict,
        )
        c = accept("旅行方案第一版")
        b = accept("旅行方案第二版", relation="SUPERSEDE", target=c)
        accept("旅行方案第三版", relation="SUPERSEDE", target=b)
        runtime = test_model_runtime(postgres_dsn=database.runtime_dsn)
        app = LocalWebApplication(
            settings=runtime.settings,
            model_runtime=runtime,
            catalog=runtime.catalog,
            workspace_input=HostWorkspaceInput(
                "transient", quick.workspace_root, memory_domain_id=domain
            ),
            permission_policy=default_permission_policy(),
        )
        await app.start()
        print(
            f"MEMORY_UI_DOGFOOD {app.origin} database={database.database_name}",
            flush=True,
        )
        await asyncio.Event().wait()
    finally:
        if app is not None:
            await app.aclose()
        drop_postgres_test_database(admin_root_dsn(), database.database_name)
        resources.close()


if __name__ == "__main__":
    asyncio.run(main())
