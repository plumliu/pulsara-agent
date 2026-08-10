"""Small process-local tool execution boundary for the canonical Kernel."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from pulsara_agent.message import ToolResultState
from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    freeze_json,
    thaw_json,
)


class FrozenToolJsonDict(dict[str, object]):
    """JSON-compatible recursively immutable mapping."""

    @staticmethod
    def _immutable(*_args: object, **_kwargs: object) -> None:
        raise TypeError("tool JSON carrier is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable

    def __copy__(self) -> "FrozenToolJsonDict":
        return self

    def __deepcopy__(self, _memo: dict[int, object]) -> "FrozenToolJsonDict":
        return self


def freeze_tool_json_object(value: Mapping[str, object]) -> FrozenToolJsonDict:
    normalized = freeze_json(value)
    if not isinstance(normalized, FrozenJsonObjectFact):
        raise TypeError("tool JSON carrier must be an object")

    def freeze(item: object) -> object:
        if isinstance(item, Mapping):
            result = FrozenToolJsonDict()
            for key, nested in item.items():
                dict.__setitem__(result, str(key), freeze(nested))
            return result
        if isinstance(item, (list, tuple)):
            return tuple(freeze(nested) for nested in item)
        return item

    return freeze(thaw_json(normalized))  # type: ignore[return-value]


def thaw_tool_json_object(value: Mapping[str, object]) -> dict[str, object]:
    def thaw(item: object) -> object:
        if isinstance(item, Mapping):
            return {str(key): thaw(nested) for key, nested in item.items()}
        if isinstance(item, tuple):
            return [thaw(nested) for nested in item]
        return item

    return {str(key): thaw(item) for key, item in value.items()}


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: FrozenToolJsonDict = field(default_factory=FrozenToolJsonDict)

    def __post_init__(self) -> None:
        if not self.id or not self.name:
            raise ValueError("tool call identity and name are required")
        object.__setattr__(self, "arguments", freeze_tool_json_object(self.arguments))


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    call_id: str
    tool_name: str
    status: ToolResultState
    output: str
    metadata: FrozenToolJsonDict = field(default_factory=FrozenToolJsonDict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", freeze_tool_json_object(self.metadata))


class Tool(Protocol):
    name: str

    def execute(self, call: ToolCall) -> ToolExecutionResult: ...


class AsyncTool(Protocol):
    name: str

    async def execute_async(self, call: ToolCall) -> ToolExecutionResult: ...


class ToolInvocationOwnerKind(StrEnum):
    HOST_MAIN_RUN = "host_main_run"
    SUBAGENT_CHILD = "subagent_child"


__all__ = [
    "AsyncTool",
    "FrozenToolJsonDict",
    "Tool",
    "ToolCall",
    "ToolExecutionResult",
    "ToolInvocationOwnerKind",
    "freeze_tool_json_object",
    "thaw_tool_json_object",
]
