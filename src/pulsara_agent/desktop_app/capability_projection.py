"""Desktop projections of Skill, MCP, and Plugin observations and outcomes."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Literal, Mapping

from pulsara_agent.capability.local_skill_management import LocalSkillInstallOutcome
from pulsara_agent.capability.mcp_management import (
    LocalMcpTarget,
    config_guard,
    config_to_entry,
    credential_presence,
)
from pulsara_agent.capability.public_projection import (
    public_connection_editor,
    public_connection_review,
    public_credentials,
    public_mcp_config,
)
from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.capability.resolver import (
    CompleteEffectiveSkillCatalogInspection,
    UnavailableEffectiveSkillCatalogInspection,
)
from pulsara_agent.capability.types import (
    ConflictingSkillCandidateIssue,
    InvalidSkillCandidateIssue,
    ProducerUnavailableCause,
    ResolutionUnavailableCause,
    ShadowedSkillCandidateIssue,
)
from pulsara_agent.capability.user_skill_config import workspace_skill_config_path
from pulsara_agent.conversation_kernel.host import KernelCapabilityCatalogInspection
from pulsara_agent.desktop_app.skill_inventory import _check_skill_target_collisions
from pulsara_agent.identity import derive_id
from pulsara_agent.mcp_config import (
    LegacySseTransportConfig,
    McpLocalConfigSourceKind,
    McpScopePolicy,
    McpServerConfig,
    StdioTransportConfig,
    StreamableHttpTransportConfig,
    default_user_mcp_config_path,
    mcp_server_workspace_approval_identity,
)
from pulsara_agent.plugins.contracts import (
    AlreadyPresentPluginInstallOutcome,
    CleanupUnavailablePluginInstallOutcome,
    FailedPluginEnablementOutcome,
    FailedPluginInstallOutcome,
    FailedPluginRemovalOutcome,
    PluginInspectionAbort,
    PluginInspectionDisposition,
    PluginInspectionOutcome,
    PluginScopeKind,
    RemovedPluginOutcome,
    SettledPluginEnablementOutcome,
    SuccessfulPluginInstallOutcome,
)
from pulsara_agent.process_credential_boundary import ProcessCredentialScrubSet
from pulsara_agent.public_projection import public_record


SessionWorkspaceKind = Literal["quick", "project"]


def _workspace_adoption_payload(pending_sessions: int) -> dict[str, object]:
    return {
        "scope": "workspace",
        "pending_sessions": pending_sessions,
        "when": "next_provider_dispatch",
    }


def _user_capability_payload(
    *,
    skills: dict[str, object],
    mcp_configs: tuple[McpServerConfig, ...],
    plugins: object,
    secret_resolver,
    live_inspections: tuple[KernelCapabilityCatalogInspection, ...],
) -> dict[str, object]:
    # Select one coherent observation for each exact current USER config.
    # A working connection anywhere wins over another Session still connecting
    # or failing. Never union catalogs: remote state can differ across Sessions.
    state_priority = {
        "READY": 7,
        "DISCOVERING": 6,
        "CONNECTING": 5,
        "FAILED_RETRYABLE": 4,
        "FAILED_TERMINAL": 3,
        "RETIRING": 2,
        "CLOSED": 1,
        "DISABLED": 0,
    }
    current_configs = {item.server_id: item for item in mcp_configs}
    observations = {}
    for inspection in live_inspections:
        catalogs = {item.server_id: item for item in inspection.mcp_catalog.servers}
        for observed in inspection.mcp_configured_servers:
            config = current_configs.get(observed.server_id)
            live = catalogs.get(observed.server_id)
            if (
                config is None
                or live is None
                or not observed.status_matches_config
                or observed.source_kind != McpLocalConfigSourceKind.USER.value
                or observed.resolved_config_identity != config.resolved_config_identity
            ):
                continue
            # Prefer the fuller coherent catalog within the same live state;
            # equal observations retain the first stable Session-id ordering.
            rank = (
                state_priority[live.status.value],
                live.exposed_tool_count,
                live.resource_count,
                live.resource_template_count,
                live.prompt_count,
            )
            prior = observations.get(config.server_id)
            if prior is None or rank > prior[0]:
                observations[config.server_id] = (rank, live, inspection)
    mcp_servers = []
    for config in mcp_configs:
        observation = observations.get(config.server_id)
        live = observation[1] if observation is not None else None
        live_tools = [
            {
                "name": item.provider_name,
                "remote_name": item.remote_name,
                "description": item.description,
                "effect": item.effect,
                "available_to_subagents": item.available_to_subagents,
                "parallel_safe": item.parallel_safe,
            }
            for item in observation[2].mcp_tools
            if item.server_id == config.server_id
        ] if observation is not None else []
        if not config.enabled:
            status = "DISABLED"
        elif live is None:
            status = "CONFIGURED"
        else:
            status = live.status.value
        if isinstance(config.transport, StdioTransportConfig):
            transport = {
                "kind": "stdio",
                "summary": config.transport.command,
            }
        elif isinstance(
            config.transport, (StreamableHttpTransportConfig, LegacySseTransportConfig)
        ):
            transport = {
                "kind": "http",
                "summary": config.transport.endpoint,
            }
        else:  # pragma: no cover - closed transport union
            raise TypeError("MCP transport is open")
        mcp_servers.append(
            {
                "id": config.server_id,
                "config": config_to_entry(config),
                "current_identity": config_guard(config),
                "credentials": credential_presence(config),
                "name": config.display_name,
                "enabled": config.enabled,
                "status": status,
                "available_to_subagents": (
                    config.scope_policy is McpScopePolicy.ROOT_AND_SUBAGENTS
                ),
                "tool_count": live.exposed_tool_count if live is not None else 0,
                "resource_count": live.resource_count if live is not None else 0,
                "resource_template_count": (
                    live.resource_template_count if live is not None else 0
                ),
                "prompt_count": live.prompt_count if live is not None else 0,
                "instructions": live.sanitized_instructions if live is not None else "",
                "has_failure": (
                    live is not None and live.stable_failure_category is not None
                ),
                "failure_category": (
                    live.stable_failure_category if live is not None else None
                ),
                "transport": transport,
                "tools": live_tools,
            }
        )
    return {
        "roots": [
            {"kind": "agents", "path": str(Path.home() / ".agents")},
            {"kind": "pulsara", "path": str(require_pulsara_home())},
        ],
        "skills": skills,
        "mcp": {
            "config_path": str(default_user_mcp_config_path()),
            "servers": mcp_servers,
        },
        "plugins": _user_plugins_payload(plugins, secret_resolver),
    }


def _public_user_capabilities(value, scrub):
    from pulsara_agent.plugins.contracts import public_package_id

    def roots(items):
        return [public_record(item, scrub, fixed=("kind",)) for item in items]

    skills = value["skills"]
    skill_children = {
        "items": [
            public_record(
                item,
                scrub,
                fixed=("root", "root_kind", "authoring_notes"),
                children={
                    "removal_identity": public_record(item["removal_identity"], scrub)
                    if item.get("removal_identity") is not None else None,
                },
            )
            for item in skills["items"]
        ],
        "issues": [public_record(item, scrub, fixed=("kind", "diagnostic_codes")) for item in skills["issues"]],
        "roots": roots(skills["roots"]),
    }
    mcp = value["mcp"]
    servers = []
    for server in mcp["servers"]:
        servers.append(public_record(server, scrub,
            fixed=("status", "failure_category"), children={
                "config": public_mcp_config(server["config"], scrub),
                "credentials": public_credentials(server["credentials"], scrub),
                "transport": public_record(server["transport"], scrub, fixed=("kind",)),
                "tools": [public_record(tool, scrub, fixed=("effect",)) for tool in server["tools"]],
            }))
    plugins = value["plugins"]
    items = []
    for item in plugins["items"]:
        items.append(public_record(item, scrub, children={
            "package_install_id": public_package_id(item["package_install_id"], scrub),
            "mcp_connections": [public_connection_editor(connection, scrub) for connection in item["mcp_connections"]],
            "connection_review": public_connection_review(item["connection_review"], scrub),
        }))
    return {
        "roots": roots(value["roots"]),
        "skills": public_record(skills, scrub, fixed=("status", "effective_catalog_status"), children=skill_children),
        "mcp": public_record(mcp, scrub, children={"servers": servers}),
        "plugins": public_record(plugins, scrub, fixed=("status",), children={"items": items}),
    }


def _user_plugins_payload(inspection: object, secret_resolver) -> dict[str, object]:
    from pulsara_agent.plugins.mcp_connection import connection_review, review_to_dict

    if isinstance(inspection, PluginInspectionAbort):
        return {
            "status": "attention",
            "items": [],
            "details": ["插件清单暂时无法读取。"],
        }
    if not isinstance(inspection, PluginInspectionOutcome):
        raise TypeError("Plugin inspection result is open")
    if inspection.disposition is not PluginInspectionDisposition.COMPLETE:
        return {
            "status": "attention",
            "items": [],
            "details": [_diagnostic_message(item) for item in inspection.diagnostics],
        }
    items = []
    for item in inspection.instances:
        if item.identity.scope is not PluginScopeKind.USER:
            continue
        manifest = item.summary.manifest
        items.append(
            {
                "id": item.identity.plugin_id,
                "name": manifest.name,
                "description": manifest.description or "这个插件没有提供说明。",
                "version": manifest.version,
                "author": manifest.author.name if manifest.author is not None else None,
                "enabled": item.enabled,
                "package_install_id": item.package_install_id,
                "package_root": str(item.package_root),
                "skill_count": len(item.summary.skills.skills),
                "mcp_count": len(item.summary.mcp.mcp_servers),
                "hook_count": len(item.summary.hooks.hook_definitions),
                "effective_skill_names": list(item.effective_skill_names),
                "effective_mcp_server_ids": list(item.effective_mcp_server_ids),
                "mcp_connections": _plugin_connection_editors(item),
                "connection_review": review_to_dict(
                    connection_review(
                        item.mcp_connection_overlays,
                        secret_resolver,
                        servers=item.summary.mcp.mcp_servers,
                        identity=item.identity,
                    )
                ),
                "details": [_diagnostic_message(value) for value in item.diagnostics],
            }
        )
    return {
        "status": "ready",
        "items": sorted(items, key=lambda item: str(item["name"])),
        "details": [_diagnostic_message(item) for item in inspection.diagnostics],
    }


def _plugin_connection_editors(instance):
    from pulsara_agent.plugins.mcp_connection import (
        connection_editor_definition,
        overlay_to_dict,
        plugin_connection_owner,
    )
    from dataclasses import asdict

    result = []
    for server in instance.summary.mcp.mcp_servers:
        overlay = next(
            (
                item
                for item in instance.mcp_connection_overlays
                if item.local_server_id == server.local_server_id
            ),
            None,
        )
        defaults, effective = connection_editor_definition(
            server,
            overlay,
            owner=plugin_connection_owner(instance.identity, server.local_server_id),
        )
        result.append(
            {
                "server_id": server.local_server_id,
                "defaults": defaults,
                "config": effective,
                "connection_inputs": [
                    asdict(value) for value in server.connection_inputs.inputs
                ],
                "overlay": overlay_to_dict(overlay) if overlay is not None else None,
                "credential_owner": asdict(
                    plugin_connection_owner(instance.identity, server.local_server_id)
                ),
            }
        )
    return result


def _diagnostic_message(value: object) -> str:
    message = getattr(value, "message", None)
    return (
        str(message) if isinstance(message, str) and message else type(value).__name__
    )


def _public_plugin_operation(outcome: object, payload: dict[str, object]) -> dict[str, object]:
    from pulsara_agent.plugins.contracts import public_package_id

    prior = getattr(outcome, "prior", None)
    source = prior if prior is not None else outcome
    children = {}
    for field in ("package_install_id", "attempted_package_install_id",
                  "expected_package_install_id", "observed_package_install_id",
                  "prior_package_install_id", "last_known_cut", "intended_enabled",
                  "desired_enabled"):
        if hasattr(source, field):
            payload[field] = getattr(source, field)
    scrub = ProcessCredentialScrubSet()
    for current in (outcome, prior):
        observed = getattr(current, "scrub_set", None)
        if observed is not None:
            for secret in observed.values:
                scrub.observe(secret)
    for field in ("package_install_id", "attempted_package_install_id",
                  "expected_package_install_id", "observed_package_install_id",
                  "prior_package_install_id"):
        if field in payload:
            children[field] = public_package_id(payload[field], scrub)
    return public_record(payload, scrub,
        fixed=("status", "prior_operation_status", "last_known_cut"), children=children)


def _plugin_install_operation(outcome: object) -> dict[str, object]:
    if isinstance(outcome, CleanupUnavailablePluginInstallOutcome):
        result = _plugin_install_operation(outcome.prior)
        result["prior_operation_status"] = outcome.prior.disposition.value
        result["status"] = outcome.disposition.value
        result["message"] = f"{result['message']} 旧安装文件稍后可以清理。"
        return result
    if isinstance(
        outcome, (SuccessfulPluginInstallOutcome, AlreadyPresentPluginInstallOutcome)
    ):
        installed = isinstance(outcome, SuccessfulPluginInstallOutcome)
        return {
            "status": outcome.disposition.value,
            "success": True,
            "message": (
                "插件已安装；部分旧连接凭据清理需要检查。"
                if installed and outcome.cleanup_attention
                else "插件已经安装；开启后会在安全时机用于会话。"
                if installed
                else "这个插件已经安装。"
            ),
            "plugin_id": outcome.identity.plugin_id,
            "cleanup_attention": installed and outcome.cleanup_attention,
            "details": [_diagnostic_message(item) for item in outcome.diagnostics]
            if installed
            else [],
        }
    if isinstance(outcome, FailedPluginInstallOutcome):
        return {
            "status": outcome.disposition.value,
            "success": False,
            "message": ("插件安装结果尚未确认，请刷新检查。" if outcome.disposition.value == "ACK_UNKNOWN"
                        else "插件没有安装，请检查所选目录。"),
            "details": [_diagnostic_message(item) for item in outcome.diagnostics],
        }
    raise TypeError("Plugin install outcome is open")


def _plugin_enablement_operation(outcome: object) -> dict[str, object]:
    if isinstance(outcome, SettledPluginEnablementOutcome):
        return {
            "status": outcome.disposition.value,
            "success": True,
            "message": "插件已开启。" if outcome.enabled else "插件已关闭。",
        }
    if isinstance(outcome, FailedPluginEnablementOutcome):
        return {
            "status": outcome.disposition.value,
            "success": False,
            "message": ("插件状态结果尚未确认，请刷新检查。" if outcome.disposition.value == "ACK_UNKNOWN"
                        else "插件状态没有改变，请刷新后重试。"),
            "details": [_diagnostic_message(item) for item in outcome.diagnostics],
        }
    raise TypeError("Plugin enablement outcome is open")


def _plugin_removal_operation(outcome: object) -> dict[str, object]:
    if isinstance(outcome, RemovedPluginOutcome):
        return {
            "status": outcome.disposition.value,
            "success": True,
            "message": "插件已移除；部分本机凭据清理需要检查。"
            if outcome.cleanup_attention
            else "插件已移除。",
            "cleanup_attention": outcome.cleanup_attention,
        }
    if isinstance(outcome, FailedPluginRemovalOutcome):
        return {
            "status": outcome.disposition.value,
            "success": False,
            "message": ("插件移除结果尚未确认，请刷新检查。" if outcome.disposition.value == "ACK_UNKNOWN"
                        else "插件没有移除，请刷新后重试。"),
            "details": [_diagnostic_message(item) for item in outcome.diagnostics],
        }
    raise TypeError("Plugin removal outcome is open")


def _mcp_transport_payload(config: McpServerConfig) -> dict[str, str]:
    if isinstance(config.transport, StdioTransportConfig):
        return {
            "kind": "stdio",
            "summary": config.transport.command,
            "detail": shlex.join((config.transport.command, *config.transport.args)),
        }
    if isinstance(
        config.transport, (StreamableHttpTransportConfig, LegacySseTransportConfig)
    ):
        return {
            "kind": "http",
            "summary": config.transport.endpoint,
            "detail": config.transport.endpoint,
        }
    raise TypeError("MCP transport is open")


def _mcp_source_label(source_kind: str | None) -> str:
    return {
        McpLocalConfigSourceKind.WORKSPACE.value: "workspace",
        McpLocalConfigSourceKind.USER.value: "user",
        McpLocalConfigSourceKind.HOST_OVERRIDE.value: "host",
        "MANAGED_PACKAGE": "plugin",
    }.get(source_kind, "host")


def _capability_payload(
    *,
    session_id: str,
    workspace_root: Path,
    workspace_kind: SessionWorkspaceKind,
    workspace_skills: dict[str, object],
    workspace_mcp_configs: tuple[McpServerConfig, ...],
    trust_all_workspace_mcp: bool,
    workspace_mcp_approvals: Mapping[str, str],
    refresh_pending: bool,
    refresh_attention: str | None,
    inspection: KernelCapabilityCatalogInspection,
) -> dict[str, object]:
    tools_by_server: dict[str, list[dict[str, object]]] = {}
    for item in inspection.mcp_tools:
        tools_by_server.setdefault(item.server_id, []).append(
            {
                "name": item.provider_name,
                "remote_name": item.remote_name,
                "description": item.description,
                "effect": item.effect,
                "available_to_subagents": item.available_to_subagents,
                "parallel_safe": item.parallel_safe,
            }
        )
    catalog = inspection.mcp_catalog
    catalog_by_id = {item.server_id: item for item in catalog.servers}
    configured_by_id = {
        item.server_id: item for item in inspection.mcp_configured_servers
    }
    mcp_servers: list[dict[str, object]] = []
    workspace_mcp_ids = {item.server_id for item in workspace_mcp_configs}
    for config in workspace_mcp_configs:
        configured = configured_by_id.get(config.server_id)
        exact_workspace = (
            configured is not None
            and configured.source_kind == McpLocalConfigSourceKind.WORKSPACE.value
            and configured.resolved_config_identity == config.resolved_config_identity
        )
        live = catalog_by_id.get(config.server_id) if exact_workspace else None
        entry_trusted = trust_all_workspace_mcp or (
            workspace_mcp_approvals.get(config.server_id)
            == mcp_server_workspace_approval_identity(config)
        )
        enabled = config.enabled and entry_trusted
        status = (
            "DISABLED"
            if not config.enabled
            else "CONFIGURED"
            if not entry_trusted or live is None
            else live.status.value
        )
        mcp_servers.append(
            {
                "id": config.server_id,
                "name": config.display_name,
                "source": "workspace",
                "editable": True,
                "config_identity": mcp_server_workspace_approval_identity(config),
                "config": config_to_entry(config),
                "credentials": credential_presence(config),
                "enabled": enabled,
                "configured_enabled": config.enabled,
                "needs_approval": config.enabled and not entry_trusted,
                "effective": exact_workspace,
                "status": status,
                "available_to_subagents": (
                    config.scope_policy is McpScopePolicy.ROOT_AND_SUBAGENTS
                ),
                "tool_count": live.exposed_tool_count if live is not None else 0,
                "discovered_tool_count": (
                    live.discovered_tool_count if live is not None else 0
                ),
                "resource_count": live.resource_count if live is not None else 0,
                "resource_template_count": (
                    live.resource_template_count if live is not None else 0
                ),
                "prompt_count": live.prompt_count if live is not None else 0,
                "instructions": (
                    live.sanitized_instructions if live is not None else ""
                ),
                "has_failure": (
                    live is not None and live.stable_failure_category is not None
                ),
                "failure_category": (
                    live.stable_failure_category if live is not None else None
                ),
                "transport": _mcp_transport_payload(config),
                "tools": tools_by_server.get(config.server_id, []) if live else [],
            }
        )
    for live in catalog.servers:
        configured = configured_by_id.get(live.server_id)
        if (
            live.server_id in workspace_mcp_ids
            and configured is not None
            and configured.source_kind == McpLocalConfigSourceKind.WORKSPACE.value
        ):
            continue
        source = _mcp_source_label(
            configured.source_kind if configured is not None else None
        )
        mcp_servers.append(
            {
                "id": live.server_id,
                "name": live.display_name,
                "source": source,
                "editable": False,
                "config_identity": None,
                "enabled": True,
                "configured_enabled": True,
                "needs_approval": False,
                "effective": True,
                "status": live.status.value,
                "available_to_subagents": live.scope_subagents,
                "tool_count": live.exposed_tool_count,
                "discovered_tool_count": live.discovered_tool_count,
                "resource_count": live.resource_count,
                "resource_template_count": live.resource_template_count,
                "prompt_count": live.prompt_count,
                "instructions": live.sanitized_instructions,
                "has_failure": live.stable_failure_category is not None,
                "failure_category": live.stable_failure_category,
                "transport": None,
                "tools": tools_by_server.get(live.server_id, []),
            }
        )

    skill_catalog = inspection.skill_catalog
    skill_items: list[dict[str, object]] = []
    skill_target_facts = []
    skill_issues: list[dict[str, object]] = []
    unavailable_details: list[str] = []
    if isinstance(skill_catalog, CompleteEffectiveSkillCatalogInspection):
        winner_paths = {
            item.path.resolve(strict=False): item for item in skill_catalog.winners
        }
        workspace_paths: set[Path] = set()
        raw_workspace_items = workspace_skills.get("items", [])
        if not isinstance(raw_workspace_items, list):
            raw_workspace_items = []
        for raw in raw_workspace_items:
            if not isinstance(raw, dict) or not isinstance(raw.get("path"), str):
                continue
            path = Path(str(raw["path"])).resolve(strict=False)
            workspace_paths.add(path)
            skill_target_facts.append((str(raw.get("id", "")), (str(raw.get("root", "")), str(raw.get("name", "")))))
            skill_items.append(
                {
                    "id": str(raw.get("id", "")),
                    "name": str(raw.get("name", "")),
                    "description": str(raw.get("description", "")),
                    "skill_path": str(raw.get("skill_path", "")),
                    "path": str(path),
                    "source": "workspace",
                    "editable": True,
                    "removal_identity": raw.get("removal_identity"),
                    "enabled": bool(raw.get("enabled", True)),
                    "effective": path in winner_paths,
                    "configured": str(raw.get("name", ""))
                    in inspection.configured_active_skill_names,
                    "authoring_notes": list(raw.get("authoring_notes", [])),
                }
            )
        for item in skill_catalog.winners:
            path = item.path.resolve(strict=False)
            if path in workspace_paths:
                continue
            target_id = derive_id("skill", "ui-target", item.source.value, item.name)
            skill_target_facts.append((target_id, (item.source.value, item.name)))
            skill_items.append(
                {
                    "id": target_id,
                    "name": item.name,
                    "description": item.description,
                    "skill_path": item.skill_path,
                    "path": str(path),
                    "source": item.source.value,
                    "editable": False,
                    "enabled": True,
                    "effective": True,
                    "configured": (
                        item.name in inspection.configured_active_skill_names
                    ),
                    "authoring_notes": [
                        code.value for code in item.authoring_diagnostic_codes
                    ],
                }
            )
        _check_skill_target_collisions(skill_target_facts)
        skill_issues = [
            _skill_issue_payload(item) for item in skill_catalog.candidate_issues
        ]
    elif isinstance(skill_catalog, UnavailableEffectiveSkillCatalogInspection):
        for cause in skill_catalog.unavailable_causes:
            if isinstance(cause, ProducerUnavailableCause):
                unavailable_details.extend(item.message for item in cause.diagnostics)
            elif isinstance(cause, ResolutionUnavailableCause):
                unavailable_details.append(cause.diagnostic.message)
            else:  # pragma: no cover - closed cause union
                raise TypeError("Skill catalog unavailable cause is open")
    else:  # pragma: no cover - closed inspection union
        raise TypeError("Skill catalog inspection is open")

    return {
        "session_id": session_id,
        "workspace_path": str(workspace_root),
        "workspace_kind": workspace_kind,
        "credential_scope_key": LocalMcpTarget("draft", workspace_root).owner.scope_key,
        "adoption": {
            "scope": "workspace",
            "pending": refresh_pending,
            "attention": refresh_attention,
            "when": "next_provider_dispatch",
        },
        "skills": {
            "status": (
                "ready"
                if isinstance(skill_catalog, CompleteEffectiveSkillCatalogInspection)
                else "attention"
            ),
            "items": skill_items,
            "issues": [
                *skill_issues,
                *(
                    workspace_skills.get("issues", [])
                    if isinstance(workspace_skills.get("issues"), list)
                    else []
                ),
            ],
            "details": [
                *unavailable_details,
                *(
                    workspace_skills.get("details", [])
                    if isinstance(workspace_skills.get("details"), list)
                    else []
                ),
            ],
            "config_path": str(
                workspace_skills.get(
                    "config_path", workspace_skill_config_path(workspace_root)
                )
            ),
            "roots": [
                {
                    "path": str(item.path),
                    "scope": (
                        "workspace"
                        if item.root_kind.value.startswith("WORKSPACE_")
                        else "user"
                    ),
                }
                for item in skill_catalog.root_policy.roots
            ],
        },
        "mcp": {
            "config_path": str(
                workspace_root.resolve(strict=False) / ".pulsara/mcp.yaml"
            ),
            "servers": mcp_servers,
            "collisions": [
                {
                    "name": item.provider_name,
                    "members": [
                        {
                            "server_id": member.server_id,
                            "tool_name": member.remote_tool_name,
                        }
                        for member in item.members
                    ],
                }
                for item in catalog.provider_name_collision_facts
            ],
        },
    }


def _skill_issue_payload(issue: object) -> dict[str, object]:
    if isinstance(issue, InvalidSkillCandidateIssue):
        return {
            "kind": "invalid",
            "title": "有一个技能没有通过检查",
            "path": str(issue.path),
            "details": [item.message for item in issue.diagnostics],
        }
    if isinstance(issue, ShadowedSkillCandidateIssue):
        return {
            "kind": "shadowed",
            "title": f"{issue.name} 使用了优先级更高的版本",
            "path": str(issue.path),
            "details": [f"当前使用：{issue.winner_path}"],
        }
    if isinstance(issue, ConflictingSkillCandidateIssue):
        return {
            "kind": "conflict",
            "title": f"{issue.name} 存在同级重名",
            "details": [str(item.path) for item in issue.candidates],
        }
    raise TypeError("Skill candidate issue is open")


def _skill_install_payload(outcome: LocalSkillInstallOutcome) -> dict[str, object]:
    messages = {
        "INSTALLED": "技能已经安装。",
        "SOURCE_INVALID": "这个目录不是有效的技能。",
        "SOURCE_UNAVAILABLE": "无法读取这个技能目录。",
        "SOURCE_RACED": "安装时目录发生了变化，请重试。",
        "TARGET_CONFIGURATION_UNAVAILABLE": "无法准备技能安装位置。",
        "UNSUPPORTED_ENTRY": "技能目录包含不支持的内容。",
        "DESTINATION_EXISTS": "同名技能已经存在。",
        "STAGING_UNAVAILABLE": "暂时无法准备安装文件。",
        "PUBLISH_UNAVAILABLE": "暂时无法完成安装。",
        "CANCELLED": "安装已取消。",
        "CLEANUP_UNAVAILABLE": "安装没有完成，临时文件需要留意。",
    }
    return {
        "status": outcome.disposition.value,
        "installed": outcome.disposition.value == "INSTALLED",
        "message": messages[outcome.disposition.value],
        "source_path": str(outcome.source_path),
        "destination_path": (
            str(outcome.destination_path)
            if outcome.destination_path is not None
            else None
        ),
        "details": [item.message for item in outcome.diagnostics],
    }
