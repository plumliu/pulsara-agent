"""Controlled memory scope and domain helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Literal


CTX_USER = "ctx:user"
WORKSPACE_SCOPE_PREFIX = "ctx:workspace/"

_FLAT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_WORKSPACE_SCOPE_KEY_CHARS = 16


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


def parse_scope(scope: str) -> tuple[Literal["user"], str | None] | tuple[Literal["workspace"], str]:
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
            raise ValueError(f"memory_domain_id must be a flat id: {self.memory_domain_id!r}")
        if self.workspace_kind not in {"project", "transient"}:
            raise ValueError(f"workspace_kind must be 'project' or 'transient': {self.workspace_kind!r}")
        if self.workspace_kind == "project":
            if self.stable_project_key is None:
                raise ValueError("project memory domain requires stable_project_key")
            object.__setattr__(self, "stable_project_key", canonical_project_key(self.stable_project_key))
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
