"""Closed MCP credential references; values belong only to local settings/clients."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
import re
from typing import Callable, Literal


@dataclass(frozen=True, slots=True)
class McpCredentialOwner:
    kind: Literal["local", "plugin"]
    scope_key: str
    server_id: str
    plugin_id: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"local", "plugin"}:
            raise ValueError("invalid MCP credential owner kind")
        if not self.scope_key or not self.server_id:
            raise ValueError("MCP credential owner is incomplete")
        if (self.kind == "plugin") != bool(self.plugin_id):
            raise ValueError("MCP plugin credential owner is incomplete")
        if any(
            "\0" in item
            for item in (self.scope_key, self.server_id, self.plugin_id or "")
        ):
            raise ValueError("invalid MCP credential owner")


@dataclass(frozen=True, slots=True)
class McpCredentialBinding:
    owner: McpCredentialOwner
    name: str

    def __post_init__(self) -> None:
        if not isinstance(self.owner, McpCredentialOwner):
            raise TypeError("MCP credential owner must be typed")
        if self.name not in {"bearer", "oauth-client-secret"} and not (
            self.name.startswith(("header:", "env:", "input:")) and self.name.partition(":")[2]
        ):
            raise ValueError("invalid MCP credential binding")
        if "\0" in self.name:
            raise ValueError("invalid MCP credential binding")
        if self.name.startswith("input:") and self.owner.kind != "plugin":
            raise ValueError("named connection inputs belong to Plugin definitions")


@dataclass(frozen=True, slots=True)
class EnvironmentVariableReference:
    name: str

    def __post_init__(self) -> None:
        if (
            not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.name)
            or self.name == "PULSARA_API_KEY"
        ):
            raise ValueError("MCP environment reference is invalid or reserved")


@dataclass(frozen=True, slots=True)
class ManagedLocalCredentialReference:
    binding: McpCredentialBinding

    def __post_init__(self) -> None:
        if not isinstance(self.binding, McpCredentialBinding):
            raise TypeError("MCP managed reference must have a typed binding")


McpSecretReference = EnvironmentVariableReference | ManagedLocalCredentialReference
McpSecretResolver = Callable[[McpCredentialBinding], str | None]


@dataclass(frozen=True, slots=True)
class BoundSecretValue:
    parts: tuple[str | McpSecretReference, ...]

    def __post_init__(self) -> None:
        if not self.parts or not any(
            isinstance(
                p, (EnvironmentVariableReference, ManagedLocalCredentialReference)
            )
            for p in self.parts
        ):
            raise ValueError("MCP secret template requires a reference")
        if any(
            not isinstance(
                p, (str, EnvironmentVariableReference, ManagedLocalCredentialReference)
            )
            for p in self.parts
        ):
            raise TypeError("MCP secret template is not closed")


McpSecretInput = McpSecretReference | BoundSecretValue


class McpCredentialMissing(ValueError):
    def __init__(self) -> None:
        super().__init__("MCP credentials are unavailable; configure this connection")


def resolve_secret(
    value: McpSecretInput, resolver: McpSecretResolver | None = None
) -> str:
    if isinstance(value, BoundSecretValue):
        return "".join(
            p if isinstance(p, str) else resolve_secret(p, resolver)
            for p in value.parts
        )
    if isinstance(value, EnvironmentVariableReference):
        result = os.environ.get(value.name)
    elif isinstance(value, ManagedLocalCredentialReference):
        result = resolver(value.binding) if resolver is not None else None
    else:
        raise TypeError("MCP secret reference is not closed")
    if not result:
        raise McpCredentialMissing()
    return result


def resolved_secret_values(
    value: McpSecretInput, resolver: McpSecretResolver | None = None
) -> tuple[str, ...]:
    """Retain both injected templates and their secret inputs for client egress."""
    if isinstance(value, BoundSecretValue):
        parts = tuple(
            p if isinstance(p, str) else resolve_secret(p, resolver)
            for p in value.parts
        )
        return tuple(
            dict.fromkeys(
                (
                    "".join(parts),
                    *(
                        resolved
                        for p, resolved in zip(value.parts, parts, strict=True)
                        if not isinstance(p, str)
                    ),
                )
            )
        )
    return (resolve_secret(value, resolver),)


def owner_to_dict(owner: McpCredentialOwner) -> dict[str, object]:
    return {
        "kind": owner.kind,
        "scope_key": owner.scope_key,
        "server_id": owner.server_id,
        "plugin_id": owner.plugin_id,
    }


def owner_from_dict(value: object) -> McpCredentialOwner:
    if not isinstance(value, dict) or set(value) != {
        "kind",
        "scope_key",
        "server_id",
        "plugin_id",
    }:
        raise ValueError("invalid MCP credential owner shape")
    if any(
        not isinstance(value[k], str) for k in ("kind", "scope_key", "server_id")
    ) or (value["plugin_id"] is not None and not isinstance(value["plugin_id"], str)):
        raise ValueError("invalid MCP credential owner fields")
    return McpCredentialOwner(**value)


def binding_to_dict(binding: McpCredentialBinding) -> dict[str, object]:
    return {"owner": owner_to_dict(binding.owner), "name": binding.name}


def binding_from_dict(value: object) -> McpCredentialBinding:
    if (
        not isinstance(value, dict)
        or set(value) != {"owner", "name"}
        or not isinstance(value["name"], str)
    ):
        raise ValueError("invalid MCP credential binding shape")
    return McpCredentialBinding(owner_from_dict(value["owner"]), value["name"])


def secret_to_dict(value: McpSecretInput) -> dict[str, object]:
    if isinstance(value, EnvironmentVariableReference):
        return {"source": "environment", "name": value.name}
    if isinstance(value, ManagedLocalCredentialReference):
        return {"source": "managed", "binding": binding_to_dict(value.binding)}
    if isinstance(value, BoundSecretValue):
        return {
            "parts": [
                p if isinstance(p, str) else secret_to_dict(p) for p in value.parts
            ]
        }
    raise TypeError("MCP secret reference is not closed")


def secret_from_dict(value: object) -> McpSecretInput:
    if not isinstance(value, dict):
        raise ValueError("MCP secret must be a reference object")
    if (
        set(value) == {"source", "name"}
        and value["source"] == "environment"
        and isinstance(value["name"], str)
    ):
        return EnvironmentVariableReference(value["name"])
    if set(value) == {"source", "binding"} and value["source"] == "managed":
        return ManagedLocalCredentialReference(binding_from_dict(value["binding"]))
    if set(value) == {"parts"} and isinstance(value["parts"], list):
        parts = tuple(
            p if isinstance(p, str) else secret_from_dict(p) for p in value["parts"]
        )
        return BoundSecretValue(parts)
    raise ValueError("invalid MCP secret reference shape")


@dataclass(frozen=True, slots=True)
class LocalMcpCredential:
    binding: McpCredentialBinding
    value: str = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.binding, McpCredentialBinding)
            or not isinstance(self.value, str)
            or not self.value
            or "\0" in self.value
        ):
            raise ValueError("invalid MCP credential")


@dataclass(frozen=True, slots=True)
class LocalMcpOAuthRecord:
    owner: McpCredentialOwner
    resource_url: str
    issuer: str
    client_id: str
    scope: str
    # SDK serialization retained privately, never exported to capability inspection.
    token_json: str = field(repr=False)
    client_json: str = field(repr=False)
    expires_at: float | None = None
    metadata_json: str = field(default='{"authorization_server":null,"protected_resource":null}', repr=False)
    auth_json: str = field(default="{}", repr=False)

    def __post_init__(self) -> None:
        import json
        import math

        if not isinstance(self.owner, McpCredentialOwner):
            raise TypeError("OAuth owner must be typed")
        if any(
            not isinstance(v, str)
            for v in (
                self.resource_url,
                self.issuer,
                self.client_id,
                self.scope,
                self.token_json,
                self.client_json,
            )
        ):
            raise ValueError("invalid MCP OAuth record")
        if not all((self.resource_url, self.issuer, self.client_id)):
            raise ValueError("incomplete MCP OAuth binding")
        try:
            if any(
                not isinstance(json.loads(value), dict)
                for value in (
                    self.token_json,
                    self.client_json,
                    self.metadata_json,
                    self.auth_json,
                )
            ):
                raise ValueError
        except (ValueError, TypeError):
            raise ValueError("invalid private OAuth data") from None
        if self.expires_at is not None and (
            isinstance(self.expires_at, bool)
            or not isinstance(self.expires_at, (int, float))
            or not math.isfinite(self.expires_at)
        ):
            raise ValueError("invalid OAuth expiration")
