"""Exact MCP standard resource reads and user prompt operations."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Mapping


from .contracts import (
    McpInstallationCandidate,
    McpProtocolConformanceError,
)
from .input_required import McpInputRequiredRoundOwner
from .resource_uris import _standard_resource_uri

if TYPE_CHECKING:
    from .sdk_facade import BoundedMcpSdkClient, types


async def _invoke_standard_remote_result(
    *,
    client: BoundedMcpSdkClient,
    candidate: McpInstallationCandidate,
    tool_name: str,
    arguments: Mapping[str, object],
    input_owner: McpInputRequiredRoundOwner,
) -> types.ReadResourceResult | types.GetPromptResult:
    from .sdk_facade import types

    request_state: str | None = None
    if tool_name == "read_mcp_resource":
        uri = _standard_resource_uri(arguments, candidate.discovery_snapshot)
        while True:
            result = await client.session.read_resource(
                uri,
                input_responses={} if request_state is not None else None,
                request_state=request_state,
                allow_input_required=True,
            )
            result_type = client.require_closed_result_type(result)
            if result_type == "complete":
                if not isinstance(result, types.ReadResourceResult):
                    raise McpProtocolConformanceError(
                        "MCP_RESULT_TYPE_PAYLOAD_CONTRADICTION"
                    )
                return result
            if not isinstance(result, types.InputRequiredResult):
                raise McpProtocolConformanceError(
                    "MCP_RESULT_TYPE_PAYLOAD_CONTRADICTION"
                )
            request_state = input_owner.prepare_state_only_continuation(result)
    raise RuntimeError("unknown MCP standard operation")


def user_prompt_text(result):
    """Only plain user text has a lossless representation in the current composer."""
    if any(message.role != "user" or message.content.type != "text" for message in result.messages):
        return {"state": "unsupported_content", "message": "此提示词包含暂不支持的消息内容。"}
    text = "\n\n".join(message.content.text for message in result.messages)
    if not text.strip():
        return {"state": "empty", "message": "提示词内容为空。"}
    return {"state": "ready", "text": text}


async def _get_prompt_result(client, prompt_name, prompt_arguments, input_owner):
    from .sdk_facade import types

    request_state = None
    while True:
        result = await client.session.get_prompt(
            prompt_name,
            prompt_arguments,
            input_responses={} if request_state is not None else None,
            request_state=request_state,
            allow_input_required=True,
        )
        result_type = client.require_closed_result_type(result)
        if result_type == "complete":
            if not isinstance(result, types.GetPromptResult):
                raise McpProtocolConformanceError(
                    "MCP_RESULT_TYPE_PAYLOAD_CONTRADICTION"
                )
            return result
        if not isinstance(result, types.InputRequiredResult):
            raise McpProtocolConformanceError(
                "MCP_RESULT_TYPE_PAYLOAD_CONTRADICTION"
            )
        request_state = input_owner.prepare_state_only_continuation(result)


def _remaining_mcp_timeout(deadline: float) -> float:
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise TimeoutError("MCP tool operation deadline expired")
    return remaining
