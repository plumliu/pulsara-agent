"""Controlled memory scope and domain helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Literal


CTX_USER = "ctx:user"
WORKSPACE_SCOPE_PREFIX = "ctx:workspace/"

_FLAT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_WORKSPACE_SCOPE_KEY_CHARS = 16


class MemoryScopeKind(StrEnum):
    USER = "USER"
    WORKSPACE = "WORKSPACE"


class MemoryHostWorkspaceKind(StrEnum):
    PROJECT = "PROJECT"
    TRANSIENT = "TRANSIENT"


@dataclass(frozen=True, slots=True)
class FrozenMemoryScope:
    kind: MemoryScopeKind
    scope_id: str

    def __post_init__(self) -> None:
        if self.kind is MemoryScopeKind.USER:
            if self.scope_id != CTX_USER:
                raise ValueError("USER memory scope must use ctx:user")
        elif not self.scope_id.startswith(WORKSPACE_SCOPE_PREFIX) or not is_valid_scope(
            self.scope_id
        ):
            raise ValueError("WORKSPACE memory scope identity is invalid")


@dataclass(frozen=True, slots=True)
class FrozenMemoryReadScopeBinding:
    """Host-selected advisory-memory visibility; never a durable authority."""

    memory_domain_id: str
    host_workspace_id: str
    host_workspace_kind: MemoryHostWorkspaceKind
    readable_scopes: tuple[FrozenMemoryScope, ...]
    binding_fingerprint: str

    def __post_init__(self) -> None:
        if not is_valid_flat_id(self.memory_domain_id) or not self.host_workspace_id:
            raise ValueError("memory read binding identity is invalid")
        expected_scopes = (FrozenMemoryScope(MemoryScopeKind.USER, CTX_USER),)
        if self.host_workspace_kind is MemoryHostWorkspaceKind.PROJECT:
            if len(self.readable_scopes) != 2:
                raise ValueError("project memory binding needs USER and WORKSPACE")
            if self.readable_scopes[0] != expected_scopes[0]:
                raise ValueError("USER memory scope must be first")
            if self.readable_scopes[1].kind is not MemoryScopeKind.WORKSPACE:
                raise ValueError("project memory binding lacks WORKSPACE scope")
        elif self.readable_scopes != expected_scopes:
            raise ValueError("transient memory binding can only read USER memory")
        if self.binding_fingerprint != memory_read_scope_binding_fingerprint(
            memory_domain_id=self.memory_domain_id,
            host_workspace_id=self.host_workspace_id,
            host_workspace_kind=self.host_workspace_kind,
            readable_scopes=self.readable_scopes,
        ):
            raise ValueError("memory read binding fingerprint mismatch")

    def can_read(self, kind: MemoryScopeKind | str, scope_id: str) -> bool:
        try:
            target = FrozenMemoryScope(MemoryScopeKind(kind), scope_id)
        except (ValueError, TypeError):
            return False
        return target in self.readable_scopes


def memory_read_scope_binding_fingerprint(
    *,
    memory_domain_id: str,
    host_workspace_id: str,
    host_workspace_kind: MemoryHostWorkspaceKind,
    readable_scopes: tuple[FrozenMemoryScope, ...],
) -> str:
    import json

    payload = {
        "memory_domain_id": memory_domain_id,
        "host_workspace_id": host_workspace_id,
        "host_workspace_kind": host_workspace_kind.value,
        "readable_scopes": tuple(
            (scope.kind.value, scope.scope_id) for scope in readable_scopes
        ),
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return "sha256:" + sha256(
        b"pulsara:memory-read-scope-binding:v1\x00" + encoded
    ).hexdigest()


def freeze_memory_read_scope_binding(
    *, domain: "MemoryDomainContext", host_workspace_id: str
) -> FrozenMemoryReadScopeBinding:
    scopes = [FrozenMemoryScope(MemoryScopeKind.USER, CTX_USER)]
    kind = MemoryHostWorkspaceKind.TRANSIENT
    if domain.workspace_kind == "project":
        assert domain.stable_project_key is not None
        kind = MemoryHostWorkspaceKind.PROJECT
        scopes.append(
            FrozenMemoryScope(
                MemoryScopeKind.WORKSPACE, workspace_scope(domain.stable_project_key)
            )
        )
    ordered = tuple(scopes)
    return FrozenMemoryReadScopeBinding(
        memory_domain_id=domain.memory_domain_id,
        host_workspace_id=host_workspace_id,
        host_workspace_kind=kind,
        readable_scopes=ordered,
        binding_fingerprint=memory_read_scope_binding_fingerprint(
            memory_domain_id=domain.memory_domain_id,
            host_workspace_id=host_workspace_id,
            host_workspace_kind=kind,
            readable_scopes=ordered,
        ),
    )


def is_valid_flat_id(value: str) -> bool:
    return bool(_FLAT_ID_RE.fullmatch(value))


def canonical_project_key(stable_project_key: str) -> str:
    value = stable_project_key.strip()
    if not value:
        raise ValueError("stable_project_key must not be empty")
    path = Path(value).expanduser()
    if path.is_absolute() or "/" in value:
        return path.resolve(strict=False).as_posix()
    return value


def workspace_scope_key(stable_project_key: str) -> str:
    canonical = canonical_project_key(stable_project_key)
    return sha256(canonical.encode("utf-8")).hexdigest()[:_WORKSPACE_SCOPE_KEY_CHARS]


def workspace_scope(stable_project_key: str) -> str:
    return f"{WORKSPACE_SCOPE_PREFIX}{workspace_scope_key(stable_project_key)}"


def is_valid_scope(scope: str) -> bool:
    if scope == CTX_USER:
        return True
    if scope.startswith(WORKSPACE_SCOPE_PREFIX):
        key = scope[len(WORKSPACE_SCOPE_PREFIX) :]
        return is_valid_flat_id(key)
    return False


def parse_scope(
    scope: str,
) -> tuple[Literal["user"], str | None] | tuple[Literal["workspace"], str]:
    if scope == CTX_USER:
        return ("user", None)
    if scope.startswith(WORKSPACE_SCOPE_PREFIX):
        key = scope[len(WORKSPACE_SCOPE_PREFIX) :]
        if is_valid_flat_id(key):
            return ("workspace", key)
    raise ValueError(f"invalid memory scope: {scope!r}")


@dataclass(frozen=True, slots=True)
class MemoryDomainContext:
    memory_domain_id: str
    workspace_kind: Literal["project", "transient"]
    stable_project_key: str | None = None
    workspace_label: str | None = None

    def __post_init__(self) -> None:
        if not is_valid_flat_id(self.memory_domain_id):
            raise ValueError(
                f"memory_domain_id must be a flat id: {self.memory_domain_id!r}"
            )
        if self.workspace_kind not in {"project", "transient"}:
            raise ValueError(
                f"workspace_kind must be 'project' or 'transient': {self.workspace_kind!r}"
            )
        if self.workspace_kind == "project":
            if self.stable_project_key is None:
                raise ValueError("project memory domain requires stable_project_key")
            object.__setattr__(
                self,
                "stable_project_key",
                canonical_project_key(self.stable_project_key),
            )
        elif self.stable_project_key is not None:
            raise ValueError("transient memory domain must not set stable_project_key")

    @property
    def graph_id(self) -> str:
        return f"graph:user/{self.memory_domain_id}"

    @property
    def read_scopes(self) -> frozenset[str]:
        return scopes_for_domain(self)

    @property
    def allowed_write_scopes(self) -> frozenset[str]:
        return scopes_for_domain(self)


def scopes_for_domain(domain: MemoryDomainContext) -> frozenset[str]:
    scopes = {CTX_USER}
    if domain.workspace_kind == "project":
        assert domain.stable_project_key is not None
        scopes.add(workspace_scope(domain.stable_project_key))
    return frozenset(scopes)


def format_scope_list(scopes: frozenset[str] | tuple[str, ...] | list[str]) -> str:
    return ", ".join(sorted(scopes))
