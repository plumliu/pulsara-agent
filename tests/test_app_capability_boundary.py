"""Observable management contracts for the GUI/Host capability boundary."""

import asyncio
import json
from pathlib import Path

import pytest

from tests.test_capability_management_preparation import preparation, install_plugin
from pulsara_agent.capability.management_form import AcceptedCapabilityFormSubmission
from pulsara_agent.capability.management_intent import (
    parse_capability_management_intent,
)
from pulsara_agent.capability.mcp_management import McpManagementConflict
from pulsara_agent.conversation_kernel.capability_management_execution import (
    CapabilityManagementCall,
)
from pulsara_agent.primitives.context import thaw_json


def skill_source(tmp_path, name="boundary-example"):
    source = tmp_path / "download" / name
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Boundary test Skill.\n---\n\nUse the reference.\n"
    )
    (source / "reference.txt").write_text("preserved resource")
    return source


async def invoke(service, **arguments):
    return await CapabilityManagementCall(
        service, await service.prepare(arguments)
    ).execute()


async def query_sources(service, **arguments):
    from pulsara_agent.conversation_kernel.capability_query import CapabilitySourceQuery
    from pulsara_agent.capability.source_query import parse_query
    from pulsara_agent.model_input.contracts import ModelInputScopeKind
    inspect = "target" in arguments
    return await CapabilitySourceQuery(service, runtime=None, plan=None, scope=ModelInputScopeKind.ROOT).execute(
        parse_query(arguments, inspect=inspect), inspect=inspect)


async def inspect_hook(service, *, scope, source_kind, plugin_id=None):
    target = {"kind": "HOOK_SOURCE", "scope": scope, "source_kind": source_kind}
    if plugin_id is not None:
        target["plugin_id"] = plugin_id
    return await query_sources(service, target=target)


@pytest.mark.parametrize("scope", ["USER", "WORKSPACE"])
def test_skill_lifecycle_keeps_source_and_uses_frozen_target(
    tmp_path, monkeypatch, scope
):
    async def run():
        service = preparation(tmp_path)
        source = skill_source(tmp_path)
        original = (source / "SKILL.md").read_bytes()
        monkeypatch.chdir(source)
        monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "foreign-home"))
        args = dict(action="INSTALL_LOOSE_SKILL", scope=scope, source_path=str(source))
        prepared = await service.prepare(args)
        assert not (service.workspace_root / ".pulsara").exists()
        result = await CapabilityManagementCall(service, prepared).execute()
        assert result["status"] == "APPLIED"
        target_root = (
            tmp_path / "private"
            if scope == "USER"
            else service.workspace_root / ".pulsara"
        )
        skill = target_root / "skills" / "boundary-example" / "SKILL.md"
        assert Path(result["current"]["skill_root"]) == skill.parent
        assert (skill.parent / "reference.txt").read_text() == "preserved resource"
        assert (source / "SKILL.md").read_bytes() == original
        assert not (tmp_path / "foreign-home").exists()
        assert (await invoke(service, **args))["status"] == "CONFLICT"
        inspected = await query_sources(service, kind="SKILL", scope=scope, source_kind="LOCAL")
        assert inspected["completeness"] == "COMPLETE"
        assert inspected["items"][0]["target"]["skill_path"] == str(skill)
        assert result["identity"]["skill_path"] == str(skill)
        enabled = await invoke(
            service,
            action="SET_LOOSE_SKILL_ENABLED",
            scope=scope,
            skill_path=str(skill),
            enabled=False,
        )
        assert enabled["status"] == "APPLIED"
        inspected = await query_sources(service, target={"kind":"SKILL", "skill_path":str(skill)})
        assert inspected["enabled"] is False and inspected["selected"] is False
        removed = await invoke(
            service, action="REMOVE_LOOSE_SKILL", scope=scope, skill_path=str(skill)
        )
        assert removed["status"] == "APPLIED" and not skill.exists()
        await service.mcp.aclose()

    asyncio.run(run())


def test_install_metadata_override_uses_native_candidate_and_preserves_source(tmp_path):
    async def run():
        service = preparation(tmp_path)
        source = skill_source(tmp_path)
        (source / "SKILL.md").write_text(
            "---\nname: boundary-example\n---\n# Imported Skill\n\nInstructions.\n"
        )
        result = await invoke(
            service,
            action="INSTALL_LOOSE_SKILL",
            scope="WORKSPACE",
            source_path=str(source),
            name="imported",
            description="Native normalization.",
        )
        assert result["status"] == "APPLIED"
        assert "description:" not in (source / "SKILL.md").read_text()
        assert (
            "name: imported"
            in (Path(result["current"]["skill_root"]) / "SKILL.md").read_text()
        )
        await service.mcp.aclose()

    asyncio.run(run())


def test_inventory_preserves_shadowed_invalid_and_distinct_removal_eligibility(
    tmp_path,
):
    async def run():
        service = preparation(tmp_path)
        for parent in (
            service.workspace_root / ".pulsara" / "skills",
            service.workspace_root / ".agents" / "skills",
        ):
            source = parent / "same-name"
            source.mkdir(parents=True)
            (source / "SKILL.md").write_text(
                "---\nname: same-name\ndescription: Example.\n---\nBody\n"
            )
        invalid = service.workspace_root / ".pulsara" / "skills" / "invalid"
        invalid.mkdir()
        (invalid / "SKILL.md").write_text("invalid")
        observed = await query_sources(service, kind="SKILL", scope="WORKSPACE", source_kind="LOCAL")
        items = [await query_sources(service, target=row["target"]) for row in observed["items"] if row["name"] == "same-name"]
        assert len(items) == 2 and {item["selected"] for item in items} == {False, True}
        agent = next(item for item in items if ".agents" in item["skill_path"])
        assert agent["enable_eligible"] and not agent["remove_eligible"]
        invalid_item = next(row for row in observed["items"] if row["name"] == "invalid")
        assert invalid_item["target"]["skill_path"] == str(invalid / "SKILL.md")
        assert observed["completeness"] == "PARTIAL"
        assert (
            await invoke(
                service,
                action="SET_LOOSE_SKILL_ENABLED",
                scope="WORKSPACE",
                skill_path=agent["skill_path"],
                enabled=False,
            )
        )["status"] == "APPLIED"
        with pytest.raises(ValueError):
            await service.prepare(
                dict(
                    action="REMOVE_LOOSE_SKILL",
                    scope="WORKSPACE",
                    skill_path=agent["skill_path"],
                )
            )
        assert (
            await invoke(
                service,
                action="REMOVE_LOOSE_SKILL",
                scope="WORKSPACE",
                skill_path=str(invalid / "SKILL.md"),
            )
        )["status"] == "APPLIED"
        await service.mcp.aclose()

    asyncio.run(run())


def hook_document(command="printf 'original  bytes'"):
    return {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "read_file",
                    "hooks": [
                        {
                            "type": "command",
                            "command": command,
                            "commandWindows": "Write-Output 'windows'",
                            "timeout": 7,
                            "async": False,
                            "statusMessage": "review this",
                            "additionalContextLimit": 300,
                        }
                    ],
                }
            ]
        }
    }


def write_hook(service, document=None):
    path = service.workspace_root / ".pulsara" / "hooks.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(document or hook_document()))
    return path


def test_hook_trust_requires_full_review_then_future_source_adopts_trust(tmp_path):
    async def run():
        service = preparation(tmp_path)
        write_hook(service)
        args = dict(action="TRUST_HOOK_SOURCE", scope="WORKSPACE", source_kind="LOCAL")
        prepared = await service.prepare(args)
        assert prepared.user_inputs == ("hook_source_review",)
        assert (
            prepared.effects.outside_workspace_write
            and not prepared.effects.process_control
        )
        public = thaw_json(prepared.public_prefill)["hook_source"]
        assert public["definitions"][0]["command"] == "printf 'original  bytes'"
        assert public["definitions"][0]["additionalContextLimit"] == 300
        assert public["definitions"][0]["commandWindows"] == "Write-Output 'windows'"
        call = CapabilityManagementCall(service, prepared)
        form = call.form("review")
        with pytest.raises(ValueError):
            await form.prepare_submission({})
        with pytest.raises(ValueError):
            await form.prepare_submission({"enable_review_accepted": True})
        values = await form.prepare_submission({"hook_review_accepted": True})
        assert not service.hooks.discover().source_snapshots[-1].runnable
        call.accept(AcceptedCapabilityFormSubmission(values))
        assert (await call.execute())["status"] == "APPLIED"
        assert service.hooks.discover().source_snapshots[-1].runnable
        await invoke(
            service,
            action="SET_HOOK_SOURCE_ENABLED",
            scope="WORKSPACE",
            source_kind="LOCAL",
            enabled=False,
        )
        assert not service.hooks.discover().source_snapshots[-1].runnable
        await invoke(
            service, action="REVOKE_HOOK_TRUST", scope="WORKSPACE", source_kind="LOCAL"
        )
        await invoke(
            service,
            action="SET_HOOK_SOURCE_ENABLED",
            scope="WORKSPACE",
            source_kind="LOCAL",
            enabled=True,
        )
        assert not service.hooks.discover().source_snapshots[-1].runnable
        await service.mcp.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("after_submission", [False, True])
def test_hook_review_never_retargets_changed_definitions(tmp_path, after_submission):
    async def run():
        service = preparation(tmp_path)
        path = write_hook(service)
        call = CapabilityManagementCall(
            service,
            await service.prepare(
                dict(action="TRUST_HOOK_SOURCE", scope="WORKSPACE", source_kind="LOCAL")
            ),
        )
        form = call.form("review")
        if after_submission:
            call.accept(
                AcceptedCapabilityFormSubmission(
                    await form.prepare_submission({"hook_review_accepted": True})
                )
            )
        path.write_text(json.dumps(hook_document("printf changed")))
        with pytest.raises(McpManagementConflict):
            if after_submission:
                await call.execute()
            else:
                await form.prepare_submission({"hook_review_accepted": True})
        assert not service.hooks.discover().source_snapshots[-1].runnable
        await service.mcp.aclose()

    asyncio.run(run())


def test_user_hook_inspection_does_not_read_project_and_never_enables_plugin(
    tmp_path, monkeypatch
):
    async def run():
        service = preparation(tmp_path)
        write_hook(service, {"invalid": "project config"})
        await install_plugin(service, tmp_path)
        original = service.hooks._read_source

        def user_only(**kwargs):
            assert kwargs["workspace_key"] is None
            return original(**kwargs)

        monkeypatch.setattr(service.hooks, "_read_source", user_only)
        result = await query_sources(service, kind="HOOK_SOURCE", scope="USER", source_kind="LOCAL")
        assert result["completeness"] == "COMPLETE" and len(result["items"]) == 1
        plugin = await service._plugin(None, "example")
        assert not plugin.enabled
        await service.mcp.aclose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "action",
    [
        "INSTALL_LOOSE_SKILL",
        "TRUST_HOOK_SOURCE",
    ],
)
def test_transient_workspace_rejects_project_capability_scope(tmp_path, action):
    async def run():
        service = preparation(tmp_path)
        service.workspace_kind = "transient"
        fields = (
            {"source_path": str(skill_source(tmp_path))}
            if action == "INSTALL_LOOSE_SKILL"
            else {"source_kind": "LOCAL"}
            if action == "TRUST_HOOK_SOURCE"
            else {}
        )
        with pytest.raises(ValueError, match="GUI project"):
            await service.prepare(dict(action=action, scope="WORKSPACE", **fields))
        assert not (service.workspace_root / ".pulsara").exists()
        await service.mcp.aclose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "fields",
    [
        {"source_kind": "PLUGIN"},
        {"source_kind": "LOCAL", "plugin_id": "example"},
        {"source_kind": "LOCAL", "hook_review_accepted": True},
        {"source_kind": "LOCAL", "expected_digest": "model-supplied"},
        {
            "source_kind": "PLUGIN",
            "plugin_id": "example",
            "config_relative_path": "elsewhere",
        },
    ],
)
def test_hook_model_intent_is_closed_and_cannot_supply_review_authority(fields):
    with pytest.raises(ValueError):
        parse_capability_management_intent(
            dict(action="TRUST_HOOK_SOURCE", scope="USER", **fields)
        )


def test_disabled_plugin_hook_full_inspection_trust_and_enable_are_separate(tmp_path):
    async def run():
        service = preparation(tmp_path)
        await install_plugin(service, tmp_path, hooks=hook_document())
        observed = await inspect_hook(
            service,
            scope="USER",
            source_kind="PLUGIN",
            plugin_id="example",
        )
        source = observed
        assert (
            source["authorization"] == "UNTRUSTED" and source["selected"] is False
        )
        assert source["definitions"][0]["event"] == "PreToolUse"
        assert source["definitions"][0]["command"]
        assert "declaration_environment" not in source and "definition_digest" not in source
        call = CapabilityManagementCall(
            service,
            await service.prepare(
                dict(
                    action="TRUST_HOOK_SOURCE",
                    scope="USER",
                    source_kind="PLUGIN",
                    plugin_id="example",
                )
            ),
        )
        form = call.form("review")
        call.accept(
            AcceptedCapabilityFormSubmission(
                await form.prepare_submission({"hook_review_accepted": True})
            )
        )
        assert (await call.execute())["status"] == "APPLIED"
        plugin = await service._plugin(None, "example")
        assert not plugin.enabled
        observed = await inspect_hook(
            service,
            scope="USER",
            source_kind="PLUGIN",
            plugin_id="example",
        )
        source = observed
        assert source["authorization"] == "TRUSTED" and source["selected"] is False
        enable = CapabilityManagementCall(
            service,
            await service.prepare(
                dict(
                    action="SET_PLUGIN_ENABLED",
                    scope="USER",
                    plugin_id="example",
                    enabled=True,
                )
            ),
        )
        form = enable.form("review")
        enable.accept(
            AcceptedCapabilityFormSubmission(
                await form.prepare_submission({"enable_review_accepted": True})
            )
        )
        assert (await enable.execute())["status"] == "APPLIED"
        observed = await inspect_hook(
            service,
            scope="USER",
            source_kind="PLUGIN",
            plugin_id="example",
        )
        assert observed["authorization"] == "TRUSTED" and observed["selected"] is None
        assert (await service._plugin(None, "example")).enabled
        await service.mcp.aclose()

    asyncio.run(run())


def test_source_change_during_skill_confirmation_is_a_conflict(tmp_path):
    async def run():
        service = preparation(tmp_path)
        source = skill_source(tmp_path)
        call = CapabilityManagementCall(
            service,
            await service.prepare(
                dict(
                    action="INSTALL_LOOSE_SKILL",
                    scope="WORKSPACE",
                    source_path=str(source),
                )
            ),
        )
        form = call.form("review")
        (source / "SKILL.md").write_text(
            "---\nname: different\ndescription: Changed.\n---\nChanged body\n"
        )
        with pytest.raises(McpManagementConflict):
            await form.prepare_submission({})
        assert not (service.workspace_root / ".pulsara").exists()
        await service.mcp.aclose()

    asyncio.run(run())


def test_capability_refresh_publishes_local_hooks_and_reports_partial_parts():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from pulsara_agent.conversation_kernel.host import KernelHostSession

    async def run():
        async def serialized(deadline):
            return {"status": "RELOADED", "mcp": "RELOADED", "plugin_view": "COMPLETE"}

        session = SimpleNamespace(
            _require_open=lambda: None,
            _capability_reload_settlement_lock=asyncio.Lock(),
            mcp_management=SimpleNamespace(load_configs=lambda **kwargs: ()),
            workspace=SimpleNamespace(
                workspace_root=Path("/project"), trust_workspace_mcp_config=False
            ),
            _local_mcp_configs=(),
            _plugin_view=object(),
            _reload_capabilities_serialized=serialized,
            reload_hooks=AsyncMock(
                return_value={
                    "status": "RELOADED",
                    "sources": [
                        {
                            "source": "WORKSPACE_FILE",
                            "scan": "UNAVAILABLE",
                            "trust": "UNAVAILABLE",
                        }
                    ],
                }
            ),
        )
        result = await KernelHostSession.reload_capabilities(
            session, deadline_monotonic=100000000000.0
        )
        assert result["status"] == "PARTIAL" and result["local_hooks"] == "PARTIAL"
        assert result["mcp"] == "RELOADED" and result["plugin_view"] == "COMPLETE"
        session.reload_hooks.assert_awaited_once()

    asyncio.run(run())


@pytest.mark.parametrize("published", [False, True])
def test_skill_install_cancellation_signals_native_publisher_and_joins_exact_outcome(
    tmp_path, monkeypatch, published
):
    import threading
    import pulsara_agent.capability.local_skill_publisher as publisher

    async def run():
        service = preparation(tmp_path)
        source = skill_source(tmp_path)
        started, release = threading.Event(), threading.Event()
        seen = {}
        if published:
            original = service.skills.install_loose_local_skill

            def paused(request, *, cancellation):
                seen["probe"] = cancellation
                outcome = original(request, cancellation=cancellation)
                started.set()
                assert release.wait(5), "test did not release physical owner"
                return outcome

            monkeypatch.setattr(service.skills, "install_loose_local_skill", paused)
        else:
            original = publisher._copy_source_to_stage

            def paused(source_fd, stage_fd, observation, probe):
                seen["probe"] = probe
                started.set()
                assert release.wait(5), "test did not release physical owner"
                return original(source_fd, stage_fd, observation, probe)

            monkeypatch.setattr(publisher, "_copy_source_to_stage", paused)
        prepared = await service.prepare(
            dict(
                action="INSTALL_LOOSE_SKILL", scope="WORKSPACE", source_path=str(source)
            )
        )
        task = asyncio.create_task(
            CapabilityManagementCall(service, prepared).execute()
        )
        try:
            assert await asyncio.to_thread(started.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
            assert seen["probe"].cancellation_requested()
            assert not task.done(), "waiter must join the admitted native worker"
            release.set()
            result = await task
        finally:
            release.set()
        target = service.workspace_root / ".pulsara" / "skills" / "boundary-example"
        assert result["status"] == ("APPLIED" if published else "CANCELLED")
        assert target.exists() is published
        assert not list(target.parent.glob(".pulsara-skill-install-*"))
        await service.mcp.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("cancel_local", [True, False])
def test_refresh_keeps_settled_mcp_truth_and_parts_after_local_failure(cancel_local):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from pulsara_agent.conversation_kernel.host import KernelHostSession

    async def run():
        old, new = ("old-config",), ("new-config",)

        async def serialized(deadline):
            session._plugin_view = object()
            return {
                "status": "RELOADED",
                "mcp": "RELOADED",
                "plugin_view": "COMPLETE",
                "skill_producer": "COMPLETE",
            }

        session = SimpleNamespace(
            _require_open=lambda: None,
            _capability_reload_settlement_lock=asyncio.Lock(),
            mcp_management=SimpleNamespace(load_configs=lambda **kwargs: new),
            workspace=SimpleNamespace(
                workspace_root=Path("/project"), trust_workspace_mcp_config=False
            ),
            _local_mcp_configs=old,
            _plugin_view=object(),
            _reload_capabilities_serialized=serialized,
            reload_hooks=AsyncMock(
                side_effect=asyncio.CancelledError()
                if cancel_local
                else OSError("unreadable")
            ),
        )
        result = await KernelHostSession.reload_capabilities(
            session, deadline_monotonic=100000000000.0
        )
        assert session._local_mcp_configs is new
        assert result["mcp"] == "RELOADED" and result["plugin_view"] == "COMPLETE"
        assert result["status"] == result["local_hooks"] == "PARTIAL"
        assert not session._capability_reload_settlement_lock.locked()

    asyncio.run(run())


def test_refresh_abort_before_any_publication_preserves_predecessor():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from pulsara_agent.conversation_kernel.host import KernelHostSession

    async def run():
        old = ("old-config",)
        session = SimpleNamespace(
            _require_open=lambda: None,
            _capability_reload_settlement_lock=asyncio.Lock(),
            mcp_management=SimpleNamespace(
                load_configs=lambda **kwargs: ("new-config",)
            ),
            workspace=SimpleNamespace(
                workspace_root=Path("/project"), trust_workspace_mcp_config=False
            ),
            _local_mcp_configs=old,
            _plugin_view=object(),
            _reload_capabilities_serialized=AsyncMock(
                side_effect=asyncio.CancelledError()
            ),
            reload_hooks=AsyncMock(),
        )
        with pytest.raises(asyncio.CancelledError):
            await KernelHostSession.reload_capabilities(
                session, deadline_monotonic=100000000000.0
            )
        assert session._local_mcp_configs is old
        session.reload_hooks.assert_not_awaited()

    asyncio.run(run())


def test_unavailable_native_hook_composition_remains_unknown_in_query(
    tmp_path, monkeypatch
):
    import pulsara_agent.plugins.inspection as inspection

    async def run():
        service = preparation(tmp_path)
        await install_plugin(service, tmp_path, hooks=hook_document())
        enable = CapabilityManagementCall(
            service,
            await service.prepare(
                dict(
                    action="SET_PLUGIN_ENABLED",
                    scope="USER",
                    plugin_id="example",
                    enabled=True,
                )
            ),
        )
        form = enable.form("review")
        enable.accept(
            AcceptedCapabilityFormSubmission(
                await form.prepare_submission({"enable_review_accepted": True})
            )
        )
        await enable.execute()

        def unavailable(**kwargs):
            raise OSError("composition unavailable fixture")

        monkeypatch.setattr(inspection, "compose_hook_definition_view", unavailable)
        result = await query_sources(service, kind="HOOK_SOURCE", source_kind="PLUGIN")
        assert result["completeness"] == "PARTIAL"
        assert result["items"] and "来源已禁用" not in result["items"][0]["status"]
        source = await inspect_hook(service, scope="USER", source_kind="PLUGIN", plugin_id="example")
        assert source["definitions"] and source["selected"] is None
        assert result["diagnostics"]
        await service.mcp.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("cancel_mcp", [False, True, "timeout_then_cancel"])
def test_serialized_reload_reports_plugin_hook_failure_and_joins_mcp_cut(
    tmp_path, monkeypatch, cancel_mcp
):
    from dataclasses import replace
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from pulsara_agent.conversation_kernel import host as host_module
    from pulsara_agent.conversation_kernel.host import KernelHostSession
    from pulsara_agent.hooks.contracts import (
        FrozenHookDefinitionView,
        HookDiagnostic,
        HookTrustDisposition,
    )
    from pulsara_agent.plugins.contracts import EnabledPluginViewDisposition
    from pulsara_agent.plugins.view import FrozenEnabledPluginView
    from pulsara_agent.plugins.skill_producer import PluginSkillDefinitionProducer

    async def run():
        service = preparation(tmp_path)
        await install_plugin(service, tmp_path, hooks=hook_document())
        plugin = await service._plugin(None, "example")
        snapshot = service._plugin_hook_snapshot(plugin)
        snapshot = replace(
            snapshot,
            trust=replace(snapshot.trust, disposition=HookTrustDisposition.UNAVAILABLE),
            diagnostics=(
                HookDiagnostic("HOOK_TRUST_STATE_UNAVAILABLE", "fixture read failure"),
            ),
        )
        monkeypatch.setattr(
            host_module,
            "compose_hook_definition_view",
            lambda **kwargs: FrozenHookDefinitionView((snapshot,)),
        )
        initial = FrozenEnabledPluginView(EnabledPluginViewDisposition.COMPLETE)
        replacement = FrozenEnabledPluginView(EnabledPluginViewDisposition.COMPLETE)
        started, release = asyncio.Event(), asyncio.Event()
        timed_out = asyncio.Event()
        if cancel_mcp == "timeout_then_cancel":
            original_wait_for = asyncio.wait_for

            async def timeout_mcp_confirmation(awaitable, timeout):
                if isinstance(awaitable, asyncio.Future):
                    await started.wait()
                    awaitable.cancel()
                    timed_out.set()
                    raise TimeoutError
                return await original_wait_for(awaitable, timeout)

            monkeypatch.setattr(
                host_module.asyncio, "wait_for", timeout_mcp_confirmation
            )

        async def mcp_reload(configs, *, deadline_monotonic):
            started.set()
            if cancel_mcp:
                await release.wait()
            return frozenset()

        async def publish_slice(
            *, plugin_snapshots, deadline_monotonic, publish_scanned_view
        ):
            replacement_hooks = FrozenHookDefinitionView(plugin_snapshots)
            assert await publish_scanned_view(
                FrozenHookDefinitionView(()), replacement_hooks
            )
            return replacement_hooks

        session = SimpleNamespace(
            _plugin_view=initial,
            _plugin_view_owner=SimpleNamespace(
                observe_for_runtime=lambda **kwargs: replacement,
                current_state=lambda identity: None,
            ),
            workspace=SimpleNamespace(
                workspace_root=service.workspace_root, workspace_kind="project"
            ),
            _plugin_skill_producer=PluginSkillDefinitionProducer(),
            _hook_source_provider=service.hooks,
            _local_mcp_configs=(),
            mcp_management=service.mcp,
            _lock=asyncio.Lock(),
            _closing=False,
            _closed=False,
            _hooks=SimpleNamespace(
                publish_plugin_slice=publish_slice,
                publish_scanned_view=lambda predecessor, replacement: True,
            ),
            _tools=SimpleNamespace(
                reload_mcp_configs=AsyncMock(side_effect=mcp_reload)
            ),
        )
        task = asyncio.create_task(
            KernelHostSession._reload_capabilities_serialized(session, 100000000000.0)
        )
        if cancel_mcp:
            await (timed_out if cancel_mcp == "timeout_then_cancel" else started).wait()
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            release.set()
        result = await task
        assert session._plugin_view is replacement
        assert result["status"] == "PARTIAL"
        assert result["mcp"] == ("PARTIAL" if cancel_mcp else "RELOADED")
        if cancel_mcp:
            assert result["interruption"] == "CANCELLED"
        assert result["plugin_view"] == result["skill_producer"] == "COMPLETE"
        source = result["plugin_hook_sources"][0]
        assert (
            source["plugin_id"] == "example"
            and source["trust_disposition"] == "UNAVAILABLE"
        )
        assert source["path"].endswith("dev.pulsara/hooks/hooks.json")
        assert source["diagnostics"][0]["code"] == "HOOK_TRUST_STATE_UNAVAILABLE"
        replacement.close()
        await service.mcp.aclose()

    asyncio.run(run())
