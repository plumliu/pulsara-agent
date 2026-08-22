"""Host-scoped, process-local Round 9 MCP meta-tool reference owner."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import base64
import hashlib
import hmac
import secrets
from threading import RLock
from uuid import uuid4

from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.primitives.context import canonical_json_bytes


MAXIMUM_LIVE_NEW_MCP_TOOL_REFS_PER_EPOCH = 1_024


class McpToolRefCapacityExceeded(RuntimeError):
    """The exact scope/epoch reached its closed process-local ref bound."""


class NewMcpToolRefState(StrEnum):
    PREPARED = "PREPARED"
    DORMANT = "DORMANT"
    CALLABLE = "CALLABLE"


@dataclass(frozen=True, slots=True)
class NewMcpToolRef:
    opaque_token: str
    conversation_scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    continuity_epoch_nonce: str
    capability_identity_fingerprint: str
    tool_semantic_fingerprint: str
    mcp_execution_policy_fingerprint: str
    tool_route_fingerprint: str
    issued_by_host_authority: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if (self.conversation_scope_kind is ModelInputScopeKind.ROOT) != (
            self.scope_subagent_task_id is None
        ):
            raise ValueError("new MCP ref scope identity is invalid")
        if (
            not self.opaque_token.startswith("mcpref_")
            or not self.continuity_epoch_nonce
            or not all(
                (
                    self.capability_identity_fingerprint,
                    self.tool_semantic_fingerprint,
                    self.mcp_execution_policy_fingerprint,
                    self.tool_route_fingerprint,
                )
            )
        ):
            raise ValueError("new MCP ref identity is incomplete")


@dataclass(frozen=True, slots=True)
class PreparedNewMcpToolRefSettlement:
    settlement_token_id: str
    result_entry_id: str
    ref: NewMcpToolRef


@dataclass(slots=True)
class _RefRecord:
    ref: NewMcpToolRef
    state: NewMcpToolRefState
    committed_result_entry_ids: set[str]


class ProcessLocalNewMcpToolRefOwner:
    """Sealed HMAC references; no slot, durable row, replay, or LRU owner."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._authority = object()
        self._secret = secrets.token_bytes(32)
        self._refs: dict[str, _RefRecord] = {}
        self._prepared: dict[str, PreparedNewMcpToolRefSettlement] = {}
        self._closed = False

    def prepare(
        self,
        *,
        conversation_scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
        continuity_epoch_nonce: str,
        capability_identity_fingerprint: str,
        tool_semantic_fingerprint: str,
        mcp_execution_policy_fingerprint: str,
        tool_route_fingerprint: str,
        result_entry_id: str,
    ) -> PreparedNewMcpToolRefSettlement:
        semantic = {
            "scope": conversation_scope_kind.value,
            "scope_subagent_task_id": scope_subagent_task_id,
            "epoch_nonce": continuity_epoch_nonce,
            "capability_identity": capability_identity_fingerprint,
            "tool_semantic": tool_semantic_fingerprint,
            "execution_policy": mcp_execution_policy_fingerprint,
            "tool_route": tool_route_fingerprint,
        }
        digest = hmac.new(
            self._secret,
            canonical_json_bytes(semantic),
            hashlib.sha256,
        ).digest()
        token = "mcpref_" + base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        ref = NewMcpToolRef(
            opaque_token=token,
            conversation_scope_kind=conversation_scope_kind,
            scope_subagent_task_id=scope_subagent_task_id,
            continuity_epoch_nonce=continuity_epoch_nonce,
            capability_identity_fingerprint=capability_identity_fingerprint,
            tool_semantic_fingerprint=tool_semantic_fingerprint,
            mcp_execution_policy_fingerprint=mcp_execution_policy_fingerprint,
            tool_route_fingerprint=tool_route_fingerprint,
            issued_by_host_authority=self._authority,
        )
        settlement_id = f"mcp-ref-settlement:{uuid4().hex}"
        prepared = PreparedNewMcpToolRefSettlement(
            settlement_token_id=settlement_id,
            result_entry_id=result_entry_id,
            ref=ref,
        )
        with self._lock:
            if self._closed:
                raise RuntimeError("new MCP ref owner is closed")
            record = self._refs.get(token)
            if record is None:
                epoch_count = sum(
                    item.ref.conversation_scope_kind is conversation_scope_kind
                    and item.ref.scope_subagent_task_id == scope_subagent_task_id
                    and item.ref.continuity_epoch_nonce == continuity_epoch_nonce
                    for item in self._refs.values()
                )
                if epoch_count >= MAXIMUM_LIVE_NEW_MCP_TOOL_REFS_PER_EPOCH:
                    raise McpToolRefCapacityExceeded(
                        "MCP_REF_CAPACITY_EXCEEDED"
                    )
                self._refs[token] = _RefRecord(
                    ref=ref,
                    state=NewMcpToolRefState.PREPARED,
                    committed_result_entry_ids=set(),
                )
            elif record.ref != ref:
                raise RuntimeError("new MCP ref token collision")
            existing = self._prepared.get(settlement_id)
            if existing is not None and existing != prepared:
                raise RuntimeError("new MCP ref settlement identity conflicts")
            self._prepared[settlement_id] = prepared
        return prepared

    def settle(
        self,
        *,
        prepared: PreparedNewMcpToolRefSettlement,
        committed: bool,
    ) -> None:
        with self._lock:
            retained = self._prepared.pop(prepared.settlement_token_id, None)
            if retained is None:
                if committed:
                    raise RuntimeError("committed MCP ref settlement is absent")
                return
            if retained is not prepared:
                raise RuntimeError("MCP ref settlement token conflicts")
            record = self._refs.get(prepared.ref.opaque_token)
            if record is None or record.ref != prepared.ref:
                raise RuntimeError("MCP ref record is absent")
            if committed:
                record.committed_result_entry_ids.add(prepared.result_entry_id)
                if record.state is NewMcpToolRefState.PREPARED:
                    record.state = NewMcpToolRefState.DORMANT
            elif (
                record.state is NewMcpToolRefState.PREPARED
                and not record.committed_result_entry_ids
                and not any(
                    item.ref.opaque_token == prepared.ref.opaque_token
                    for item in self._prepared.values()
                )
            ):
                self._refs.pop(prepared.ref.opaque_token, None)

    def install_full_result(
        self,
        *,
        result_entry_id: str,
        conversation_scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
        continuity_epoch_nonce: str,
    ) -> bool:
        installed = False
        with self._lock:
            for record in self._refs.values():
                ref = record.ref
                if (
                    result_entry_id in record.committed_result_entry_ids
                    and ref.conversation_scope_kind is conversation_scope_kind
                    and ref.scope_subagent_task_id == scope_subagent_task_id
                    and ref.continuity_epoch_nonce == continuity_epoch_nonce
                ):
                    record.state = NewMcpToolRefState.CALLABLE
                    installed = True
        return installed

    def resolve_callable(
        self,
        token: str,
        *,
        conversation_scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
        continuity_epoch_nonce: str,
    ) -> NewMcpToolRef:
        with self._lock:
            record = self._refs.get(token)
            if record is None:
                raise LookupError("MCP_TOOL_REF_NOT_FOUND")
            ref = record.ref
            if (
                record.state is not NewMcpToolRefState.CALLABLE
                or ref.issued_by_host_authority is not self._authority
                or ref.conversation_scope_kind is not conversation_scope_kind
                or ref.scope_subagent_task_id != scope_subagent_task_id
                or ref.continuity_epoch_nonce != continuity_epoch_nonce
            ):
                raise LookupError("MCP_TOOL_REF_STALE_OR_DORMANT")
            return ref

    def retire_scope_except_epoch(
        self,
        *,
        conversation_scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
        continuity_epoch_nonce: str,
    ) -> None:
        with self._lock:
            stale = tuple(
                token
                for token, record in self._refs.items()
                if record.ref.conversation_scope_kind is conversation_scope_kind
                and record.ref.scope_subagent_task_id == scope_subagent_task_id
                and record.ref.continuity_epoch_nonce != continuity_epoch_nonce
            )
            for token in stale:
                self._refs.pop(token, None)
            stale_settlements = tuple(
                token_id
                for token_id, prepared in self._prepared.items()
                if prepared.ref.opaque_token in stale
            )
            for token_id in stale_settlements:
                self._prepared.pop(token_id, None)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._prepared.clear()
            self._refs.clear()


__all__ = [
    "MAXIMUM_LIVE_NEW_MCP_TOOL_REFS_PER_EPOCH",
    "McpToolRefCapacityExceeded",
    "NewMcpToolRef",
    "NewMcpToolRefState",
    "PreparedNewMcpToolRefSettlement",
    "ProcessLocalNewMcpToolRefOwner",
]
