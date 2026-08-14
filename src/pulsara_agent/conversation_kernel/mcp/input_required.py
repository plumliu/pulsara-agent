"""Bounded process-local owner for MCP state-only continuation rounds."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from pulsara_agent.primitives.context import canonical_json_bytes

from .sdk_facade import types


MAXIMUM_MCP_INPUT_REQUEST_ITEMS = 16
MAXIMUM_MCP_STATE_ONLY_ROUNDS = 16
MAXIMUM_MCP_TOTAL_INPUT_REQUIRED_ROUNDS = 24
MAXIMUM_MCP_REQUEST_KEY_BYTES = 256
MAXIMUM_MCP_PUBLIC_REQUEST_BYTES = 64 * 1024
MAXIMUM_MCP_REQUEST_STATE_BYTES = 1024 * 1024
MAXIMUM_MCP_INPUT_ROUND_WORKING_SET_BYTES = 4 * 1024 * 1024


class McpInputRequiredFailure(StrEnum):
    ELICITATION_UNSUPPORTED = "ELICITATION_UNSUPPORTED"
    SAMPLING_UNSUPPORTED = "SAMPLING_UNSUPPORTED"
    ROOTS_UNSUPPORTED = "ROOTS_UNSUPPORTED"
    UNKNOWN_UNSUPPORTED = "UNKNOWN_UNSUPPORTED"
    STATE_REQUIRED = "STATE_REQUIRED"
    PHYSICAL_BOUND_EXCEEDED = "PHYSICAL_BOUND_EXCEEDED"
    ROUND_LIMIT_EXCEEDED = "ROUND_LIMIT_EXCEEDED"


class McpInputRequiredUnsupported(RuntimeError):
    def __init__(self, failure: McpInputRequiredFailure) -> None:
        super().__init__(f"MCP_INPUT_REQUIRED_{failure.value}")
        self.failure = failure


@dataclass(slots=True)
class McpInputRequiredRoundOwner:
    """One exact tool operation's non-recoverable continuation owner."""

    operation_identity: str
    connection_generation: int
    _state_only_rounds: int = 0
    _total_rounds: int = 0
    _closed: bool = False
    _authority: object = field(default_factory=object, repr=False)

    def prepare_state_only_continuation(
        self, result: types.InputRequiredResult
    ) -> str:
        if self._closed:
            raise RuntimeError("MCP input-required owner is closed")
        self._total_rounds += 1
        if self._total_rounds > MAXIMUM_MCP_TOTAL_INPUT_REQUIRED_ROUNDS:
            raise McpInputRequiredUnsupported(
                McpInputRequiredFailure.ROUND_LIMIT_EXCEEDED
            )
        requests = result.input_requests or {}
        if len(requests) > MAXIMUM_MCP_INPUT_REQUEST_ITEMS:
            raise McpInputRequiredUnsupported(
                McpInputRequiredFailure.PHYSICAL_BOUND_EXCEEDED
            )
        round_working_set_bytes = 0
        for key in sorted(requests, key=lambda value: value.encode("utf-8")):
            key_bytes = len(key.encode("utf-8"))
            if key_bytes > MAXIMUM_MCP_REQUEST_KEY_BYTES:
                raise McpInputRequiredUnsupported(
                    McpInputRequiredFailure.PHYSICAL_BOUND_EXCEEDED
                )
            request = requests[key]
            if not isinstance(
                request,
                types.ElicitRequest | types.CreateMessageRequest | types.ListRootsRequest,
            ):
                raise McpInputRequiredUnsupported(
                    McpInputRequiredFailure.UNKNOWN_UNSUPPORTED
                )
            request_bytes = canonical_json_bytes(
                request.model_dump(by_alias=True, mode="json", exclude_none=True)
            )
            if len(request_bytes) > MAXIMUM_MCP_PUBLIC_REQUEST_BYTES:
                raise McpInputRequiredUnsupported(
                    McpInputRequiredFailure.PHYSICAL_BOUND_EXCEEDED
                )
            round_working_set_bytes += key_bytes + len(request_bytes)
            if isinstance(request, types.ElicitRequest):
                failure = McpInputRequiredFailure.ELICITATION_UNSUPPORTED
            elif isinstance(request, types.CreateMessageRequest):
                failure = McpInputRequiredFailure.SAMPLING_UNSUPPORTED
            elif isinstance(request, types.ListRootsRequest):
                failure = McpInputRequiredFailure.ROOTS_UNSUPPORTED
            else:
                failure = McpInputRequiredFailure.UNKNOWN_UNSUPPORTED
            raise McpInputRequiredUnsupported(failure)
        state = result.request_state
        if not isinstance(state, str) or not state:
            raise McpInputRequiredUnsupported(McpInputRequiredFailure.STATE_REQUIRED)
        state_bytes = len(state.encode("utf-8"))
        if (
            state_bytes > MAXIMUM_MCP_REQUEST_STATE_BYTES
            or round_working_set_bytes + state_bytes
            > MAXIMUM_MCP_INPUT_ROUND_WORKING_SET_BYTES
        ):
            raise McpInputRequiredUnsupported(
                McpInputRequiredFailure.PHYSICAL_BOUND_EXCEEDED
            )
        self._state_only_rounds += 1
        if self._state_only_rounds > MAXIMUM_MCP_STATE_ONLY_ROUNDS:
            raise McpInputRequiredUnsupported(
                McpInputRequiredFailure.ROUND_LIMIT_EXCEEDED
            )
        return state

    def close(self) -> None:
        self._closed = True


__all__ = [
    "McpInputRequiredFailure",
    "McpInputRequiredRoundOwner",
    "McpInputRequiredUnsupported",
]
