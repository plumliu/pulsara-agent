"""The HTTP shell and kernel execute against one set of concrete owners."""

import asyncio
from types import SimpleNamespace

import pytest

from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.desktop_app.application import DesktopApplication
from pulsara_agent.tool_permission import default_permission_policy
from tests.support.model_config import test_model_runtime as make_runtime


def test_explicit_workbench_exit_requires_http_children_to_have_stopped():
    app = object.__new__(DesktopApplication)
    app.core = SimpleNamespace(workbench_exit_ready=True)
    app.http = SimpleNamespace(local_children_stopped=False)
    assert not app.workbench_exit_ready
    app.http.local_children_stopped = True
    assert app.workbench_exit_ready
    app.core.workbench_exit_ready = False
    assert not app.workbench_exit_ready


@pytest.mark.parametrize("source", ["runtime", "core"])
def test_partial_execution_owner_injection_reuses_the_actual_chain(source):
    async def exercise():
        runtime = make_runtime()
        core = KernelHostCore.production(model_runtime=runtime)
        app = DesktopApplication(
            permission_policy=default_permission_policy(),
            **({"core": core} if source == "core" else {"model_runtime": runtime}),
        )
        assert app.model_runtime is runtime
        assert app.core.model_runtime is runtime
        assert app.settings is runtime.settings
        assert app.catalog is runtime.catalog
        assert app.scheduled_tasks is app.core.scheduled_tasks
        await app.aclose()
        if source != "core":
            await core.shutdown()
    asyncio.run(exercise())


@pytest.mark.parametrize("conflict", ["settings", "catalog", "model_runtime", "memory_domain_id"])
def test_explicit_conflicting_owner_is_rejected(conflict):
    async def exercise():
        runtime, other = make_runtime(), make_runtime()
        core = KernelHostCore.production(model_runtime=runtime)
        value = {
            "settings": other.settings, "catalog": other.catalog,
            "model_runtime": other, "memory_domain_id": "other-domain",
        }[conflict]
        try:
            with pytest.raises(ValueError, match="same"):
                DesktopApplication(core=core, permission_policy=default_permission_policy(), **{conflict: value})
        finally:
            await core.shutdown()
    asyncio.run(exercise())
