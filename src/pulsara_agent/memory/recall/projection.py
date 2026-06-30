"""Build prompt-safe recalled-memory projections."""

from __future__ import annotations

from dataclasses import dataclass

from pulsara_agent.memory.recall.service import RecallItem, RecallResult


_OPENING = '<recalled-memory-projection do_not_write_back="true">'
_CLOSING = "</recalled-memory-projection>"


@dataclass(frozen=True, slots=True)
class ProjectionBuilder:
    max_item_chars: int = 400

    def build(self, result: RecallResult, *, token_budget: int) -> dict:
        max_chars = max(200, token_budget * 4)
        by_id = {item.memory_id: item for item in result.items}
        all_conflict_groups = _conflict_groups(result)
        conflict_ids = {
            memory_id
            for group in all_conflict_groups
            for memory_id in group["memory_ids"]
        }

        units: list[str] = []
        included_ids: set[str] = set()
        included_conflicts: list[dict[str, list[str] | str]] = []
        remaining = max_chars - len(_OPENING) - len(_CLOSING) - 1

        # A contradiction pair is one indivisible projection unit. It is either
        # written in full (both ids + relation, with bounded snippets when space
        # permits) or omitted in full; arbitrary character clipping can never
        # leave the model with only one side of a known conflict.
        for group in all_conflict_groups:
            left_id, right_id = group["memory_ids"]
            unit = _render_conflict_unit(
                by_id[left_id],
                by_id[right_id],
                max_chars=max(0, remaining - 3),
            )
            units.append(unit)
            included_ids.update((left_id, right_id))
            included_conflicts.append(group)
            remaining -= len(unit) + 3

        # Ordinary memories are packed only after safety units. Each line is
        # rendered to the available space before insertion, so the final
        # projection always contains complete units and a closing fence.
        for item in result.items:
            if item.memory_id in conflict_ids:
                continue
            unit = _render_item_unit(
                item,
                max_chars=min(self.max_item_chars, max(0, remaining - 3)),
            )
            if unit is None:
                continue
            units.append(unit)
            included_ids.add(item.memory_id)
            remaining -= len(unit) + 3

        lines = [_OPENING, *(f"- {unit}" for unit in units), _CLOSING]
        summary = "\n".join(lines)
        included = [item.memory_id for item in result.items if item.memory_id in included_ids]
        return {
            "summary": summary,
            # These are the exact complete text units present in summary, not
            # reconstructed full item renderings that the model did not see.
            "items": list(units),
            "included_memory_ids": included,
            "filtered_memory_ids": list(result.filtered_ids),
            "conflict_groups": included_conflicts,
            "do_not_write_back": True,
        }


def _render_conflict_unit(
    left: RecallItem,
    right: RecallItem,
    *,
    max_chars: int,
) -> str:
    base = f"Conflicting recalled memories: [{left.memory_id}] <-> [{right.memory_id}]"
    detail_prefix = " | left="
    detail_middle = "; right="
    detail_overhead = len(detail_prefix) + len(detail_middle)
    # Conflict visibility outranks the soft projection budget. Always retain a
    # short summary of both sides, even when the ids alone consume the nominal
    # budget; ordinary memories are then omitted. This matches the no-cap
    # contradiction safety contract instead of silently recreating half-conflict.
    minimum_snippet_chars = 12
    available = max(
        max_chars - len(base) - detail_overhead,
        minimum_snippet_chars * 2,
    )
    left_budget = available // 2
    right_budget = available - left_budget
    left_snippet = _clip_inline(left.snippet, max_chars=left_budget)
    right_snippet = _clip_inline(right.snippet, max_chars=right_budget)
    return f"{base}{detail_prefix}{left_snippet}{detail_middle}{right_snippet}"


def _render_item_unit(item: RecallItem, *, max_chars: int) -> str | None:
    prefix = f"[{item.memory_id}] "
    if len(prefix) + 1 > max_chars:
        return None
    why = ", ".join(item.why) if item.why else "recall_match"
    full = (
        f"{prefix}{item.snippet} "
        f"(type={item.memory_type}; scope={item.scope}; status={item.status.value}; "
        f"why={why}; conflicts_with={list(item.conflicts_with)!r}; "
        f'deep_recall="{item.deep_recall}")'
    )
    if len(full) <= max_chars:
        return full
    snippet = _clip_inline(item.snippet, max_chars=max_chars - len(prefix))
    return f"{prefix}{snippet}"


def _conflict_groups(result: RecallResult) -> list[dict[str, list[str] | str]]:
    groups: list[dict[str, list[str] | str]] = []
    seen: set[tuple[str, ...]] = set()
    included = {item.memory_id for item in result.items}
    for item in result.items:
        for peer_id in item.conflicts_with:
            if peer_id not in included:
                continue
            ids = tuple(sorted((item.memory_id, peer_id)))
            if ids in seen:
                continue
            seen.add(ids)
            groups.append({"kind": "contradiction", "memory_ids": list(ids)})
    return groups


def _clip_inline(text: str, *, max_chars: int) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= max_chars:
        return normalized
    if max_chars <= 3:
        return "." * max_chars
    return normalized[: max_chars - 3] + "..."
