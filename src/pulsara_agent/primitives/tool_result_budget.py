"""One call-local quote for complete text pages and native result displays.

The provider owner supplies the optional wire/token predicate.  A standalone
or program-internal invocation uses the same logical page boundary without
pretending that a provider window has been reserved for it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Callable, Mapping

from pulsara_agent.ports.tool_output import tool_output_read_call
from pulsara_agent.ports.tool_execution import freeze_tool_json_object
from pulsara_agent.primitives.context import canonical_json_bytes
from pulsara_agent.primitives.tool_observation import (
    MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES,
)
from pulsara_agent.primitives.tool_result_projection import (
    conservative_tool_result_page_logical_utf8_bytes,
)


class ToolResultPageBoundary(ValueError):
    """The smallest complete result unit cannot fit this call's allowance."""


def with_output_read(body: str, read: Mapping[str, object]) -> str:
    """Add recovery to an explicitly JSON native projection.

    This function is for Pulsara-owned JSON projections, not a guess about the
    format of a business TEXT value.  TEXT previews must be wrapped by their
    projection owner before calling it.
    """
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError("native projection with a read call must be a JSON object")
    return canonical_json_bytes({**value, "read": dict(read)}).decode("utf-8")


@dataclass(frozen=True, slots=True)
class ToolResultPageBudget:
    tool_call_id: str
    result_target: Mapping[str, object] | None = None
    accepts_provider_body: Callable[[str, tuple[str, ...]], bool] | None = field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if not isinstance(self.tool_call_id, str) or not self.tool_call_id:
            raise ValueError("tool result page quote requires the actual call ID")
        if self.result_target is not None:
            target = self.result_target
            if target.get("kind") != "SESSION_ENTRY" or set(target) != {"kind", "session_id", "entry_id"}:
                raise ValueError("a result recovery quote requires the complete original SESSION_ENTRY target")
            tool_output_read_call(target["session_id"], target["entry_id"])
            object.__setattr__(self, "result_target", freeze_tool_json_object(target))

    @property
    def read(self) -> dict[str, object] | None:
        return (
            None if self.result_target is None
            else tool_output_read_call(self.result_target["session_id"], self.result_target["entry_id"])
        )

    def fits(
        self,
        body: str,
        *,
        model_visible_memory_fact_ids: tuple[str, ...] = (),
        include_read: bool = False,
    ) -> bool:
        if include_read:
            read = self.read
            if read is None:
                raise ValueError("recovery quote requires the original result target")
            body = with_output_read(body, read)
        if conservative_tool_result_page_logical_utf8_bytes(
            tool_call_id=self.tool_call_id,
            body=body,
            model_visible_memory_ids=model_visible_memory_fact_ids,
        ) > MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES:
            return False
        return (
            self.accepts_provider_body is None
            or self.accepts_provider_body(body, model_visible_memory_fact_ids)
        )
