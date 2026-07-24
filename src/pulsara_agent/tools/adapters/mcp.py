"""MCP tool execution adapters.

The adapter is intentionally thin: it owns no MCP client or server process and
only delegates to the session-owned ``McpClientManager``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pulsara_agent.message import ToolResultState
from pulsara_agent.primitives.mcp import McpBindingIdentityFact
from pulsara_agent.primitives.runtime_event_vocabulary import (
    prepare_mcp_input_required_suspension,
)
from pulsara_agent.runtime.mcp.supervisor import McpServerSupervisor
from pulsara_agent.runtime.mcp.types import (
    McpBindingIdentity,
    McpContentArtifact,
    McpInputRequired,
    McpInputRequiredResolution,
    McpOriginalRequest,
    McpRequestSourceMethod,
    McpToolResult,
    redact_mcp_error_message,
)
from pulsara_agent.tools.base import (
    ToolCall,
    ToolExecutionResult,
    ToolExecutionSuspended,
    ToolResultArtifactCandidate,
    ToolRuntimeContext,
)


@dataclass(frozen=True, slots=True)
class McpCapabilityTool:
    name: str
    description: str
    parameters: dict[str, Any]
    server_id: str
    original_tool_name: str
    supervisor: McpServerSupervisor
    binding_identity: McpBindingIdentity
    timeout_ms: int
    is_read_only: bool = False
    is_concurrency_safe: bool = False

    async def execute_async(
        self,
        call: ToolCall,
        *,
        runtime_context: ToolRuntimeContext,
    ) -> ToolExecutionResult | ToolExecutionSuspended:
        del runtime_context
        lease = None
        try:
            lease = self.supervisor.acquire_binding_lease(self.binding_identity)
            manager = self.supervisor.manager_for_lease(lease)
            result = await manager.call_tool(
                self.server_id,
                self.original_tool_name,
                dict(call.arguments),
                timeout_ms=self.timeout_ms,
            )
        except Exception as exc:
            if lease is not None:
                self.supervisor.release_lease(lease)
            return ToolExecutionResult(
                call_id=call.id,
                tool_name=call.name,
                status=ToolResultState.ERROR,
                output=f"[MCP_ERROR] {type(exc).__name__}: {redact_mcp_error_message(exc)}",
            )
        if isinstance(result, McpInputRequired):
            payload = result.to_payload()
            interaction_id = str(payload["interaction_id"])
            assert lease is not None
            reservation = None
            try:
                reservation = self.supervisor.promote_lease_to_pending(
                    lease,
                    interaction_id,
                )
                payload.update(
                    {
                        "tool_call_id": call.id,
                        "tool_name": call.name,
                        "wrapper_tool_call_id": call.id,
                        "wrapper_tool_name": call.name,
                        "server_id": self.server_id,
                        "original_tool_name": self.original_tool_name,
                        "mcp_binding_identity": _binding_identity_payload(
                            self.binding_identity
                        ),
                        "mcp_pending_lease_reservation_id": reservation.reservation_id,
                    }
                )
                return ToolExecutionSuspended(
                    tool_call_id=call.id,
                    tool_name=call.name,
                    interaction_kind="mcp_input_required",
                    prepared_mcp_input_required=(
                        prepare_mcp_input_required_suspension(
                            interaction_id=interaction_id,
                            tool_call_id=call.id,
                            tool_name=call.name,
                            server_id=self.server_id,
                            round_count=result.round_count,
                            binding_identity=_binding_identity_fact(
                                self.binding_identity
                            ),
                            pending_lease_reservation_id=reservation.reservation_id,
                            protocol_version=result.protocol_version,
                            input_requests=tuple(
                                request.to_dict()
                                for request in result.input_requests
                            ),
                            original_request=result.original_request.to_dict(),
                            request_state=result.request_state,
                            deadline_monotonic=result.deadline_monotonic,
                        )
                    ),
                )
            except Exception as exc:
                if reservation is None:
                    self.supervisor.release_lease(lease)
                else:
                    self.supervisor.abort_pending_lease(
                        interaction_id,
                        reservation.reservation_id,
                    )
                return ToolExecutionResult(
                    call_id=call.id,
                    tool_name=call.name,
                    status=ToolResultState.ERROR,
                    output=(
                        "[MCP_ERROR] pending input ownership failed: "
                        f"{type(exc).__name__}: {redact_mcp_error_message(exc)}"
                    ),
                )
        assert lease is not None
        self.supervisor.release_lease(lease)
        normalized = _normalize_mcp_result(result)
        return ToolExecutionResult(
            call_id=call.id,
            tool_name=call.name,
            status=ToolResultState.ERROR if normalized.is_error else ToolResultState.SUCCESS,
            output=normalized.output,
            metadata={
                "provider_kind": "mcp",
                "server_id": self.server_id,
                "original_tool_name": self.original_tool_name,
                **normalized.metadata,
            },
            artifact_candidates=_artifact_candidates(normalized.artifacts),
        )

    async def resume_input_required(
        self,
        *,
        original_request: dict[str, Any],
        request_state: str | None,
        resolution: McpInputRequiredResolution,
        runtime_context: ToolRuntimeContext,
    ) -> ToolExecutionResult | ToolExecutionSuspended:
        del runtime_context
        lease = self.supervisor.borrow_pending_lease(
            resolution.interaction_id,
            self.binding_identity,
        )
        try:
            manager = self.supervisor.manager_for_lease(lease)
            result = await manager.resume_suspended_request(
                server_id=self.server_id,
                original_request=_original_request_from_payload(original_request),
                request_state=request_state,
                resolution=resolution,
                timeout_ms=self.timeout_ms,
            )
        finally:
            self.supervisor.return_pending_borrow(resolution.interaction_id)
        if isinstance(result, McpInputRequired):
            pending = self.supervisor.pending_lease_reservation(
                resolution.interaction_id
            )
            tool_call_id = str(resolution.tool_call_id or "")
            return ToolExecutionSuspended(
                tool_call_id=tool_call_id,
                tool_name=self.name,
                interaction_kind="mcp_input_required",
                prepared_mcp_input_required=prepare_mcp_input_required_suspension(
                    interaction_id=resolution.interaction_id,
                    tool_call_id=tool_call_id,
                    tool_name=self.name,
                    server_id=self.server_id,
                    round_count=result.round_count,
                    binding_identity=_binding_identity_fact(self.binding_identity),
                    pending_lease_reservation_id=pending.reservation_id,
                    protocol_version=result.protocol_version,
                    input_requests=tuple(
                        request.to_dict() for request in result.input_requests
                    ),
                    original_request=result.original_request.to_dict(),
                    request_state=result.request_state,
                    deadline_monotonic=result.deadline_monotonic,
                ),
            )
        normalized = _normalize_mcp_result(result)
        tool_call_id = str(resolution.tool_call_id or "")
        return ToolExecutionResult(
            call_id=tool_call_id,
            tool_name=self.name,
            status=ToolResultState.ERROR if normalized.is_error else ToolResultState.SUCCESS,
            output=normalized.output,
            metadata={
                "provider_kind": "mcp",
                "server_id": self.server_id,
                "original_tool_name": self.original_tool_name,
                "mcp_input_required_interaction_id": resolution.interaction_id,
                **normalized.metadata,
            },
            artifact_candidates=_artifact_candidates(normalized.artifacts),
        )


def _normalize_mcp_result(result: Any) -> McpToolResult:
    if isinstance(result, McpToolResult):
        return result
    if isinstance(result, str):
        return McpToolResult(output=result)
    try:
        return McpToolResult(output=json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    except TypeError:
        return McpToolResult(output=str(result))


def _artifact_candidates(artifacts: tuple[McpContentArtifact, ...]) -> tuple[ToolResultArtifactCandidate, ...]:
    candidates = []
    for artifact in artifacts:
        candidates.append(
            ToolResultArtifactCandidate(
                role=artifact.role,
                media_type=artifact.media_type,
                text=artifact.text,
                data=artifact.data,
                metadata=artifact.metadata,
            )
        )
    return tuple(candidates)


def _original_request_from_payload(payload: dict[str, Any]) -> McpOriginalRequest:
    source = McpRequestSourceMethod(str(payload["source_method"]))
    arguments = payload.get("arguments")
    prompt_arguments = payload.get("prompt_arguments")
    return McpOriginalRequest(
        source_method=source,
        tool_name=str(payload["tool_name"]) if payload.get("tool_name") is not None else None,
        arguments=dict(arguments) if isinstance(arguments, dict) else None,
        resource_uri=str(payload["resource_uri"]) if payload.get("resource_uri") is not None else None,
        prompt_name=str(payload["prompt_name"]) if payload.get("prompt_name") is not None else None,
        prompt_arguments=dict(prompt_arguments) if isinstance(prompt_arguments, dict) else None,
    )


def _binding_identity_payload(identity: McpBindingIdentity) -> dict[str, object]:
    return {
        "server_id": identity.server_id,
        "slot_id": identity.slot_id,
        "snapshot_id": identity.snapshot_id,
        "discovery_generation": identity.discovery_generation,
    }


def _binding_identity_fact(identity: McpBindingIdentity) -> McpBindingIdentityFact:
    return McpBindingIdentityFact(
        server_id=identity.server_id,
        slot_id=identity.slot_id,
        snapshot_id=identity.snapshot_id,
        discovery_generation=identity.discovery_generation,
    )
