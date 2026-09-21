"""Provider-neutral contracts for directly written advisory memory."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
import json
import unicodedata
from typing import Mapping

from pulsara_agent.memory.scope import (
    CTX_GLOBAL,
    WORKSPACE_CONTEXT_PREFIX,
    FrozenMemoryReadContextBinding,
    is_valid_context_id,
)


MAXIMUM_MEMORY_STATEMENT_BYTES = 8 * 1024
MAXIMUM_RESPONSE_PREFERENCE_STATEMENT_BYTES = 2 * 1024
MAXIMUM_MEMORY_REFERENCE_ITEMS = 8


class MemoryFactKind(StrEnum):
    USER_PROFILE = "USER_PROFILE"
    RESPONSE_PREFERENCE = "RESPONSE_PREFERENCE"
    FACT = "FACT"
    DECISION = "DECISION"


class MemoryRelationKind(StrEnum):
    BASED_ON = "BASED_ON"
    SUPERSEDES = "SUPERSEDES"
    CONTRADICTS = "CONTRADICTS"


class MemorySupersedeMode(StrEnum):
    SAME_KIND_REPLACEMENT = "SAME_KIND_REPLACEMENT"
    TAXONOMY_CORRECTION = "TAXONOMY_CORRECTION"


class AutomaticMemoryTriggerDisposition(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    DISABLED_BY_EXPLICIT_USER_DIRECTIVE = "DISABLED_BY_EXPLICIT_USER_DIRECTIVE"
    SKIPPED_LOW_INFORMATION = "SKIPPED_LOW_INFORMATION"


class MemoryUsePolicy(StrEnum):
    ENABLED = "ENABLED"
    WRITE_DISABLED_BY_USER = "WRITE_DISABLED_BY_USER"
    ALL_DISABLED_BY_USER = "ALL_DISABLED_BY_USER"

    @property
    def allows_reads(self) -> bool:
        return self is not MemoryUsePolicy.ALL_DISABLED_BY_USER

    @property
    def allows_writes(self) -> bool:
        return self is MemoryUsePolicy.ENABLED


def strongest_memory_use_policy(
    current: MemoryUsePolicy, candidate: MemoryUsePolicy
) -> MemoryUsePolicy:
    strength = {
        MemoryUsePolicy.ENABLED: 0,
        MemoryUsePolicy.WRITE_DISABLED_BY_USER: 1,
        MemoryUsePolicy.ALL_DISABLED_BY_USER: 2,
    }
    return candidate if strength[candidate] > strength[current] else current


@dataclass(frozen=True, slots=True)
class FrozenMemoryTriggerPolicy:
    automatic_recall: AutomaticMemoryTriggerDisposition
    memory_use: MemoryUsePolicy
    write_hint: bool


@dataclass(frozen=True, slots=True)
class FrozenModelCallMemoryContext:
    memory_use_policy: MemoryUsePolicy = MemoryUsePolicy.ENABLED


@dataclass(frozen=True, slots=True)
class PreparedMemoryBasisReference:
    target_fact_id: str
    target_context_id: str
    ordinal: int

    def __post_init__(self) -> None:
        if not self.target_fact_id or not 0 <= self.ordinal < 8:
            raise ValueError("memory basis reference is invalid")


def memory_response_preference_item_payload(
    *, memory_id: str, context_id: str, statement: str, recorded_at: str
) -> Mapping[str, object]:
    validate_canonical_memory_recorded_at(recorded_at)
    return {
        "memory_id": memory_id,
        "kind": MemoryFactKind.RESPONSE_PREFERENCE.value,
        "context_product_label": memory_context_product_label(context_id),
        "current_context": context_id != CTX_GLOBAL,
        "statement": statement,
        "recorded_at": recorded_at,
        "advisory": True,
    }


def memory_relation_id(
    *,
    memory_domain_id: str,
    source_context_id: str,
    source_fact_id: str,
    relation_kind: MemoryRelationKind,
    target_context_id: str,
    target_fact_id: str,
    supersede_mode: MemorySupersedeMode | None,
) -> str:
    if relation_kind is MemoryRelationKind.CONTRADICTS:
        source_endpoint = source_context_id, source_fact_id
        target_endpoint = target_context_id, target_fact_id
        if target_endpoint < source_endpoint:
            source_context_id, target_context_id, source_fact_id, target_fact_id = (
                target_context_id, source_context_id, target_fact_id, source_fact_id
            )
    identity = canonical_json_bytes(
        (
            "pulsara.memory-relation.v2-context-hard-cut",
            memory_domain_id,
            source_context_id,
            source_fact_id,
            relation_kind.value,
            target_context_id,
            target_fact_id,
            None if supersede_mode is None else supersede_mode.value,
        )
    )
    return "memory-relation:" + sha256(identity).hexdigest()


def memory_fact_semantic_digest(*, kind: MemoryFactKind, statement: str) -> str:
    return digest(
        "pulsara:memory-fact-semantic:v3",
        {"fact_kind": kind.value, "statement": normalize_memory_text(statement)},
    )


def visible_context_predicate(binding: FrozenMemoryReadContextBinding) -> tuple[str, ...]:
    return binding.readable_context_ids


def memory_basis_context_allowed(source_context_id: str, target_context_id: str) -> bool:
    if source_context_id == CTX_GLOBAL:
        return target_context_id == CTX_GLOBAL
    return source_context_id.startswith(WORKSPACE_CONTEXT_PREFIX) and (
        target_context_id == CTX_GLOBAL or target_context_id == source_context_id
    )


def memory_context_product_label(context_id: str) -> str:
    if context_id == CTX_GLOBAL:
        return "available across conversations; use only as limited by the statement"
    if context_id.startswith(WORKSPACE_CONTEXT_PREFIX) and is_valid_context_id(context_id):
        return "available only in the current project/context"
    raise ValueError("memory context identity is invalid")


def canonical_memory_recorded_at(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("memory recorded_at must be timezone-aware")
    return value.astimezone(timezone.utc).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def validate_canonical_memory_recorded_at(value: str) -> None:
    if not value or not value.endswith("Z"):
        raise ValueError("memory recorded_at is not canonical UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("memory recorded_at is not RFC 3339") from exc
    if canonical_memory_recorded_at(parsed) != value:
        raise ValueError("memory recorded_at is not canonical UTC seconds")


def normalize_memory_text(value: str) -> str:
    return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n")).strip()


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(domain: str, value: object) -> str:
    return "sha256:" + sha256(domain.encode("utf-8") + b"\x00" + canonical_json_bytes(value)).hexdigest()
