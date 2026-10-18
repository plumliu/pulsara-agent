"""Real-provider capability UI provenance and damaged-owner recovery, isolated."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.capability.local_skill_management import LocalSkillManagementService
from pulsara_agent.capability.local_skill_publisher import LocalSkillInstallScope
from pulsara_agent.capability.mcp_management import LocalMcpTarget
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.llm.input import PromptContent
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, ModelsDevCatalogClient
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.settings import LocalSettingsStore, LocalPostgresConfig
from pulsara_agent.tool_permission import preset_to_policy
from pulsara_agent.desktop_app.session_controller import LocalSessionController
from tests.dogfood.credentials import secret_scrubber
from tests.dogfood.run_model_switch_handover_dogfood import (
    _ReadOnlySettingsStore,
    _RecordingModelRuntime,
    _binding,
    _create_database,
    _drop_database,
)


async def run(selected=None):
    selected = set(selected or ("ui", "plugin", "skill", "mcp", "hook"))
    saved = LocalSettingsStore(require_pulsara_home() / "local-settings.yaml").read()
    connection = next(
        (
            c
            for c in saved.model_connections
            if c.target.model_id == "deepseek-flash"
            and c.target.wire_api.value == "openai_chat_completions"
        ),
        None,
    )
    if connection is None or saved.model_api_key(connection.id) is None:
        raise RuntimeError(
            "Saved deepseek-flash Chat connection/credential is required"
        )
    scrub = secret_scrubber(saved)
    evidence = {
        "model": connection.target.model_id,
        "calls": [],
        "turns": [],
        "checks": {},
        "scenarios": sorted(selected),
    }
    suffix = "" if len(selected) == 5 else "-" + "-".join(sorted(selected))
    output = Path(
        "output/diagnostics/capability-change-observation/real-provider"
        + suffix
        + ".json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    database, _, admin, runtime_dsn = _create_database(saved)
    original_home = os.environ.get("PULSARA_HOME")
    try:
        with TemporaryDirectory(prefix="pulsara-capability-change-") as temporary:
            base = Path(temporary).resolve()
            home = base / "home"
            home.mkdir()
            os.environ["PULSARA_HOME"] = str(home)
            settings = _ReadOnlySettingsStore(
                replace(
                    saved,
                    postgres=LocalPostgresConfig(runtime_dsn, admin),
                    mcp_credentials=(),
                    mcp_oauth=(),
                )
            )
            catalog = ModelCatalogOwner(ModelsDevCatalogClient())
            await catalog.refresh()
            delegate = ModelRuntime.production(settings=settings, catalog=catalog)
            runtime = _RecordingModelRuntime(delegate, evidence["calls"], None)
            core = KernelHostCore.production(model_runtime=runtime)
            controller = LocalSessionController(
                core=core,
                permission_policy=preset_to_policy("bypass-permissions"),
                active_skill_names=frozenset(),
            )
            installed_inputs = {}

            async def session(name):
                root = base / name
                root.mkdir()
                handle = await controller.create_session(
                    workspace_kind="project",
                    workspace_path=str(root),
                    model_call_binding=_binding(delegate, connection),
                )
                host = handle.session
                previous = host._runner._provider_input_installed_observer

                def observed(request):
                    compiled = request.compiled_input
                    inputs = installed_inputs.setdefault(host.session_id, [])
                    if inputs:
                        old = inputs[-1]
                        assert compiled.system_prompt == old.system_prompt
                        assert compiled.tools == old.tools
                        assert compiled.messages[: len(old.messages)] == old.messages
                    inputs.append(compiled)
                    if previous:
                        previous(request)

                host._runner._provider_input_installed_observer = observed
                return host

            async def turn(host, label, text):
                print(f"Running {label}", flush=True)
                before = len(evidence["calls"])
                result = await host.run_turn(PromptContent.text(text))
                evidence["turns"].append(
                    {
                        "label": label,
                        "session_id": host.session_id,
                        "prompt": text,
                        "reply": result.final_text,
                        "calls": [before, len(evidence["calls"])],
                    }
                )
                print(
                    scrub.scrub_text(f"Completed {label}: {result.final_text[:160]}"),
                    flush=True,
                )
                assert result.final_text
                actions = {
                    json.loads(block["arguments"])["action"]
                    for call in evidence["calls"][before:]
                    for block in call["normalized_blocks"]
                    if block.get("kind") == "tool_call"
                    and block.get("tool_name") == "manage_capability"
                }
                expected_actions = {
                    "plugin_reinstall": {"REMOVE_PLUGIN", "INSTALL_PLUGIN"},
                    "skill_reinstall": {"REMOVE_LOOSE_SKILL", "INSTALL_LOOSE_SKILL"},
                    "mcp_recreate": {"ADD_LOCAL_MCP"},
                    "hook_disable_revoke": {
                        "REVOKE_HOOK_TRUST",
                        "SET_HOOK_SOURCE_ENABLED",
                    },
                    "hook_bad_trust_disable_revoke": {
                        "REVOKE_HOOK_TRUST",
                        "SET_HOOK_SOURCE_ENABLED",
                    },
                }
                if label in expected_actions:
                    assert expected_actions[label].issubset(actions), (label, actions)
                if label.endswith("diagnose") or label == "mcp_bad_config":
                    assert not actions, "Diagnosis must not mutate capability state"
                if label in {"ui_deleted", "hook_gui_notice"}:
                    assert not any(
                        block.get("kind") == "tool_call"
                        for call in evidence["calls"][before:]
                        for block in call["normalized_blocks"]
                    ), "Acknowledging a settled UI fact must not need another query"
                return result

            def source(name):
                path = base / "sources" / name
                path.mkdir(parents=True)
                (path / "SKILL.md").write_text(
                    f"---\nname: {name}\ndescription: Isolated test Skill.\n---\n\nOnly respond to an explicit request for this demo.\n"
                )
                return path

            try:
                if "ui" in selected:
                    ui = await session("ui")
                    original = source("ui-demo")
                    installed = await controller.install_user_skill(
                        source_path=str(original)
                    )
                    assert installed["operation"]["status"] == "INSTALLED"
                    await turn(
                        ui, "ui_baseline", "这是隔离验证会话，请只回复准备好了。"
                    )
                    path = home / "skills" / "ui-demo" / "SKILL.md"
                    expected = (
                        LocalSkillManagementService().inspect_loose_skill_removal(
                            skill_path=path, scope=LocalSkillInstallScope.USER
                        )
                    )
                    await controller.remove_user_skill(
                        skill_path=str(path), expected=expected
                    )
                    answer = await turn(
                        ui,
                        "ui_deleted",
                        "我刚刚在能力页删除了 ui-demo。你能看到这次操作的系统提示吗？简短说明你看到的来源和结果。",
                    )
                    assert "能力" in answer.final_text and (
                        "删除" in answer.final_text or "REMOVED" in answer.final_text
                    )
                    assert ui._pending_capability_change is None
                    await turn(ui, "ui_consumed", "明白了，请只回复收到。")
                    latest = evidence["calls"][-1]["provider_input"]
                    notices = []
                    for message in latest["messages"]:
                        content = message.get("content")
                        if isinstance(content, str) and content.startswith(
                            '{"pulsara_runtime_observation":'
                        ):
                            observation = json.loads(content)[
                                "pulsara_runtime_observation"
                            ]
                            if observation["source"] == "CAPABILITY_CHANGE":
                                notices.append(observation)
                    assert len(notices) == 2 and all(
                        notice["presence"] == "VALUE" for notice in notices
                    )
                    assert [
                        json.loads(notice["body"])["action"] for notice in notices
                    ] == [
                        "INSTALL_LOOSE_SKILL",
                        "REMOVE_LOOSE_SKILL",
                    ]
                    evidence["checks"]["ui_notice_consumed_once"] = True

                if "plugin" in selected:
                    plugin_source = base / "sources" / "plugin"
                    plugin_source.mkdir(parents=True)
                    (plugin_source / "plugin.json").write_text(
                        json.dumps(
                            {
                                "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
                                "name": "damage-demo",
                            }
                        )
                    )
                    published = await controller.install_user_plugin(
                        source_path=str(plugin_source)
                    )
                    assert published["operation"]["success"]
                    inspection = await core.inspect_user_plugins()
                    installed_instance = next(
                        item
                        for item in inspection.instances
                        if item.identity.plugin_id == "damage-demo"
                    )
                    package = installed_instance.package_root
                    package_id = installed_instance.package_install_id
                    inspection.close()
                    enabled = await controller.set_user_plugin_enabled(
                        plugin_id="damage-demo",
                        package_install_id=package_id,
                        enabled=True,
                        connection_review=(),
                    )
                    assert enabled["operation"]["success"]
                    os.chmod(package, 0o700)
                    (package / "plugin.json").unlink()
                    plugin = await session("plugin")
                    await turn(
                        plugin,
                        "plugin_diagnose",
                        "请按 pulsara-plugin-installer 的诊断指导，排查 USER 范围已经启用的 damage-demo 为什么不能用了。只排查，不修改或卸载；简短说明事实和边界。",
                    )
                    await turn(
                        plugin,
                        "plugin_reinstall",
                        f"请正式卸载这个损坏的 damage-demo，然后从 {plugin_source} 重新安装到 USER，保持禁用。不要直接改受管理 package 或实例文件。",
                    )
                    inspection = await core.inspect_user_plugins()
                    item = next(
                        item
                        for item in inspection.instances
                        if item.identity.plugin_id == "damage-demo"
                    )
                    assert (
                        not item.enabled
                        and (item.package_root / "plugin.json").is_file()
                    )
                    inspection.close()
                    evidence["checks"]["plugin_reinstalled"] = True

                if "skill" in selected:
                    skill_src = source("damaged-skill")
                    assert (
                        await controller.install_user_skill(source_path=str(skill_src))
                    )["operation"]["status"] == "INSTALLED"
                    skill_path = home / "skills" / "damaged-skill" / "SKILL.md"
                    skill_path.unlink()
                    skill = await session("skill")
                    await turn(
                        skill,
                        "skill_diagnose",
                        f"USER 的 loose Skill 损坏了，安装目标是 {skill_path}。请只排查。",
                    )
                    await turn(
                        skill,
                        "skill_reinstall",
                        f"请正式删除这个损坏的安装副本，再从 {skill_src} 安装到 USER。",
                    )
                    assert skill_path.is_file() and (skill_src / "SKILL.md").is_file()
                    evidence["checks"]["skill_reinstalled"] = True

                if "mcp" in selected:
                    mcp = await session("mcp")
                    await controller.create_user_mcp_server(
                        server_id="damaged-mcp",
                        config={
                            "enabled": False,
                            "transport": {
                                "type": "stdio",
                                "command": "/missing/demo-mcp",
                            },
                        },
                    )
                    core.mcp_management.user_config_path.write_text("servers: [")
                    await turn(
                        mcp,
                        "mcp_bad_config",
                        "请按 pulsara-mcp-installer 技能的诊断指导，排查 USER 的 damaged-mcp 为什么用不了。只排查，不要改配置或执行它；简短说明事实和处理边界。",
                    )
                    assert (
                        core.mcp_management.user_config_path.read_text() == "servers: ["
                    )
                    core.mcp_management.user_config_path.unlink()
                    await turn(
                        mcp,
                        "mcp_recreate",
                        "我已删除坏掉的 MCP 配置文件。请在 USER 重新新增 damaged-mcp：enabled=false，stdio command=/missing/demo-mcp，args=[]。保持禁用，不要测试执行。",
                    )
                    configured = core.mcp_management.inspect(
                        LocalMcpTarget("damaged-mcp")
                    )
                    assert configured is not None and not configured.enabled
                    evidence["checks"]["mcp_missing_add_and_bad_config_preserved"] = (
                        True
                    )

                if "hook" in selected:
                    hook = await session("hook")
                    await controller.save_user_hook_config(
                        exists=False, expected_text="", text='{"hooks":{}}'
                    )
                    await turn(
                        hook,
                        "hook_gui_notice",
                        "我刚刚在能力页保存了 Hook 配置，你能看到这次操作提示吗？不必执行 Hook。",
                    )
                    (home / "hooks.json").write_text("{bad")
                    await turn(
                        hook,
                        "hook_diagnose",
                        "请按 pulsara-plugin-installer 的 Hook 诊断指导，排查 USER 的本地 Hook 定义。只排查，不修改或运行脚本，简短说明。",
                    )
                    await turn(
                        hook,
                        "hook_disable_revoke",
                        "请撤销 USER 本地 Hook 来源的信任，并停用这个来源。不要修改定义文件，不要授信或运行任何脚本。",
                    )
                    sources = (
                        controller._user_hook_provider().discover().source_snapshots
                    )
                    assert all(not snapshot.runnable for snapshot in sources)
                    assert (home / "hooks.json").read_text() == "{bad"
                    evidence["checks"][
                        "hook_disable_revoke_without_definition_repair"
                    ] = True
                    # The definitions are now readable, but a broken trust row
                    # must not expose its owner's conservative enabled default.
                    from pulsara_agent.hooks.contracts import HookSourceKind
                    from pulsara_agent.hooks.presentation import hook_snapshot_model_public

                    (home / "hooks.json").write_text('{"hooks":{}}')
                    provider = controller._user_hook_provider()
                    trust_path = provider.trust_store._state_path(
                        provider.source_subject(HookSourceKind.USER_FILE)
                    )
                    trust_path.write_text("{bad")
                    snapshot = next(s for s in provider.discover().source_snapshots if s.provenance.identity.kind is HookSourceKind.USER_FILE)
                    assert hook_snapshot_model_public(snapshot)["enabled"] is None
                    await turn(
                        hook,
                        "hook_bad_trust_diagnose",
                        "USER 本地 Hook 又出现了异常，请按已有 Hook 诊断指导检查当前定义和来源状态。只排查，不修改、不运行脚本，简短说明能确认和不能确认的状态。",
                    )
                    assert trust_path.read_text() == "{bad"
                    await turn(
                        hook,
                        "hook_bad_trust_disable_revoke",
                        "请通过正式入口撤销 USER 本地 Hook 来源的信任，并停用这个来源，不要改定义或授信。",
                    )
                    state = provider.trust_store.read(provider.source_subject(HookSourceKind.USER_FILE))
                    assert not state.enabled and state.trusted_definition_digest is None
                    evidence["checks"]["hook_bad_trust_unknown_then_native_disable_revoke"] = True
                evidence["checks"]["same_epoch_prefix_continuity"] = True
                evidence["status"] = "PASSED"
            finally:
                await controller.aclose()
                await core.shutdown()
    except BaseException as exc:
        evidence["status"] = "FAILED"
        evidence["failure"] = scrub.scrub_text(f"{type(exc).__name__}: {exc}")
        raise
    finally:
        if original_home is None:
            os.environ.pop("PULSARA_HOME", None)
        else:
            os.environ["PULSARA_HOME"] = original_home
        _drop_database(saved, database)
        evidence["completed_at"] = datetime.now(timezone.utc).isoformat()
        output.write_text(
            json.dumps(scrub.scrub_json(evidence), ensure_ascii=False, indent=2) + "\n"
        )
    print(
        json.dumps(
            {
                "status": evidence["status"],
                "checks": evidence["checks"],
                "evidence": str(output),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "scenarios", nargs="*", choices=("ui", "plugin", "skill", "mcp", "hook")
    )
    asyncio.run(run(parser.parse_args().scenarios))
