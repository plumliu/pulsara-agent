"""Pure provider-neutral projection for Round 9 MCP inspection results."""

from __future__ import annotations

from dataclasses import dataclass
from pulsara_agent.identity import derive_id, require_id

from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    canonical_json_bytes,
    context_fingerprint,
)
from pulsara_agent.primitives.tool_result_projection import (
    conservative_tool_result_page_logical_utf8_bytes,
)


MCP_INSPECT_INVOCATION_NOTICE = "invoke only with use_new_mcp_tool"
_CONSERVATIVE_INSPECT_TOOL_REF = derive_id("mcpref", "inspection-output-budget")
MAXIMUM_NEW_MCP_TOOL_REF_CHARACTERS = len(_CONSERVATIVE_INSPECT_TOOL_REF)
_CONSERVATIVE_INSPECT_TOOL_CALL_ID = "call_" + ("x" * 123)


@dataclass(frozen=True, slots=True)
class McpInspectionDescriptorValues:
    server_id: str
    remote_tool_name: str
    provider_tool_name: str
    description: str
    input_schema: FrozenJsonObjectFact
    output_schema: FrozenJsonObjectFact | None
    effect_kind: str

    def __post_init__(self) -> None:
        if not all((self.server_id, self.remote_tool_name, self.provider_tool_name)):
            raise ValueError("MCP inspection descriptor identity is incomplete")
        if not isinstance(self.input_schema, FrozenJsonObjectFact):
            raise TypeError("MCP inspection input schema is not frozen")
        if self.output_schema is not None and not isinstance(
            self.output_schema, FrozenJsonObjectFact
        ):
            raise TypeError("MCP inspection output schema is not frozen")
        if self.effect_kind not in {"READ_ONLY", "EXTERNAL_EFFECT"}:
            raise ValueError("MCP inspection effect kind is not closed")


def mcp_inspection_descriptor_payload_fingerprint(
    values: McpInspectionDescriptorValues,
) -> str:
    return context_fingerprint(
        "mcp-inspection-descriptor-payload:v1",
        {
            "server_id": values.server_id,
            "remote_tool_name": values.remote_tool_name,
            "provider_tool_name": values.provider_tool_name,
            "description": values.description,
            "input_schema": values.input_schema,
            "output_schema": values.output_schema,
            "effect_kind": values.effect_kind,
            "invocation_notice": MCP_INSPECT_INVOCATION_NOTICE,
        },
    )


def render_inspected_new_mcp_tool_provider_result(
    values: McpInspectionDescriptorValues,
    *,
    tool_ref: str,
) -> str:
    require_id(tool_ref, "mcpref")
    return canonical_json_bytes(
        {
            "target": {
                "kind": "MCP_TOOL",
                "server_id": values.server_id,
                "tool_name": values.provider_tool_name,
            },
            "invocation": {
                "mode": "META",
                "target": {"kind": "MCP_TOOL_REF", "tool_ref": tool_ref},
            },
            "description": values.description,
            "effect_kind": values.effect_kind,
            "input_schema": values.input_schema,
            "invocation_notice": MCP_INSPECT_INVOCATION_NOTICE,
            "output_schema": values.output_schema,
        }
    ).decode("utf-8")


def conservative_mcp_inspection_logical_utf8_bytes(
    values: McpInspectionDescriptorValues,
) -> int:
    return conservative_tool_result_page_logical_utf8_bytes(
        tool_call_id=_CONSERVATIVE_INSPECT_TOOL_CALL_ID,
        body=render_inspected_new_mcp_tool_provider_result(
            values,
            tool_ref=_CONSERVATIVE_INSPECT_TOOL_REF,
        ),
        model_visible_memory_ids=(),
    )


__all__ = [
    "MAXIMUM_NEW_MCP_TOOL_REF_CHARACTERS",
    "MCP_INSPECT_INVOCATION_NOTICE",
    "McpInspectionDescriptorValues",
    "conservative_mcp_inspection_logical_utf8_bytes",
    "mcp_inspection_descriptor_payload_fingerprint",
    "render_inspected_new_mcp_tool_provider_result",
]
