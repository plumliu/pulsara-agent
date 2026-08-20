"""Pure provider-neutral projection for Round 9 MCP inspection results."""

from __future__ import annotations

from dataclasses import dataclass

from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    canonical_json_bytes,
    context_fingerprint,
)
from pulsara_agent.primitives.tool_result_projection import (
    conservative_artifact_page_logical_utf8_bytes,
)


MCP_INSPECT_PERMISSION_NOTICE = "availability does not grant permission"
MCP_INSPECT_INVOCATION_NOTICE = "invoke only with use_new_mcp_tool"
MAXIMUM_NEW_MCP_TOOL_REF_CHARACTERS = 160
_CONSERVATIVE_INSPECT_TOOL_CALL_ID = "call_" + ("x" * 123)
_CONSERVATIVE_INSPECT_TOOL_REF = "mcpref_" + (
    "x" * (MAXIMUM_NEW_MCP_TOOL_REF_CHARACTERS - len("mcpref_"))
)


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
        if not all(
            (self.server_id, self.remote_tool_name, self.provider_tool_name)
        ):
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
            "permission_notice": MCP_INSPECT_PERMISSION_NOTICE,
            "invocation_notice": MCP_INSPECT_INVOCATION_NOTICE,
        },
    )


def render_inspected_new_mcp_tool_provider_result(
    values: McpInspectionDescriptorValues,
    *,
    tool_ref: str,
) -> str:
    if (
        not tool_ref.startswith("mcpref_")
        or len(tool_ref) > MAXIMUM_NEW_MCP_TOOL_REF_CHARACTERS
        or not tool_ref.isascii()
    ):
        raise ValueError("MCP tool reference is invalid")
    return canonical_json_bytes(
        {
            "access_mode": "NEW_MCP_META_ONLY",
            "description": values.description,
            "effect_kind": values.effect_kind,
            "input_schema": values.input_schema,
            "invocation_notice": MCP_INSPECT_INVOCATION_NOTICE,
            "output_schema": values.output_schema,
            "permission_notice": MCP_INSPECT_PERMISSION_NOTICE,
            "provider_tool_name": values.provider_tool_name,
            "remote_tool_name": values.remote_tool_name,
            "server_id": values.server_id,
            "tool_ref": tool_ref,
        }
    ).decode("utf-8")


def conservative_mcp_inspection_logical_utf8_bytes(
    values: McpInspectionDescriptorValues,
) -> int:
    return conservative_artifact_page_logical_utf8_bytes(
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
    "MCP_INSPECT_PERMISSION_NOTICE",
    "McpInspectionDescriptorValues",
    "conservative_mcp_inspection_logical_utf8_bytes",
    "mcp_inspection_descriptor_payload_fingerprint",
    "render_inspected_new_mcp_tool_provider_result",
]
