"""Capability control receipts keep settled facts independently of long reports."""

from pulsara_agent.conversation_kernel.capability_management_execution import (
    capability_management_required_facts,
    capability_reload_required_facts,
)
from pulsara_agent.identity import derive_id


def test_management_receipt_keeps_applied_adoption_and_exact_package_guards():
    target = {"kind": "PLUGIN", "scope": "USER", "plugin_id": "fixture"}
    package = {"visibility": "VISIBLE", "value": derive_id("package", "control-projection")}
    authorization = {"owner": {"source_kind": "PLUGIN", "server_id": "fixture"},
                     "package_install_id": package, "config_identity": "configured-connection"}
    body = {"status": "APPLIED", "action": "SET_PLUGIN_ENABLED", "target": target,
            "adoption": "PARTIAL", "authorization": authorization,
            "current": {"operation_status": "ENABLED", "package_install_id": package,
                        "expected_package_install_id": package,
                        "desired_enabled": True, "cleanup_attention": True,
                        "staging_path": "/private/staging/fixture", "config": {"description": "large" * 20000}},
            "diagnostics": [{"message": "large diagnostic" * 20000}]}
    facts = capability_management_required_facts(body)
    assert facts["status"] == "APPLIED" and facts["adoption"] == "PARTIAL"
    assert facts["target"] == target and "authorization" not in facts
    assert facts["current"] == {"operation_status": "ENABLED", "expected_package_install_id": package,
                                "desired_enabled": True, "cleanup_attention": True}
    assert body["authorization"] == authorization
    assert body["current"]["package_install_id"] == package
    assert body["current"]["staging_path"] == "/private/staging/fixture"
    assert "diagnostics" not in facts and "config" not in facts["current"]
    assert body["diagnostics"] and body["current"]["config"]


def test_reload_receipt_keeps_partial_adoption_and_epoch_fact_without_reloading():
    body = {"status": "PARTIAL", "mcp": "PARTIAL", "plugin_view": "COMPLETE",
            "skill_producer": "COMPLETE", "hook_sources": 2, "same_epoch_prefix": "UNCHANGED",
            "local_hooks": "PENDING", "interruption": "CANCELLED",
            "mcp_changed_server_ids": ["first", "second"],
            "plugin_hook_sources": [{"definitions": "large" * 20000}]}
    facts = capability_reload_required_facts(body)
    assert facts == {"status": "PARTIAL", "mcp": "PARTIAL", "plugin_view": "COMPLETE",
                     "skill_producer": "COMPLETE",
                     "same_epoch_prefix": "UNCHANGED", "local_hooks": "PENDING",
                     "interruption": "CANCELLED"}
    assert len(body["mcp_changed_server_ids"]) == 2



def _budget(accepts=None):
    from pulsara_agent.identity import random_id
    from pulsara_agent.primitives.tool_result_budget import ToolResultPageBudget
    return ToolResultPageBudget(random_id("call"), result_target={"kind": "SESSION_ENTRY",
        "session_id": random_id("session"), "entry_id": random_id("entry")},
        accepts_provider_body=accepts)


def test_low_budget_management_does_not_write_local_mcp_configuration(tmp_path, monkeypatch):
    import asyncio
    from pulsara_agent.capability.mcp_management import LocalMcpTarget
    from pulsara_agent.conversation_kernel.capability_management_execution import CapabilityManagementCall
    from tests.test_capability_management_preparation import preparation
    async def run():
        service = preparation(tmp_path)
        args = {"action": "ADD_LOCAL_MCP", "target": {"kind": "MCP_CONNECTION", "scope": "WORKSPACE",
            "source_kind": "LOCAL", "server_id": "budget-probe"},
            "config": {"transport": {"type": "streamable_http", "endpoint": "https://example.invalid/mcp"}}}
        prepared = await service.prepare(args)
        invoked = []
        original = service.mcp.execute_prepared_mutation
        async def execute(mutation):
            invoked.append(mutation)
            return await original(mutation)
        monkeypatch.setattr(service.mcp, "execute_prepared_mutation", execute)
        try:
            result = await CapabilityManagementCall(service, prepared).execute(page_budget=_budget(lambda *_: False))
            assert result["status"] == "REJECTED" and result["error"] == "OUTPUT_PAGE_BOUNDARY"
            assert result["physical_outcome"] == "NO_EFFECT" and result["retry_performed"] is False
            assert invoked == []
            assert not service.mcp.path(LocalMcpTarget("budget-probe", service.workspace_root)).exists()
        finally:
            await service.mcp.aclose()
    asyncio.run(run())


def test_low_budget_skill_install_preserves_real_destination_without_creating_it(tmp_path, monkeypatch):
    import asyncio
    from pathlib import Path
    from pulsara_agent.capability.local_skill_management import InstallLooseLocalSkillRequest
    from pulsara_agent.capability.local_skill_publisher import LocalSkillInstallScope
    from pulsara_agent.conversation_kernel.capability_management_execution import CapabilityManagementCall
    from tests.test_capability_management_preparation import preparation
    from tests.test_app_capability_boundary import skill_source
    async def run():
        service = preparation(tmp_path)
        source = skill_source(tmp_path)
        args = {"action": "INSTALL_LOOSE_SKILL", "scope": "USER", "source_path": str(source)}
        prepared = await service.prepare(args)
        destination = service.skills.loose_skill_install_destination(
            InstallLooseLocalSkillRequest(Path(source), LocalSkillInstallScope.USER), prepared.skill_validation)
        invoked = []
        original = service.skills.install_loose_local_skill
        def install(*args, **kwargs):
            invoked.append(True)
            return original(*args, **kwargs)
        monkeypatch.setattr(service.skills, "install_loose_local_skill", install)
        quoted = []
        def reject(body, _ids):
            import json
            quoted.append(json.loads(body))
            return False
        try:
            result = await CapabilityManagementCall(service, prepared).execute(page_budget=_budget(reject))
            assert result["physical_outcome"] == "NO_EFFECT" and invoked == []
            assert destination is not None and not destination.exists()
            assert quoted[0]["target"] == {"kind": "SKILL", "skill_path": str(destination / "SKILL.md")}
        finally:
            await service.mcp.aclose()
    asyncio.run(run())


def test_admitted_capability_effect_executes_once_and_quotes_exact_recovery_target(tmp_path, monkeypatch):
    import asyncio
    import json
    import pytest
    from pulsara_agent.capability.mcp_management import LocalMcpTarget
    from pulsara_agent.conversation_kernel.capability_management_execution import CapabilityManagementCall
    from tests.test_capability_management_preparation import preparation
    async def run():
        service = preparation(tmp_path)
        target = {"kind": "MCP_CONNECTION", "scope": "USER", "source_kind": "LOCAL", "server_id": "once"}
        prepared = await service.prepare({"action": "ADD_LOCAL_MCP", "target": target,
            "config": {"transport": {"type": "streamable_http", "endpoint": "https://example.invalid/mcp"}}})
        quoted, invoked = [], []
        original = service.mcp.execute_prepared_mutation
        async def execute(mutation):
            invoked.append(mutation)
            return await original(mutation)
        monkeypatch.setattr(service.mcp, "execute_prepared_mutation", execute)
        def accept(body, _ids):
            quoted.append(json.loads(body))
            return True
        budget = _budget(accept)
        call = CapabilityManagementCall(service, prepared)
        try:
            result = await call.execute(page_budget=budget)
            assert result["status"] == "APPLIED" and len(invoked) == 1
            assert service.mcp.inspect(LocalMcpTarget("once")) is not None
            assert quoted[0]["read"] == budget.read and quoted[0]["target"] == target
            with pytest.raises(RuntimeError, match="cannot execute twice"):
                await call.execute(page_budget=budget)
            assert len(invoked) == 1
        finally:
            await service.mcp.aclose()
    asyncio.run(run())


def test_reload_low_budget_does_not_adopt_future_capabilities():
    import asyncio
    import json
    from types import SimpleNamespace
    from pulsara_agent.conversation_kernel.execution_watchdogs import KernelExecutionDeadlineFactory
    from tests.test_query_runtime_integration import _runtime, _dispatch
    async def run():
        port, context = _runtime(None, None)
        port._deadlines = KernelExecutionDeadlineFactory()
        calls = []
        async def reload(**kwargs):
            calls.append(kwargs)
            return {"status": "RELOADED", "same_epoch_prefix": "UNCHANGED"}
        port._capability_reload = SimpleNamespace(reload_capabilities=reload)
        context.page_budget = _budget(lambda *_: False)
        # This is a standalone dispatch fixture; bind the quote to its exact call.
        from dataclasses import replace
        context.page_budget = replace(context.page_budget, tool_call_id=context.tool_call_id)
        result = await _dispatch(port, context, "reload_capabilities", {})
        value = json.loads(result.content)
        assert calls == [] and result.state == "APPLICATION_ERROR"
        assert value["physical_outcome"] == "NO_EFFECT" and value["error"] == "OUTPUT_PAGE_BOUNDARY"
    asyncio.run(run())
