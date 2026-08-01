"""Process-local W3C trace propagation for MCP transport operations.

Trace context is deliberately absent from durable MCP facts and semantic
fingerprints.  The HTTP hook is failure-isolated: telemetry can disappear, but
it cannot change a domain result or the ownership of a physical request.
"""

from __future__ import annotations

import re
import secrets
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator, Mapping
from urllib.parse import quote


_TRACEPARENT_RE = re.compile(
    r"^00-(?P<trace_id>[0-9a-f]{32})-(?P<span_id>[0-9a-f]{16})-(?P<flags>[0-9a-f]{2})$"
)
_TRACESTATE_RE = re.compile(r"^[\x20-\x7e]{1,512}$")
_BAGGAGE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_ALLOWED_BAGGAGE_KEYS = frozenset(
    {
        "pulsara.mcp.method",
        "pulsara.mcp.server",
    }
)


@dataclass(frozen=True, slots=True)
class McpOperationTraceContext:
    """Non-durable transport correlation carried only by process context."""

    traceparent: str
    tracestate: str | None = None
    baggage: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        match = _TRACEPARENT_RE.fullmatch(self.traceparent)
        if match is None:
            raise ValueError("invalid W3C traceparent")
        if (
            int(match.group("trace_id"), 16) == 0
            or int(match.group("span_id"), 16) == 0
        ):
            raise ValueError("W3C trace identifiers cannot be all zero")
        if (
            self.tracestate is not None
            and _TRACESTATE_RE.fullmatch(self.tracestate) is None
        ):
            raise ValueError("invalid W3C tracestate")
        normalized = tuple(
            sorted((str(key), str(value)) for key, value in self.baggage)
        )
        if normalized != self.baggage or len({key for key, _ in normalized}) != len(
            normalized
        ):
            raise ValueError("MCP trace baggage must be ordered and unique")
        for key, value in normalized:
            if (
                key not in _ALLOWED_BAGGAGE_KEYS
                or _BAGGAGE_KEY_RE.fullmatch(key) is None
            ):
                raise ValueError("MCP trace baggage key is not allowlisted")
            if (
                not value
                or len(value.encode("utf-8")) > 256
                or any(char in value for char in "\r\n")
            ):
                raise ValueError("MCP trace baggage value is invalid")

    def headers(self) -> dict[str, str]:
        headers = {"traceparent": self.traceparent}
        if self.tracestate is not None:
            headers["tracestate"] = self.tracestate
        if self.baggage:
            headers["baggage"] = ",".join(
                f"{key}={quote(value, safe='-._~:/')}" for key, value in self.baggage
            )
        return headers


_CURRENT_MCP_TRACE: ContextVar[McpOperationTraceContext | None] = ContextVar(
    "pulsara_current_mcp_trace",
    default=None,
)


def new_mcp_operation_trace_context(
    *,
    server_id: str | None = None,
    method: str | None = None,
) -> McpOperationTraceContext:
    baggage: list[tuple[str, str]] = []
    if method:
        baggage.append(("pulsara.mcp.method", _bounded_baggage_value(method)))
    if server_id:
        baggage.append(("pulsara.mcp.server", _bounded_baggage_value(server_id)))
    return McpOperationTraceContext(
        traceparent=f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01",
        baggage=tuple(sorted(baggage)),
    )


@contextmanager
def mcp_operation_trace_scope(
    *,
    server_id: str | None = None,
    method: str | None = None,
    trace_context: McpOperationTraceContext | None = None,
) -> Iterator[McpOperationTraceContext]:
    context = trace_context or new_mcp_operation_trace_context(
        server_id=server_id,
        method=method,
    )
    token = _CURRENT_MCP_TRACE.set(context)
    try:
        yield context
    finally:
        _CURRENT_MCP_TRACE.reset(token)


def current_mcp_trace_headers() -> Mapping[str, str]:
    context = _CURRENT_MCP_TRACE.get()
    return {} if context is None else context.headers()


async def inject_mcp_trace_headers_safely(request: object) -> None:
    """httpx2 request hook whose failure cannot affect MCP execution."""

    try:
        headers = current_mcp_trace_headers()
        if not headers:
            headers = new_mcp_operation_trace_context().headers()
        request_headers = getattr(request, "headers")
        for key, value in headers.items():
            request_headers.setdefault(key, value)
    except BaseException:
        return


def _bounded_baggage_value(value: str) -> str:
    normalized = " ".join(str(value).split())
    encoded = normalized.encode("utf-8")
    if len(encoded) <= 256:
        return normalized
    return encoded[:256].decode("utf-8", errors="ignore")


__all__ = [
    "McpOperationTraceContext",
    "current_mcp_trace_headers",
    "inject_mcp_trace_headers_safely",
    "mcp_operation_trace_scope",
    "new_mcp_operation_trace_context",
]
