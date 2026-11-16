"""Resolve Hook declaration shapes without turning a bad item into package failure.

Package owners supply file access and diagnostics. This only combines the chosen
manifest's declarations; the existing Hook parser still owns handler semantics.
"""

from __future__ import annotations

from collections.abc import Callable


def merge_hook_sources(
    value: object,
    *,
    read_json: Callable[[str], object],
    report: Callable[[str | None, str], None],
) -> dict[str, object]:
    hooks: dict[str, list[object]] = {}
    result: dict[str, object] = {"hooks": hooks}
    declarations = value if isinstance(value, list) else [value]
    for ordinal, declaration in enumerate(declarations):
        path = declaration if isinstance(declaration, str) else None
        label = path or f"hooks[{ordinal}]"
        if isinstance(declaration, str):
            try:
                declaration = read_json(declaration)
            except (
                ValueError,
                UnicodeError,
                FileNotFoundError,
                PermissionError,
            ) as exc:
                report(path, f"Hook {label} 未导入：{exc}")
                continue
        if not isinstance(declaration, dict):
            report(path, f"Hook {label} 未导入：需要文件路径或包含 hooks 的对象。")
            continue
        for field in declaration.keys() - {"hooks", "description"}:
            report(path, f"Hook {label} 的扩展 {field} 不受支持，未启用。")
        description = declaration.get("description")
        if isinstance(description, str) and "description" not in result:
            result["description"] = description
        events = declaration.get("hooks", {})
        if not isinstance(events, dict):
            report(path, f"Hook {label} 未导入：hooks 必须是对象。")
            continue
        for event, groups in events.items():
            if not isinstance(groups, list):
                report(path, f"Hook {label} 的 {event} 未导入：需要处理器组数组。")
                continue
            hooks.setdefault(event, []).extend(groups)
    return result


__all__ = ["merge_hook_sources"]
