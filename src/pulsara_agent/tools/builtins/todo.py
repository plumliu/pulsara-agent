"""Runtime todo built-in tool."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pulsara_agent.message import ToolResultState
from pulsara_agent.tools.base import ToolCall, ToolExecutionResult
from pulsara_agent.tools.builtins.schemas import (
    json_text,
    object_schema,
    required_str_arg,
    str_arg,
)


TODO_STATUSES = {"pending", "in_progress", "completed"}


@dataclass(slots=True)
class TodoItem:
    id: str
    text: str
    status: str = "pending"


@dataclass(slots=True)
class TodoTool:
    name: str = "todo"
    description: str = "Track the current runtime task plan."
    parameters: dict[str, Any] = field(default_factory=lambda: object_schema(
        properties={
            "action": {
                "type": "string",
                "enum": ["add", "update", "list", "clear"],
                "description": "Todo operation.",
            },
            "text": {"type": "string", "description": "Todo text for add/update."},
            "id": {"type": "string", "description": "Todo id for update."},
            "status": {
                "type": "string",
                "enum": ["pending", "in_progress", "completed"],
                "description": "Todo status for update/add.",
            },
        },
        required=["action"],
    ))
    is_read_only: bool = False
    is_concurrency_safe: bool = False
    _items: list[TodoItem] = field(default_factory=list)
    _next_id: int = 1

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        action = required_str_arg(call.arguments, "action")
        if action == "add":
            text = required_str_arg(call.arguments, "text")
            status = str_arg(call.arguments, "status") or "pending"
            _validate_todo_status(status)
            item = TodoItem(id=f"todo:{self._next_id}", text=text, status=status)
            self._next_id += 1
            self._items.append(item)
            output = json_text({"items": [_todo_item_to_dict(item) for item in self._items]})
        elif action == "update":
            item_id = required_str_arg(call.arguments, "id")
            item = self._find_item(item_id)
            text = str_arg(call.arguments, "text")
            status = str_arg(call.arguments, "status")
            if text is not None:
                item.text = text
            if status is not None:
                _validate_todo_status(status)
                item.status = status
            output = json_text({"items": [_todo_item_to_dict(item) for item in self._items]})
        elif action == "list":
            output = json_text({"items": [_todo_item_to_dict(item) for item in self._items]})
        elif action == "clear":
            self._items.clear()
            output = json_text({"items": []})
        else:
            raise ValueError(f"unsupported todo action: {action}")
        return ToolExecutionResult(
            call_id=call.id,
            tool_name=call.name,
            status=ToolResultState.SUCCESS,
            output=output,
        )

    def _find_item(self, item_id: str) -> TodoItem:
        for item in self._items:
            if item.id == item_id:
                return item
        raise KeyError(f"unknown todo id: {item_id}")


def _todo_item_to_dict(item: TodoItem) -> dict[str, str]:
    return {"id": item.id, "text": item.text, "status": item.status}


def _validate_todo_status(status: str) -> None:
    if status not in TODO_STATUSES:
        allowed = ", ".join(sorted(TODO_STATUSES))
        raise ValueError(f"unsupported todo status: {status} (allowed: {allowed})")
