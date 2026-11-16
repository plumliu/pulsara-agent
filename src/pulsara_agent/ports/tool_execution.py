"""Small process-local tool execution boundary for the canonical Kernel."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
import json
from typing import TYPE_CHECKING, Protocol, Callable

from pulsara_agent.message import ToolResultState
from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    freeze_json,
    thaw_json,
)
from pulsara_agent.primitives.tool_observation import TrustedToolObservationSupplement

if TYPE_CHECKING:
    from pulsara_agent.conversation_kernel.content_parts import ResultPart
    from pulsara_agent.ports.tool_output import NativeToolContinuation


def encode_tool_public_json(value: object, *, indent: int | None = None, sort_keys: bool = False) -> bytes:
    """Preserve ECMAScript string code units in a UTF-8 JSON carrier."""
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":") if indent is None else None,
                      indent=indent, sort_keys=sort_keys, allow_nan=False)
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:
        return json.dumps(value, ensure_ascii=True, separators=(",", ":") if indent is None else None,
                          indent=indent, sort_keys=sort_keys, allow_nan=False).encode("ascii")


def tool_json_values_equal(left: object, right: object) -> bool:
    """Compare JSON kinds, exact JSON numbers and UTF-16 strings.

    JSON decoding normalizes paired code units in strings and keys. Decimal
    decoding retains the JSON number's value across jsonb's numeric spelling
    changes; Python float equality would lose that property for e.g. 1e30.
    """
    def decoded(value: object) -> object:
        return json.loads(json.dumps(value, ensure_ascii=True, allow_nan=False),
                          parse_int=Decimal, parse_float=Decimal)

    def same(a: object, b: object) -> bool:
        if type(a) is not type(b):
            return False
        if isinstance(a, dict):
            return a.keys() == b.keys() and all(same(value, b[key]) for key, value in a.items())
        if isinstance(a, list):
            return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
        return a == b

    return same(decoded(left), decoded(right))


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


def tool_business_value_schema(
    *, fields: Mapping[str, object] | None = None,
    required_fields: Sequence[str] = (),
    meta: Mapping[str, object] | None = None,
    required_meta: Sequence[str] = (),
    content_schema: Mapping[str, object] | None = None,
    statuses: Sequence[str] = ("SUCCESS",),
) -> dict[str, object]:
    """Describe one declared business-value branch for tools and SDK reference.

    Domain owners supply actual public fields, business metadata and ordered
    content. This is schema construction, not an execution or acceptance gate.
    """
    text_part = {"type": "object", "properties": {
        "type": {"const": "text"}, "value": {"type": "string"}},
        "required": ["type", "value"], "additionalProperties": False}
    return {"type": "object", "properties": {
        **dict(fields or {}),
        "meta": {"type": "object", "properties": {
            "status": {"enum": list(statuses)}, **dict(meta or {})},
            "required": ["status", *required_meta], "additionalProperties": False},
        "content": dict(content_schema) if content_schema is not None else {
            "type": "array", "items": text_part},
    }, "required": [*required_fields, "meta", "content"],
        "additionalProperties": False}


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: FrozenToolJsonDict = field(default_factory=FrozenToolJsonDict)
    defer_file_observation: bool = False
    cancel_requested: Callable[[], bool] = field(default=lambda: False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.id or not self.name:
            raise ValueError("tool call identity and name are required")
        object.__setattr__(self, "arguments", freeze_tool_json_object(self.arguments))


class ToolOutputSourceCoverage(StrEnum):
    """What portion of the physical tool observation the candidate owns."""

    COMPLETE = "COMPLETE"
    RETAINED_SNAPSHOT = "RETAINED_SNAPSHOT"


class ToolOutputSourceCoverageReason(StrEnum):
    """Closed reason why a candidate is not the complete source observation."""

    TERMINAL_RETENTION_GAP = "TERMINAL_RETENTION_GAP"
    TERMINAL_SANITIZER_UNAVAILABLE = "TERMINAL_SANITIZER_UNAVAILABLE"


class ToolOutputSourceFormatHint(StrEnum):
    """Closed encoding tag selected explicitly by the result owner."""

    TEXT = "TEXT"
    JSON = "JSON"


@dataclass(frozen=True, slots=True)
class ToolBusinessValue:
    """A producer-declared value, separated from authoritative execution facts.

    Producers own their field schemas. This carrier does not parse serialized
    output or infer fields from a display projection.
    """

    public_fields: FrozenToolJsonDict = field(default_factory=FrozenToolJsonDict)
    content: tuple[ResultPart, ...] = ()

    def __post_init__(self) -> None:
        from pulsara_agent.conversation_kernel.content_parts import ResultPart

        fields = freeze_tool_json_object(self.public_fields)
        if "content" in fields:
            raise ValueError("public_fields cannot duplicate ordered content")
        if "observed_at_utc" in fields or "observation_duration_microseconds" in fields:
            raise ValueError("observation facts belong to the execution owner")
        meta = fields.get("meta", {})
        if not isinstance(meta, Mapping):
            raise TypeError("public meta must be an object")
        if "status" in meta:
            raise ValueError("result status belongs to the execution owner")
        parts = tuple(self.content)
        if any(not isinstance(part, ResultPart) for part in parts):
            raise TypeError("business content requires explicit ResultPart values")
        object.__setattr__(self, "public_fields", fields)
        object.__setattr__(self, "content", parts)


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    call_id: str
    tool_name: str
    status: ToolResultState
    business_value: ToolBusinessValue
    native_continuation: NativeToolContinuation | None = None
    result_facts: FrozenToolJsonDict = field(default_factory=FrozenToolJsonDict)
    acceptance_succeeded: Callable[[], None] | None = field(default=None, repr=False, compare=False)
    metadata: FrozenToolJsonDict = field(default_factory=FrozenToolJsonDict)
    trusted_observation: TrustedToolObservationSupplement | None = None
    delivery_succeeded: Callable[[], None] | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.business_value, ToolBusinessValue):
            raise TypeError("tool result requires its complete declared business value")
        object.__setattr__(self, "result_facts", freeze_tool_json_object(self.result_facts))
        object.__setattr__(self, "metadata", freeze_tool_json_object(self.metadata))


class Tool(Protocol):
    name: str

    def execute(self, call: ToolCall) -> ToolExecutionResult: ...


class AsyncTool(Protocol):
    name: str

    async def execute_async(self, call: ToolCall) -> ToolExecutionResult: ...


class ToolInvocationOwnerKind(StrEnum):
    HOST_MAIN_RUN = "host_main_run"
    SUBAGENT = "subagent"


__all__ = [
    "AsyncTool",
    "FrozenToolJsonDict",
    "Tool",
    "ToolCall",
    "ToolExecutionResult",
    "tool_business_value_schema",
    "ToolBusinessValue",
    "ToolInvocationOwnerKind",
    "ToolOutputSourceCoverage",
    "ToolOutputSourceCoverageReason",
    "ToolOutputSourceFormatHint",
    "freeze_tool_json_object",
    "encode_tool_public_json",
    "tool_json_values_equal",
    "thaw_tool_json_object",
]
