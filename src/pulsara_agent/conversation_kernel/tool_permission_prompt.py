"""Describe the action being approved without copying tool payloads into the card."""

from pathlib import Path
from typing import Mapping

from pulsara_agent.capability.management_intent import CapabilityManagementAction as Action
from pulsara_agent.primitives.context import thaw_json
from pulsara_agent.tools.builtins.filesystem import path_target_arg

from .capability_management import PreparedCapabilityManagementInvocation


def builtin_permission_prompt(name: str, arguments: Mapping[str, object], workspace: Path) -> str:
    if name in {"write_file", "edit_file"}:
        raw_path = path_target_arg(arguments)
        verb = "创建文件" if name == "write_file" else "编辑文件"
        try:
            path = (workspace / Path(raw_path).expanduser()).resolve()
        except (OSError, RuntimeError, ValueError):
            return f"{verb}（路径未能解析）：{raw_path}"
        scope = "工作区内" if path.is_relative_to(workspace) else "工作区外"
        return f"在{scope}{verb}：{path}"
    if name == "exec_process":
        directory = arguments.get("workdir") or "."
        try:
            directory = (workspace / Path(directory).expanduser()).resolve()
        except (OSError, RuntimeError, ValueError):
            return f"在指定目录 {directory}（未能解析）执行命令：\n{arguments['command']}"
        return f"在 {directory} 执行命令：\n{arguments['command']}"
    if name == "manage_process":
        verb = {"write": "写入输入", "close_stdin": "关闭标准输入", "kill": "终止", "wait": "等待"}
        return f"对进程 {arguments['target']['process_id']} 执行「{verb[arguments['action']]}」。"
    return f"运行工具 {name}。"


def management_permission_prompt(prepared: PreparedCapabilityManagementInvocation, workspace: Path) -> str:
    intent = prepared.intent
    target = thaw_json(intent.target)
    fields = thaw_json(intent.fields)
    actions = {
        Action.INSTALL_LOOSE_SKILL: "安装技能",
        Action.SET_LOOSE_SKILL_ENABLED: "启用技能" if fields.get("enabled") else "停用技能",
        Action.REMOVE_LOOSE_SKILL: "删除技能",
        Action.REVOKE_HOOK_TRUST: "撤销 Hook 来源信任",
        Action.SET_HOOK_SOURCE_ENABLED: "启用 Hook 来源" if fields.get("enabled") else "停用 Hook 来源",
        Action.REMOVE_LOCAL_MCP: "删除 MCP 连接",
        Action.INSTALL_PLUGIN: "安装插件（允许替换）" if fields.get("replace") else "安装插件",
        Action.SET_PLUGIN_ENABLED: "启用插件" if fields.get("enabled") else "停用插件",
        Action.REMOVE_PLUGIN: "删除插件",
        Action.CLEAR_MCP_AUTHORIZATION: "清除 MCP 登录授权",
    }
    if target["kind"] == "PATH":
        path = workspace / Path(target["path"]).expanduser()
    else:
        path = next(target[key] for key in ("skill_path", "plugin_path", "config_path") if key in target)
    location = f"{target['server_id']}（{path}）" if "server_id" in target else path
    scope = "当前项目" if prepared.resolved_scope.value == "WORKSPACE" else "用户配置"
    prompt = f"{actions[intent.action]}（{scope}）：{location}"
    if intent.action in {Action.REMOVE_PLUGIN, Action.REMOVE_LOCAL_MCP}:
        prompt += "。关联的已保存连接凭据也会清除。"
    if prepared.scrub_set is not None:
        return prepared.scrub_set.scrub_text(prompt)
    return prompt
