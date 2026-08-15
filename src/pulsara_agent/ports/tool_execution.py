"""Small process-local tool execution boundary for the canonical Kernel."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal, Protocol

from pulsara_agent.message import ToolResultState
from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    freeze_json,
    thaw_json,
)
from pulsara_agent.primitives.tool_observation import TrustedToolObservationSupplement


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


class ToolOutputSourceCoverage(StrEnum):
    """What portion of the physical tool observation the candidate owns."""

    COMPLETE = "COMPLETE"
    RETAINED_SNAPSHOT = "RETAINED_SNAPSHOT"


class ToolOutputSourceCoverageReason(StrEnum):
    """Closed reason why a candidate is not the complete source observation."""

    TERMINAL_RETENTION_GAP = "TERMINAL_RETENTION_GAP"
    TERMINAL_SANITIZER_UNAVAILABLE = "TERMINAL_SANITIZER_UNAVAILABLE"


class ToolOutputSourceFormatHint(StrEnum):
    """Process-local hint used only to render the model-facing preview."""

    TEXT = "TEXT"
    JSON = "JSON"


@dataclass(frozen=True, slots=True)
class ToolOutputArtifactCandidate:
    """One process-local, sanitized primary tool-output body.

    This carrier is deliberately not serializable or durable.  The text is the
    exact body eligible for immutable blob publication, before any lossy
    model-facing preview is built.
    """

    role: Literal["OUTPUT"]
    text: str
    source_coverage: ToolOutputSourceCoverage
    source_coverage_reason: ToolOutputSourceCoverageReason | None = None
    original_utf8_bytes: int | None = None
    source_format_hint: ToolOutputSourceFormatHint = ToolOutputSourceFormatHint.TEXT

    def __post_init__(self) -> None:
        if self.role != "OUTPUT":
            raise ValueError("Round 1 supports only the primary OUTPUT artifact")
        encoded = self.text.encode("utf-8")
        if self.source_coverage is ToolOutputSourceCoverage.COMPLETE:
            if self.source_coverage_reason is not None:
                raise ValueError("complete tool output cannot carry a coverage reason")
        elif self.source_coverage is ToolOutputSourceCoverage.RETAINED_SNAPSHOT and (
            self.source_coverage_reason
            not in {
                ToolOutputSourceCoverageReason.TERMINAL_RETENTION_GAP,
                ToolOutputSourceCoverageReason.TERMINAL_SANITIZER_UNAVAILABLE,
            }
        ):
            raise ValueError("retained tool output requires its closed coverage reason")
        if self.original_utf8_bytes is not None:
            if self.original_utf8_bytes < len(encoded):
                raise ValueError(
                    "original tool output size cannot be smaller than candidate"
                )
            if (
                self.source_coverage is ToolOutputSourceCoverage.COMPLETE
                and self.original_utf8_bytes != len(encoded)
            ):
                raise ValueError("complete tool output size must match its exact bytes")


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    call_id: str
    tool_name: str
    status: ToolResultState
    output: str
    metadata: FrozenToolJsonDict = field(default_factory=FrozenToolJsonDict)
    output_artifact_candidate: ToolOutputArtifactCandidate | None = None
    artifact_source_read: bool = False
    trusted_observation: TrustedToolObservationSupplement | None = None
    model_visible_memory_fact_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", freeze_tool_json_object(self.metadata))
        # Reject surrogate-bearing or otherwise non-UTF-8-encodable public
        # results at the process-local boundary.  Artifact publication must
        # never replace raw truth with an implicit errors="replace" value.
        self.output.encode("utf-8")
        if self.artifact_source_read and self.output_artifact_candidate is not None:
            raise ValueError("artifact_read results cannot recursively own artifacts")
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
    SUBAGENT_CHILD = "subagent_child"


__all__ = [
    "AsyncTool",
    "FrozenToolJsonDict",
    "Tool",
    "ToolCall",
    "ToolExecutionResult",
    "ToolInvocationOwnerKind",
    "ToolOutputArtifactCandidate",
    "ToolOutputSourceCoverage",
    "ToolOutputSourceCoverageReason",
    "ToolOutputSourceFormatHint",
    "freeze_tool_json_object",
    "thaw_tool_json_object",
]
