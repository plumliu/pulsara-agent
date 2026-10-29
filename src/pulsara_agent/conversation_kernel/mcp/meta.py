"""Host-scoped, process-local Round 9 MCP meta-tool reference owner."""

from __future__ import annotations

from pulsara_agent.identity import random_id

from dataclasses import dataclass, field
from enum import StrEnum
import base64
import hashlib
import hmac
import secrets
from threading import RLock

from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.primitives.context import canonical_json_bytes


MAXIMUM_LIVE_NEW_MCP_TOOL_REFS_PER_EPOCH = 1_024


class McpToolRefCapacityExceeded(RuntimeError):
    """The exact scope/epoch reached its closed process-local ref bound."""


class McpToolRefConsumerKind(StrEnum):
    MODEL_EPOCH = "MODEL_EPOCH"
    CODE_CELL = "CODE_CELL"


class NewMcpToolRefState(StrEnum):
    PREPARED = "PREPARED"
    DORMANT = "DORMANT"
    CALLABLE = "CALLABLE"


@dataclass(frozen=True, slots=True)
class NewMcpToolRef:
    opaque_token: str
    conversation_scope_kind: ModelInputScopeKind
    session_id: str
    continuity_epoch_nonce: str
    capability_identity_fingerprint: str
    tool_semantic_fingerprint: str
    mcp_execution_policy_fingerprint: str
    tool_route_fingerprint: str
    issued_by_host_authority: object = field(repr=False, compare=False)
    consumer_kind: McpToolRefConsumerKind = McpToolRefConsumerKind.MODEL_EPOCH
    consumer_id: str | None = None
    runtime_generation: int | None = None

    def __post_init__(self) -> None:
        if (self.consumer_kind is McpToolRefConsumerKind.CODE_CELL) != (self.consumer_id is not None):
            raise ValueError("MCP ref consumer identity is invalid")
        if not self.session_id:
            raise ValueError("new MCP ref session identity is missing")
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
    record: "_RefRecord" = field(repr=False, compare=False)


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
        session_id: str,
        continuity_epoch_nonce: str,
        capability_identity_fingerprint: str,
        tool_semantic_fingerprint: str,
        mcp_execution_policy_fingerprint: str,
        tool_route_fingerprint: str,
        result_entry_id: str,
        consumer_kind: McpToolRefConsumerKind = McpToolRefConsumerKind.MODEL_EPOCH,
        consumer_id: str | None = None,
        runtime_generation: int | None = None,
    ) -> PreparedNewMcpToolRefSettlement:
        semantic = {
            "scope": conversation_scope_kind.value,
            "consumer_kind": consumer_kind.value,
            "consumer_id": consumer_id,
            "runtime_generation": runtime_generation,
            "session_id": session_id,
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
            session_id=session_id,
            continuity_epoch_nonce=continuity_epoch_nonce,
            capability_identity_fingerprint=capability_identity_fingerprint,
            tool_semantic_fingerprint=tool_semantic_fingerprint,
            mcp_execution_policy_fingerprint=mcp_execution_policy_fingerprint,
            tool_route_fingerprint=tool_route_fingerprint,
            issued_by_host_authority=self._authority,
            consumer_kind=consumer_kind,
            consumer_id=consumer_id,
            runtime_generation=runtime_generation,
        )
        with self._lock:
            if self._closed:
                raise RuntimeError("new MCP ref owner is closed")
            settlement_id = random_id("settlement")
            while settlement_id in self._prepared:
                settlement_id = random_id("settlement")
            record = self._refs.get(token)
            if record is None:
                epoch_count = sum(
                    item.ref.conversation_scope_kind is conversation_scope_kind
                    and item.ref.session_id == session_id
                    and item.ref.continuity_epoch_nonce == continuity_epoch_nonce
                    and item.ref.consumer_kind is consumer_kind
                    and item.ref.consumer_id == consumer_id
                    for item in self._refs.values()
                )
                if (consumer_kind is McpToolRefConsumerKind.MODEL_EPOCH
                        and epoch_count >= MAXIMUM_LIVE_NEW_MCP_TOOL_REFS_PER_EPOCH):
                    raise McpToolRefCapacityExceeded(
                        "MCP_REF_CAPACITY_EXCEEDED"
                    )
                record = self._refs[token] = _RefRecord(
                    ref=ref,
                    state=NewMcpToolRefState.PREPARED,
                    committed_result_entry_ids=set(),
                )
            elif record.ref != ref:
                raise RuntimeError("new MCP ref token collision")
            prepared = PreparedNewMcpToolRefSettlement(
                settlement_token_id=settlement_id, result_entry_id=result_entry_id,
                ref=record.ref, record=record,
            )
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
                if self._refs.get(prepared.ref.opaque_token) is not prepared.record:
                    return  # Retired occurrence, including a delayed ABA settlement.
                if committed:
                    raise RuntimeError("committed MCP ref settlement is absent")
                return
            if retained is not prepared:
                raise RuntimeError("MCP ref settlement token conflicts")
            record = self._refs.get(prepared.ref.opaque_token)
            if record is not prepared.record:
                return  # A newer inspect with the same deterministic token owns its own result IDs.
            if committed:
                if record.state is not NewMcpToolRefState.CALLABLE:
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
        session_id: str,
        continuity_epoch_nonce: str,
        consumer_kind: McpToolRefConsumerKind = McpToolRefConsumerKind.MODEL_EPOCH,
        consumer_id: str | None = None,
    ) -> bool:
        installed = False
        with self._lock:
            for record in self._refs.values():
                ref = record.ref
                if (
                    result_entry_id in record.committed_result_entry_ids
                    and ref.conversation_scope_kind is conversation_scope_kind
                    and ref.session_id == session_id
                    and ref.continuity_epoch_nonce == continuity_epoch_nonce
                    and ref.consumer_kind is consumer_kind
                    and ref.consumer_id == consumer_id
                ):
                    record.state = NewMcpToolRefState.CALLABLE
                    record.committed_result_entry_ids.clear()
                    installed = True
        return installed

    def resolve_callable(
        self,
        token: str,
        *,
        conversation_scope_kind: ModelInputScopeKind,
        session_id: str,
        continuity_epoch_nonce: str,
        consumer_kind: McpToolRefConsumerKind = McpToolRefConsumerKind.MODEL_EPOCH,
        consumer_id: str | None = None,
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
                or ref.session_id != session_id
                or ref.continuity_epoch_nonce != continuity_epoch_nonce
                or ref.consumer_kind is not consumer_kind
                or ref.consumer_id != consumer_id
            ):
                raise LookupError("MCP_TOOL_REF_STALE_OR_DORMANT")
            return ref

    def retire_unroutable(
        self, *, conversation_scope_kind, session_id,
        continuity_epoch_nonce, allowed,
        consumer_kind=McpToolRefConsumerKind.MODEL_EPOCH, consumer_id=None,
    ) -> None:
        """Retire obsolete records, not their already admitted physical operations.

        Callers hold exact executor/permit objects for in-flight settlement.
        Existing settlement/result IDs isolate delayed FULL delivery from an ABA
        re-inspection; no token tombstones, TTLs, or another generation owner.
        """
        with self._lock:
            stale = set()
            for token, record in self._refs.items():
                ref = record.ref
                key = (ref.capability_identity_fingerprint, ref.tool_semantic_fingerprint,
                       ref.mcp_execution_policy_fingerprint, ref.tool_route_fingerprint, ref.runtime_generation)
                if (ref.conversation_scope_kind is conversation_scope_kind
                        and ref.session_id == session_id
                        and ref.continuity_epoch_nonce == continuity_epoch_nonce
                        and ref.consumer_kind is consumer_kind
                        and ref.consumer_id == consumer_id
                        and key not in allowed):
                    stale.add(token)
            for token in stale:
                self._refs.pop(token)
            for key, prepared in tuple(self._prepared.items()):
                if prepared.ref.opaque_token in stale:
                    self._prepared.pop(key)

    def retire_scope_except_epoch(
        self,
        *,
        conversation_scope_kind: ModelInputScopeKind,
        session_id: str,
        continuity_epoch_nonce: str,
    ) -> None:
        with self._lock:
            stale = tuple(
                token
                for token, record in self._refs.items()
                if record.ref.consumer_kind is McpToolRefConsumerKind.MODEL_EPOCH
                and record.ref.conversation_scope_kind is conversation_scope_kind
                and record.ref.session_id == session_id
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

    def retire_consumer(self, *, consumer_kind: McpToolRefConsumerKind, consumer_id: str) -> None:
        """Release one live consumer without disturbing accepted physical calls."""
        with self._lock:
            stale = {token for token, record in self._refs.items()
                     if record.ref.consumer_kind is consumer_kind
                     and record.ref.consumer_id == consumer_id}
            for token in stale:
                self._refs.pop(token)
            for token, prepared in tuple(self._prepared.items()):
                if prepared.ref.opaque_token in stale:
                    self._prepared.pop(token)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._prepared.clear()
            self._refs.clear()


__all__ = [
    "MAXIMUM_LIVE_NEW_MCP_TOOL_REFS_PER_EPOCH",
    "McpToolRefCapacityExceeded",
    "NewMcpToolRef",
    "McpToolRefConsumerKind",
    "NewMcpToolRefState",
    "PreparedNewMcpToolRefSettlement",
    "ProcessLocalNewMcpToolRefOwner",
]
