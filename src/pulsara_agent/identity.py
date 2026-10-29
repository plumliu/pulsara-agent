"""Canonical Pulsara IDs; allocation and conflict ownership stay with callers.

See archived_docs/PULSARA_UNIFIED_IDENTITY_HARD_CUT_IMPLEMENTATION_SPEC.zh.md.
These functions encode identities, never resolve objects or grant access.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from typing import Literal, get_args


IdentityKind = Literal[
    "workspace",
    "session",
    "blob",
    "image",
    "command",
    "queue",
    "turn",
    "entry",
    "snapshot",
    "binding",
    "history",
    "block",
    "replay",
    "attempt",
    "result",
    "artifact",
    "call",
    "batch",
    "task",
    "agent-result",
    "agent-message",
    "decision",
    "plan",
    "plan-interaction",
    "permission",
    "schedule",
    "memory",
    "relation",
    "event",
    "connection",
    "package",
    "process",
    "cell",
    "stream",
    "monitor",
    "monitor-settlement",
    "subscription",
    "terminal-decision",
    "observation",
    "todo-run",
    "todo-settlement",
    "borrow",
    "interaction",
    "host",
    "launch",
    "attachment",
    "transport",
    "request",
    "operation",
    "compact-attempt",
    "epoch",
    "permit",
    "planning",
    "bootstrap",
    "preparation",
    "replay-reservation",
    "extension",
    "observer",
    "generation",
    "terminal-owner",
    "mcp-candidate",
    "slot",
    "settlement",
    "fence",
    "context",
    "segment",
    "page",
]

# A closed format vocabulary, not a registry of objects or allocation state.
_KINDS = frozenset(get_args(IdentityKind))
ID_PAYLOAD_PATTERN = r"[a-z2-7]{25}[aeimquy4]"
REVISION_PATTERN = r"revision:[a-z2-7]{51}[aq]"


def _base32(value: bytes) -> str:
    return base64.b32encode(value).decode("ascii").rstrip("=").lower()


def _check_kind(kind: str) -> None:
    if kind not in _KINDS:
        raise ValueError("INVALID_ID: unknown identity kind")


def random_id(kind: IdentityKind) -> str:
    _check_kind(kind)
    return f"{kind}:{_base32(secrets.token_bytes(16))}"


def derive_id(kind: IdentityKind, role: str, *parts: str) -> str:
    _check_kind(kind)
    values = ["pulsara-id-v1", kind, role, *parts]
    if not role or any(not isinstance(value, str) for value in values):
        raise ValueError("identity derivation requires a role and string parts")
    encoded = json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return f"{kind}:{_base32(hashlib.sha256(encoded).digest()[:16])}"


def is_valid_id(value: object, *kinds: IdentityKind) -> bool:
    if not kinds or any(kind not in _KINDS for kind in kinds):
        raise ValueError("identity validation requires explicit canonical kinds")
    return isinstance(value, str) and any(
        re.fullmatch(re.escape(kind) + ":" + ID_PAYLOAD_PATTERN, value) is not None
        for kind in kinds
    )


def require_id(value: str, *kinds: IdentityKind) -> str:
    if not is_valid_id(value, *kinds):
        raise ValueError("INVALID_ID: noncanonical identity or wrong kind")
    return value


def content_revision(content: bytes) -> str:
    """Full 256-bit content version; deliberately not a business ID."""
    return "revision:" + _base32(hashlib.sha256(content).digest())
