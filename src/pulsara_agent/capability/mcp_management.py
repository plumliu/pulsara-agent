"""Canonical MCP mutation owner shared by user and model control surfaces."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Mapping

from pulsara_agent.mcp_config import (
    default_user_mcp_config_path,
    LocalConfiguredMcpRuntimeSource,
    McpLocalConfigSourceKind,
    McpServerConfig,
    StdioTransportConfig,
    NoAuth,
    BearerSecret,
    StaticHeaderSecretReferences,
    OAuthAuthorization,
    WorkspaceRelativeMcpCwd,
    _load_raw,
    _parse_server,
    _write_mcp_raw,
    mcp_server_workspace_approval_identity,
    workspace_mcp_config_path,
    load_mcp_server_configs,
    validate_workspace_mcp_server_addition_capacity,
    MAXIMUM_MCP_CONFIGURED_SERVERS,
    McpConfiguredServerBoundExceeded,
)
from pulsara_agent.mcp_credentials import (
    BoundSecretValue,
    ManagedLocalCredentialReference,
    McpCredentialBinding,
    McpCredentialOwner,
    McpSecretInput,
    secret_to_dict,
    resolve_secret,
    McpCredentialMissing,
)
from pulsara_agent.settings import LocalSettingsStore
from pulsara_agent.capability.workspace_mcp_trust import (
    approve_workspace_mcp_server_config,
    remove_workspace_mcp_server_approval,
)
from pulsara_agent.memory.scope import workspace_context_id
from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary
from pulsara_agent.capability.management_effects import (
    ResolvedCapabilityEffectProjection,
)
from pulsara_agent.capability.management_intent import CapabilityManagementAction
from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    freeze_json,
    thaw_json,
)


@dataclass(frozen=True, slots=True)
class LocalMcpTarget:
    server_id: str
    workspace_root: Path | None = None

    @property
    def owner(self) -> McpCredentialOwner:
        return McpCredentialOwner(
            "local",
            "user"
            if self.workspace_root is None
            else workspace_context_id(str(self.workspace_root.resolve())),
            self.server_id,
        )

    @property
    def source(self) -> LocalConfiguredMcpRuntimeSource:
        return LocalConfiguredMcpRuntimeSource(
            McpLocalConfigSourceKind.USER
            if self.workspace_root is None
            else McpLocalConfigSourceKind.WORKSPACE
        )


@dataclass(frozen=True, slots=True)
class McpSecretMutation:
    binding: McpCredentialBinding
    value: str | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class McpMutationOutcome:
    config: McpServerConfig | None
    applied: bool
    cleanup_attention: bool = False


@dataclass(frozen=True, slots=True)
class McpConnectionTestOutcome:
    status: str
    tools: int = 0
    resources: int = 0
    resource_templates: int = 0
    prompts: int = 0


class McpManagementConflict(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PreparedLocalMcpMutation:
    """Public preparation facts; callbacks holding credential values are excluded."""

    action: CapabilityManagementAction
    target: LocalMcpTarget
    expected: str | None
    entry: FrozenJsonObjectFact | None
    effects: ResolvedCapabilityEffectProjection
    missing_credentials: tuple[str, ...]
    requires_destination_confirmation: bool

    def __post_init__(self):
        if self.action not in {"ADD_LOCAL_MCP", "UPDATE_LOCAL_MCP", "REMOVE_LOCAL_MCP"}:
            raise ValueError("invalid local MCP management action")
        if (self.action == "REMOVE_LOCAL_MCP") != (self.entry is None):
            raise ValueError("local MCP mutation entry does not match its action")
        if (self.action == "ADD_LOCAL_MCP") != (self.expected is None):
            raise ValueError("local MCP mutation guard does not match its action")


def config_guard(config: McpServerConfig) -> str:
    if (
        isinstance(config.runtime_source, LocalConfiguredMcpRuntimeSource)
        and config.runtime_source.source_kind is McpLocalConfigSourceKind.WORKSPACE
    ):
        return mcp_server_workspace_approval_identity(config)
    return config.resolved_config_identity


def _destination(config: McpServerConfig):
    transport = config.transport
    if isinstance(transport, StdioTransportConfig):
        return transport.kind, transport.command, transport.args, transport.cwd
    return transport.kind, transport.endpoint


def _require_destination_confirmation(current, candidate, secrets, confirmed):
    if (
        confirmed
        or current is None
        or candidate is None
        or _destination(current) == _destination(candidate)
    ):
        return
    changed = {item.binding for item in secrets}
    old = config_secret_inputs(current)
    for ref in config_secret_inputs(candidate):
        if ref not in old:
            continue
        references = tuple(
            part
            for part in (ref.parts if isinstance(ref, BoundSecretValue) else (ref,))
            if not isinstance(part, str)
        )
        if references and all(
            isinstance(part, ManagedLocalCredentialReference)
            and part.binding in changed
            for part in references
        ):
            continue
        try:
            resolve_secret(ref, current.secret_resolver)
        except McpCredentialMissing:
            continue
        raise ValueError("请确认将保留的凭据用于新的连接目标。")


def auth_to_entry(auth) -> dict[str, object]:
    if isinstance(auth, NoAuth):
        return {"type": "none"}
    if isinstance(auth, BearerSecret):
        return {"type": "bearer", "reference": secret_to_dict(auth.reference)}
    if isinstance(auth, StaticHeaderSecretReferences):
        return {
            "type": "static_headers",
            "headers": {name: secret_to_dict(ref) for name, ref in auth.headers},
        }
    if isinstance(auth, OAuthAuthorization):
        return {
            "type": "oauth",
            "client_id": auth.client_id,
            "client_secret": secret_to_dict(auth.client_secret)
            if auth.client_secret is not None
            else None,
            "scope": auth.scope,
            "redirect_uri": auth.redirect_uri,
            "resource": auth.resource,
            "client_metadata_url": auth.client_metadata_url,
        }
    raise TypeError("MCP auth union is open")


def config_to_entry(config: McpServerConfig) -> dict[str, object]:
    transport = config.transport
    if isinstance(transport, StdioTransportConfig):
        if not isinstance(transport.cwd, WorkspaceRelativeMcpCwd):
            raise ValueError("package-derived MCP is not an editable local entry")
        wire = {
            "type": "stdio",
            "command": transport.command,
            "args": list(transport.args),
            "cwd": transport.cwd.relative_path,
            "env": dict(transport.environment),
            "secret_env": {
                name: secret_to_dict(ref) for name, ref in transport.secret_environment
            },
        }
    else:
        wire = {
            "type": transport.kind.value,
            "endpoint": transport.endpoint,
            "allow_http_localhost": transport.allow_http_localhost,
            "network_policy": transport.network_policy.value,
            "proved_stateless": transport.proved_stateless,
        }
    return {
        "display_name": config.display_name,
        "enabled": config.enabled,
        "required": config.required,
        "transport": wire,
        "auth": auth_to_entry(config.auth),
        "public_headers": dict(config.public_headers),
        "scope_policy": config.scope_policy.value,
        "exposure_policy": {
            "include_tool_names": list(config.exposure_policy.include_tool_names)
            if config.exposure_policy.include_tool_names is not None
            else None,
            "exclude_tool_names": list(config.exposure_policy.exclude_tool_names),
            "invalid_tool_policy": config.exposure_policy.invalid_tool_policy.value,
        },
        "effect_policy": {
            "default_effect": config.effect_policy.default_effect.value,
            "tool_effect_overrides": {
                name: value.value
                for name, value in config.effect_policy.tool_effect_overrides
            },
        },
        "supports_parallel_tool_calls": config.supports_parallel_tool_calls,
        "stateless_http_max_in_flight": config.stateless_http_max_in_flight,
        "catalog_refresh_interval_ms": config.catalog_refresh_interval_ms
        if config.catalog_refresh_interval_ms is not None
        else "DISABLED",
        "default_tool_timeout_ms": config.default_tool_timeout_ms,
        "per_tool_timeout_ms": dict(config.per_tool_timeout_ms),
    }


def config_secret_inputs(config: McpServerConfig) -> tuple[McpSecretInput, ...]:
    auth = config.auth
    refs = (
        (auth.reference,)
        if isinstance(auth, BearerSecret)
        else tuple(ref for _, ref in auth.headers)
        if isinstance(auth, StaticHeaderSecretReferences)
        else (auth.client_secret,)
        if isinstance(auth, OAuthAuthorization) and auth.client_secret is not None
        else ()
    )
    if isinstance(config.transport, StdioTransportConfig):
        refs += tuple(ref for _, ref in config.transport.secret_environment)
    return refs


def managed_bindings(config: McpServerConfig) -> frozenset[McpCredentialBinding]:
    result = set()
    for value in config_secret_inputs(config):
        for part in value.parts if isinstance(value, BoundSecretValue) else (value,):
            if isinstance(part, ManagedLocalCredentialReference):
                result.add(part.binding)
    return frozenset(result)


def credential_presence(config: McpServerConfig) -> tuple[dict[str, object], ...]:
    auth = config.auth
    named = []
    if isinstance(auth, BearerSecret):
        named.append(("bearer", auth.reference))
    elif isinstance(auth, StaticHeaderSecretReferences):
        named.extend((f"header:{name}", ref) for name, ref in auth.headers)
    elif isinstance(auth, OAuthAuthorization) and auth.client_secret is not None:
        named.append(("oauth-client-secret", auth.client_secret))
    if isinstance(config.transport, StdioTransportConfig):
        named.extend(
            (f"env:{name}", ref) for name, ref in config.transport.secret_environment
        )
    result = []
    for name, ref in named:
        try:
            resolve_secret(ref, config.secret_resolver)
            present = True
        except McpCredentialMissing:
            present = False
        parts = ref.parts if isinstance(ref, BoundSecretValue) else (ref,)
        sources = sorted(
            {
                "managed_local"
                if isinstance(part, ManagedLocalCredentialReference)
                else "environment"
                for part in parts
                if not isinstance(part, str)
            }
        )
        result.append({"name": name, "present": present, "sources": sources})
    return tuple(result)


class LocalMcpManagementService:
    def __init__(
        self,
        settings: LocalSettingsStore,
        *,
        user_config_path: Path | None = None,
        lane: asyncio.Lock | None = None,
        credential_boundary: ProcessCredentialBoundary | None = None,
        open_browser=None,
    ):
        self.settings = settings
        self.user_config_path = (
            user_config_path
            if user_config_path is not None
            else default_user_mcp_config_path()
        )
        self.lane = lane if lane is not None else asyncio.Lock()
        self.credential_boundary = credential_boundary or ProcessCredentialBoundary()
        # Match the existing serial catalog-discovery reservation. Queue additional
        # local tests; do not multiply bounded discovery buffers per click.
        self._test_lane = asyncio.Lock()
        from pulsara_agent.conversation_kernel.mcp.oauth import McpOAuthManager

        async def launch_browser(url):
            import webbrowser

            opened = await asyncio.to_thread(webbrowser.open, url)
            if not opened:
                raise ValueError("MCP authorization browser could not be opened")

        self.oauth = McpOAuthManager(
            settings,
            lane=self.lane,
            credential_boundary=self.credential_boundary,
            open_browser=open_browser or launch_browser,
        )

    def load_configs(
        self,
        *,
        workspace_root: Path | None = None,
        trust_workspace_config: bool = False,
        approved_workspace_server_identities: Mapping[str, str] | None = None,
    ) -> tuple[McpServerConfig, ...]:
        selected = load_mcp_server_configs(
            workspace_root=workspace_root,
            user_config_path=self.user_config_path,
            trust_workspace_config=trust_workspace_config,
            approved_workspace_server_identities=approved_workspace_server_identities,
        )
        return tuple(
            self.parse(
                LocalMcpTarget(
                    config.server_id,
                    workspace_root
                    if config.runtime_source.source_kind
                    is McpLocalConfigSourceKind.WORKSPACE
                    else None,
                ),
                config_to_entry(config),
            )
            for config in selected
        )

    async def authorize(self, target: LocalMcpTarget):
        return await self.oauth.begin(target.owner, lambda: self.inspect(target))

    async def aclose(self):
        await self.oauth.aclose()

    def path(self, target: LocalMcpTarget) -> Path:
        return (
            self.user_config_path
            if target.workspace_root is None
            else workspace_mcp_config_path(target.workspace_root)
        )

    def inspect(self, target: LocalMcpTarget) -> McpServerConfig | None:
        raw = _load_raw(self.path(target)).get(target.server_id)
        return self.parse(target, raw) if raw is not None else None

    def inspect_scope(
        self, workspace_root: Path | None = None
    ) -> tuple[McpServerConfig, ...]:
        raw = _load_raw(self.path(LocalMcpTarget("inventory", workspace_root)))
        return tuple(
            self.parse(LocalMcpTarget(server_id, workspace_root), entry)
            for server_id, entry in sorted(raw.items())
        )

    async def prepare_mutation(
        self,
        action: str,
        target: LocalMcpTarget,
        entry: Mapping[str, object] | None = None,
        *,
        expected: str | None = None,
    ) -> PreparedLocalMcpMutation:
        """Resolve effects/current guards before the tool permission decision.

        This is an observation, not an execution permit: create/update/remove
        still exact-join the captured guard at their existing publication cut.
        Neither the public draft nor this result owns live credential values.
        """
        async with self.lane:
            return await asyncio.to_thread(
                self._prepare_mutation, action, target, entry, expected
            )

    def _prepare_mutation(self, action, target, entry, expected):
        action = CapabilityManagementAction(action)
        if action not in {"ADD_LOCAL_MCP", "UPDATE_LOCAL_MCP", "REMOVE_LOCAL_MCP"}:
            raise ValueError("unsupported local MCP management action")
        if action == "REMOVE_LOCAL_MCP":
            if entry is not None:
                raise ValueError("MCP removal cannot contain a replacement config")
        elif not isinstance(entry, Mapping):
            raise ValueError("MCP mutation requires a typed configuration")
        current = self.inspect(target)
        if action == "ADD_LOCAL_MCP":
            if expected is not None:
                raise ValueError("MCP creation cannot contain an existing-config guard")
            if current is not None:
                raise McpManagementConflict("MCP connection already exists")
        elif current is None:
            raise McpManagementConflict("MCP connection no longer exists")
        current_guard = config_guard(current) if current is not None else None
        if expected is not None and expected != current_guard:
            raise McpManagementConflict("MCP connection changed before preparation")
        candidate = self.parse(target, entry) if entry is not None else None
        requires_confirmation = False
        try:
            _require_destination_confirmation(current, candidate, (), False)
        except ValueError:
            requires_confirmation = True
        presence = credential_presence(candidate) if candidate is not None else ()
        missing = tuple(item["name"] for item in presence if not item["present"])
        settings = self.settings.read()
        referenced = (
            managed_bindings(candidate) if candidate is not None else frozenset()
        )
        obsolete_private = any(
            item.binding.owner == target.owner and item.binding not in referenced
            for item in settings.mcp_credentials
        )
        auth_changed = current is not None and (
            candidate is None
            or current.auth != candidate.auth
            or current.transport != candidate.transport
        )
        obsolete_grant = (
            auth_changed and settings.mcp_authorization(target.owner) is not None
        )
        # Unfilled managed references require a user-owned credential write.
        # Environment-only references never acquire that physical effect.
        needs_private_input = any(
            not item["present"] and "managed_local" in item["sources"]
            for item in presence
        )
        effects = ResolvedCapabilityEffectProjection(
            workspace_write=target.workspace_root is not None,
            outside_workspace_write=(
                target.workspace_root is None
                or obsolete_private
                or obsolete_grant
                or needs_private_input
            ),
            process_control=any(
                config is not None
                and config.enabled
                and isinstance(config.transport, StdioTransportConfig)
                for config in (current, candidate)
            ),
            destructive=action == "REMOVE_LOCAL_MCP",
        )
        return PreparedLocalMcpMutation(
            action,
            target,
            current_guard,
            freeze_json(config_to_entry(candidate)) if candidate is not None else None,
            effects,
            missing,
            requires_confirmation,
        )

    async def execute_prepared_mutation(
        self,
        prepared: PreparedLocalMcpMutation,
        *,
        secrets: tuple[McpSecretMutation, ...] = (),
        retain_credentials_confirmed: bool = False,
        managed_server_ids: tuple[str, ...] = (),
    ) -> McpMutationOutcome:
        if secrets and not prepared.effects.outside_workspace_write:
            raise McpManagementConflict(
                "credential writes were not part of the prepared operation"
            )
        return await self._settle(
            prepared.target,
            thaw_json(prepared.entry) if prepared.entry is not None else None,
            secrets,
            expected=prepared.expected,
            create=prepared.action == "ADD_LOCAL_MCP",
            retain_credentials_confirmed=retain_credentials_confirmed,
            managed_server_ids=managed_server_ids,
            expected_preparation=prepared,
        )

    def parse(
        self,
        target: LocalMcpTarget,
        entry: Mapping[str, object],
        *,
        transient_secrets: tuple[McpSecretMutation, ...] = (),
    ) -> McpServerConfig:
        # Capture the private document once. The resolver exposes only this exact owner.
        current = self.settings.read()
        owner = target.owner
        values = {
            item.binding: item.value
            for item in current.mcp_credentials
            if item.binding.owner == owner
        }
        for change in transient_secrets:
            if change.binding.owner != owner:
                raise ValueError("MCP credential reference crosses its owner")
            if change.value is None:
                values.pop(change.binding, None)
            else:
                values[change.binding] = change.value

        def resolve(binding: McpCredentialBinding) -> str | None:
            if binding.owner != owner:
                raise ValueError("MCP credential reference crosses its owner")
            return values.get(binding)

        config = _parse_server(
            target.server_id,
            entry,
            runtime_source=target.source,
            secret_resolver=resolve,
        )
        if any(binding.owner != owner for binding in managed_bindings(config)):
            raise ValueError("MCP credential reference crosses its owner")
        if isinstance(config.auth, OAuthAuthorization):

            async def authorization():
                return await self.oauth.authorization(
                    owner, config, lambda: self.inspect(target)
                )

            config = replace(config, authorization_provider=authorization)
        return config

    async def test(
        self,
        target: LocalMcpTarget,
        entry: Mapping[str, object],
        *,
        workspace_root: Path,
        secrets: tuple[McpSecretMutation, ...] = (),
        retain_credentials_confirmed: bool = False,
    ) -> McpConnectionTestOutcome:
        async with self._test_lane:
            work = asyncio.create_task(
                self._test(
                    target,
                    entry,
                    workspace_root=workspace_root,
                    secrets=secrets,
                    retain_credentials_confirmed=retain_credentials_confirmed,
                )
            )
            try:
                return await asyncio.shield(work)
            except asyncio.CancelledError:
                work.cancel()
                while not work.done():
                    try:
                        await asyncio.shield(work)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not work.cancelled():
                    work.exception()
                raise

    async def _test(
        self, target, entry, *, workspace_root, secrets, retain_credentials_confirmed
    ):
        from pulsara_agent.conversation_kernel.mcp.oauth import McpOAuthNeedsLogin
        from pulsara_agent.conversation_kernel.mcp.sdk_facade import BoundedMcpSdkClient
        from pulsara_agent.conversation_kernel.mcp.wire import McpSchemaBoundExceeded
        from pulsara_agent.conversation_kernel.mcp.supervisor import (
            DEFAULT_MCP_CONNECT_ATTEMPT_TIMEOUT_SECONDS,
            discover_mcp_catalog,
        )

        config = self.parse(target, entry, transient_secrets=secrets)
        _require_destination_confirmation(
            self.inspect(target), config, secrets, retain_credentials_confirmed
        )
        if any(change.binding not in managed_bindings(config) for change in secrets):
            raise ValueError("secret mutation is not referenced by this connection")

        async def ignore_notification(_method):
            pass

        client = BoundedMcpSdkClient(
            config,
            workspace_root=workspace_root,
            notification_callback=ignore_notification,
            credential_boundary=self.credential_boundary,
        )
        try:
            # The existing discovery operation watchdog, never a stream lifetime cap.
            async with asyncio.timeout(DEFAULT_MCP_CONNECT_ATTEMPT_TIMEOUT_SECONDS):
                await client.open()
                snapshot, _ = await discover_mcp_catalog(client, config)
            return McpConnectionTestOutcome(
                "ready",
                snapshot.discovered_tool_count,
                len(snapshot.resources),
                len(snapshot.resource_templates),
                len(snapshot.prompts),
            )
        except McpCredentialMissing:
            return McpConnectionTestOutcome("credential_required")
        except McpOAuthNeedsLogin:
            return McpConnectionTestOutcome("authorization_required")
        except TimeoutError:
            return McpConnectionTestOutcome("timeout")
        except McpSchemaBoundExceeded:
            return McpConnectionTestOutcome("schema_bound_exceeded")
        except Exception:
            # Remote exception strings may contain authentication material. The
            # test reports a typed diagnostic, never a raw SDK exception.
            return McpConnectionTestOutcome("failed")
        finally:
            # ClientSession owns an AnyIO cancel scope: enter and exit it in this
            # same physical task, while the caller joins this task as a whole.
            await client.aclose()

    async def create(
        self,
        target: LocalMcpTarget,
        entry: Mapping[str, object],
        secrets: tuple[McpSecretMutation, ...] = (),
        *,
        managed_server_ids: tuple[str, ...] = (),
    ) -> McpMutationOutcome:
        return await self._settle(
            target,
            entry,
            secrets,
            expected=None,
            create=True,
            managed_server_ids=managed_server_ids,
        )

    async def update(
        self,
        target: LocalMcpTarget,
        entry: Mapping[str, object],
        *,
        expected: str,
        secrets: tuple[McpSecretMutation, ...] = (),
        retain_credentials_confirmed: bool = False,
    ) -> McpMutationOutcome:
        return await self._settle(
            target,
            entry,
            secrets,
            expected=expected,
            create=False,
            retain_credentials_confirmed=retain_credentials_confirmed,
        )

    async def remove(
        self, target: LocalMcpTarget, *, expected: str
    ) -> McpMutationOutcome:
        return await self._settle(target, None, (), expected=expected, create=False)

    async def _settle(
        self,
        target,
        entry,
        secrets,
        *,
        expected,
        create,
        managed_server_ids=(),
        retain_credentials_confirmed=False,
        expected_preparation=None,
    ):
        invalidated: list[asyncio.Task] = []
        task = asyncio.create_task(
            self._mutate(
                target,
                entry,
                secrets,
                expected=expected,
                create=create,
                invalidated=invalidated,
                managed_server_ids=managed_server_ids,
                retain_credentials_confirmed=retain_credentials_confirmed,
                expected_preparation=expected_preparation,
            )
        )
        # A submitted local write must be joined even if the HTTP caller disappears.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        try:
            return task.result()
        finally:
            # The canonical lane is released before joining cancelled refresh/login.
            joining = asyncio.gather(*invalidated, return_exceptions=True)
            while not joining.done():
                try:
                    await asyncio.shield(joining)
                except asyncio.CancelledError:
                    continue
            joining.result()

    async def _mutate(
        self,
        target,
        entry,
        secrets,
        *,
        expected,
        create,
        invalidated,
        managed_server_ids,
        retain_credentials_confirmed,
        expected_preparation=None,
    ):
        async with self.lane:
            if expected_preparation is not None:
                fresh = await asyncio.to_thread(
                    self._prepare_mutation,
                    expected_preparation.action,
                    target,
                    entry,
                    expected,
                )
                if fresh != expected_preparation:
                    raise McpManagementConflict(
                        "MCP preparation changed before execution"
                    )
            path = self.path(target)
            raw = await asyncio.to_thread(_load_raw, path)
            prior_entry = raw.get(target.server_id)
            current = (
                self.parse(target, prior_entry) if prior_entry is not None else None
            )
            if create:
                if current is not None:
                    raise McpManagementConflict("MCP connection already exists")
                if target.workspace_root is not None:
                    await asyncio.to_thread(
                        validate_workspace_mcp_server_addition_capacity,
                        workspace_root=target.workspace_root,
                        server_id=target.server_id,
                        user_config_path=self.user_config_path,
                        managed_server_ids=managed_server_ids,
                    )
                elif (
                    len(set(raw) | set(managed_server_ids) | {target.server_id})
                    > MAXIMUM_MCP_CONFIGURED_SERVERS
                ):
                    raise McpConfiguredServerBoundExceeded(
                        "too many configured MCP servers"
                    )
            elif current is None or config_guard(current) != expected:
                raise McpManagementConflict(
                    "MCP connection changed; refresh before editing"
                )
            candidate = self.parse(target, entry) if entry is not None else None
            _require_destination_confirmation(
                current, candidate, secrets, retain_credentials_confirmed
            )
            referenced = (
                managed_bindings(candidate) if candidate is not None else frozenset()
            )
            if any(change.binding not in referenced for change in secrets):
                raise ValueError("secret mutation is not referenced by this connection")
            auth_changed = current is not None and (
                candidate is None
                or current.auth != candidate.auth
                or current.transport != candidate.transport
                or bool(secrets)
            )
            if auth_changed:
                invalidated.extend(self.oauth.invalidate(target.owner))
            changes = tuple((change.binding, change.value) for change in secrets)
            previous_private = self.settings.read()
            rollback = tuple(
                (binding, previous_private.mcp_secret(binding))
                for binding, _ in changes
            )
            if changes:
                await self.settings.replace_mcp_secrets(target.owner, changes)
            if candidate is None:
                raw.pop(target.server_id)
            else:
                raw[target.server_id] = config_to_entry(candidate)
            try:
                await asyncio.to_thread(
                    _write_mcp_raw, path, raw, workspace_root=target.workspace_root
                )
            except Exception:
                # Determine whether publication actually happened before rolling values back.
                observed = await asyncio.to_thread(_load_raw, path)
                if observed != raw:
                    if changes:
                        await self.settings.replace_mcp_secrets(target.owner, rollback)
                    raise
            attention = False
            try:
                if candidate is None:
                    if target.workspace_root is not None:
                        await asyncio.to_thread(
                            remove_workspace_mcp_server_approval,
                            target.workspace_root,
                            target.server_id,
                        )
                    if (
                        any(
                            item.binding.owner == target.owner
                            for item in previous_private.mcp_credentials
                        )
                        or previous_private.mcp_authorization(target.owner) is not None
                    ):
                        await self.settings.remove_mcp_credentials(target.owner)
                else:
                    candidate = self.parse(target, raw[target.server_id])
                    if target.workspace_root is not None:
                        if candidate.enabled:
                            await asyncio.to_thread(
                                approve_workspace_mcp_server_config,
                                target.workspace_root,
                                candidate,
                            )
                        else:
                            await asyncio.to_thread(
                                remove_workspace_mcp_server_approval,
                                target.workspace_root,
                                target.server_id,
                            )
                    obsolete = tuple(
                        (item.binding, None)
                        for item in previous_private.mcp_credentials
                        if item.binding.owner == target.owner
                        and item.binding not in referenced
                    )
                    if obsolete:
                        await self.settings.replace_mcp_secrets(target.owner, obsolete)
                    if auth_changed:
                        old = self.settings.read().mcp_authorization(target.owner)
                        if old is not None:
                            await self.settings.replace_mcp_authorization(
                                target.owner, None, expected=old
                            )
            except Exception:
                attention = True
            return McpMutationOutcome(candidate, True, attention)
