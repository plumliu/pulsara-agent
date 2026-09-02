"""Controlled advisory-memory context and domain helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Literal


CTX_GLOBAL = "ctx:global"
WORKSPACE_CONTEXT_PREFIX = "ctx:workspace/"

_FLAT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_WORKSPACE_CONTEXT_KEY_CHARS = 16


class MemoryHostWorkspaceKind(StrEnum):
    PROJECT = "PROJECT"
    TRANSIENT = "TRANSIENT"


@dataclass(frozen=True, slots=True)
class FrozenMemoryReadContextBinding:
    """Host-selected advisory-memory visibility; never a durable authority."""

    memory_domain_id: str
    host_workspace_id: str
    host_workspace_kind: MemoryHostWorkspaceKind
    readable_context_ids: tuple[str, ...]
    binding_fingerprint: str

    def __post_init__(self) -> None:
        if not is_valid_flat_id(self.memory_domain_id) or not self.host_workspace_id:
            raise ValueError("memory read binding identity is invalid")
        if self.host_workspace_kind is MemoryHostWorkspaceKind.PROJECT:
            if (
                len(self.readable_context_ids) != 2
                or self.readable_context_ids[0] != CTX_GLOBAL
                or not self.readable_context_ids[1].startswith(
                    WORKSPACE_CONTEXT_PREFIX
                )
            ):
                raise ValueError(
                    "project memory binding needs global then current-project context"
                )
        elif self.readable_context_ids != (CTX_GLOBAL,):
            raise ValueError("transient memory binding can only read global memory")
        if len(set(self.readable_context_ids)) != len(self.readable_context_ids) or any(
            not is_valid_context_id(value) for value in self.readable_context_ids
        ):
            raise ValueError("memory read binding contains an invalid context")
        if self.binding_fingerprint != memory_read_context_binding_fingerprint(
            memory_domain_id=self.memory_domain_id,
            host_workspace_id=self.host_workspace_id,
            host_workspace_kind=self.host_workspace_kind,
            readable_context_ids=self.readable_context_ids,
        ):
            raise ValueError("memory read binding fingerprint mismatch")

    def can_read(self, context_id: str) -> bool:
        return context_id in self.readable_context_ids

    @property
    def current_project_context_id(self) -> str | None:
        if self.host_workspace_kind is MemoryHostWorkspaceKind.PROJECT:
            return self.readable_context_ids[1]
        return None


def memory_read_context_binding_fingerprint(
    *,
    memory_domain_id: str,
    host_workspace_id: str,
    host_workspace_kind: MemoryHostWorkspaceKind,
    readable_context_ids: tuple[str, ...],
) -> str:
    import json

    payload = {
        "memory_domain_id": memory_domain_id,
        "host_workspace_id": host_workspace_id,
        "host_workspace_kind": host_workspace_kind.value,
        "readable_context_ids": readable_context_ids,
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return "sha256:" + sha256(
        b"pulsara:memory-read-context-binding:v2\x00" + encoded
    ).hexdigest()


def freeze_memory_read_context_binding(
    *, domain: "MemoryDomainContext", host_workspace_id: str
) -> FrozenMemoryReadContextBinding:
    contexts = [CTX_GLOBAL]
    kind = MemoryHostWorkspaceKind.TRANSIENT
    if domain.workspace_kind == "project":
        assert domain.stable_project_key is not None
        kind = MemoryHostWorkspaceKind.PROJECT
        contexts.append(workspace_context_id(domain.stable_project_key))
    ordered = tuple(contexts)
    return FrozenMemoryReadContextBinding(
        memory_domain_id=domain.memory_domain_id,
        host_workspace_id=host_workspace_id,
        host_workspace_kind=kind,
        readable_context_ids=ordered,
        binding_fingerprint=memory_read_context_binding_fingerprint(
            memory_domain_id=domain.memory_domain_id,
            host_workspace_id=host_workspace_id,
            host_workspace_kind=kind,
            readable_context_ids=ordered,
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


def workspace_context_key(stable_project_key: str) -> str:
    canonical = canonical_project_key(stable_project_key)
    return sha256(canonical.encode("utf-8")).hexdigest()[
        :_WORKSPACE_CONTEXT_KEY_CHARS
    ]


def workspace_context_id(stable_project_key: str) -> str:
    return f"{WORKSPACE_CONTEXT_PREFIX}{workspace_context_key(stable_project_key)}"


def is_valid_context_id(context_id: str) -> bool:
    if context_id == CTX_GLOBAL:
        return True
    if context_id.startswith(WORKSPACE_CONTEXT_PREFIX):
        key = context_id[len(WORKSPACE_CONTEXT_PREFIX) :]
        return is_valid_flat_id(key)
    return False


def parse_context_id(
    context_id: str,
) -> tuple[Literal["global"], None] | tuple[Literal["workspace"], str]:
    if context_id == CTX_GLOBAL:
        return ("global", None)
    if context_id.startswith(WORKSPACE_CONTEXT_PREFIX):
        key = context_id[len(WORKSPACE_CONTEXT_PREFIX) :]
        if is_valid_flat_id(key):
            return ("workspace", key)
    raise ValueError(f"invalid memory context: {context_id!r}")


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
    def read_context_ids(self) -> frozenset[str]:
        return context_ids_for_domain(self)

    @property
    def allowed_write_context_ids(self) -> frozenset[str]:
        return context_ids_for_domain(self)


def context_ids_for_domain(domain: MemoryDomainContext) -> frozenset[str]:
    context_ids = {CTX_GLOBAL}
    if domain.workspace_kind == "project":
        assert domain.stable_project_key is not None
        context_ids.add(workspace_context_id(domain.stable_project_key))
    return frozenset(context_ids)


def format_context_list(
    context_ids: frozenset[str] | tuple[str, ...] | list[str],
) -> str:
    return ", ".join(sorted(context_ids))


__all__ = [
    "CTX_GLOBAL",
    "WORKSPACE_CONTEXT_PREFIX",
    "FrozenMemoryReadContextBinding",
    "MemoryDomainContext",
    "MemoryHostWorkspaceKind",
    "canonical_project_key",
    "context_ids_for_domain",
    "format_context_list",
    "freeze_memory_read_context_binding",
    "is_valid_context_id",
    "is_valid_flat_id",
    "memory_read_context_binding_fingerprint",
    "parse_context_id",
    "workspace_context_id",
    "workspace_context_key",
]
