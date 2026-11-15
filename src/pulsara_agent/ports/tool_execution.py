"""Small process-local tool execution boundary for the canonical Kernel."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
import json
from typing import TYPE_CHECKING, Protocol, Callable

from pulsara_agent.llm.input import FrozenPromptContent, LLMImagePart, LLMTextPart
from pulsara_agent.message import ToolResultState
from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    freeze_json,
    thaw_json,
)
from pulsara_agent.primitives.tool_observation import TrustedToolObservationSupplement

if TYPE_CHECKING:
    from pulsara_agent.conversation_kernel.content_parts import ResultPart
    from pulsara_agent.ports.tool_output import ToolOutputBodyCandidate
    from pulsara_agent.primitives.tool_result_budget import ToolResultPageBudget


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


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: FrozenToolJsonDict = field(default_factory=FrozenToolJsonDict)
    defer_file_observation: bool = False
    page_budget: ToolResultPageBudget | None = field(default=None, repr=False, compare=False)
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
    output: str | FrozenPromptContent
    metadata: FrozenToolJsonDict = field(default_factory=FrozenToolJsonDict)
    output_body_candidate: ToolOutputBodyCandidate | None = None
    native_output: str | None = None
    page_complete: bool = False
    required_facts: FrozenToolJsonDict = field(default_factory=FrozenToolJsonDict)
    trusted_observation: TrustedToolObservationSupplement | None = None
    model_visible_memory_fact_ids: tuple[str, ...] = ()
    public_format: ToolOutputSourceFormatHint = ToolOutputSourceFormatHint.TEXT
    delivery_succeeded: Callable[[], None] | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.public_format, ToolOutputSourceFormatHint):
            raise TypeError("public tool result format must be explicit TEXT or JSON")
        object.__setattr__(self, "metadata", freeze_tool_json_object(self.metadata))
        object.__setattr__(self, "required_facts", freeze_tool_json_object(self.required_facts))
        if self.native_output is not None:
            self.native_output.encode("utf-8")
        if type(self.page_complete) is not bool:
            raise TypeError("page_complete must be a boolean")
        # Reject surrogate-bearing or otherwise non-UTF-8-encodable public
        # results at the process-local boundary. Body publication must
        # never replace raw truth with an implicit errors="replace" value.
        if isinstance(self.output, str):
            self.output.encode("utf-8")
        elif isinstance(self.output, FrozenPromptContent):
            images = tuple(
                part for part in self.output.parts if isinstance(part, LLMImagePart)
            )
            if (
                self.tool_name not in {"view_image", "read_video_frames"}
                or self.status is not ToolResultState.SUCCESS
                or not images
                or (self.tool_name == "view_image" and len(images) != 1)
                or not any(isinstance(part, LLMTextPart) for part in self.output.parts)
            ):
                raise ValueError("typed image output is invalid for this Tool result")
        else:
            raise TypeError("tool output must be text or frozen prompt content")
        if (
            len(self.model_visible_memory_fact_ids) > 50
            or len(set(self.model_visible_memory_fact_ids))
            != len(self.model_visible_memory_fact_ids)
        ):
            raise ValueError("tool result memory provenance header is invalid")


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
