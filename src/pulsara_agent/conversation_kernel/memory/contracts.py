"""Current advisory-memory objects and process-local observation candidates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import json
import unicodedata
from typing import Mapping

from pulsara_agent.identity import require_id
from pulsara_agent.memory.scope import CTX_GLOBAL, is_valid_context_id
from pulsara_agent.memory.targets import memory_target
from pulsara_agent.ports.tool_execution import (
    FrozenToolJsonDict,
    freeze_tool_json_object,
)

MAXIMUM_MEMORY_TITLE_CHARACTERS = 128
MAXIMUM_MEMORY_STATEMENT_CHARACTERS = 8000


class MemoryFactKind(StrEnum):
    USER_PROFILE = "USER_PROFILE"
    RESPONSE_PREFERENCE = "RESPONSE_PREFERENCE"
    FACT = "FACT"


class AutomaticMemoryTriggerDisposition(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    DISABLED_BY_EXPLICIT_USER_DIRECTIVE = "DISABLED_BY_EXPLICIT_USER_DIRECTIVE"
    SKIPPED_LOW_INFORMATION = "SKIPPED_LOW_INFORMATION"


class MemoryUsePolicy(StrEnum):
    ENABLED = "ENABLED"
    WRITE_DISABLED_BY_USER = "WRITE_DISABLED_BY_USER"
    ALL_DISABLED_BY_USER = "ALL_DISABLED_BY_USER"

    @property
    def allows_reads(self):
        return self is not MemoryUsePolicy.ALL_DISABLED_BY_USER

    @property
    def allows_writes(self):
        return self is MemoryUsePolicy.ENABLED


def strongest_memory_use_policy(current, candidate):
    order = tuple(MemoryUsePolicy)
    return max((current, candidate), key=order.index)


@dataclass(frozen=True, slots=True)
class FrozenMemoryTriggerPolicy:
    automatic_recall: AutomaticMemoryTriggerDisposition
    memory_use: MemoryUsePolicy
    write_hint: bool


@dataclass(frozen=True, slots=True)
class FrozenModelCallMemoryContext:
    memory_use_policy: MemoryUsePolicy = MemoryUsePolicy.ENABLED


def normalize_memory_text(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("记忆文字必须是字符串。")
    return unicodedata.normalize(
        "NFC", value.replace("\r\n", "\n").replace("\r", "\n")
    ).strip()


def memory_title(value: str) -> str:
    value = normalize_memory_text(value)
    if not 1 <= len(value) <= MAXIMUM_MEMORY_TITLE_CHARACTERS or "\n" in value:
        raise ValueError("记忆标题须为非空单行，最多128个字符。")
    return value


def memory_statement(kind: MemoryFactKind | str, value: str = "") -> str:
    if not isinstance(value, str):
        raise TypeError("记忆正文必须是字符串。")
    if MemoryFactKind(kind) is MemoryFactKind.RESPONSE_PREFERENCE:
        return ""
    value = normalize_memory_text(value)
    if not 1 <= len(value) <= MAXIMUM_MEMORY_STATEMENT_CHARACTERS:
        raise ValueError("记忆正文须为非空文字，最多8000个字符。")
    return value


def canonical_memory_recorded_at(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("记忆时间缺少时区。")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def memory_timestamp(value: str | datetime) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("记忆时间须为带时区的时间。")
    return value.astimezone(timezone.utc)


def validate_canonical_memory_recorded_at(value: str) -> None:
    memory_timestamp(value)


def memory_retrieval_text(title: str, statement: str) -> str:
    return f"title: {title}\nstatement: {statement}"


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    fact_id: str
    memory_domain_id: str
    context_id: str
    fact_kind: str
    title: str
    statement: str
    recorded_at: str
    updated_at: str

    def __post_init__(self):
        require_id(self.fact_id, "memory")
        MemoryFactKind(self.fact_kind)
        if not is_valid_context_id(self.context_id):
            raise ValueError("记忆范围无效。")
        memory_timestamp(self.recorded_at)
        memory_timestamp(self.updated_at)

    @classmethod
    def from_row(cls, row, **extra):
        return cls(
            fact_id=str(row["id"]),
            memory_domain_id=str(row["memory_domain_id"]),
            context_id=str(row["context_id"]),
            fact_kind=str(row["fact_kind"]),
            title=row["title"],
            statement=row["statement"],
            recorded_at=canonical_memory_recorded_at(row["accepted_at"]),
            updated_at=canonical_memory_recorded_at(row["updated_at"]),
            **extra,
        )

    @property
    def retrieval_text(self):
        return memory_retrieval_text(self.title, self.statement)

    def value(self):
        return {
            "target": memory_target(self.fact_id),
            "context_kind": "GLOBAL"
            if self.context_id == CTX_GLOBAL
            else "CURRENT_PROJECT",
            "kind": self.fact_kind,
            "title": self.title,
            "statement": self.statement,
            "recorded_at": self.recorded_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class MemoryObservationState:
    preferences: tuple[FrozenToolJsonDict, ...] | None = None
    recall: tuple[FrozenToolJsonDict, ...] = ()

    def __post_init__(self):
        if self.preferences is not None:
            object.__setattr__(
                self,
                "preferences",
                tuple(freeze_tool_json_object(x) for x in self.preferences),
            )
        object.__setattr__(
            self, "recall", tuple(freeze_tool_json_object(x) for x in self.recall)
        )


@dataclass(frozen=True, slots=True)
class MemoryObservationText:
    text: str
    target: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class MemoryObservationCandidate:
    body: tuple[MemoryObservationText, ...]
    next_state: MemoryObservationState | None


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
