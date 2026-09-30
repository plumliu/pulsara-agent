"""Shared public Hook source review from native frozen observations."""

from pulsara_agent.hooks.contracts import PluginHookSourceIdentity


def hook_snapshot_public(snapshot, *, inspect: bool) -> dict[str, object]:
    value: dict[str, object] = {
        "scope": snapshot.provenance.identity.visibility_scope.value.lower(),
        "path": str(snapshot.provenance.identity.canonical_path),
        "description": snapshot.provenance.description,
        "source_disposition": snapshot.disposition.value,
        "trust_disposition": snapshot.trust.disposition.value,
        "enabled": snapshot.trust.enabled,
        "definition_digest": snapshot.trust.current_definition_digest,
        "trusted_definition_digest": snapshot.trust.trusted_definition_digest,
        "trusted_at": snapshot.trust.trusted_at,
        "runnable_handler_count": len(snapshot.definitions) if snapshot.runnable else 0,
        "declaration_environment": dict(snapshot.provenance.declaration_environment),
        "diagnostics": [
            {"code": item.code, "message": item.message}
            for item in snapshot.diagnostics
        ],
    }
    identity = snapshot.provenance.identity
    if isinstance(identity, PluginHookSourceIdentity):
        value["plugin_id"] = identity.plugin_id
        value["package_install_id"] = identity.package_install_id
    if inspect:
        value["definitions"] = [
            {
                "ordinal": item.source_local_definition_ordinal,
                "event": item.event_type.external_name,
                "matcher": item.matcher.pattern,
                "command": item.command,
                "commandWindows": item.command_windows,
                "timeout": item.timeout_seconds,
                "async": item.asynchronous,
                "statusMessage": item.status_message,
                "additionalContextLimit": item.additional_context_limit,
            }
            for item in snapshot.definitions
        ]
    return value


def add_matching_tool_review(
    value, snapshot, *, subjects, complete: bool, workspace_root
):
    """Display-only matching, sharing the production matcher and real identities."""
    from pulsara_agent.hooks.contracts import HookEventType
    from pulsara_agent.hooks.matcher import definition_matches

    labels = {
        "terminal": "运行终端命令",
        "read_file": "读取文件",
        "edit_file": "编辑文件",
        "write_file": "写入文件",
        "search_content": "搜索内容",
        "find_files": "查找文件",
        "spawn_agent": "创建子任务",
    }
    value["workspace_path"] = str(workspace_root)
    value["tool_inventory_complete"] = complete
    for public, definition in zip(
        value["definitions"], snapshot.definitions, strict=True
    ):
        tool_event = definition.event_type in {
            HookEventType.PRE_TOOL_USE_EVENT,
            HookEventType.POST_TOOL_USE_EVENT,
            HookEventType.PERMISSION_REQUEST_EVENT,
        }
        matched = [
            subject
            for subject in subjects
            if tool_event and definition_matches(definition, subject)
        ]
        public["matched_operations"] = list(
            dict.fromkeys(
                labels.get(subject.canonical_subject, subject.canonical_subject)
                for subject in matched
            )
        )
        public["matching_aliases"] = [
            {"tool_name": subject.canonical_subject, "aliases": list(subject.aliases)}
            for subject in matched
            if subject.aliases
        ]
        public["is_tool_event"] = tool_event
        public["matches_all"] = definition.matcher.matches_all
