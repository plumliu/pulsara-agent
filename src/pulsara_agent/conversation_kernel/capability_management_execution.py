"""Call-local management execution through the existing canonical write owners.

No mutation is performed by form validation. A prepared call can transfer to a
user submission or execute directly, never both; no lookup registry is involved.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path

from pulsara_agent.capability.management_form import (
    AcceptedCapabilityFormSubmission,
    CapabilityFormValues,
    PendingCapabilityForm,
    parse_mcp_secret_changes,
)
from pulsara_agent.capability.management_intent import (
    CapabilityManagementAction as Action,
    CapabilityManagementScope,
)
from pulsara_agent.capability.mcp_management import (
    LocalMcpTarget,
    McpManagementConflict,
    config_guard,
    config_to_entry,
    credential_presence,
)
from pulsara_agent.plugins.connection_management import (
    ReplacePluginMcpConnectionRequest,
)
from pulsara_agent.plugins.contracts import (
    ExternalProcessAcceptance,
    InstallLocalPluginRequest,
    PluginInstanceState,
    PluginScopeKind,
    PreparedPluginInstanceObservation,
    RemoveLocalPluginRequest,
    SetLocalPluginEnabledRequest,
    SettledPluginEnablementOutcome,
    SuccessfulPluginInstallOutcome,
    RemovedPluginOutcome,
)
from pulsara_agent.plugins.management import _request_identity
from pulsara_agent.plugins.mcp_connection import (
    overlay_from_dict,
    plugin_connection_owner,
    connection_editor_definition,
)
from pulsara_agent.primitives.context import freeze_json, thaw_json

from .capability_management import (
    CapabilityManagementPreparation,
    PreparedCapabilityManagementInvocation,
)


def _plugin_observation(prepared, root):
    plugin = prepared.plugin
    if plugin is None:
        return PreparedPluginInstanceObservation(
            _request_identity(
                PluginScopeKind(prepared.intent.scope.value),
                thaw_json(prepared.expected_current)["plugin_id"],
                root,
            ),
            None,
        )
    return PreparedPluginInstanceObservation(
        plugin.identity,
        PluginInstanceState(
            plugin.identity.plugin_id,
            plugin.identity.scope,
            plugin.package_install_id,
            plugin.enabled,
            plugin.identity.workspace_state_key,
            plugin.mcp_connection_overlays,
        ),
    )


class CapabilityManagementCall:
    """The unique prepared call owner, carried directly by tool authorization."""

    def __init__(
        self,
        service: CapabilityManagementPreparation,
        prepared: PreparedCapabilityManagementInvocation,
    ):
        self.service = service
        self.prepared = prepared
        self.subject: tuple[str, str, str, str] | None = None
        self._state = "prepared"
        self._submission: AcceptedCapabilityFormSubmission | None = None

    def form(self, reason: str) -> PendingCapabilityForm:
        if self._state != "prepared":
            raise RuntimeError("capability call was already transferred")
        self._state = "form"
        prepared = self.prepared
        fields = thaw_json(prepared.intent.fields)
        editor = {}
        plugin = prepared.plugin
        if plugin is None and "server_id" in fields:
            editor["credential_owner"] = asdict(LocalMcpTarget(fields["server_id"], self._root()).owner)
        if plugin is not None:
            connections = []
            for server in plugin.summary.mcp.mcp_servers:
                config = self.service._connection(plugin, server)
                connections.append({
                    "server_id": server.local_server_id,
                    "config": config_to_entry(config),
                    "credentials": credential_presence(config),
                })
            editor["plugin"] = {
                "id": plugin.identity.plugin_id, "name": plugin.summary.manifest.name,
                "enabled": plugin.enabled, "package_install_id": plugin.package_install_id,
                "skills": [asdict(item) for item in plugin.summary.skills.skills],
                "mcp": connections,
                "hooks": [asdict(item) for item in plugin.summary.hooks.hook_definitions],
            }
            if "server_id" in fields:
                server = next(item for item in plugin.summary.mcp.mcp_servers if item.local_server_id == fields["server_id"])
                overlay = thaw_json(prepared.public_prefill).get("overlay")
                defaults, config = connection_editor_definition(server, overlay_from_dict(overlay) if overlay is not None else None,
                    owner=plugin_connection_owner(plugin.identity, server.local_server_id))
                editor["connection"] = {"server_id": fields["server_id"], "defaults": defaults,
                    "connection_inputs": [asdict(value) for value in server.connection_inputs.inputs],
                    "config": config, "overlay": overlay,
                    "credential_owner": asdict(plugin_connection_owner(plugin.identity, fields["server_id"]))}
        return PendingCapabilityForm(
            freeze_json(
                {
                    "action": prepared.intent.action.value,
                    "scope": prepared.intent.scope.value,
                    "prefill": thaw_json(prepared.public_prefill),
                    "expected_current": thaw_json(prepared.expected_current),
                    "user_inputs": list(prepared.user_inputs),
                    "reason": reason,
                    "effects": {
                        "workspace_write": prepared.effects.workspace_write,
                        "outside_workspace_write": prepared.effects.outside_workspace_write,
                        "process_control": prepared.effects.process_control,
                        "destructive": prepared.effects.destructive,
                    },
                    **editor,
                }
            ),
            "请检查并确认这次能力配置；密钥只交给本机配置服务，不会发送给模型。",
            self._prepare_submission,
        )

    async def _prepare_submission(self, body):
        if self._state != "form":
            raise McpManagementConflict("capability form is no longer current")
        if set(body) - {
            "config",
            "overlay",
            "secret_changes",
            "retain_credentials_confirmed",
            "enable_review_accepted",
        }:
            raise ValueError("unsupported capability submission fields")
        for flag in ("retain_credentials_confirmed", "enable_review_accepted"):
            if flag in body and type(body[flag]) is not bool:
                raise ValueError("capability review must be an explicit boolean")
        prepared = self.prepared
        action = prepared.intent.action
        editable = (
            {"config"}
            if action in {Action.ADD_LOCAL_MCP, Action.UPDATE_LOCAL_MCP}
            else {"overlay"}
            if action is Action.CONFIGURE_PLUGIN_MCP_CONNECTION
            else set()
        )
        if (set(body) & {"config", "overlay"}) - editable:
            raise ValueError("this operation cannot edit connection configuration")
        changes = parse_mcp_secret_changes(body.get("secret_changes", []))
        if changes and not editable:
            raise ValueError("this operation cannot write credentials")
        arguments = {
            **prepared.intent.to_dict(),
            **{key: body[key] for key in editable if key in body},
        }
        # Browser cannot replace guards, target, action, scope or source. The
        # original prepared observation remains the authority for this editor.
        arguments.update(
            {
                key: value
                for key, value in thaw_json(prepared.expected_current).items()
                if key.startswith("expected_")
            }
        )
        candidate = await self.service.prepare(arguments)
        self._same_target(candidate)
        if "connection_configuration" in candidate.user_inputs:
            raise ValueError("connection configuration is required")
        if "plugin_enable_review" in candidate.user_inputs and not body.get(
            "enable_review_accepted"
        ):
            raise ValueError("review the current Plugin before enabling it")
        retained = body.get("retain_credentials_confirmed", False)
        if (
            "credential_destination_confirmation" in candidate.user_inputs
            and not retained
        ):
            raise ValueError("confirm credential reuse for the changed destination")
        # The native parser validates secret bindings against the actual target,
        # without persisting them or returning a secret-bearing config to the UI.
        if candidate.local_mutation and candidate.local_mutation.entry is not None:
            self.service.mcp.parse(
                candidate.local_mutation.target,
                thaw_json(candidate.local_mutation.entry),
                transient_secrets=changes,
            )
        elif changes:
            owner = plugin_connection_owner(
                candidate.plugin.identity, arguments["server_id"]
            )
            if any(change.binding.owner != owner for change in changes):
                raise ValueError("credential does not belong to this Plugin component")
        return CapabilityFormValues(
            freeze_json(arguments),
            changes,
            retained,
            body.get("enable_review_accepted", False),
        )

    def accept(self, submission: AcceptedCapabilityFormSubmission) -> None:
        if self._state != "form" or not isinstance(
            submission, AcceptedCapabilityFormSubmission
        ):
            raise RuntimeError("capability form submission has no waiting owner")
        self._submission = submission
        self._state = "submitted"

    def discard(self) -> None:
        self._state = "consumed"
        self._submission = None

    def _same_target(self, candidate):
        if candidate.expected_current != self.prepared.expected_current:
            raise McpManagementConflict("capability changed while the form was open")
        if candidate.plugin_connection_review != self.prepared.plugin_connection_review:
            raise McpManagementConflict("Plugin connection review changed while the form was open")
        if candidate.plugin is not None or self.prepared.plugin is not None:
            root = self._root()
            if _plugin_observation(candidate, root) != _plugin_observation(
                self.prepared, root
            ):
                raise McpManagementConflict("Plugin changed while the form was open")

    def _root(self):
        return (
            self.service.workspace_root
            if self.prepared.intent.scope is CapabilityManagementScope.WORKSPACE
            else None
        )

    async def execute(self):
        state = self._state
        if state not in {"prepared", "submitted"}:
            raise RuntimeError(
                "capability call cannot execute twice or before submission"
            )
        self._state = "consumed"
        submission, self._submission = self._submission, None
        values = submission.take() if submission is not None else None
        prepared = self.prepared
        if state == "submitted":
            assert values is not None
            prepared = await self.service.prepare(thaw_json(values.public_fields))
            self._same_target(prepared)
        elif prepared.user_inputs:
            raise ValueError("capability call requires user input")
        changes = values.secret_changes if values else ()
        retained = values.retain_credentials_confirmed if values else False
        fields = thaw_json(prepared.intent.fields)
        action = prepared.intent.action
        root = self._root()
        service = self.service
        if prepared.local_mutation is not None:
            mutation = prepared.local_mutation
            if values is None:
                outcome = await service.mcp.execute_prepared_mutation(mutation)
            elif action is Action.ADD_LOCAL_MCP:
                outcome = await service.mcp.create(
                    mutation.target, thaw_json(mutation.entry), secrets=changes
                )
            elif action is Action.UPDATE_LOCAL_MCP:
                outcome = await service.mcp.update(
                    mutation.target,
                    thaw_json(mutation.entry),
                    expected=mutation.expected,
                    secrets=changes,
                    retain_credentials_confirmed=retained,
                )
            else:
                outcome = await service.mcp.execute_prepared_mutation(mutation)
            return self._result(
                prepared,
                "APPLIED",
                {
                    "config": config_to_entry(outcome.config)
                    if outcome.config is not None
                    else None,
                    "cleanup_attention": outcome.cleanup_attention,
                },
            )
        if action in {Action.AUTHORIZE_MCP, Action.CLEAR_MCP_AUTHORIZATION}:
            return await self._authorization(prepared)
        observed = _plugin_observation(prepared, root)
        scope = PluginScopeKind(prepared.intent.scope.value)
        if action is Action.INSTALL_PLUGIN:
            outcome = await service.plugins.install_local_plugin(
                InstallLocalPluginRequest(
                    Path(fields["source_path"]),
                    scope,
                    service.deadline(),
                    root,
                    fields.get("replace", False),
                    prepared_current=observed,
                    source_format=fields.get("source_format", "native"),
                ),
                connections=service.mcp,
            )
            applied = isinstance(outcome, SuccessfulPluginInstallOutcome)
        elif action is Action.SET_PLUGIN_ENABLED:
            request = SetLocalPluginEnabledRequest(
                scope,
                fields["plugin_id"],
                fields["enabled"],
                prepared.plugin.package_install_id,
                service.deadline(),
                workspace_root=root,
                connection_review=prepared.plugin_connection_review,
                external_process_acceptance=(
                    ExternalProcessAcceptance.ACCEPTED
                    if values and values.enable_review_accepted and fields["enabled"]
                    else None
                ),
                prepared_current=observed,
            )
            # The existing synchronous owner has a bounded physical operation.
            # Join it before reporting cancellation; do not abandon a state rename.
            work = asyncio.create_task(
                asyncio.to_thread(service.plugins.set_local_plugin_enabled, request)
            )
            while True:
                try:
                    outcome = await asyncio.shield(work)
                    break
                except asyncio.CancelledError:
                    if work.done():
                        outcome = work.result()
                        break
            applied = isinstance(outcome, SettledPluginEnablementOutcome)
        elif action is Action.REMOVE_PLUGIN:
            outcome = await service.plugins.remove_local_plugin(
                RemoveLocalPluginRequest(
                    scope,
                    fields["plugin_id"],
                    service.deadline(),
                    prepared.plugin.package_install_id,
                    workspace_root=root,
                    prepared_current=observed,
                ),
                connections=service.mcp,
            )
            applied = isinstance(outcome, RemovedPluginOutcome)
        elif action is Action.CONFIGURE_PLUGIN_MCP_CONNECTION:
            previous = thaw_json(prepared.expected_current)["expected_overlay"]
            outcome = await service.plugins.replace_plugin_mcp_connection_overlay(
                ReplacePluginMcpConnectionRequest(
                    observed.identity,
                    fields["server_id"],
                    prepared.plugin.package_install_id,
                    overlay_from_dict(previous) if previous is not None else None,
                    overlay_from_dict(fields["overlay"])
                    if fields["overlay"] is not None
                    else None,
                    service.deadline(),
                    changes,
                    retained,
                    prepared_current=observed,
                ),
                connections=service.mcp,
            )
            return self._result(
                prepared,
                "APPLIED",
                {
                    "overlay": fields["overlay"],
                    "cleanup_attention": outcome.cleanup_attention,
                },
            )
        else:
            raise ValueError("unsupported capability operation")
        disposition = outcome.disposition.value
        return self._result(
            prepared,
            "APPLIED"
            if applied
            else "CONFLICT"
            if disposition in {"STALE", "ALREADY_PRESENT"}
            else "PARTIAL"
            if disposition == "ACK_UNKNOWN"
            else "REJECTED",
            {
                "operation_status": disposition,
                "cleanup_attention": getattr(outcome, "cleanup_attention", False),
            },
        )

    async def _authorization(self, prepared):
        service = self.service
        fields = thaw_json(prepared.intent.fields)
        if prepared.plugin is None:
            target = LocalMcpTarget(fields["server_id"], self._root())
            owner = target.owner
            expected = thaw_json(prepared.expected_current)["expected_identity"]

            def current():
                value = service.mcp.inspect(target)
                if value is None or config_guard(value) != expected:
                    raise McpManagementConflict("MCP changed before authorization")
                return value
        else:
            plugin = prepared.plugin
            owner = plugin_connection_owner(plugin.identity, fields["server_id"])
            observed = _plugin_observation(prepared, self._root())
            store = service.plugins._store()
            server = next(
                item
                for item in plugin.summary.mcp.mcp_servers
                if item.local_server_id == fields["server_id"]
            )

            def current():
                if store.read_state(store.layout(plugin.identity)) != observed.current:
                    raise McpManagementConflict("Plugin changed before authorization")
                return service._connection(plugin, server)

        if prepared.intent.action is Action.CLEAR_MCP_AUTHORIZATION:
            await service.mcp.oauth.logout(owner, require_current=current)
        else:
            flow = await service.mcp.oauth.begin(owner, current)
            assert flow.task is not None
            await flow.task
        return self._result(
            prepared,
            "APPLIED",
            {"authorized": prepared.intent.action is Action.AUTHORIZE_MCP},
        )

    @staticmethod
    def _result(prepared, status, current):
        fields = thaw_json(prepared.intent.fields)
        return {
            "status": status,
            "action": prepared.intent.action.value,
            "scope": prepared.intent.scope.value,
            "identity": {
                key: fields[key] for key in ("server_id", "plugin_id") if key in fields
            },
            "current": current,
            "adoption": "NOT_APPLICABLE",
        }
