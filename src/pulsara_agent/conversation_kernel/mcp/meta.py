"""Session-scoped MCP call references, accepted with their query result."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock

from pulsara_agent.capability.contracts import CapabilityIdentity
from pulsara_agent.identity import is_valid_id, random_id, require_id
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.primitives.context import freeze_json


def executor_reference_basis(executor) -> tuple[object, ...]:
    """Freeze actual tool and target facts, independent of reconnect generations.

    Existing config helpers provide credential-generation commitments without
    exposing secrets. The full config also carries unrelated display/discovery
    settings and other tools, so it is not the reference's target identity.
    """
    from pulsara_agent.mcp_config import _auth_fingerprint_payload, _transport_fingerprint_payload

    semantic, policy = executor.semantic, executor.policy
    config = executor.lease._slot.client.config
    authorization = config.authorization_provider
    account = authorization.reference_identity() if authorization is not None else None
    return (
        semantic.server_id, semantic.remote_tool_name, semantic.description,
        semantic.input_schema, semantic.output_schema,
        semantic.root_visible, semantic.subagent_visible,
        policy.effect_kind, policy.timeout_ms, policy.parallel_safe, policy.classification_source,
        config.runtime_source,
        freeze_json(_transport_fingerprint_payload(config.transport, config.secret_resolver)),
        freeze_json(_auth_fingerprint_payload(config.auth, config.secret_resolver)),
        config.public_headers, account,
    )


@dataclass(frozen=True, slots=True)
class NewMcpToolRef:
    opaque_token: str
    conversation_scope_kind: ModelInputScopeKind
    session_id: str
    capability_identity: CapabilityIdentity
    binding: tuple[object, ...] = field(repr=False)
    issued_by_host_authority: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        require_id(self.opaque_token, "mcpref")
        require_id(self.session_id, "session")
        if not isinstance(self.capability_identity, CapabilityIdentity) or not self.binding:
            raise ValueError("MCP reference requires a complete tool binding")


@dataclass(frozen=True, slots=True)
class PreparedNewMcpToolRefSettlement:
    settlement_token_id: str
    result_entry_id: str
    ref: NewMcpToolRef
    record: "_RefRecord" = field(repr=False, compare=False)


@dataclass(slots=True)
class _RefRecord:
    ref: NewMcpToolRef
    accepted: bool = False


class ProcessLocalNewMcpToolRefOwner:
    """Reuse exact current bindings across native calls, Cells and epochs."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._authority = object()
        self._refs: dict[str, _RefRecord] = {}
        self._prepared: dict[str, PreparedNewMcpToolRefSettlement] = {}
        self._closed = False

    def prepare(self, *, conversation_scope_kind: ModelInputScopeKind, session_id: str,
                capability_identity: CapabilityIdentity, binding: tuple[object, ...],
                result_entry_id: str) -> PreparedNewMcpToolRefSettlement:
        with self._lock:
            if self._closed:
                raise RuntimeError("MCP reference owner is closed")
            record = next((item for item in self._refs.values()
                if item.ref.conversation_scope_kind is conversation_scope_kind
                and item.ref.session_id == session_id
                and item.ref.capability_identity == capability_identity
                and item.ref.binding == binding), None)
            if record is None:
                token = random_id("mcpref")
                ref = NewMcpToolRef(token, conversation_scope_kind, session_id,
                                   capability_identity, binding, self._authority)
                record = self._refs[token] = _RefRecord(ref)
            prepared = PreparedNewMcpToolRefSettlement(random_id("settlement"),
                result_entry_id, record.ref, record)
            self._prepared[prepared.settlement_token_id] = prepared
            return prepared

    def settle(self, *, prepared: PreparedNewMcpToolRefSettlement, committed: bool) -> None:
        with self._lock:
            retained = self._prepared.pop(prepared.settlement_token_id, None)
            if retained is None:
                if committed and self._refs.get(prepared.ref.opaque_token) is prepared.record:
                    raise RuntimeError("committed MCP reference settlement is absent")
                return
            if retained is not prepared:
                raise RuntimeError("MCP reference settlement conflicts")
            record = self._refs.get(prepared.ref.opaque_token)
            if record is not prepared.record:
                return
            if committed:
                record.accepted = True
            elif not record.accepted and not any(
                item.record is record for item in self._prepared.values()
            ):
                self._refs.pop(prepared.ref.opaque_token, None)

    def resolve_callable(self, token: str, *, conversation_scope_kind: ModelInputScopeKind,
                         session_id: str) -> NewMcpToolRef:
        if not is_valid_id(token, "mcpref"):
            raise LookupError("MCP_TOOL_REF_NOT_FOUND")
        with self._lock:
            record = self._refs.get(token)
            if record is None:
                raise LookupError("MCP_TOOL_REF_NOT_FOUND")
            ref = record.ref
            if (not record.accepted or ref.issued_by_host_authority is not self._authority
                or ref.conversation_scope_kind is not conversation_scope_kind
                or ref.session_id != session_id):
                raise LookupError("MCP_TOOL_REF_NOT_ACCEPTED_OR_FOREIGN")
            return ref

    def reconcile_directory(self, runtime) -> None:
        """Retire issued references as each accepted directory is installed."""
        with self._lock:
            by_consumer = {}
            for record in self._refs.values():
                ref = record.ref
                by_consumer.setdefault((ref.conversation_scope_kind, ref.session_id), set()).add(
                    ref.capability_identity
                )
            for (scope, session_id), identities in by_consumer.items():
                servers = runtime.catalog_for_scope(scope).servers
                bindings = {}
                for identity in identities:
                    executor = runtime.executors.get((identity.source.stable_source_id, identity.stable_name))
                    if executor is None:
                        continue
                    semantic = executor.semantic
                    visible = semantic.root_visible if scope is ModelInputScopeKind.ROOT else semantic.subagent_visible
                    if visible:
                        bindings[identity] = executor_reference_basis(executor)
                self.reconcile(
                    conversation_scope_kind=scope, session_id=session_id,
                    server_ids={s.server_id for s in servers},
                    complete_server_ids={s.server_id for s in servers if s.status.value == "READY"},
                    bindings=bindings,
                )

    def reconcile(self, *, conversation_scope_kind, session_id, server_ids,
                  complete_server_ids, bindings) -> None:
        """Forget known removals/changes while preserving unknown directories.

        Transport failure is not a tool removal. Accepted operations retain
        their own exact physical permits independently of these call references.
        """
        with self._lock:
            stale = {token for token, item in self._refs.items()
                if item.ref.conversation_scope_kind is conversation_scope_kind
                and item.ref.session_id == session_id
                and (
                    item.ref.capability_identity.source.stable_source_id not in server_ids
                    or (item.ref.capability_identity in bindings
                        and item.ref.binding != bindings[item.ref.capability_identity])
                    or (item.ref.capability_identity.source.stable_source_id in complete_server_ids
                        and item.ref.capability_identity not in bindings)
                )}
            for token in stale:
                self._refs.pop(token)
            for token, prepared in tuple(self._prepared.items()):
                if prepared.ref.opaque_token in stale:
                    self._prepared.pop(token)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._refs.clear()
            self._prepared.clear()


__all__ = ["NewMcpToolRef", "PreparedNewMcpToolRefSettlement",
           "ProcessLocalNewMcpToolRefOwner", "executor_reference_basis"]
