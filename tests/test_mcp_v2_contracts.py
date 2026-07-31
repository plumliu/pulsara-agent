from __future__ import annotations

import dataclasses
import pickle

import mcp.types as sdk_types
import pytest

from pulsara_agent.event import ReplyStartEvent
from pulsara_agent.event_log.serialization import freeze_event_write_candidate
from pulsara_agent.event_log.protocol import (
    build_prepared_candidate_batch_identity,
    rebind_stored_candidate_batch,
)
from pulsara_agent.inspector.service import _json_safe
from pulsara_agent.host.mcp_elicitation import HostSessionMcpFormInteractionPort
from pulsara_agent.memory.artifacts.archive import InMemoryArchiveStore
from pulsara_agent.ports.mcp_elicitation import (
    McpElicitationCapabilityDisabled,
    build_full_mcp_elicitation_capability,
)
from pulsara_agent.ports.mcp_secret import (
    McpElicitationAction,
    McpSealedElicitationResponseFactory,
    SealedMcpContinuationSecretBase,
    build_awaiting_input_carrier_plaintext,
    build_retryable_tool_call_payload,
)
from pulsara_agent.primitives._context_base import freeze_json
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.frozen import FrozenFactBase
from pulsara_agent.primitives.mcp_continuation import (
    McpContinuationCarrierState,
    McpFormElicitationRequestFact,
    build_mcp_continuation_fact,
    default_mcp_continuation_bounds,
)
from pulsara_agent.primitives.mcp_continuation_storage import (
    McpContinuationCarrierControlFact,
    build_mcp_continuation_storage_fact,
)
from pulsara_agent.primitives.mcp_protocol import (
    McpCacheableMethod,
    McpCachePageAttributionFact,
    McpClientInputMethod,
    McpDiscoveryPageSetAttributionFact,
    McpProtocolBehaviorEra,
    behavior_era_for_protocol_revision,
    build_mcp_protocol_fact,
)
from pulsara_agent.primitives.storage_frozen import FrozenStorageFactBase
from pulsara_agent.runtime.mcp.contracts import build_raw_tool_call_result_carrier
from pulsara_agent.runtime.mcp.continuation_store import (
    McpContinuationAadContext,
    McpContinuationBoundsExceeded,
    McpContinuationDecryptFailed,
    McpContinuationKeyProvider,
    McpContinuationSecretCodec,
    McpContinuationSecretKeyUnavailable,
    prepare_mcp_awaiting_continuation,
)
from pulsara_agent.runtime.mcp.schema import (
    McpOutputSchemaMismatch,
    McpSchemaContractError,
    build_conformed_tool_schema,
    validate_structured_tool_result,
)
from pulsara_agent.runtime.mcp.browser import SystemMcpExternalBrowserPort
from pulsara_agent.runtime.mcp.protocol import (
    McpClientInputRuntimeBinding,
    lower_input_required_result,
)
from pulsara_agent.runtime.mcp.supervisor import McpServerSupervisor


def test_protocol_revision_maps_to_closed_behavior_era() -> None:
    assert behavior_era_for_protocol_revision("2026-07-28") is (
        McpProtocolBehaviorEra.STATELESS_PER_REQUEST
    )
    assert behavior_era_for_protocol_revision("2025-11-25") is (
        McpProtocolBehaviorEra.HANDSHAKE_SESSIONFUL
    )
    with pytest.raises(ValueError, match="unsupported"):
        behavior_era_for_protocol_revision("future")


def test_elicitation_advertisement_requires_exact_full_host_composition() -> None:
    form_port = HostSessionMcpFormInteractionPort()
    browser_port = SystemMcpExternalBrowserPort()
    capability = build_full_mcp_elicitation_capability(
        form_interaction_port=form_port,
        external_browser_port=browser_port,
    )
    codec = McpContinuationSecretCodec(
        McpContinuationKeyProvider.from_master_key(
            key_id="test-elicitation-composition",
            master_key=b"test-elicitation-composition-key" * 2,
        )
    )
    binding = McpClientInputRuntimeBinding(
        commitment_key_id=codec.key_id,
        elicitation_capability=capability,
        _keyed_commitment=codec.keyed_commitment,
    )
    supervisor = McpServerSupervisor(
        client_input_binding=binding,
        elicitation_capability=capability,
        continuation_codec=codec,
    )
    assert supervisor.external_browser_port is browser_port
    assert binding.host_contract_fingerprint == capability.contract_fingerprint

    with pytest.raises(ValueError, match="disabled MCP elicitation"):
        McpServerSupervisor(
            client_input_binding=binding,
            elicitation_capability=McpElicitationCapabilityDisabled(),
            continuation_codec=codec,
        )


def test_discovery_page_set_rejects_mixed_cache_scope() -> None:
    pages = tuple(
        build_mcp_protocol_fact(
            McpCachePageAttributionFact,
            schema_version="mcp_cache_page_attribution.v1",
            method=McpCacheableMethod.TOOLS_LIST,
            request_params_fingerprint=context_fingerprint(
                "test-mcp-page-params:v1",
                {"ordinal": ordinal},
            ),
            request_cursor=None if ordinal == 0 else "page:2",
            page_ordinal=ordinal,
            received_at_utc=f"2026-07-31T00:00:0{ordinal}Z",
            raw_ttl_ms=1000,
            resolved_ttl_ms=1000,
            raw_cache_scope=scope,
            resolved_cache_scope=scope,
            hint_disposition="exact",
            result_payload_fingerprint=context_fingerprint(
                "test-mcp-page-result:v1",
                {"ordinal": ordinal},
            ),
            next_cursor="page:2" if ordinal == 0 else None,
        )
        for ordinal, scope in enumerate(("public", "private"))
    )
    with pytest.raises(ValueError, match="one cache scope"):
        build_mcp_protocol_fact(
            McpDiscoveryPageSetAttributionFact,
            schema_version="mcp_discovery_page_set_attribution.v1",
            method=McpCacheableMethod.TOOLS_LIST,
            started_from_cursor_none=True,
            ordered_pages=pages,
            page_receipt_accumulator=context_fingerprint(
                "mcp-discovery-page-receipt-accumulator:v1",
                tuple(page.page_receipt_fingerprint for page in pages),
            ),
            common_resolved_cache_scope="private",
            complete_capture=True,
        )


def test_stored_batch_rebind_normalizes_only_canonical_sequence() -> None:
    first = ReplyStartEvent(
        id="event:rebind:1",
        created_at="2026-07-31T00:00:00Z",
        run_id="run:rebind",
        turn_id="turn:rebind",
        reply_id="reply:rebind",
        name="assistant",
    )
    second = ReplyStartEvent(
        id="event:rebind:2",
        created_at="2026-07-31T00:00:01Z",
        run_id="run:rebind",
        turn_id="turn:rebind",
        reply_id="reply:rebind",
        name="assistant",
    )
    prepared = build_prepared_candidate_batch_identity(
        tuple(freeze_event_write_candidate(item) for item in (first, second))
    )
    stored = (
        first.model_copy(update={"sequence": 9}),
        second.model_copy(update={"sequence": 10}),
    )

    receipt = rebind_stored_candidate_batch(prepared, stored)

    assert receipt.ordered_event_ids == (first.id, second.id)
    assert receipt.ordered_assigned_sequences == (9, 10)
    assert receipt.ordered_normalized_payload_fingerprints == (
        prepared.ordered_candidate_payload_fingerprints
    )
    with pytest.raises(ValueError, match="identity/sequence"):
        rebind_stored_candidate_batch(prepared, tuple(reversed(stored)))
    with pytest.raises(ValueError, match="not contiguous"):
        rebind_stored_candidate_batch(
            prepared,
            (
                stored[0],
                stored[1].model_copy(update={"sequence": 11}),
            ),
        )
    with pytest.raises(ValueError, match="differs from sequence-null"):
        rebind_stored_candidate_batch(
            prepared,
            (
                stored[0],
                stored[1].model_copy(update={"name": "changed"}),
            ),
        )


def test_continuation_key_is_disabled_only_when_both_settings_are_absent() -> None:
    assert McpContinuationKeyProvider.optional_from_environment({}) is None
    with pytest.raises(McpContinuationSecretKeyUnavailable, match="are required"):
        McpContinuationKeyProvider.optional_from_environment(
            {"PULSARA_MCP_CONTINUATION_KEY_ID": "generation:1"}
        )
    with pytest.raises(McpContinuationSecretKeyUnavailable, match="canonical base64"):
        McpContinuationKeyProvider.optional_from_environment(
            {
                "PULSARA_MCP_CONTINUATION_KEY_ID": "generation:1",
                "PULSARA_MCP_CONTINUATION_MASTER_KEY": "not-base64",
            }
        )


def test_storage_only_fact_is_not_event_safe_fact() -> None:
    fact = build_mcp_continuation_storage_fact(
        McpContinuationCarrierControlFact,
        schema_version="mcp_continuation_carrier_control.v1",
        continuation_carrier_id="carrier:1",
        carrier_state=McpContinuationCarrierState.AWAITING_CLIENT_INPUT,
        control_revision=1,
        source_event_id="event:1",
        stored_envelope_fingerprint="f" * 64,
    )
    assert isinstance(fact, FrozenStorageFactBase)
    assert not isinstance(fact, FrozenFactBase)


def test_generic_event_artifact_and_inspector_sinks_reject_storage_only_fact() -> None:
    fact = build_mcp_continuation_storage_fact(
        McpContinuationCarrierControlFact,
        schema_version="mcp_continuation_carrier_control.v1",
        continuation_carrier_id="carrier:guard",
        carrier_state=McpContinuationCarrierState.AWAITING_CLIENT_INPUT,
        control_revision=1,
        source_event_id="event:guard",
        stored_envelope_fingerprint="f" * 64,
    )
    event = ReplyStartEvent(
        run_id="run:guard",
        turn_id="turn:guard",
        reply_id="reply:guard",
        name="assistant",
        metadata={"nested": {"storage": fact}},
    )
    with pytest.raises(TypeError, match="EventLog rejects MCP storage-only"):
        freeze_event_write_candidate(event)
    with pytest.raises(TypeError, match="ArtifactStore rejects MCP storage-only"):
        InMemoryArchiveStore().put_text(
            "artifact:guard",
            "safe body",
            metadata={"nested": {"storage": fact}},
        )
    with pytest.raises(TypeError, match="Inspector rejects MCP storage-only"):
        _json_safe({"nested": {"storage": fact}})


def test_sdk_conformed_input_schema_is_rejected_not_repaired() -> None:
    with pytest.raises(McpSchemaContractError, match="root type"):
        build_conformed_tool_schema(
            server_id="server",
            name="bad",
            title=None,
            description=None,
            input_schema={"properties": {}},
            output_schema=None,
            annotations={},
        )


def test_output_schema_container_can_describe_scalar_root() -> None:
    contract = build_conformed_tool_schema(
        server_id="server",
        name="scalar",
        title=None,
        description=None,
        input_schema={"type": "object", "properties": {}},
        output_schema={"type": "string"},
        annotations={},
    )
    validate_structured_tool_result(
        tool=contract.semantic,
        structured_content_present=True,
        structured_content="ok",
    )
    with pytest.raises(McpOutputSchemaMismatch):
        validate_structured_tool_result(
            tool=contract.semantic,
            structured_content_present=True,
            structured_content=3,
        )


def test_raw_result_preserves_absent_and_explicit_null() -> None:
    absent = sdk_types.CallToolResult(content=[])
    explicit_null = sdk_types.CallToolResult.model_validate(
        {"content": [], "structuredContent": None}
    )
    absent_carrier = build_raw_tool_call_result_carrier(
        result=absent,
        operation_id="operation:1",
        sdk_client_generation_id="generation:1",
        tool_semantic_fingerprint="t" * 64,
    )
    null_carrier = build_raw_tool_call_result_carrier(
        result=explicit_null,
        operation_id="operation:2",
        sdk_client_generation_id="generation:1",
        tool_semantic_fingerprint="t" * 64,
    )
    assert not absent_carrier.structured_content_present
    assert null_carrier.structured_content_present
    assert null_carrier.structured_content is None


def test_form_response_is_sealed_and_not_generically_serializable() -> None:
    request = build_mcp_continuation_fact(
        McpFormElicitationRequestFact,
        schema_version="mcp_form_elicitation_request.v1",
        key="email",
        method=McpClientInputMethod.ELICITATION_CREATE,
        mode="form",
        wire_mode_was_omitted=False,
        message="Email",
        requested_schema=freeze_json(
            {
                "type": "object",
                "properties": {"email": {"type": "string"}},
                "required": ["email"],
            }
        ),
        requested_schema_fingerprint="s" * 64,
    )
    response = McpSealedElicitationResponseFactory(
        commitment_key_id="test-key",
        commitment_key=b"k" * 32,
        bounds=default_mcp_continuation_bounds(),
    ).form_response(
        request=request,
        action=McpElicitationAction.ACCEPT,
        content_present=True,
        content={"email": "person@example.com"},
    )
    assert isinstance(response, SealedMcpContinuationSecretBase)
    assert repr(response) == "<sealed-mcp-continuation-secret>"
    with pytest.raises(TypeError):
        pickle.dumps(response)
    with pytest.raises(TypeError):
        dataclasses.asdict(response)
    assert not hasattr(response, "model_dump")
    assert not hasattr(response, "__dict__")
    with pytest.raises(TypeError):
        response._request_key = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError, match="EventLog rejects MCP continuation secret"):
        freeze_event_write_candidate(
            ReplyStartEvent(
                run_id="run:secret",
                turn_id="turn:secret",
                reply_id="reply:secret",
                name="assistant",
                metadata={"nested": {"response": response}},
            )
        )
    with pytest.raises(TypeError, match="ArtifactStore rejects MCP continuation secret"):
        InMemoryArchiveStore().put_text(
            "artifact:secret",
            "safe body",
            metadata={"nested": {"response": response}},
        )
    with pytest.raises(TypeError, match="Inspector rejects MCP continuation secret"):
        _json_safe({"nested": {"response": response}})


def test_request_state_legs_cannot_be_dumped_pickled_or_rendered() -> None:
    retryable = build_retryable_tool_call_payload(
        tool_name="lookup",
        arguments={},
        source_method_schema_fingerprint="m" * 64,
    )
    leg, _ = lower_input_required_result(
        input_requests=None,
        request_state="request-state-canary",
        leg_ordinal=1,
        retryable_payload=retryable,
        operation_deadline_monotonic=1.0,
        commitment_key_id="test-key",
        keyed_commitment=lambda _domain, _payload: "hmac-sha256:" + "a" * 64,
        elicitation_advertised=True,
        bounds=default_mcp_continuation_bounds(),
    )
    assert "request-state-canary" not in repr(leg)
    with pytest.raises(TypeError):
        leg.model_dump()
    with pytest.raises(TypeError):
        leg.model_dump_json()
    with pytest.raises(TypeError):
        pickle.dumps(leg)
    event = ReplyStartEvent(
        run_id="run:request-state",
        turn_id="turn:request-state",
        reply_id="reply:request-state",
        name="assistant",
        metadata={"nested": {"leg": leg}},
    )
    with pytest.raises(TypeError, match="process-local runtime state"):
        freeze_event_write_candidate(event)
    with pytest.raises(TypeError, match="process-local runtime state"):
        InMemoryArchiveStore().put_text(
            "artifact:request-state",
            "safe body",
            metadata={"nested": {"leg": leg}},
        )
    with pytest.raises(TypeError, match="process-local runtime state"):
        _json_safe({"nested": {"leg": leg}})


def test_encrypted_continuation_round_trips_only_through_typed_codec() -> None:
    bounds = default_mcp_continuation_bounds()
    plaintext = build_awaiting_input_carrier_plaintext(
        runtime_session_id="session:1",
        interaction_id="interaction:1",
        suspension_event_id="event:suspended",
        round_ordinal=1,
        retryable_request_payload=build_retryable_tool_call_payload(
            tool_name="lookup",
            arguments={"email": "person@example.com"},
            source_method_schema_fingerprint="m" * 64,
        ),
        request_state="opaque-low-entropy-state",
        request_set_fingerprint="r" * 64,
        private_url_requests=(),
        protocol_semantic_fingerprint="p" * 64,
        endpoint_attribution_fingerprint="e" * 64,
        auth_attribution_fingerprint="a" * 64,
        binding_contract_fingerprint="b" * 64,
        created_at_utc="2026-07-31T00:00:00Z",
        operation_expires_at_utc="2026-07-31T00:05:00Z",
        expiry_fingerprint="x" * 64,
    )
    aad = McpContinuationAadContext(
        runtime_session_id="session:1",
        interaction_id="interaction:1",
        source_event_id="event:suspended",
        round_ordinal=1,
        operation_expires_at_utc="2026-07-31T00:05:00Z",
        expiry_fingerprint="x" * 64,
    )
    codec = McpContinuationSecretCodec(
        McpContinuationKeyProvider.from_master_key(
            key_id="key:1",
            master_key=b"master" * 8,
        )
    )
    prepared = codec.prepare_envelope(
        carrier_id="carrier:1",
        carrier_kind="awaiting_client_input",
        plaintext=plaintext,
        aad_context=aad,
        bounds=bounds,
        created_at_utc="2026-07-31T00:00:00Z",
        initial_state=McpContinuationCarrierState.AWAITING_CLIENT_INPUT,
    )
    assert b"person@example.com" not in prepared.envelope.ciphertext_bytes
    rebound = codec.decrypt_and_rebind(
        envelope=prepared.envelope,
        aad_context=aad,
        expected_plaintext_commitment=prepared.plaintext_commitment,
        bounds=bounds,
    )
    assert type(rebound) is type(plaintext)
    assert repr(rebound) == "<sealed-mcp-continuation-secret>"

    wrong_codec = McpContinuationSecretCodec(
        McpContinuationKeyProvider.from_master_key(
            key_id="key:1",
            master_key=b"another-master" * 3,
        )
    )
    with pytest.raises(McpContinuationDecryptFailed):
        wrong_codec.decrypt_and_rebind(
            envelope=prepared.envelope,
            aad_context=aad,
            expected_plaintext_commitment=prepared.plaintext_commitment,
            bounds=bounds,
        )


def test_continuation_expiry_is_fixed_for_the_entire_operation() -> None:
    bounds = default_mcp_continuation_bounds()
    codec = McpContinuationSecretCodec(
        McpContinuationKeyProvider.from_master_key(
            key_id="key:expiry",
            master_key=b"expiry-master-key" * 2,
        )
    )
    retryable = build_retryable_tool_call_payload(
        tool_name="lookup",
        arguments={},
        source_method_schema_fingerprint="m" * 64,
    )

    with pytest.raises(McpContinuationBoundsExceeded, match="expired"):
        prepare_mcp_awaiting_continuation(
            codec=codec,
            runtime_session_id="session:expired",
            interaction_id="interaction:expired",
            suspension_event_id="event:expired",
            round_ordinal=1,
            retryable_request_payload=retryable,
            request_state="opaque-state",
            request_set_fingerprint="r" * 64,
            private_url_requests=(),
            protocol_semantic_fingerprint="p" * 64,
            endpoint_attribution_fingerprint="e" * 64,
            auth_attribution_fingerprint="a" * 64,
            binding_contract_fingerprint="b" * 64,
            first_input_required_observed_at_utc="2026-07-31T00:00:00Z",
            created_at_utc="2026-07-31T00:05:00Z",
            bounds=bounds,
            configured_ttl_seconds=300,
        )

    first = prepare_mcp_awaiting_continuation(
        codec=codec,
        runtime_session_id="session:successor",
        interaction_id="interaction:successor",
        suspension_event_id="event:successor:1",
        round_ordinal=1,
        retryable_request_payload=retryable,
        request_state="opaque-state",
        request_set_fingerprint="r" * 64,
        private_url_requests=(),
        protocol_semantic_fingerprint="p" * 64,
        endpoint_attribution_fingerprint="e" * 64,
        auth_attribution_fingerprint="a" * 64,
        binding_contract_fingerprint="b" * 64,
        first_input_required_observed_at_utc="2026-07-31T00:00:00Z",
        created_at_utc="2026-07-31T00:00:01Z",
        bounds=bounds,
        configured_ttl_seconds=300,
    )
    with pytest.raises(McpContinuationBoundsExceeded, match="expired"):
        prepare_mcp_awaiting_continuation(
            codec=codec,
            runtime_session_id="session:successor",
            interaction_id="interaction:successor",
            suspension_event_id="event:successor:2",
            round_ordinal=2,
            retryable_request_payload=retryable,
            request_state="next-opaque-state",
            request_set_fingerprint="s" * 64,
            private_url_requests=(),
            protocol_semantic_fingerprint="p" * 64,
            endpoint_attribution_fingerprint="e" * 64,
            auth_attribution_fingerprint="a" * 64,
            binding_contract_fingerprint="b" * 64,
            first_input_required_observed_at_utc="2026-07-31T00:05:00Z",
            created_at_utc="2026-07-31T00:05:00Z",
            bounds=bounds,
            configured_ttl_seconds=300,
            inherited_expiry=first.durable_fact.expiry,
            predecessor_control_revision=first.stored_record.control.control_revision,
        )
