"""One call-local quote for complete text pages and native result displays.

The provider owner supplies the optional wire/token predicate.  A standalone
or program-internal invocation uses the same logical page boundary without
pretending that a provider window has been reserved for it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Mapping

from pulsara_agent.identity import require_id
from pulsara_agent.ports.tool_execution import freeze_tool_json_object
from pulsara_agent.primitives.tool_observation import (
    MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES,
)
from pulsara_agent.primitives.tool_result_projection import (
    conservative_tool_result_page_logical_utf8_bytes,
)


class ToolResultPageBoundary(ValueError):
    """The smallest complete result unit cannot fit this call's allowance."""


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
            require_id(target["session_id"], "session")
            require_id(target["entry_id"], "entry")
            object.__setattr__(self, "result_target", freeze_tool_json_object(target))

    def fits(
        self,
        body: str,
        *,
        model_visible_memory_fact_ids: tuple[str, ...] = (),
    ) -> bool:
        if not self.fits_logical(
            body,
            model_visible_memory_fact_ids=model_visible_memory_fact_ids,
        ):
            return False
        return (
            self.accepts_provider_body is None
            or self.accepts_provider_body(body, model_visible_memory_fact_ids)
        )

    def fits_logical(
        self,
        body: str,
        *,
        model_visible_memory_fact_ids: tuple[str, ...] = (),
    ) -> bool:
        """Check the same complete carrier without a text-only provider quote.

        Image results replace this call's text slot and add its attachments in
        one joint provider quote. They still require this ordinary/late result
        boundary; invoking the text predicate as well would quote a different
        candidate from the complete multimodal page.
        """
        return conservative_tool_result_page_logical_utf8_bytes(
            tool_call_id=self.tool_call_id,
            body=body,
            model_visible_memory_ids=model_visible_memory_fact_ids,
        ) <= MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES
