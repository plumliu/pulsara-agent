"""User-owned memory management values and deletion graph algebra.

These values are local to one operation. Canonical rows, not a plan registry,
remain the authority; the executor replans before comparing user confirmation.
"""

from __future__ import annotations

import base64
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
import json
from typing import Iterable, Mapping

from pulsara_agent.memory.scope import CTX_GLOBAL, is_valid_context_id
from .contracts import (
    MAXIMUM_MEMORY_STATEMENT_BYTES,
    MemoryFactKind,
    canonical_memory_recorded_at,
    canonical_json_bytes,
    validate_canonical_memory_recorded_at,
)


MAXIMUM_MEMORY_MANAGEMENT_SEARCH_BYTES = 1024


class MemoryRelativeRole(StrEnum):
    BASED_ON = "BASED_ON"
    BASIS_FOR = "BASIS_FOR"
    UPDATES = "UPDATES"
    UPDATED_BY = "UPDATED_BY"
    CONFLICTS_WITH = "CONFLICTS_WITH"


class MemoryDeletionDisposition(StrEnum):
    READY = "READY"
    NEEDS_RESOLUTION = "NEEDS_RESOLUTION"


class MemoryRestorationReason(StrEnum):
    ACTIVE_SEMANTIC_COLLISION = "ACTIVE_SEMANTIC_COLLISION"
    RESTORATION_SEMANTIC_COLLISION = "RESTORATION_SEMANTIC_COLLISION"
    RESPONSE_PREFERENCE_CAPACITY = "RESPONSE_PREFERENCE_CAPACITY"
    SURVIVING_SUPERSEDE_ANCESTRY = "SURVIVING_SUPERSEDE_ANCESTRY"


class MemoryManagementError(RuntimeError):
    def __init__(self, code: str, status: int, message: str, *, preview=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.preview = preview


@dataclass(frozen=True, slots=True)
class MemoryManagementSelection:
    view: str = "global"
    workspace_id: str | None = None

    def __post_init__(self):
        if self.view not in {"global", "project"} or (
            (self.view == "global") != (self.workspace_id is None)
        ):
            raise ValueError("请选择跨对话或一个项目")
        if self.workspace_id is not None and (
            self.workspace_id == CTX_GLOBAL
            or not is_valid_context_id(self.workspace_id)
        ):
            raise ValueError("项目标识无效")


@dataclass(frozen=True, slots=True)
class MemoryManagementFact:
    fact_id: str
    context_id: str
    kind: str
    lifecycle: str
    statement: str
    recorded_at: str
    updated_at: str

    def __post_init__(self):
        if not isinstance(self.fact_id, str) or not self.fact_id:
            raise ValueError("记忆标识无效")
        MemoryFactKind(self.kind)
        if not is_valid_context_id(self.context_id) or self.lifecycle not in {
            "ACTIVE",
            "SUPERSEDED",
        }:
            raise ValueError("记忆位置或状态无效")
        if (
            not 1
            <= len(self.statement.encode("utf-8"))
            <= MAXIMUM_MEMORY_STATEMENT_BYTES
        ):
            raise ValueError("记忆正文超出单条记录边界")
        validate_canonical_memory_recorded_at(self.recorded_at)
        instant = datetime.fromisoformat(self.updated_at)
        if instant.tzinfo is None:
            raise ValueError("记忆更新时间缺少时区")

    @classmethod
    def from_row(cls, row):
        return cls(
            fact_id=row["id"],
            context_id=row["context_id"],
            kind=row["fact_kind"],
            lifecycle=row["lifecycle"],
            statement=row["statement"],
            recorded_at=canonical_memory_recorded_at(row["accepted_at"]),
            updated_at=row["updated_at"].isoformat(),
        )


@dataclass(frozen=True, slots=True)
class MemoryManagementRelationProjection:
    relation_id: str
    subject: MemoryManagementFact
    companion: MemoryManagementFact
    relative_role: MemoryRelativeRole
    recorded_at: str
    public_summary: str | None


def relative_role(kind: str, *, selected_is_source: bool) -> MemoryRelativeRole:
    if kind == "CONTRADICTS":
        return MemoryRelativeRole.CONFLICTS_WITH
    if kind == "BASED_ON":
        return (
            MemoryRelativeRole.BASED_ON
            if selected_is_source
            else MemoryRelativeRole.BASIS_FOR
        )
    if kind == "SUPERSEDES":
        return (
            MemoryRelativeRole.UPDATES
            if selected_is_source
            else MemoryRelativeRole.UPDATED_BY
        )
    raise ValueError("未知记忆关系")


@dataclass(frozen=True, slots=True)
class MemoryDeletionGraph:
    delete_ids: frozenset[str]
    relation_ids: frozenset[str]
    restore_ids: frozenset[str]
    blocked_ancestry: tuple[tuple[str, str], ...]


def deletion_graph(
    seeds: Iterable[str], relations: Iterable[Mapping]
) -> MemoryDeletionGraph:
    """Only incoming BASED_ON edges propagate deletion; cycles terminate by set membership."""
    edges = tuple(relations)
    incoming_basis: dict[str, set[str]] = {}
    incoming_supersedes: dict[str, set[str]] = {}
    for edge in edges:
        source, target = edge["source_fact_id"], edge["target_fact_id"]
        if edge["relation_kind"] == "BASED_ON":
            incoming_basis.setdefault(target, set()).add(source)
        elif edge["relation_kind"] == "SUPERSEDES":
            incoming_supersedes.setdefault(target, set()).add(source)
    deleted = set(seeds)
    pending = list(deleted)
    while pending:
        for dependent in incoming_basis.get(pending.pop(), ()):
            if dependent not in deleted:
                deleted.add(dependent)
                pending.append(dependent)
    removed = {
        e["id"]
        for e in edges
        if e["source_fact_id"] in deleted or e["target_fact_id"] in deleted
    }
    restoration = {
        target
        for target, sources in incoming_supersedes.items()
        if target not in deleted and sources and sources <= deleted
    }
    ancestry = set()
    for target in restoration:
        visited = {target}
        pending = list(incoming_supersedes[target])
        while pending:
            node = pending.pop()
            if node in visited:
                continue
            visited.add(node)
            if node not in deleted:
                ancestry.add((target, node))
            pending.extend(incoming_supersedes.get(node, ()))
    return MemoryDeletionGraph(
        frozenset(deleted),
        frozenset(removed),
        frozenset(restoration),
        tuple(sorted(ancestry)),
    )


def normalized_search(value: str | None) -> str:
    value = (value or "").strip()
    if len(value.encode("utf-8")) > MAXIMUM_MEMORY_MANAGEMENT_SEARCH_BYTES:
        raise ValueError("搜索内容最多 1024 UTF-8 字节")
    return value


def page_size(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 100:
        raise ValueError("每页条数必须在 1 到 100 之间")
    return value


def encode_cursor(filters: Mapping, key: tuple) -> str:
    return (
        base64.urlsafe_b64encode(canonical_json_bytes({"filters": filters, "key": key}))
        .decode()
        .rstrip("=")
    )


def decode_cursor(value: str | None, filters: Mapping, arity: int) -> tuple | None:
    if value is None:
        return None
    try:
        payload = json.loads(
            base64.b64decode(
                value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
            )
        )
        if set(payload) != {"filters", "key"} or payload["filters"] != filters:
            raise ValueError
        key = payload["key"]
        if (
            not isinstance(key, list)
            or len(key) != arity
            or not all(isinstance(v, str) for v in key)
        ):
            raise ValueError
        return tuple(key)
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError("分页位置与当前筛选不一致") from exc


def confirmation_records(
    *,
    selection: MemoryManagementSelection,
    root: str,
    additional: tuple[str, ...],
    deletes: Iterable[MemoryManagementFact],
    effects: Iterable[dict],
    restores: Iterable[MemoryManagementFact],
    conflicts: Iterable[dict],
):
    conflicts = tuple(conflicts)
    yield {
        "type": "HEADER",
        **asdict(selection),
        "root": root,
        "disposition": "NEEDS_RESOLUTION" if conflicts else "READY",
    }
    for fact_id in additional:
        yield {"type": "ADDITIONAL_ROOT", "fact_id": fact_id}
    for fact in deletes:
        yield {"type": "FACT_DELETE", "fact": asdict(fact)}
    yield from effects
    for fact in restores:
        yield {
            "type": "FACT_RESTORE",
            "fact": asdict(fact),
            "planned_lifecycle": "ACTIVE",
        }
    yield from conflicts


def with_end(records: Iterable[dict]):
    counts: dict[str, int] = {}
    for record in records:
        counts[record["type"]] = counts.get(record["type"], 0) + 1
        yield record
    yield {"type": "END", "counts": counts}


def validate_product_record(record: object) -> dict:
    """Closed typed record decoder shared by upload validation and tests."""
    if not isinstance(record, dict):
        raise ValueError("删除确认记录不是对象")
    kind = record.get("type")
    if kind == "HEADER":
        allowed = {"type", "view", "workspace_id", "root"}
        if "disposition" in record:
            MemoryDeletionDisposition(record["disposition"])
            allowed.add("disposition")
        if "result" in record:
            if record["result"] != "DELETED" or "disposition" in record:
                raise ValueError("删除结果无效")
            allowed.add("result")
        MemoryManagementSelection(record["view"], record["workspace_id"])
        if not isinstance(record["root"], str) or not record["root"]:
            raise ValueError("删除根记录无效")
    elif kind == "ADDITIONAL_ROOT":
        allowed = {"type", "fact_id"}
        if not isinstance(record["fact_id"], str) or not record["fact_id"]:
            raise ValueError("一并删除记录无效")
    elif kind in {"FACT_DELETE", "FACT_RESTORE"}:
        allowed = {"type", "fact"}
        MemoryManagementFact(**record["fact"])
        if kind == "FACT_RESTORE":
            allowed.add("planned_lifecycle")
            if record["planned_lifecycle"] != "ACTIVE":
                raise ValueError("恢复状态无效")
    elif kind == "RELATION_EFFECT":
        allowed = {
            "type",
            "relation_id",
            "subject",
            "companion",
            "relative_role",
            "recorded_at",
            "public_summary",
            "effect",
        }
        MemoryManagementFact(**record["subject"])
        MemoryManagementFact(**record["companion"])
        MemoryRelativeRole(record["relative_role"])
        validate_canonical_memory_recorded_at(record["recorded_at"])
        if record["effect"] not in {"REMOVED", "BECOMES_ACTIVE_CONFLICT"}:
            raise ValueError("关系变化无效")
        if (
            record["public_summary"] is not None
            and len(record["public_summary"].encode()) > 2048
        ):
            raise ValueError("整理摘要超出边界")
    elif kind == "RESTORATION_CONFLICT":
        allowed = {"type", "subject", "companion", "reason", "group"}
        MemoryManagementFact(**record["subject"])
        if record["companion"] is not None:
            MemoryManagementFact(**record["companion"])
        MemoryRestorationReason(record["reason"])
        if not isinstance(record["group"], str):
            raise ValueError("恢复组无效")
    elif kind == "END":
        allowed = {"type", "counts"}
        if not isinstance(record["counts"], dict) or not all(
            type(v) is int and v > 0 for v in record["counts"].values()
        ):
            raise ValueError("删除确认计数无效")
    else:
        raise ValueError("未知删除确认记录")
    if set(record) != allowed:
        raise ValueError("删除确认记录字段不匹配")
    return record
