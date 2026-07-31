"""Owner-bound system browser adapter for MCP URL elicitation."""

from __future__ import annotations

import asyncio
import webbrowser
from threading import RLock
from uuid import uuid4

from pulsara_agent.ports.mcp_elicitation import (
    McpConfirmedUrlLaunchAuthority,
    McpExternalBrowserPort,
    McpUrlLaunchDisposition,
    McpUrlLaunchOutcome,
)
from pulsara_agent.ports.mcp_secret import (
    McpContinuationSecretBorrowIssuer,
    McpPrivateUrlElicitationPayload,
    McpSecretAccessPurpose,
)
from pulsara_agent.primitives.context import context_fingerprint


MCP_SYSTEM_BROWSER_CONTRACT_FINGERPRINT = context_fingerprint(
    "mcp-system-browser-contract:v1",
    {
        "launch": "owner-bound-one-shot",
        "consent": "required-before-launch",
        "prefetch": "forbidden",
        "redirect_observation": "none",
        "page_content": "none",
    },
)


class SystemMcpExternalBrowserPort(McpExternalBrowserPort):
    """Keep exact URLs private while presenting only owner-bound launch verbs."""

    __slots__ = ("_owners", "_borrows", "_lock")

    def __init__(self) -> None:
        self._owners: dict[
            str, dict[str, McpPrivateUrlElicitationPayload]
        ] = {}
        self._borrows = McpContinuationSecretBorrowIssuer(
            f"mcp-system-browser:{uuid4().hex}"
        )
        self._lock = RLock()

    @property
    def contract_fingerprint(self) -> str:
        return MCP_SYSTEM_BROWSER_CONTRACT_FINGERPRINT

    def register_owner(
        self,
        *,
        owner_id: str,
        private_url_payloads: tuple[McpPrivateUrlElicitationPayload, ...],
    ) -> None:
        by_key = {item.request_key: item for item in private_url_payloads}
        if len(by_key) != len(private_url_payloads):
            raise ValueError("MCP URL owner contains duplicate request keys")
        with self._lock:
            existing = self._owners.get(owner_id)
            if existing is not None and existing != by_key:
                raise ValueError("MCP browser owner cannot be rebound")
            self._owners[owner_id] = by_key

    def exact_url_for_display(self, *, owner_id: str, request_key: str) -> str:
        payload = self._payload(owner_id=owner_id, request_key=request_key)
        borrow = self._borrows.issue(McpSecretAccessPurpose.URL_DISPLAY)
        try:
            return borrow.exact_private_url(payload)
        finally:
            borrow.revoke()

    def release_owner(self, *, owner_id: str) -> None:
        with self._lock:
            self._owners.pop(owner_id, None)

    async def launch(
        self,
        authority: McpConfirmedUrlLaunchAuthority,
    ) -> McpUrlLaunchOutcome:
        payload = self._payload(
            owner_id=authority.owner_id,
            request_key=authority.request_key,
        )
        if (
            payload.process_local_private_payload_fingerprint
            != authority.private_url_payload_fingerprint
        ):
            raise ValueError("MCP browser launch authority payload mismatch")
        borrow = self._borrows.issue(McpSecretAccessPurpose.URL_LAUNCH)
        try:
            exact_url = borrow.exact_private_url(payload)
        finally:
            borrow.revoke()
        operation_id = f"mcp_browser_launch:{uuid4().hex}"
        try:
            launched = await asyncio.to_thread(
                webbrowser.open,
                exact_url,
                new=2,
                autoraise=True,
            )
        except Exception:
            return McpUrlLaunchOutcome(
                disposition=McpUrlLaunchDisposition.FAILED,
                physical_operation_id=operation_id,
                sanitized_diagnostic="The system browser could not be opened.",
            )
        if not launched:
            return McpUrlLaunchOutcome(
                disposition=McpUrlLaunchDisposition.REJECTED_BY_PLATFORM,
                physical_operation_id=operation_id,
                sanitized_diagnostic="The platform rejected the browser launch.",
            )
        return McpUrlLaunchOutcome(
            disposition=McpUrlLaunchDisposition.LAUNCHED,
            physical_operation_id=operation_id,
        )

    def _payload(
        self,
        *,
        owner_id: str,
        request_key: str,
    ) -> McpPrivateUrlElicitationPayload:
        with self._lock:
            try:
                return self._owners[owner_id][request_key]
            except KeyError as exc:
                raise KeyError("unknown MCP browser launch authority") from exc


__all__ = [
    "MCP_SYSTEM_BROWSER_CONTRACT_FINGERPRINT",
    "SystemMcpExternalBrowserPort",
]
