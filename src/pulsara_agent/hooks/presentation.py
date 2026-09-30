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
