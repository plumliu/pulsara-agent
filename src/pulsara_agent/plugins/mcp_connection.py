"""Non-secret instance overrides; immutable portable definitions remain untouched."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Mapping

from pulsara_agent.mcp_config import (
    McpAuthConfig,
    NoAuth,
    StdioTransportConfig,
    _parse_server,
)
from pulsara_agent.mcp_credentials import (
    McpCredentialOwner,
    McpSecretInput,
    secret_to_dict,
)


def plugin_connection_owner(identity, local_server_id):
    return McpCredentialOwner(
        "plugin",
        "user"
        if identity.workspace_state_key is None
        else f"workspace:{identity.workspace_state_key}",
        local_server_id,
        identity.plugin_id,
    )


@dataclass(frozen=True, slots=True)
class PluginConnectionReview:
    overlay: PluginMcpConnectionOverlay
    # Only presence is reviewed. Rotation never exposes or versions the value.
    credentials: tuple[tuple[str, bool, tuple[str, ...]], ...]


def connection_review(overlays, secret_resolver, *, servers, identity):
    from pulsara_agent.capability.mcp_management import credential_presence

    effective = []
    for server in servers:
        overlay = next(
            (
                item
                for item in overlays
                if item.local_server_id == server.local_server_id
            ),
            None,
        )
        overlay = resolve_connection_overlay(
            server,
            overlay,
            owner=plugin_connection_owner(identity, server.local_server_id),
        )
        if overlay is not None:
            effective.append(overlay)
    return tuple(
        PluginConnectionReview(
            overlay,
            tuple(
                (item["name"], item["present"], tuple(item["sources"]))
                for item in credential_presence(
                    _parse_server(
                        overlay.local_server_id,
                        overlay.validation_entry(),
                        secret_resolver=secret_resolver,
                    )
                )
            ),
        )
        for overlay in effective
    )


def review_to_dict(review):
    return [
        {
            "overlay": overlay_to_dict(item.overlay),
            "credentials": [
                {"name": name, "present": present, "sources": list(sources)}
                for name, present, sources in item.credentials
            ],
        }
        for item in review
    ]


def review_from_dict(value):
    if not isinstance(value, list):
        raise ValueError("Plugin connection review must be an array")
    result = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"overlay", "credentials"}:
            raise ValueError("Plugin connection review fields are invalid")
        if not isinstance(item["credentials"], list):
            raise ValueError("Plugin credential review must be an array")
        credentials = []
        for entry in item["credentials"]:
            if (
                not isinstance(entry, dict)
                or set(entry) != {"name", "present", "sources"}
                or not isinstance(entry["name"], str)
                or not isinstance(entry["present"], bool)
                or not isinstance(entry["sources"], list)
                or any(
                    not isinstance(source, str)
                    or source not in {"managed_local", "environment"}
                    for source in entry["sources"]
                )
            ):
                raise ValueError("Plugin credential review is invalid")
            credentials.append(
                (entry["name"], entry["present"], tuple(entry["sources"]))
            )
        result.append(
            PluginConnectionReview(
                overlay_from_dict(item["overlay"]), tuple(credentials)
            )
        )
    return tuple(result)


@dataclass(frozen=True, slots=True)
class PluginMcpConnectionOverlay:
    local_server_id: str
    transport_kind: str
    endpoint: str | None = None
    public_headers: tuple[tuple[str, str], ...] = ()
    environment: tuple[tuple[str, str], ...] = ()
    secret_environment: tuple[tuple[str, McpSecretInput], ...] = ()
    auth: McpAuthConfig = NoAuth()

    def __post_init__(self):
        if self.transport_kind not in {"stdio", "streamable_http", "sse"}:
            raise ValueError("Plugin connection transport is invalid")
        if not self.local_server_id or len(self.local_server_id.encode()) > 128:
            raise ValueError("Plugin connection server id is invalid")
        for pairs in (self.public_headers, self.environment, self.secret_environment):
            if pairs != tuple(sorted(pairs)) or len({key for key, _ in pairs}) != len(
                pairs
            ):
                raise ValueError("Plugin connection fields must be unique and ordered")
        if self.transport_kind == "stdio":
            if (
                self.endpoint is not None
                or self.public_headers
                or not isinstance(self.auth, NoAuth)
            ):
                raise ValueError("Plugin stdio connection cannot override HTTP fields")
        elif self.environment or self.secret_environment:
            raise ValueError(
                "Plugin HTTP connection cannot override process environment"
            )
        if {name for name, _ in (*self.environment, *self.secret_environment)} & {
            "PLUGIN_ROOT",
            "PLUGIN_DATA",
        }:
            raise ValueError("Plugin reserved environment cannot be overridden")
        # One native validator owns connection/auth syntax. The stand-in command
        # is never executable and does not grant a package/process authority.
        _parse_server(self.local_server_id, self.validation_entry())

    def validation_entry(self) -> dict:
        from pulsara_agent.capability.mcp_management import auth_to_entry

        if self.transport_kind == "stdio":
            transport = {
                "type": "stdio",
                "command": "plugin-connection-validation",
                "env": dict(self.environment),
                "secret_env": {
                    key: secret_to_dict(ref) for key, ref in self.secret_environment
                },
            }
        else:
            endpoint = self.endpoint or "https://example.invalid/mcp"
            transport = {
                "type": self.transport_kind,
                "endpoint": endpoint,
                "allow_http_localhost": endpoint.lower().startswith("http://"),
            }
        return {
            "transport": transport,
            "public_headers": dict(self.public_headers),
            "auth": auth_to_entry(self.auth),
        }


def overlay_to_dict(overlay: PluginMcpConnectionOverlay) -> dict:
    from pulsara_agent.capability.mcp_management import auth_to_entry

    return {
        "local_server_id": overlay.local_server_id,
        "transport_kind": overlay.transport_kind,
        "endpoint": overlay.endpoint,
        "public_headers": dict(overlay.public_headers),
        "environment": dict(overlay.environment),
        "secret_environment": {
            key: secret_to_dict(ref) for key, ref in overlay.secret_environment
        },
        "auth": auth_to_entry(overlay.auth),
    }


def overlay_from_dict(value: object) -> PluginMcpConnectionOverlay:
    from pulsara_agent.mcp_config import _parse_auth, _string_mapping
    from pulsara_agent.mcp_credentials import secret_from_dict

    if not isinstance(value, Mapping) or set(value) != {
        "local_server_id",
        "transport_kind",
        "endpoint",
        "public_headers",
        "environment",
        "secret_environment",
        "auth",
    }:
        raise ValueError("Plugin connection overlay has an invalid shape")
    if not isinstance(value["local_server_id"], str) or not isinstance(
        value["transport_kind"], str
    ):
        raise ValueError("Plugin connection identity must be text")
    if value["endpoint"] is not None and not isinstance(value["endpoint"], str):
        raise ValueError("Plugin endpoint must be text")
    secret_env = value["secret_environment"]
    if not isinstance(secret_env, Mapping):
        raise ValueError("Plugin secret environment must be an object")
    return PluginMcpConnectionOverlay(
        value["local_server_id"],
        value["transport_kind"],
        value["endpoint"],
        tuple(
            sorted(
                _string_mapping(
                    value["public_headers"], "Plugin public headers"
                ).items()
            )
        ),
        tuple(
            sorted(_string_mapping(value["environment"], "Plugin environment").items())
        ),
        tuple(sorted((key, secret_from_dict(ref)) for key, ref in secret_env.items())),
        _parse_auth(value["auth"]),
    )


def apply_connection_overlay(transport, public_headers, overlay):
    """Compose already-authorized package fields with an exact typed overlay."""
    if overlay is None:
        return transport, public_headers, NoAuth()
    if transport.kind.value != overlay.transport_kind:
        raise ValueError("Plugin connection transport no longer matches its package")
    if isinstance(transport, StdioTransportConfig):
        environment = dict(transport.environment)
        environment.update(overlay.environment)
        # Secret targets replace ordinary defaults only in the runtime materialization.
        for name, _ in overlay.secret_environment:
            environment.pop(name, None)
        transport = replace(
            transport,
            environment=tuple(sorted(environment.items())),
            secret_environment=overlay.secret_environment,
        )
    elif overlay.endpoint is not None:
        transport = replace(
            transport,
            endpoint=overlay.endpoint,
            allow_http_localhost=overlay.endpoint.lower().startswith("http://"),
        )
    headers = {key.lower(): (key, value) for key, value in public_headers}
    headers.update({key.lower(): (key, value) for key, value in overlay.public_headers})
    return transport, tuple(sorted(headers.values())), overlay.auth


def resolve_connection_overlay(server, overlay, *, owner):
    """Immutable input defaults are composed before the instance's finite edits."""
    from .connection_inputs import connection_input_defaults
    from pulsara_agent.mcp_config import StaticHeaderSecretReferences

    if overlay is not None:
        from pulsara_agent.capability.mcp_management import managed_bindings

        allowed = {
            "input:" + item.name
            for item in server.connection_inputs.inputs
            if item.private
        }
        refs = managed_bindings(
            _parse_server(server.local_server_id, overlay.validation_entry())
        )
        if any(
            binding.name.startswith("input:")
            and (binding.name not in allowed or binding.owner != owner)
            for binding in refs
        ):
            raise ValueError(
                "Plugin input reference no longer belongs to its immutable definition"
            )
    if not server.connection_inputs.targets:
        return overlay
    defaults = connection_input_defaults(
        server.connection_inputs,
        owner=owner,
        transport_kind="streamable_http"
        if server.kind.value == "streamable-http"
        else server.kind.value,
    )
    if overlay is None:
        return defaults
    if defaults.transport_kind != overlay.transport_kind:
        raise ValueError("Plugin input definition and instance transport differ")
    auth = defaults.auth if isinstance(overlay.auth, NoAuth) else overlay.auth
    if isinstance(defaults.auth, StaticHeaderSecretReferences) and isinstance(
        overlay.auth, StaticHeaderSecretReferences
    ):
        headers = {name.lower(): (name, value) for name, value in defaults.auth.headers}
        headers.update(
            {name.lower(): (name, value) for name, value in overlay.auth.headers}
        )
        auth = StaticHeaderSecretReferences(tuple(sorted(headers.values())))
    return replace(
        overlay,
        endpoint=overlay.endpoint
        if overlay.endpoint is not None
        else defaults.endpoint,
        public_headers=tuple(
            sorted(
                {
                    **dict(defaults.public_headers),
                    **dict(overlay.public_headers),
                }.items()
            )
        ),
        environment=tuple(
            sorted({**dict(defaults.environment), **dict(overlay.environment)}.items())
        ),
        secret_environment=tuple(
            sorted(
                {
                    **dict(defaults.secret_environment),
                    **dict(overlay.secret_environment),
                }.items()
            )
        ),
        auth=auth,
    )


def connection_editor_definition(server, overlay, *, owner):
    """Public editor fields, not a substitute for executable package composition."""
    from pulsara_agent.capability.mcp_management import auth_to_entry

    if server.kind.value == "stdio":
        transport = {
            "type": "stdio",
            "command": server.command,
            "args": list(server.args),
            "cwd": server.cwd or "${PLUGIN_ROOT}",
            "env": dict(server.environment),
            "secret_env": {},
        }
        headers = {}
    else:
        transport = {
            "type": "sse" if server.kind.value == "sse" else "streamable_http",
            "endpoint": server.endpoint,
        }
        headers = dict(server.public_headers)
    defaults = {
        "transport": transport,
        "public_headers": headers,
        "auth": {"type": "none"},
    }
    overlay = resolve_connection_overlay(server, overlay, owner=owner)
    if overlay is None:
        return defaults, defaults
    transport = dict(transport)
    if server.kind.value == "stdio":
        transport["env"] = {**transport["env"], **dict(overlay.environment)}
        transport["secret_env"] = {
            key: secret_to_dict(ref) for key, ref in overlay.secret_environment
        }
        transport["env"] = {
            key: value
            for key, value in transport["env"].items()
            if key not in transport["secret_env"]
        }
    elif overlay.endpoint is not None:
        transport["endpoint"] = overlay.endpoint
    public_headers = {key.lower(): (key, value) for key, value in headers.items()}
    public_headers.update(
        {key.lower(): (key, value) for key, value in overlay.public_headers}
    )
    return defaults, {
        "transport": transport,
        "public_headers": dict(public_headers.values()),
        "auth": auth_to_entry(overlay.auth),
    }
