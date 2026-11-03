"""The sole native text projection of one accepted business observation."""

from __future__ import annotations

import json
from typing import Mapping

from pulsara_agent.ports.tool_output import ToolOutputUnavailableReason
from pulsara_agent.primitives.context import canonical_json_bytes
from pulsara_agent.primitives.tool_result_budget import (
    ToolResultPageBoundary,
    ToolResultPageBudget,
)


DEFAULT_OUTPUT_PREVIEW_CHARS = 8_000  # Existing native preview policy, not a history cap.


def _native_preview_body(
    required_facts: Mapping[str, object], prefix: str, *,
    read: Mapping[str, object] | None,
    unavailable_reason: ToolOutputUnavailableReason | None,
) -> str:
    value: dict[str, object] = {**dict(required_facts), "output_preview": prefix}
    if read is not None:
        value["read"] = dict(read)
    elif unavailable_reason is not None:
        value["output_unavailability_reason"] = unavailable_reason.value
    return canonical_json_bytes(value).decode("utf-8")


def require_native_result_minimum(
    page_budget: ToolResultPageBudget, *, required_facts: Mapping[str, object],
    model_visible_memory_fact_ids: tuple[str, ...] = (),
) -> None:
    """Admit known control/recovery facts before a physical operation starts.

    This quotes the same empty-preview representation as the final projector,
    with the actual preallocated result target. It covers both retained and
    unavailable C. It does not predict or promise an unknown business response.
    """
    possibilities = (None, *ToolOutputUnavailableReason)
    for unavailable in possibilities:
        body = _native_preview_body(required_facts, "",
            read=page_budget.read if unavailable is None else None,
            unavailable_reason=unavailable)
        if not page_budget.fits(body, model_visible_memory_fact_ids=model_visible_memory_fact_ids):
            raise ToolResultPageBoundary("The essential result and recovery location cannot fit before execution")


def project_native_tool_result(
    *,
    public_output: str,
    native_output: str | None,
    page_complete: bool,
    required_facts: Mapping[str, object],
    output_available: bool,
    output_unavailability_reason: ToolOutputUnavailableReason | None,
    page_budget: ToolResultPageBudget,
    model_visible_memory_fact_ids: tuple[str, ...] = (),
) -> tuple[str, bool]:
    """Choose P once, after C's actual availability is known.

    Business TEXT is never decoded speculatively.  A shortened P is always an
    explicitly Pulsara-owned JSON projection; complete pages remain unchanged.
    """
    candidate = public_output if native_output is None else native_output
    omitted = candidate != public_output
    read = page_budget.read if output_available else None

    def complete(body: str) -> bool:
        return page_budget.fits(
            body, model_visible_memory_fact_ids=model_visible_memory_fact_ids
        )

    def shortened(prefix: str) -> str:
        return _native_preview_body(required_facts, prefix, read=read,
                                    unavailable_reason=output_unavailability_reason)

    if page_complete:
        if omitted:
            raise ValueError("a complete source page cannot supply a shortened native value")
        if not complete(candidate):
            raise ToolResultPageBoundary("The complete source page exceeds its call allowance")
        return candidate, False

    if omitted:
        # Domain-owned JSON P already selected its excerpt/tail.  Do not
        # reinterpret it as a new head/tail or expose a read that failed storage.
        value = json.loads(candidate)
        if not isinstance(value, dict):
            raise ValueError("an explicit shortened native result must be a JSON object")
        value.pop("read", None)
        if read is not None:
            value["read"] = read
        elif output_unavailability_reason is not None:
            value["output_unavailability_reason"] = output_unavailability_reason.value
        candidate = canonical_json_bytes(value).decode("utf-8")
        if not complete(candidate):
            raise ToolResultPageBoundary("The domain's native projection exceeds its call allowance")
        return candidate, True

    if complete(candidate):
        return candidate, False

    text = public_output[:DEFAULT_OUTPUT_PREVIEW_CHARS]
    minimum = shortened("")
    if not complete(minimum):
        raise ToolResultPageBoundary("The essential result and recovery location cannot fit")
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if complete(shortened(text[:middle])):
            low = middle
        else:
            high = middle - 1
    return shortened(text[:low]), True
