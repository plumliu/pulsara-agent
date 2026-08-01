"""Encrypted durable storage for MCP multi-round continuation state.

The store is deliberately split into three owners:

* :class:`McpContinuationSecretCodec` is the only component that may borrow
  sealed plaintext and turn it into an AEAD envelope;
* repositories may read storage-only facts but expose no production write
  method;
* :class:`McpContinuationTransactionCompanion` is the only mutation path and
  may run only after EventLog has rebound the exact sequence-null candidate
  batch to its canonical stored events.
"""

from __future__ import annotations

import base64
import hmac
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from hashlib import sha256
from threading import RLock
from time import monotonic
from typing import Any, Mapping, Sequence, TypeAlias

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from psycopg.rows import dict_row

from pulsara_agent.event.events import AgentEvent
from pulsara_agent.event_log.protocol import (
    EventLogPreparedCandidateBatchIdentity,
    EventLogStoredCandidateBatchRebindReceipt,
    build_prepared_candidate_batch_identity,
)
from pulsara_agent.ports.event_write import FrozenEventWriteCandidate
from pulsara_agent.ports.mcp import McpPreparedCompanionIdentity
from pulsara_agent.ports.mcp_secret import (
    McpAwaitingInputCarrierPlaintext,
    McpContinuationCarrierPlaintext,
    McpContinuationSecretBorrowIssuer,
    McpFrozenRoundInputResponses,
    McpReplayReadyCarrierPlaintext,
    McpRetryableRequestPayload,
    McpSealedElicitationResponseFactory,
    McpPrivateUrlElicitationPayload,
    McpSecretAccessPurpose,
    build_awaiting_input_carrier_plaintext,
    build_replay_ready_carrier_plaintext,
)
from pulsara_agent.primitives._context_base import canonical_json_bytes
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.mcp_continuation import (
    McpContinuationBoundsFact,
    McpContinuationCarrierState,
    McpContinuationCompanionChargeFact,
    McpContinuationCompanionKind,
    McpContinuationCompanionPlanFact,
    McpContinuationExpiryFact,
    McpInputRequiredDurableContinuationFact,
    build_mcp_continuation_fact,
)
from pulsara_agent.primitives.mcp_continuation_storage import (
    McpContinuationCarrierControlFact,
    McpStoredContinuationEnvelopeFact,
    build_mcp_continuation_storage_fact,
)
from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane,
    VerifiedPostgresConnectionProviderProtocol,
    postgres_operation_deadline,
)


class McpContinuationSecretStoreError(RuntimeError):
    """Stable base class for encrypted continuation storage failures."""


class McpContinuationSecretKeyUnavailable(McpContinuationSecretStoreError):
    pass


class McpContinuationBoundsExceeded(McpContinuationSecretStoreError):
    pass


class McpContinuationAuthorityConflict(McpContinuationSecretStoreError):
    pass


class McpContinuationDecryptFailed(McpContinuationSecretStoreError):
    pass


class McpContinuationMutationKind(StrEnum):
    INSERT_AWAITING = "insert_awaiting"
    REPLACE_WITH_REPLAY_READY = "replace_with_replay_ready"
    RESERVE_DISPATCH = "reserve_dispatch"
    REPLACE_WITH_SUCCESSOR = "replace_with_successor"
    DELETE_TERMINAL = "delete_terminal"


@dataclass(frozen=True, slots=True)
class McpContinuationKeyMaterial:
    key_id: str
    aead_key: bytes = field(repr=False)
    commitment_key: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not self.key_id
            or len(self.aead_key) != 32
            or len(self.commitment_key) != 32
        ):
            raise ValueError("MCP continuation key material is invalid")
        if self.aead_key == self.commitment_key:
            raise ValueError("MCP AEAD and commitment keys must be domain separated")


class McpContinuationKeyProvider:
    """Process secret owner with domain-separated AEAD and HMAC keys."""

    __slots__ = ("_material",)

    def __init__(self, material: McpContinuationKeyMaterial) -> None:
        self._material = material

    @classmethod
    def from_master_key(
        cls, *, key_id: str, master_key: bytes
    ) -> "McpContinuationKeyProvider":
        if len(master_key) < 32:
            raise McpContinuationSecretKeyUnavailable(
                "MCP continuation master key must contain at least 256 bits"
            )
        return cls(
            McpContinuationKeyMaterial(
                key_id=key_id,
                aead_key=_derive_key(master_key, b"pulsara-mcp-continuation-aead:v1"),
                commitment_key=_derive_key(
                    master_key,
                    b"pulsara-mcp-continuation-commitment:v1",
                ),
            )
        )

    @classmethod
    def optional_from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> "McpContinuationKeyProvider | None":
        """Return disabled only when both continuation settings are absent.

        A partial or malformed deployment is an authority error, not an opt-out.
        This distinction keeps secure MRTR from silently disappearing because a
        secret was mounted under the wrong name or generation.
        """

        env = os.environ if environment is None else environment
        encoded = env.get("PULSARA_MCP_CONTINUATION_MASTER_KEY")
        key_id = env.get("PULSARA_MCP_CONTINUATION_KEY_ID")
        if encoded is None and key_id is None:
            return None
        return cls.from_environment(env)

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> "McpContinuationKeyProvider":
        env = os.environ if environment is None else environment
        encoded = env.get("PULSARA_MCP_CONTINUATION_MASTER_KEY")
        key_id = env.get("PULSARA_MCP_CONTINUATION_KEY_ID")
        if not encoded or not key_id:
            raise McpContinuationSecretKeyUnavailable(
                "PULSARA_MCP_CONTINUATION_MASTER_KEY and "
                "PULSARA_MCP_CONTINUATION_KEY_ID are required"
            )
        try:
            master = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise McpContinuationSecretKeyUnavailable(
                "MCP continuation master key is not canonical base64"
            ) from exc
        return cls.from_master_key(key_id=key_id, master_key=master)

    @property
    def key_id(self) -> str:
        return self._material.key_id

    def _material_for(self, key_id: str) -> McpContinuationKeyMaterial:
        if not hmac.compare_digest(key_id, self._material.key_id):
            raise McpContinuationSecretKeyUnavailable(
                "MCP continuation envelope requires an unavailable key generation"
            )
        return self._material


@dataclass(frozen=True, slots=True)
class McpContinuationAadContext:
    runtime_session_id: str
    interaction_id: str
    source_event_id: str
    round_ordinal: int
    operation_expires_at_utc: str
    expiry_fingerprint: str
    contract_version: str = "mcp-continuation-aad:v1"

    def __post_init__(self) -> None:
        if (
            not self.runtime_session_id
            or not self.interaction_id
            or not self.source_event_id
        ):
            raise ValueError("MCP continuation AAD identity is incomplete")
        if self.round_ordinal < 1:
            raise ValueError("MCP continuation AAD round must be positive")

    def canonical_bytes(self, *, carrier_id: str, carrier_kind: str) -> bytes:
        return canonical_json_bytes(
            {
                "contract_version": self.contract_version,
                "continuation_carrier_id": carrier_id,
                "carrier_kind": carrier_kind,
                "runtime_session_id": self.runtime_session_id,
                "interaction_id": self.interaction_id,
                "source_event_id": self.source_event_id,
                "round_ordinal": self.round_ordinal,
                "operation_expires_at_utc": self.operation_expires_at_utc,
                "expiry_fingerprint": self.expiry_fingerprint,
            }
        )


@dataclass(frozen=True, slots=True)
class PreparedMcpContinuationEnvelope:
    envelope: McpStoredContinuationEnvelopeFact
    control: McpContinuationCarrierControlFact
    plaintext_commitment: str
    canonical_stored_envelope_bytes: int


@dataclass(frozen=True, slots=True)
class PreparedMcpAwaitingContinuation:
    durable_fact: McpInputRequiredDurableContinuationFact
    stored_record: "McpContinuationStoredRecord"
    plaintext: McpAwaitingInputCarrierPlaintext = field(repr=False)
    retryable_base_params_commitment: str
    request_state_commitment: str | None


@dataclass(frozen=True, slots=True)
class PreparedMcpReplayContinuation:
    stored_record: "McpContinuationStoredRecord"
    plaintext: McpReplayReadyCarrierPlaintext = field(repr=False)
    retryable_base_params_commitment: str


class McpContinuationSecretCodec:
    """The only production codec allowed to reveal sealed carrier plaintext."""

    __slots__ = ("_keys", "_borrows")

    def __init__(self, key_provider: McpContinuationKeyProvider) -> None:
        self._keys = key_provider
        self._borrows = McpContinuationSecretBorrowIssuer(
            f"mcp_continuation_codec:{key_provider.key_id}"
        )

    @property
    def key_id(self) -> str:
        return self._keys.key_id

    def keyed_commitment(self, domain: str, payload: bytes) -> str:
        material = self._keys._material_for(self._keys.key_id)
        return (
            "hmac-sha256:"
            + hmac.new(
                material.commitment_key,
                domain.encode("utf-8") + b"\0" + payload,
                sha256,
            ).hexdigest()
        )

    def response_factory(
        self,
        *,
        bounds: McpContinuationBoundsFact,
    ) -> McpSealedElicitationResponseFactory:
        """Issue the only response freezer bound to this key generation."""

        material = self._keys._material_for(self._keys.key_id)
        return McpSealedElicitationResponseFactory(
            commitment_key_id=material.key_id,
            commitment_key=material.commitment_key,
            bounds=bounds,
        )

    def retryable_payload_commitment(
        self,
        payload: McpRetryableRequestPayload,
        *,
        bounds: McpContinuationBoundsFact | None = None,
    ) -> str:
        borrow = self._borrows.issue(McpSecretAccessPurpose.ENCRYPTION)
        try:
            encoded = borrow.canonical_retry_payload_bytes(payload)
        finally:
            borrow.revoke()
        if (
            bounds is not None
            and len(encoded) > bounds.maximum_retryable_base_params_bytes
        ):
            raise McpContinuationBoundsExceeded(
                "MCP retryable base params exceed their byte bound"
            )
        return self.keyed_commitment("mcp-retryable-base-params:v1", encoded)

    def request_state_commitment(
        self,
        request_state: str | None,
        *,
        bounds: McpContinuationBoundsFact | None = None,
    ) -> str | None:
        if request_state is None:
            return None
        encoded = request_state.encode("utf-8")
        if (
            bounds is not None
            and len(encoded) > bounds.maximum_request_state_utf8_bytes
        ):
            raise McpContinuationBoundsExceeded(
                "MCP requestState exceeds its byte bound"
            )
        return self.keyed_commitment(
            "mcp-request-state:v1",
            encoded,
        )

    def prepare_envelope(
        self,
        *,
        carrier_id: str,
        carrier_kind: str,
        plaintext: McpContinuationCarrierPlaintext,
        aad_context: McpContinuationAadContext,
        bounds: McpContinuationBoundsFact,
        created_at_utc: str,
        initial_state: McpContinuationCarrierState,
        control_revision: int = 1,
    ) -> PreparedMcpContinuationEnvelope:
        if carrier_kind not in {"awaiting_client_input", "replay_ready"}:
            raise ValueError("unsupported MCP continuation carrier kind")
        if initial_state.value != carrier_kind:
            raise ValueError("initial continuation state/kind mismatch")
        if control_revision < 1:
            raise ValueError("MCP continuation control revision must be positive")
        borrow = self._borrows.issue(McpSecretAccessPurpose.ENCRYPTION)
        try:
            try:
                borrow.validate_physical_bounds(plaintext, bounds=bounds)
            except ValueError as exc:
                raise McpContinuationBoundsExceeded(str(exc)) from exc
            plaintext_bytes = borrow.canonical_plaintext_bytes(plaintext)
        finally:
            borrow.revoke()
        if len(plaintext_bytes) > bounds.maximum_plaintext_bytes:
            raise McpContinuationBoundsExceeded(
                "MCP continuation plaintext exceeds bound"
            )
        material = self._keys._material_for(self._keys.key_id)
        commitment = self.keyed_commitment(
            "mcp-carrier-plaintext:v1",
            plaintext_bytes,
        )
        aad = aad_context.canonical_bytes(
            carrier_id=carrier_id,
            carrier_kind=carrier_kind,
        )
        aad_fingerprint = context_fingerprint(
            "mcp-continuation-aad-fingerprint:v1",
            aad.decode("utf-8"),
        )
        nonce = os.urandom(12)
        ciphertext = AESGCM(material.aead_key).encrypt(nonce, plaintext_bytes, aad)
        if len(ciphertext) > bounds.maximum_ciphertext_bytes:
            raise McpContinuationBoundsExceeded(
                "MCP continuation ciphertext exceeds bound"
            )
        envelope = build_mcp_continuation_storage_fact(
            McpStoredContinuationEnvelopeFact,
            schema_version="mcp_stored_continuation_envelope.v1",
            continuation_carrier_id=carrier_id,
            carrier_kind=carrier_kind,
            algorithm="AES-256-GCM",
            key_id=material.key_id,
            nonce_bytes=nonce,
            ciphertext_bytes=ciphertext,
            aad_fingerprint=aad_fingerprint,
            carrier_plaintext_commitment=commitment,
            created_at_utc=created_at_utc,
            operation_expires_at_utc=aad_context.operation_expires_at_utc,
            expiry_fingerprint=aad_context.expiry_fingerprint,
        )
        stored_size = _stored_envelope_size(envelope)
        if stored_size > bounds.maximum_stored_envelope_bytes:
            raise McpContinuationBoundsExceeded(
                "MCP continuation stored envelope exceeds bound"
            )
        control = build_mcp_continuation_storage_fact(
            McpContinuationCarrierControlFact,
            schema_version="mcp_continuation_carrier_control.v1",
            continuation_carrier_id=carrier_id,
            carrier_state=initial_state,
            control_revision=control_revision,
            source_event_id=aad_context.source_event_id,
            stored_envelope_fingerprint=envelope.stored_envelope_fingerprint,
        )
        return PreparedMcpContinuationEnvelope(
            envelope=envelope,
            control=control,
            plaintext_commitment=commitment,
            canonical_stored_envelope_bytes=stored_size,
        )

    def decrypt_and_rebind(
        self,
        *,
        envelope: McpStoredContinuationEnvelopeFact,
        aad_context: McpContinuationAadContext,
        expected_plaintext_commitment: str,
        bounds: McpContinuationBoundsFact,
        plaintext_suspension_event_id: str | None = None,
    ) -> McpContinuationCarrierPlaintext:
        if len(envelope.ciphertext_bytes) > bounds.maximum_ciphertext_bytes:
            raise McpContinuationBoundsExceeded("stored MCP ciphertext exceeds bound")
        if _stored_envelope_size(envelope) > bounds.maximum_stored_envelope_bytes:
            raise McpContinuationBoundsExceeded("stored MCP envelope exceeds bound")
        if not hmac.compare_digest(
            envelope.carrier_plaintext_commitment,
            expected_plaintext_commitment,
        ):
            raise McpContinuationAuthorityConflict(
                "MCP continuation plaintext commitment differs from durable authority"
            )
        material = self._keys._material_for(envelope.key_id)
        aad = aad_context.canonical_bytes(
            carrier_id=envelope.continuation_carrier_id,
            carrier_kind=envelope.carrier_kind,
        )
        expected_aad = context_fingerprint(
            "mcp-continuation-aad-fingerprint:v1",
            aad.decode("utf-8"),
        )
        if envelope.aad_fingerprint != expected_aad:
            raise McpContinuationAuthorityConflict(
                "MCP continuation AAD identity drifted"
            )
        try:
            plaintext_bytes = AESGCM(material.aead_key).decrypt(
                envelope.nonce_bytes,
                envelope.ciphertext_bytes,
                aad,
            )
        except Exception as exc:
            raise McpContinuationDecryptFailed(
                "MCP continuation AEAD verification failed"
            ) from exc
        observed_commitment = self.keyed_commitment(
            "mcp-carrier-plaintext:v1",
            plaintext_bytes,
        )
        if not hmac.compare_digest(observed_commitment, expected_plaintext_commitment):
            raise McpContinuationAuthorityConflict(
                "decrypted MCP continuation plaintext commitment mismatch"
            )
        borrow = self._borrows.issue(McpSecretAccessPurpose.DECRYPTION_REBIND)
        try:
            try:
                plaintext = borrow.decode_plaintext_bytes(plaintext_bytes)
                borrow.validate_carrier_authority(
                    plaintext,
                    runtime_session_id=aad_context.runtime_session_id,
                    interaction_id=aad_context.interaction_id,
                    suspension_event_id=(
                        plaintext_suspension_event_id or aad_context.source_event_id
                    ),
                    round_ordinal=aad_context.round_ordinal,
                    operation_expires_at_utc=aad_context.operation_expires_at_utc,
                    expiry_fingerprint=aad_context.expiry_fingerprint,
                )
            except (TypeError, ValueError) as exc:
                raise McpContinuationAuthorityConflict(
                    "decrypted MCP continuation failed typed rebind"
                ) from exc
            try:
                borrow.validate_physical_bounds(plaintext, bounds=bounds)
            except ValueError as exc:
                raise McpContinuationBoundsExceeded(str(exc)) from exc
        finally:
            borrow.revoke()
        expected_type = (
            McpAwaitingInputCarrierPlaintext
            if envelope.carrier_kind == "awaiting_client_input"
            else McpReplayReadyCarrierPlaintext
        )
        if not isinstance(plaintext, expected_type):
            raise McpContinuationAuthorityConflict(
                "MCP continuation carrier kind drifted"
            )
        return plaintext

    @staticmethod
    def awaiting_recovery_parts(
        plaintext: McpAwaitingInputCarrierPlaintext,
    ) -> tuple[
        McpRetryableRequestPayload,
        str | None,
        tuple[McpPrivateUrlElicitationPayload, ...],
    ]:
        """Return sealed process carriers needed by the exact recovery owner."""

        return (
            plaintext._retryable_request_payload,
            plaintext._request_state,
            plaintext._private_url_requests,
        )

    @staticmethod
    def replay_recovery_parts(
        plaintext: McpReplayReadyCarrierPlaintext,
    ) -> tuple[
        McpRetryableRequestPayload,
        str | None,
        McpFrozenRoundInputResponses,
        str,
        str,
    ]:
        """Return sealed replay carriers without exposing ordinary mappings."""

        return (
            plaintext._retryable_request_payload,
            plaintext._request_state,
            plaintext._current_round_input_responses,
            plaintext._response_attribution_fingerprint,
            plaintext._resolution_event_id,
        )

    @staticmethod
    def recovery_attribution(
        plaintext: McpContinuationCarrierPlaintext,
    ) -> dict[str, object]:
        """Project only non-secret exact-join fields for the recovery owner."""

        return {
            "runtime_session_id": plaintext._runtime_session_id,
            "interaction_id": plaintext._interaction_id,
            "suspension_event_id": plaintext._suspension_event_id,
            "round_ordinal": plaintext._round_ordinal,
            "request_set_fingerprint": plaintext._request_set_fingerprint,
            "protocol_semantic_fingerprint": (plaintext._protocol_semantic_fingerprint),
            "endpoint_attribution_fingerprint": (
                plaintext._endpoint_attribution_fingerprint
            ),
            "auth_attribution_fingerprint": plaintext._auth_attribution_fingerprint,
            "binding_contract_fingerprint": plaintext._binding_contract_fingerprint,
            "created_at_utc": plaintext._created_at_utc,
            "operation_expires_at_utc": plaintext._operation_expires_at_utc,
            "expiry_fingerprint": plaintext._expiry_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class McpContinuationStoredRecord:
    runtime_session_id: str
    interaction_id: str
    source_event_id: str
    round_ordinal: int
    envelope: McpStoredContinuationEnvelopeFact
    control: McpContinuationCarrierControlFact


def prepare_mcp_awaiting_continuation(
    *,
    codec: McpContinuationSecretCodec,
    runtime_session_id: str,
    interaction_id: str,
    suspension_event_id: str,
    round_ordinal: int,
    retryable_request_payload: McpRetryableRequestPayload,
    request_state: str | None,
    request_set_fingerprint: str,
    private_url_requests: tuple[McpPrivateUrlElicitationPayload, ...],
    protocol_semantic_fingerprint: str,
    endpoint_attribution_fingerprint: str,
    auth_attribution_fingerprint: str,
    binding_contract_fingerprint: str,
    first_input_required_observed_at_utc: str,
    created_at_utc: str,
    bounds: McpContinuationBoundsFact,
    configured_ttl_seconds: int = 300,
    inherited_expiry: McpContinuationExpiryFact | None = None,
    predecessor_control_revision: int | None = None,
) -> PreparedMcpAwaitingContinuation:
    """Freeze one immutable awaiting carrier before any durable mutation."""

    if round_ordinal < 1 or round_ordinal > bounds.maximum_rounds:
        raise McpContinuationBoundsExceeded("MCP continuation round exceeds bound")
    if (round_ordinal == 1) != (predecessor_control_revision is None):
        raise ValueError("MCP successor control lineage is incomplete")
    if inherited_expiry is None:
        resolved_ttl = min(configured_ttl_seconds, bounds.maximum_ttl_seconds)
        if resolved_ttl < 1:
            raise ValueError("MCP continuation TTL must be positive")
        observed = _parse_utc(first_input_required_observed_at_utc)
        expiry_payload = {
            "schema_version": "mcp_continuation_expiry.v1",
            "first_input_required_observed_at_utc": _canonical_utc(observed),
            "resolved_operation_ttl_seconds": resolved_ttl,
            "operation_expires_at_utc": _canonical_utc(
                observed + timedelta(seconds=resolved_ttl)
            ),
            "expiry_policy_fingerprint": context_fingerprint(
                "mcp-continuation-expiry-policy:v1",
                {
                    "configured_ttl_seconds": configured_ttl_seconds,
                    "maximum_ttl_seconds": bounds.maximum_ttl_seconds,
                    "renewal": "forbidden",
                },
            ),
        }
        expiry = build_mcp_continuation_fact(
            McpContinuationExpiryFact,
            **expiry_payload,
        )
    else:
        expiry = inherited_expiry
        if (
            expiry.resolved_operation_ttl_seconds > bounds.maximum_ttl_seconds
            or _parse_utc(created_at_utc) >= _parse_utc(expiry.operation_expires_at_utc)
        ):
            raise McpContinuationBoundsExceeded("MCP successor continuation is expired")
    if _parse_utc(created_at_utc) >= _parse_utc(expiry.operation_expires_at_utc):
        raise McpContinuationBoundsExceeded("MCP continuation is expired")
    retry_commitment = codec.retryable_payload_commitment(
        retryable_request_payload,
        bounds=bounds,
    )
    state_commitment = codec.request_state_commitment(
        request_state,
        bounds=bounds,
    )
    carrier_id = context_fingerprint(
        "mcp-continuation-awaiting-carrier:v1",
        {
            "runtime_session_id": runtime_session_id,
            "interaction_id": interaction_id,
            "suspension_event_id": suspension_event_id,
            "round_ordinal": round_ordinal,
            "binding_contract_fingerprint": binding_contract_fingerprint,
        },
    )
    plaintext = build_awaiting_input_carrier_plaintext(
        runtime_session_id=runtime_session_id,
        interaction_id=interaction_id,
        suspension_event_id=suspension_event_id,
        round_ordinal=round_ordinal,
        retryable_request_payload=retryable_request_payload,
        request_state=request_state,
        request_set_fingerprint=request_set_fingerprint,
        private_url_requests=private_url_requests,
        protocol_semantic_fingerprint=protocol_semantic_fingerprint,
        endpoint_attribution_fingerprint=endpoint_attribution_fingerprint,
        auth_attribution_fingerprint=auth_attribution_fingerprint,
        binding_contract_fingerprint=binding_contract_fingerprint,
        created_at_utc=created_at_utc,
        operation_expires_at_utc=expiry.operation_expires_at_utc,
        expiry_fingerprint=expiry.expiry_fingerprint,
    )
    aad = McpContinuationAadContext(
        runtime_session_id=runtime_session_id,
        interaction_id=interaction_id,
        source_event_id=suspension_event_id,
        round_ordinal=round_ordinal,
        operation_expires_at_utc=expiry.operation_expires_at_utc,
        expiry_fingerprint=expiry.expiry_fingerprint,
    )
    prepared = codec.prepare_envelope(
        carrier_id=carrier_id,
        carrier_kind="awaiting_client_input",
        plaintext=plaintext,
        aad_context=aad,
        bounds=bounds,
        created_at_utc=created_at_utc,
        initial_state=McpContinuationCarrierState.AWAITING_CLIENT_INPUT,
        control_revision=(predecessor_control_revision or 0) + 1,
    )
    durable = build_mcp_continuation_fact(
        McpInputRequiredDurableContinuationFact,
        schema_version="mcp_input_required_durable_continuation.v1",
        continuation_carrier_id=carrier_id,
        initial_carrier_state="awaiting_client_input",
        carrier_plaintext_commitment=prepared.plaintext_commitment,
        retryable_base_params_commitment=retry_commitment,
        request_state_commitment=state_commitment,
        retryable_payload_kind=retryable_request_payload.payload_kind,
        source_method=retryable_request_payload.source_method,
        source_method_schema_fingerprint=(
            retryable_request_payload.source_method_schema_fingerprint
        ),
        request_set_fingerprint=request_set_fingerprint,
        stored_envelope_fingerprint=(prepared.envelope.stored_envelope_fingerprint),
        commitment_key_id=codec.key_id,
        bounds=bounds,
        protocol_semantic_fingerprint=protocol_semantic_fingerprint,
        endpoint_attribution_fingerprint=endpoint_attribution_fingerprint,
        auth_attribution_fingerprint=auth_attribution_fingerprint,
        binding_contract_fingerprint=binding_contract_fingerprint,
        round_ordinal=round_ordinal,
        expiry=expiry,
    )
    return PreparedMcpAwaitingContinuation(
        durable_fact=durable,
        stored_record=McpContinuationStoredRecord(
            runtime_session_id=runtime_session_id,
            interaction_id=interaction_id,
            source_event_id=suspension_event_id,
            round_ordinal=round_ordinal,
            envelope=prepared.envelope,
            control=prepared.control,
        ),
        plaintext=plaintext,
        retryable_base_params_commitment=retry_commitment,
        request_state_commitment=state_commitment,
    )


def prepare_mcp_replay_continuation(
    *,
    codec: McpContinuationSecretCodec,
    source: PreparedMcpAwaitingContinuation | McpContinuationStoredRecord,
    source_plaintext: McpAwaitingInputCarrierPlaintext,
    resolution_event_id: str,
    current_round_input_responses: object,
    created_at_utc: str,
    bounds: McpContinuationBoundsFact,
) -> PreparedMcpReplayContinuation:
    from pulsara_agent.ports.mcp_secret import McpFrozenRoundInputResponses

    if not isinstance(current_round_input_responses, McpFrozenRoundInputResponses):
        raise TypeError("MCP replay preparation requires sealed round responses")
    record = (
        source.stored_record
        if isinstance(source, PreparedMcpAwaitingContinuation)
        else source
    )
    awaiting = source_plaintext
    if (
        record.control.carrier_state
        is not McpContinuationCarrierState.AWAITING_CLIENT_INPUT
    ):
        raise McpContinuationAuthorityConflict(
            "MCP source carrier is not awaiting input"
        )
    if _parse_utc(created_at_utc) >= _parse_utc(awaiting._operation_expires_at_utc):
        raise McpContinuationBoundsExceeded("MCP continuation is expired")
    if (
        current_round_input_responses.request_set_fingerprint
        != awaiting._request_set_fingerprint
    ):
        raise McpContinuationAuthorityConflict("MCP response/request set mismatch")
    replay_id = context_fingerprint(
        "mcp-continuation-replay-carrier:v1",
        {
            "runtime_session_id": record.runtime_session_id,
            "interaction_id": record.interaction_id,
            "suspension_event_id": record.source_event_id,
            "resolution_event_id": resolution_event_id,
            "round_ordinal": record.round_ordinal,
            "binding_contract_fingerprint": awaiting._binding_contract_fingerprint,
        },
    )
    replay = build_replay_ready_carrier_plaintext(
        runtime_session_id=record.runtime_session_id,
        interaction_id=record.interaction_id,
        suspension_event_id=record.source_event_id,
        resolution_event_id=resolution_event_id,
        round_ordinal=record.round_ordinal,
        retryable_request_payload=awaiting._retryable_request_payload,
        request_state=awaiting._request_state,
        current_round_input_responses=current_round_input_responses,
        request_set_fingerprint=awaiting._request_set_fingerprint,
        protocol_semantic_fingerprint=awaiting._protocol_semantic_fingerprint,
        endpoint_attribution_fingerprint=awaiting._endpoint_attribution_fingerprint,
        auth_attribution_fingerprint=awaiting._auth_attribution_fingerprint,
        binding_contract_fingerprint=awaiting._binding_contract_fingerprint,
        created_at_utc=created_at_utc,
        operation_expires_at_utc=awaiting._operation_expires_at_utc,
        expiry_fingerprint=awaiting._expiry_fingerprint,
    )
    aad = McpContinuationAadContext(
        runtime_session_id=record.runtime_session_id,
        interaction_id=record.interaction_id,
        source_event_id=record.source_event_id,
        round_ordinal=record.round_ordinal,
        operation_expires_at_utc=awaiting._operation_expires_at_utc,
        expiry_fingerprint=awaiting._expiry_fingerprint,
    )
    prepared = codec.prepare_envelope(
        carrier_id=replay_id,
        carrier_kind="replay_ready",
        plaintext=replay,
        aad_context=aad,
        bounds=bounds,
        created_at_utc=created_at_utc,
        initial_state=McpContinuationCarrierState.REPLAY_READY,
        control_revision=record.control.control_revision + 1,
    )
    replay_control = build_mcp_continuation_storage_fact(
        McpContinuationCarrierControlFact,
        schema_version="mcp_continuation_carrier_control.v1",
        continuation_carrier_id=replay_id,
        carrier_state=McpContinuationCarrierState.REPLAY_READY,
        control_revision=record.control.control_revision + 1,
        source_event_id=resolution_event_id,
        stored_envelope_fingerprint=prepared.envelope.stored_envelope_fingerprint,
    )
    return PreparedMcpReplayContinuation(
        stored_record=McpContinuationStoredRecord(
            runtime_session_id=record.runtime_session_id,
            interaction_id=record.interaction_id,
            source_event_id=resolution_event_id,
            round_ordinal=record.round_ordinal,
            envelope=prepared.envelope,
            control=replay_control,
        ),
        plaintext=replay,
        retryable_base_params_commitment=codec.retryable_payload_commitment(
            awaiting._retryable_request_payload,
            bounds=bounds,
        ),
    )


class PostgresMcpContinuationSecretStore:
    """Read owner. Production mutation is available only through companions."""

    __slots__ = ("connection_provider",)

    def __init__(
        self, connection_provider: VerifiedPostgresConnectionProviderProtocol
    ) -> None:
        self.connection_provider = connection_provider

    def read(
        self,
        carrier_id: str,
        *,
        deadline_monotonic: float | None = None,
    ) -> McpContinuationStoredRecord | None:
        deadline = postgres_operation_deadline(deadline_monotonic)
        with self.connection_provider.connection(
            lane=PostgresConnectionLane.EVENT_LOG,
            row_factory=dict_row,
            deadline_monotonic=deadline,
        ) as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                _apply_statement_deadline(cursor, deadline)
                row = cursor.execute(
                    """
                    SELECT *
                    FROM mcp_continuation_secret_carriers
                    WHERE continuation_carrier_id = %s
                    """,
                    (carrier_id,),
                ).fetchone()
        return _record_from_row(row) if row is not None else None


@dataclass(slots=True)
class InMemoryMcpContinuationSecretStore:
    _records: dict[str, McpContinuationStoredRecord] = field(default_factory=dict)
    _lock: RLock = field(default_factory=RLock, repr=False)

    def read(
        self,
        carrier_id: str,
        *,
        deadline_monotonic: float | None = None,
    ) -> McpContinuationStoredRecord | None:
        del deadline_monotonic
        with self._lock:
            return self._records.get(carrier_id)


McpContinuationRepository: TypeAlias = (
    PostgresMcpContinuationSecretStore | InMemoryMcpContinuationSecretStore
)


@dataclass(slots=True)
class McpContinuationTransactionIntent:
    """Stable mutation intent bound only after the complete event batch exists."""

    companion_kind: McpContinuationCompanionKind
    mutation_kind: McpContinuationMutationKind
    runtime_session_id: str
    interaction_id: str
    round_ordinal: int
    source_event_id: str
    repository: McpContinuationRepository
    issuer_id: str
    issuer_generation: int
    source_carrier_id: str | None = None
    resulting_record: McpContinuationStoredRecord | None = None
    expected_control: McpContinuationCarrierControlFact | None = None
    resulting_control: McpContinuationCarrierControlFact | None = None
    charged_payload_bytes: int = 0
    charge_contract_fingerprint: str = ""
    storage_mutation_plan_fingerprint: str = ""
    _bound: "McpContinuationTransactionCompanion | None" = field(
        default=None,
        init=False,
        repr=False,
    )
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        if (
            not self.runtime_session_id
            or not self.interaction_id
            or not self.source_event_id
            or not self.issuer_id
            or self.issuer_generation < 1
            or self.round_ordinal < 1
            or self.charged_payload_bytes < 0
            or not self.charge_contract_fingerprint
        ):
            raise ValueError("MCP continuation transaction intent is incomplete")
        expected_plan = _storage_mutation_plan_fingerprint(
            mutation_kind=self.mutation_kind,
            runtime_session_id=self.runtime_session_id,
            interaction_id=self.interaction_id,
            round_ordinal=self.round_ordinal,
            source_event_id=self.source_event_id,
            source_carrier_id=self.source_carrier_id,
            resulting_record=self.resulting_record,
            expected_control=self.expected_control,
            resulting_control=self.resulting_control,
        )
        if self.storage_mutation_plan_fingerprint != expected_plan:
            raise ValueError("MCP continuation storage mutation plan drifted")
        _validate_intent_mutation_shape(self)

    def bind_candidate_batch(
        self,
        candidates: Sequence[FrozenEventWriteCandidate],
    ) -> "McpContinuationTransactionCompanion":
        prepared = build_prepared_candidate_batch_identity(candidates)
        if self.source_event_id not in prepared.ordered_candidate_event_ids:
            raise ValueError("MCP continuation source event is absent from full batch")
        with self._lock:
            if self._bound is not None:
                if self._bound.prepared_candidate_batch_identity != prepared:
                    raise ValueError(
                        "MCP continuation intent cannot bind a different event batch"
                    )
                return self._bound
            charge = build_mcp_continuation_fact(
                McpContinuationCompanionChargeFact,
                schema_version="mcp_continuation_companion_charge.v1",
                companion_kind=self.companion_kind,
                charged_payload_bytes=self.charged_payload_bytes,
                charge_contract_fingerprint=self.charge_contract_fingerprint,
                storage_mutation_plan_fingerprint=(
                    self.storage_mutation_plan_fingerprint
                ),
            )
            expected = self.expected_control
            resulting = self.resulting_control
            if resulting is None and self.resulting_record is not None:
                resulting = self.resulting_record.control
            plan = build_mcp_continuation_fact(
                McpContinuationCompanionPlanFact,
                schema_version="mcp_continuation_companion_plan.v1",
                companion_kind=self.companion_kind,
                runtime_session_id=self.runtime_session_id,
                source_event_id=self.source_event_id,
                source_continuation_carrier_id=self.source_carrier_id,
                resulting_continuation_carrier_id=(
                    self.resulting_record.envelope.continuation_carrier_id
                    if self.resulting_record is not None
                    else self.source_carrier_id
                    if self.mutation_kind
                    is McpContinuationMutationKind.RESERVE_DISPATCH
                    else None
                ),
                expected_row_state=(
                    expected.carrier_state if expected is not None else None
                ),
                resulting_row_state=(
                    resulting.carrier_state if resulting is not None else None
                ),
                expected_control_revision=(
                    expected.control_revision if expected is not None else None
                ),
                expected_control_fingerprint=(
                    expected.control_fingerprint if expected is not None else None
                ),
                resulting_control_revision=(
                    resulting.control_revision if resulting is not None else None
                ),
                resulting_control_fingerprint=(
                    resulting.control_fingerprint if resulting is not None else None
                ),
                source_stored_envelope_fingerprint=(
                    expected.stored_envelope_fingerprint
                    if expected is not None
                    else None
                ),
                resulting_stored_envelope_fingerprint=(
                    self.resulting_record.envelope.stored_envelope_fingerprint
                    if self.resulting_record is not None
                    else expected.stored_envelope_fingerprint
                    if (
                        expected is not None
                        and self.mutation_kind
                        is McpContinuationMutationKind.RESERVE_DISPATCH
                    )
                    else None
                ),
                ordered_candidate_event_ids=prepared.ordered_candidate_event_ids,
                ordered_candidate_schema_binding_fingerprints=(
                    prepared.ordered_candidate_schema_binding_fingerprints
                ),
                ordered_candidate_payload_fingerprints=(
                    prepared.ordered_candidate_payload_fingerprints
                ),
                exact_ordered_batch_fingerprint=(
                    prepared.exact_ordered_batch_fingerprint
                ),
                charge=charge,
            )
            companion_id = context_fingerprint(
                "mcp-continuation-companion-id:v1",
                {
                    "storage_mutation_plan_fingerprint": (
                        self.storage_mutation_plan_fingerprint
                    ),
                    "exact_ordered_batch_fingerprint": (
                        prepared.exact_ordered_batch_fingerprint
                    ),
                },
            )
            companion = McpContinuationTransactionCompanion(
                identity=build_prepared_companion_identity(
                    companion_id=companion_id,
                    plan=plan,
                    issuer_id=self.issuer_id,
                    issuer_generation=self.issuer_generation,
                    prepared=prepared,
                ),
                plan=plan,
                prepared_candidate_batch_identity=prepared,
                mutation_kind=self.mutation_kind,
                runtime_session_id=self.runtime_session_id,
                interaction_id=self.interaction_id,
                round_ordinal=self.round_ordinal,
                repository=self.repository,
                source_carrier_id=self.source_carrier_id,
                resulting_record=self.resulting_record,
                expected_control=self.expected_control,
                resulting_control=self.resulting_control,
            )
            self._bound = companion
            return companion


@dataclass(slots=True)
class McpContinuationTransactionCompanion:
    identity: McpPreparedCompanionIdentity
    plan: McpContinuationCompanionPlanFact
    prepared_candidate_batch_identity: EventLogPreparedCandidateBatchIdentity
    mutation_kind: McpContinuationMutationKind
    runtime_session_id: str
    interaction_id: str
    round_ordinal: int
    repository: PostgresMcpContinuationSecretStore | InMemoryMcpContinuationSecretStore
    source_carrier_id: str | None = None
    resulting_record: McpContinuationStoredRecord | None = None
    expected_control: McpContinuationCarrierControlFact | None = None
    resulting_control: McpContinuationCarrierControlFact | None = None
    _stored_rebind_receipt: EventLogStoredCandidateBatchRebindReceipt | None = field(
        default=None,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        prepared = self.prepared_candidate_batch_identity
        if (
            self.identity.plan_fingerprint != self.plan.plan_fingerprint
            or self.identity.companion_kind is not self.plan.companion_kind
            or self.identity.ordered_candidate_event_ids
            != prepared.ordered_candidate_event_ids
            or self.identity.ordered_candidate_schema_binding_fingerprints
            != prepared.ordered_candidate_schema_binding_fingerprints
            or self.identity.ordered_candidate_payload_fingerprints
            != prepared.ordered_candidate_payload_fingerprints
            or self.identity.exact_ordered_batch_fingerprint
            != prepared.exact_ordered_batch_fingerprint
            or self.plan.exact_ordered_batch_fingerprint
            != prepared.exact_ordered_batch_fingerprint
        ):
            raise ValueError("MCP companion exact event batch identity mismatch")
        if self.plan.runtime_session_id != self.runtime_session_id:
            raise ValueError("MCP companion runtime session mismatch")
        self._validate_mutation_shape()

    @property
    def companion_kind(self) -> McpContinuationCompanionKind:
        return self.plan.companion_kind

    @property
    def charged_payload_bytes(self) -> int:
        return self.plan.charge.charged_payload_bytes

    @property
    def charge_contract_fingerprint(self) -> str:
        return self.plan.charge.charge_contract_fingerprint

    def accept_stored_candidate_rebind_receipt(
        self,
        receipt: EventLogStoredCandidateBatchRebindReceipt,
    ) -> None:
        if receipt.exact_ordered_batch_fingerprint != (
            self.prepared_candidate_batch_identity.exact_ordered_batch_fingerprint
        ):
            raise ValueError(
                "MCP companion stored rebind receipt belongs to another batch"
            )
        if (
            self._stored_rebind_receipt is not None
            and self._stored_rebind_receipt != receipt
        ):
            raise ValueError(
                "MCP companion received conflicting stored rebind receipts"
            )
        self._stored_rebind_receipt = receipt

    def apply_postgres(self, cursor: Any, stored_events: Sequence[AgentEvent]) -> None:
        if not isinstance(self.repository, PostgresMcpContinuationSecretStore):
            raise TypeError("in-memory MCP store cannot join PostgreSQL transaction")
        self._require_stored_batch(stored_events)
        self._apply_postgres_mutation(cursor)

    def apply_in_memory(self, stored_events: Sequence[AgentEvent]) -> None:
        if not isinstance(self.repository, InMemoryMcpContinuationSecretStore):
            raise TypeError("PostgreSQL MCP store cannot join in-memory transaction")
        self._require_stored_batch(stored_events)
        with self.repository._lock:
            next_rows = dict(self.repository._records)
            self._apply_mapping_mutation(next_rows)
            self.repository._records = next_rows

    def _require_stored_batch(self, stored_events: Sequence[AgentEvent]) -> None:
        receipt = self._stored_rebind_receipt
        if receipt is None:
            raise RuntimeError("MCP companion lacks stored-candidate rebind proof")
        event_ids = tuple(event.id for event in stored_events)
        if event_ids != receipt.ordered_event_ids:
            raise ValueError("MCP companion stored event order changed after rebind")

    def _validate_mutation_shape(self) -> None:
        if self.mutation_kind is McpContinuationMutationKind.INSERT_AWAITING:
            if (
                self.source_carrier_id is not None
                or self.resulting_record is None
                or self.expected_control is not None
            ):
                raise ValueError("MCP awaiting insert mutation shape mismatch")
        elif self.mutation_kind in {
            McpContinuationMutationKind.REPLACE_WITH_REPLAY_READY,
            McpContinuationMutationKind.REPLACE_WITH_SUCCESSOR,
        }:
            if (
                self.source_carrier_id is None
                or self.resulting_record is None
                or self.expected_control is None
            ):
                raise ValueError("MCP replacement mutation shape mismatch")
        elif self.mutation_kind is McpContinuationMutationKind.RESERVE_DISPATCH:
            if (
                self.source_carrier_id is None
                or self.resulting_record is not None
                or self.expected_control is None
                or self.resulting_control is None
            ):
                raise ValueError("MCP dispatch reservation mutation shape mismatch")
        elif self.mutation_kind is McpContinuationMutationKind.DELETE_TERMINAL:
            if (
                self.source_carrier_id is None
                or self.resulting_record is not None
                or self.expected_control is None
            ):
                raise ValueError("MCP terminal delete mutation shape mismatch")

    def _apply_postgres_mutation(self, cursor: Any) -> None:
        if self.mutation_kind is McpContinuationMutationKind.INSERT_AWAITING:
            assert self.resulting_record is not None
            _insert_record_postgres(cursor, self.resulting_record)
            return
        assert self.source_carrier_id is not None
        source = cursor.execute(
            """
            SELECT * FROM mcp_continuation_secret_carriers
            WHERE continuation_carrier_id = %s
            FOR UPDATE
            """,
            (self.source_carrier_id,),
        ).fetchone()
        if source is None:
            if self.mutation_kind is McpContinuationMutationKind.DELETE_TERMINAL:
                return
            raise McpContinuationAuthorityConflict(
                "MCP source continuation row is absent"
            )
        observed = _record_from_row(source)
        if (
            self.expected_control is not None
            and observed.control != self.expected_control
        ):
            raise McpContinuationAuthorityConflict(
                "MCP continuation control CAS failed"
            )
        if self.mutation_kind is McpContinuationMutationKind.RESERVE_DISPATCH:
            assert self.resulting_control is not None
            cursor.execute(
                """
                UPDATE mcp_continuation_secret_carriers
                SET carrier_state = %s,
                    control_revision = %s,
                    source_event_id = %s,
                    control_fingerprint = %s
                WHERE continuation_carrier_id = %s
                """,
                (
                    self.resulting_control.carrier_state.value,
                    self.resulting_control.control_revision,
                    self.resulting_control.source_event_id,
                    self.resulting_control.control_fingerprint,
                    self.source_carrier_id,
                ),
            )
            return
        cursor.execute(
            "DELETE FROM mcp_continuation_secret_carriers "
            "WHERE continuation_carrier_id = %s",
            (self.source_carrier_id,),
        )
        if self.resulting_record is not None:
            _insert_record_postgres(cursor, self.resulting_record)

    def _apply_mapping_mutation(
        self,
        records: dict[str, McpContinuationStoredRecord],
    ) -> None:
        if self.mutation_kind is McpContinuationMutationKind.INSERT_AWAITING:
            assert self.resulting_record is not None
            _insert_mapping(records, self.resulting_record)
            return
        assert self.source_carrier_id is not None
        observed = records.get(self.source_carrier_id)
        if observed is None:
            if self.mutation_kind is McpContinuationMutationKind.DELETE_TERMINAL:
                return
            raise McpContinuationAuthorityConflict(
                "MCP source continuation row is absent"
            )
        if (
            self.expected_control is not None
            and observed.control != self.expected_control
        ):
            raise McpContinuationAuthorityConflict(
                "MCP continuation control CAS failed"
            )
        if self.mutation_kind is McpContinuationMutationKind.RESERVE_DISPATCH:
            assert self.resulting_control is not None
            records[self.source_carrier_id] = McpContinuationStoredRecord(
                runtime_session_id=observed.runtime_session_id,
                interaction_id=observed.interaction_id,
                source_event_id=self.resulting_control.source_event_id,
                round_ordinal=observed.round_ordinal,
                envelope=observed.envelope,
                control=self.resulting_control,
            )
            return
        del records[self.source_carrier_id]
        if self.resulting_record is not None:
            _insert_mapping(records, self.resulting_record)


def _insert_mapping(
    records: dict[str, McpContinuationStoredRecord],
    record: McpContinuationStoredRecord,
) -> None:
    key = record.envelope.continuation_carrier_id
    existing = records.get(key)
    if existing is not None and existing != record:
        if existing.envelope.carrier_plaintext_commitment != (
            record.envelope.carrier_plaintext_commitment
        ):
            raise McpContinuationAuthorityConflict(
                "MCP continuation ID names different plaintext"
            )
        raise McpContinuationAuthorityConflict(
            "MCP continuation ID names a different encrypted envelope"
        )
    records[key] = record


def _insert_record_postgres(cursor: Any, record: McpContinuationStoredRecord) -> None:
    envelope = record.envelope
    control = record.control
    row = cursor.execute(
        """
        SELECT * FROM mcp_continuation_secret_carriers
        WHERE continuation_carrier_id = %s
        FOR UPDATE
        """,
        (envelope.continuation_carrier_id,),
    ).fetchone()
    if row is not None:
        existing = _record_from_row(row)
        if existing == record:
            return
        if existing.envelope.carrier_plaintext_commitment != (
            envelope.carrier_plaintext_commitment
        ):
            raise McpContinuationAuthorityConflict(
                "MCP continuation ID names different plaintext"
            )
        raise McpContinuationAuthorityConflict(
            "MCP continuation ID names a different encrypted envelope"
        )
    cursor.execute(
        """
        INSERT INTO mcp_continuation_secret_carriers (
            continuation_carrier_id,
            runtime_session_id,
            interaction_id,
            round_ordinal,
            carrier_kind,
            algorithm,
            key_id,
            nonce_bytes,
            ciphertext_bytes,
            aad_fingerprint,
            carrier_plaintext_commitment,
            stored_envelope_fingerprint,
            carrier_state,
            control_revision,
            source_event_id,
            control_fingerprint,
            created_at_utc,
            operation_expires_at_utc,
            expiry_fingerprint
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
        """,
        (
            envelope.continuation_carrier_id,
            record.runtime_session_id,
            record.interaction_id,
            record.round_ordinal,
            envelope.carrier_kind,
            envelope.algorithm,
            envelope.key_id,
            envelope.nonce_bytes,
            envelope.ciphertext_bytes,
            envelope.aad_fingerprint,
            envelope.carrier_plaintext_commitment,
            envelope.stored_envelope_fingerprint,
            control.carrier_state.value,
            control.control_revision,
            control.source_event_id,
            control.control_fingerprint,
            envelope.created_at_utc,
            envelope.operation_expires_at_utc,
            envelope.expiry_fingerprint,
        ),
    )


def _record_from_row(row: Mapping[str, Any]) -> McpContinuationStoredRecord:
    envelope = build_mcp_continuation_storage_fact(
        McpStoredContinuationEnvelopeFact,
        schema_version="mcp_stored_continuation_envelope.v1",
        continuation_carrier_id=str(row["continuation_carrier_id"]),
        carrier_kind=str(row["carrier_kind"]),
        algorithm=str(row["algorithm"]),
        key_id=str(row["key_id"]),
        nonce_bytes=bytes(row["nonce_bytes"]),
        ciphertext_bytes=bytes(row["ciphertext_bytes"]),
        aad_fingerprint=str(row["aad_fingerprint"]),
        carrier_plaintext_commitment=str(row["carrier_plaintext_commitment"]),
        created_at_utc=_canonical_utc(row["created_at_utc"]),
        operation_expires_at_utc=_canonical_utc(row["operation_expires_at_utc"]),
        expiry_fingerprint=str(row["expiry_fingerprint"]),
    )
    if envelope.stored_envelope_fingerprint != str(row["stored_envelope_fingerprint"]):
        raise McpContinuationAuthorityConflict(
            "stored MCP envelope fingerprint drifted"
        )
    control = build_mcp_continuation_storage_fact(
        McpContinuationCarrierControlFact,
        schema_version="mcp_continuation_carrier_control.v1",
        continuation_carrier_id=envelope.continuation_carrier_id,
        carrier_state=McpContinuationCarrierState(str(row["carrier_state"])),
        control_revision=int(row["control_revision"]),
        source_event_id=str(row["source_event_id"]),
        stored_envelope_fingerprint=envelope.stored_envelope_fingerprint,
    )
    if control.control_fingerprint != str(row["control_fingerprint"]):
        raise McpContinuationAuthorityConflict("stored MCP control fingerprint drifted")
    return McpContinuationStoredRecord(
        runtime_session_id=str(row["runtime_session_id"]),
        interaction_id=str(row["interaction_id"]),
        source_event_id=control.source_event_id,
        round_ordinal=int(row["round_ordinal"]),
        envelope=envelope,
        control=control,
    )


def build_prepared_companion_identity(
    *,
    companion_id: str,
    plan: McpContinuationCompanionPlanFact,
    issuer_id: str,
    issuer_generation: int,
    prepared: EventLogPreparedCandidateBatchIdentity,
) -> McpPreparedCompanionIdentity:
    payload = {
        "companion_id": companion_id,
        "companion_kind": plan.companion_kind,
        "plan_fingerprint": plan.plan_fingerprint,
        "issuer_id": issuer_id,
        "issuer_generation": issuer_generation,
        "ordered_candidate_event_ids": prepared.ordered_candidate_event_ids,
        "ordered_candidate_schema_binding_fingerprints": (
            prepared.ordered_candidate_schema_binding_fingerprints
        ),
        "ordered_candidate_payload_fingerprints": (
            prepared.ordered_candidate_payload_fingerprints
        ),
        "exact_ordered_batch_fingerprint": prepared.exact_ordered_batch_fingerprint,
    }
    return McpPreparedCompanionIdentity(
        **payload,
        identity_fingerprint=context_fingerprint(
            "mcp-prepared-companion-identity:v1",
            payload,
        ),
    )


def build_mcp_continuation_transaction_intent(
    *,
    companion_kind: McpContinuationCompanionKind,
    mutation_kind: McpContinuationMutationKind,
    runtime_session_id: str,
    interaction_id: str,
    round_ordinal: int,
    source_event_id: str,
    repository: PostgresMcpContinuationSecretStore | InMemoryMcpContinuationSecretStore,
    issuer_id: str,
    issuer_generation: int,
    charge_contract_fingerprint: str,
    source_carrier_id: str | None = None,
    resulting_record: McpContinuationStoredRecord | None = None,
    expected_control: McpContinuationCarrierControlFact | None = None,
    resulting_control: McpContinuationCarrierControlFact | None = None,
) -> McpContinuationTransactionIntent:
    expected_kind = {
        McpContinuationMutationKind.INSERT_AWAITING: (
            McpContinuationCompanionKind.SUSPENSION_INSERT
        ),
        McpContinuationMutationKind.REPLACE_WITH_REPLAY_READY: (
            McpContinuationCompanionKind.RESOLUTION_REPLAY_READY
        ),
        McpContinuationMutationKind.RESERVE_DISPATCH: (
            McpContinuationCompanionKind.DISPATCH_RESERVE
        ),
        McpContinuationMutationKind.REPLACE_WITH_SUCCESSOR: (
            McpContinuationCompanionKind.SUCCESSOR_REPLACE
        ),
        McpContinuationMutationKind.DELETE_TERMINAL: (
            McpContinuationCompanionKind.TERMINAL_DELETE
        ),
    }[mutation_kind]
    if companion_kind is not expected_kind:
        raise ValueError("MCP continuation companion/mutation kind mismatch")
    charged_payload_bytes = (
        _stored_envelope_size(resulting_record.envelope)
        if resulting_record is not None
        else 0
    )
    storage_plan = _storage_mutation_plan_fingerprint(
        mutation_kind=mutation_kind,
        runtime_session_id=runtime_session_id,
        interaction_id=interaction_id,
        round_ordinal=round_ordinal,
        source_event_id=source_event_id,
        source_carrier_id=source_carrier_id,
        resulting_record=resulting_record,
        expected_control=expected_control,
        resulting_control=resulting_control,
    )
    return McpContinuationTransactionIntent(
        companion_kind=companion_kind,
        mutation_kind=mutation_kind,
        runtime_session_id=runtime_session_id,
        interaction_id=interaction_id,
        round_ordinal=round_ordinal,
        source_event_id=source_event_id,
        repository=repository,
        issuer_id=issuer_id,
        issuer_generation=issuer_generation,
        source_carrier_id=source_carrier_id,
        resulting_record=resulting_record,
        expected_control=expected_control,
        resulting_control=resulting_control,
        charged_payload_bytes=charged_payload_bytes,
        charge_contract_fingerprint=charge_contract_fingerprint,
        storage_mutation_plan_fingerprint=storage_plan,
    )


def _validate_intent_mutation_shape(
    intent: McpContinuationTransactionIntent,
) -> None:
    source = intent.source_carrier_id
    result = intent.resulting_record
    expected = intent.expected_control
    resulting = intent.resulting_control
    if intent.mutation_kind is McpContinuationMutationKind.INSERT_AWAITING:
        valid = source is None and result is not None and expected is None
    elif intent.mutation_kind in {
        McpContinuationMutationKind.REPLACE_WITH_REPLAY_READY,
        McpContinuationMutationKind.REPLACE_WITH_SUCCESSOR,
    }:
        valid = source is not None and result is not None and expected is not None
    elif intent.mutation_kind is McpContinuationMutationKind.RESERVE_DISPATCH:
        valid = (
            source is not None
            and result is None
            and expected is not None
            and resulting is not None
        )
    else:
        valid = source is not None and result is None and expected is not None
    if not valid:
        raise ValueError("MCP continuation intent mutation shape mismatch")
    if expected is not None and source != expected.continuation_carrier_id:
        raise ValueError("MCP continuation source/control identity mismatch")
    effective_resulting = resulting or (result.control if result is not None else None)
    if effective_resulting is not None and (
        effective_resulting.source_event_id != intent.source_event_id
    ):
        raise ValueError("MCP continuation result control names another event")
    if result is not None:
        if (
            result.runtime_session_id != intent.runtime_session_id
            or result.interaction_id != intent.interaction_id
            or result.round_ordinal != intent.round_ordinal
            or result.source_event_id != intent.source_event_id
        ):
            raise ValueError("MCP continuation resulting record identity mismatch")
    expected_charge = (
        _stored_envelope_size(result.envelope) if result is not None else 0
    )
    if intent.charged_payload_bytes != expected_charge:
        raise ValueError("MCP continuation companion storage charge mismatch")


def _storage_mutation_plan_fingerprint(
    *,
    mutation_kind: McpContinuationMutationKind,
    runtime_session_id: str,
    interaction_id: str,
    round_ordinal: int,
    source_event_id: str,
    source_carrier_id: str | None,
    resulting_record: McpContinuationStoredRecord | None,
    expected_control: McpContinuationCarrierControlFact | None,
    resulting_control: McpContinuationCarrierControlFact | None,
) -> str:
    return context_fingerprint(
        "mcp-continuation-storage-mutation-plan:v1",
        {
            "mutation_kind": mutation_kind.value,
            "runtime_session_id": runtime_session_id,
            "interaction_id": interaction_id,
            "round_ordinal": round_ordinal,
            "source_event_id": source_event_id,
            "source_carrier_id": source_carrier_id,
            "expected_control_fingerprint": (
                expected_control.control_fingerprint
                if expected_control is not None
                else None
            ),
            "resulting_carrier_id": (
                resulting_record.envelope.continuation_carrier_id
                if resulting_record is not None
                else source_carrier_id
                if resulting_control is not None
                else None
            ),
            "resulting_envelope_fingerprint": (
                resulting_record.envelope.stored_envelope_fingerprint
                if resulting_record is not None
                else expected_control.stored_envelope_fingerprint
                if expected_control is not None and resulting_control is not None
                else None
            ),
            "resulting_control_fingerprint": (
                (resulting_control or resulting_record.control).control_fingerprint
                if resulting_control is not None or resulting_record is not None
                else None
            ),
        },
    )


def _derive_key(master_key: bytes, info: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=info,
    ).derive(master_key)


def _stored_envelope_size(envelope: McpStoredContinuationEnvelopeFact) -> int:
    metadata = envelope.model_dump(
        mode="json",
        exclude={"nonce_bytes", "ciphertext_bytes"},
    )
    return (
        len(canonical_json_bytes(metadata))
        + len(envelope.nonce_bytes)
        + len(envelope.ciphertext_bytes)
    )


def _canonical_utc(value: object) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("MCP continuation timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("MCP continuation timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _apply_statement_deadline(cursor: Any, deadline_monotonic: float | None) -> None:
    if deadline_monotonic is None:
        return
    remaining_ms = int((deadline_monotonic - monotonic()) * 1000)
    if remaining_ms <= 0:
        raise TimeoutError("MCP continuation storage deadline expired")
    cursor.execute(
        "select set_config('statement_timeout', %s, true)",
        (f"{remaining_ms}ms",),
    )


__all__ = [
    "InMemoryMcpContinuationSecretStore",
    "McpContinuationAadContext",
    "McpContinuationAuthorityConflict",
    "McpContinuationBoundsExceeded",
    "McpContinuationDecryptFailed",
    "McpContinuationKeyMaterial",
    "McpContinuationKeyProvider",
    "McpContinuationMutationKind",
    "McpContinuationRepository",
    "McpContinuationSecretCodec",
    "McpContinuationSecretKeyUnavailable",
    "McpContinuationStoredRecord",
    "McpContinuationTransactionCompanion",
    "McpContinuationTransactionIntent",
    "PostgresMcpContinuationSecretStore",
    "PreparedMcpContinuationEnvelope",
    "PreparedMcpAwaitingContinuation",
    "PreparedMcpReplayContinuation",
    "build_prepared_companion_identity",
    "build_mcp_continuation_transaction_intent",
    "prepare_mcp_awaiting_continuation",
    "prepare_mcp_replay_continuation",
]
