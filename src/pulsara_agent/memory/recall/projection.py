"""Build prompt-safe recalled-memory projections."""

from __future__ import annotations

from dataclasses import dataclass

from pulsara_agent.memory.recall.service import RecallResult


@dataclass(frozen=True, slots=True)
class ProjectionBuilder:
    max_item_chars: int = 400

    def build(self, result: RecallResult, *, token_budget: int) -> dict:
        lines = ['<recalled-memory-projection do_not_write_back="true">']
        items: list[str] = []
        for item in result.items:
            rendered = _render_item(item, max_chars=self.max_item_chars)
            items.append(rendered)
            lines.append(f"- {rendered}")
        lines.append("</recalled-memory-projection>")
        summary = _clip("\n".join(lines), max_chars=max(200, token_budget * 4))
        return {
            "summary": summary,
            "items": items,
            "included_memory_ids": [item.memory_id for item in result.items],
            "filtered_memory_ids": list(result.filtered_ids),
            "do_not_write_back": True,
        }


def _render_item(item, *, max_chars: int) -> str:
    why = ", ".join(item.why) if item.why else "recall_match"
    rendered = (
        f"[{item.memory_id}] {item.snippet} "
        f"(type={item.memory_type}; scope={item.scope}; status={item.status.value}; "
        f"why={why}; deep_recall=\"{item.deep_recall}\")"
    )
    return _clip(rendered, max_chars=max_chars)


def _clip(text: str, *, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 3)] + "..."
