"""Registered event-safe authority for the MCP 2026 protocol surface.

The official SDK is intentionally absent from this module.  Runtime adapters
lower SDK-conformed wire values into these immutable, versioned facts.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal, TypeAlias, TypeVar

from pydantic import Field, model_validator

from pulsara_agent.primitives._context_base import (
    FrozenJsonObjectFact,
)
from pulsara_agent.primitives.frozen import (
    FrozenFactBase,
    build_frozen_fact,
    register_durable_fact,
)


Fingerprint: TypeAlias = str
DEFAULT_MCP_JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
SUPPORTED_MCP_PROTOCOL_REVISIONS = (
    "2024-11-05",
    "2025-03-26",
    "2025-06-18",
    "2025-11-25",
    "2026-07-28",
)


def _fact(schema_version: str, own_field: str, domain_separator: str):
    def decorate(cls):
        register_durable_fact(
            schema_version=schema_version,
            own_fingerprint_field=own_field,
            domain_separator=domain_separator,
        )
        return cls

    return decorate


class McpProtocolBehaviorEra(StrEnum):
    STATELESS_PER_REQUEST = "stateless_per_request"
    HANDSHAKE_SESSIONFUL = "handshake_sessionful"


class McpClientInputMethod(StrEnum):
    ELICITATION_CREATE = "elicitation/create"
    SAMPLING_CREATE_MESSAGE = "sampling/createMessage"
    ROOTS_LIST = "roots/list"


class McpElicitationMode(StrEnum):
    FORM = "form"
    URL = "url"


class McpProviderProjectionDisposition(StrEnum):
    EXACTLY_SUPPORTED = "exactly_supported"
    LOSSLESS_NORMALIZED = "lossless_normalized"
    NOT_EXPOSABLE = "not_exposable"


class McpToolWireRejectionCode(StrEnum):
    INVALID_INPUT_SCHEMA = "invalid_input_schema"
    INVALID_OUTPUT_SCHEMA = "invalid_output_schema"
    UNSUPPORTED_DIALECT = "unsupported_dialect"
    SCHEMA_BOUNDS_EXCEEDED = "schema_bounds_exceeded"


class McpProviderProjectionRejectCode(StrEnum):
    PROVIDER_SCHEMA_UNSUPPORTED = "provider_schema_unsupported"
    LOSSLESS_PROJECTION_UNAVAILABLE = "lossless_projection_unavailable"


class McpCacheableMethod(StrEnum):
    SERVER_DISCOVER = "server/discover"
    TOOLS_LIST = "tools/list"
    PROMPTS_LIST = "prompts/list"
    RESOURCES_LIST = "resources/list"
    RESOURCE_TEMPLATES_LIST = "resources/templates/list"
    RESOURCES_READ = "resources/read"


class McpSnapshotDirtyReason(StrEnum):
    TTL_EXPIRED = "ttl_expired"
    LIST_CHANGED = "list_changed"
    AUTH_GENERATION_CHANGED = "auth_generation_changed"
    CONFIG_GENERATION_CHANGED = "config_generation_changed"
    TRANSPORT_RECONNECTED = "transport_reconnected"
    BINDING_ERROR = "binding_error"


@_fact("mcp_extension_semantic.v1", "semantic_fingerprint", "mcp-extension-semantic:v1")
class McpExtensionSemanticFact(FrozenFactBase):
    schema_version: Literal["mcp_extension_semantic.v1"]
    extension_id: str = Field(min_length=1)
    extension_version: str | None
    declaration: FrozenJsonObjectFact
    support_disposition: Literal[
        "declared_not_activated",
        "activated_by_owned_runtime",
        "unsupported",
    ]
    semantic_fingerprint: Fingerprint


@_fact(
    "mcp_server_protocol_semantic.v1",
    "semantic_fingerprint",
    "mcp-server-protocol-semantic:v1",
)
class McpServerProtocolSemanticFact(FrozenFactBase):
    schema_version: Literal["mcp_server_protocol_semantic.v1"]
    protocol_revision: str
    behavior_era: McpProtocolBehaviorEra
    server_capabilities: FrozenJsonObjectFact
    ordered_extension_contracts: tuple[McpExtensionSemanticFact, ...]
    semantic_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _protocol_era(self) -> "McpServerProtocolSemanticFact":
        expected = behavior_era_for_protocol_revision(self.protocol_revision)
        if self.behavior_era is not expected:
            raise ValueError("MCP protocol behavior-era mismatch")
        extension_ids = tuple(
            item.extension_id for item in self.ordered_extension_contracts
        )
        if extension_ids != tuple(sorted(extension_ids)) or len(extension_ids) != len(
            set(extension_ids)
        ):
            raise ValueError("MCP extension contracts must be ordered and unique")
        return self


@_fact(
    "mcp_client_capability_policy.v1",
    "policy_fingerprint",
    "mcp-client-capability-policy:v1",
)
class McpClientCapabilityPolicyFact(FrozenFactBase):
    schema_version: Literal["mcp_client_capability_policy.v1"]
    supported_input_methods: tuple[McpClientInputMethod, ...]
    elicitation_modes: tuple[McpElicitationMode, ...]
    elicitation_host_contract_fingerprint: Fingerprint | None
    sampling_advertised: bool
    roots_advertised: bool
    logging_advertised: bool
    ordered_extension_ads: tuple[McpExtensionSemanticFact, ...]
    policy_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _closed_capability_matrix(self) -> "McpClientCapabilityPolicyFact":
        methods = self.supported_input_methods
        if methods != tuple(sorted(set(methods), key=lambda item: item.value)):
            raise ValueError("MCP input methods must be ordered and unique")
        elicitation = McpClientInputMethod.ELICITATION_CREATE in methods
        full_modes = (McpElicitationMode.FORM, McpElicitationMode.URL)
        if elicitation:
            if self.elicitation_modes != full_modes:
                raise ValueError("SDK v2 elicitation requires form and URL modes")
            if self.elicitation_host_contract_fingerprint is None:
                raise ValueError("elicitation advertisement requires Host contract")
        elif (
            self.elicitation_modes
            or self.elicitation_host_contract_fingerprint is not None
        ):
            raise ValueError("disabled elicitation cannot carry Host authority")
        if self.sampling_advertised or self.roots_advertised or self.logging_advertised:
            raise ValueError("MCP2 V1 does not advertise sampling, roots, or logging")
        return self


class _McpNegotiationWireReceiptBase(FrozenFactBase):
    physical_operation_id: str = Field(min_length=1)
    sdk_client_generation_id: str = Field(min_length=1)
    exact_protocol_revision: str
    client_capability_policy_fingerprint: Fingerprint
    endpoint_attribution_fingerprint: Fingerprint
    auth_attribution_fingerprint: Fingerprint
    raw_result_payload_fingerprint: Fingerprint
    receipt_fingerprint: Fingerprint


@_fact(
    "mcp_final_discover_wire_receipt.v1",
    "receipt_fingerprint",
    "mcp-final-discover-wire-receipt:v1",
)
class McpFinalDiscoverWireReceiptFact(_McpNegotiationWireReceiptBase):
    schema_version: Literal["mcp_final_discover_wire_receipt.v1"]

    @model_validator(mode="after")
    def _modern(self) -> "McpFinalDiscoverWireReceiptFact":
        if behavior_era_for_protocol_revision(self.exact_protocol_revision) is not (
            McpProtocolBehaviorEra.STATELESS_PER_REQUEST
        ):
            raise ValueError("final discover receipt requires a stateless revision")
        return self


@_fact(
    "mcp_legacy_initialize_wire_receipt.v1",
    "receipt_fingerprint",
    "mcp-legacy-initialize-wire-receipt:v1",
)
class McpLegacyInitializeWireReceiptFact(_McpNegotiationWireReceiptBase):
    schema_version: Literal["mcp_legacy_initialize_wire_receipt.v1"]

    @model_validator(mode="after")
    def _legacy(self) -> "McpLegacyInitializeWireReceiptFact":
        if behavior_era_for_protocol_revision(self.exact_protocol_revision) is not (
            McpProtocolBehaviorEra.HANDSHAKE_SESSIONFUL
        ):
            raise ValueError("initialize receipt requires a handshake revision")
        return self


McpNegotiationWireReceiptFact: TypeAlias = (
    McpFinalDiscoverWireReceiptFact | McpLegacyInitializeWireReceiptFact
)


@_fact(
    "mcp_endpoint_attribution.v1",
    "attribution_fingerprint",
    "mcp-endpoint-attribution:v1",
)
class McpEndpointAttributionFact(FrozenFactBase):
    schema_version: Literal["mcp_endpoint_attribution.v1"]
    transport_kind: Literal["streamable_http", "stdio"]
    canonical_target_fingerprint: Fingerprint
    tls_policy_fingerprint: Fingerprint | None
    redirect_policy: Literal["deny", "same_origin"]
    executable_identity_fingerprint: Fingerprint | None
    attribution_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _transport_shape(self) -> "McpEndpointAttributionFact":
        if self.transport_kind == "stdio":
            if (
                self.executable_identity_fingerprint is None
                or self.tls_policy_fingerprint is not None
            ):
                raise ValueError("stdio endpoint attribution shape mismatch")
        elif self.executable_identity_fingerprint is not None:
            raise ValueError("HTTP endpoint cannot carry executable identity")
        return self


@_fact(
    "mcp_auth_attribution.v1",
    "attribution_fingerprint",
    "mcp-auth-attribution:v1",
)
class McpAuthAttributionFact(FrozenFactBase):
    schema_version: Literal["mcp_auth_attribution.v1"]
    auth_kind: Literal["none", "static_headers", "bearer_env", "oauth"]
    issuer_identity_fingerprint: Fingerprint | None
    client_identity_fingerprint: Fingerprint | None
    effective_scope_fingerprint: Fingerprint | None
    credential_generation: int = Field(ge=0)
    keyed_credential_commitment: str | None
    attribution_fingerprint: Fingerprint


@_fact(
    "mcp_protocol_negotiation_attribution.v1",
    "attribution_fingerprint",
    "mcp-protocol-negotiation-attribution:v1",
)
class McpProtocolNegotiationAttributionFact(FrozenFactBase):
    schema_version: Literal["mcp_protocol_negotiation_attribution.v1"]
    protocol_semantic_fingerprint: Fingerprint
    client_capability_policy_fingerprint: Fingerprint
    negotiation_source: Literal["server_discover", "legacy_initialize"]
    negotiation_wire_receipt_fingerprint: Fingerprint
    sdk_version: Literal["2.0.0"]
    sdk_conformance_contract_fingerprint: Fingerprint
    server_info: FrozenJsonObjectFact | None
    endpoint_attribution_fingerprint: Fingerprint
    auth_attribution_fingerprint: Fingerprint
    attribution_fingerprint: Fingerprint


@_fact(
    "mcp_tool_discovery_rejection.v1",
    "rejection_fingerprint",
    "mcp-tool-discovery-rejection:v1",
)
class McpToolDiscoveryRejectionFact(FrozenFactBase):
    schema_version: Literal["mcp_tool_discovery_rejection.v1"]
    server_id: str = Field(min_length=1)
    observed_tool_name: str = Field(min_length=1)
    source_page_receipt_fingerprint: Fingerprint
    observed_tool_payload_fingerprint: Fingerprint
    reason_code: McpToolWireRejectionCode
    sdk_conformed_listing_generation_fingerprint: Fingerprint
    rejection_fingerprint: Fingerprint


@_fact("mcp_tool_semantic.v1", "tool_semantic_fingerprint", "mcp-tool-semantic:v1")
class McpToolSemanticFact(FrozenFactBase):
    schema_version: Literal["mcp_tool_semantic.v1"]
    server_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    title: str | None
    description: str
    input_schema: FrozenJsonObjectFact
    input_schema_dialect: str
    output_schema: FrozenJsonObjectFact | None
    output_schema_dialect: str | None
    annotations: FrozenJsonObjectFact
    icons: tuple[FrozenJsonObjectFact, ...]
    execution: FrozenJsonObjectFact | None
    protocol_meta: FrozenJsonObjectFact | None
    tool_semantic_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _schema_shape(self) -> "McpToolSemanticFact":
        if self.output_schema is None and self.output_schema_dialect is not None:
            raise ValueError("output dialect requires output schema")
        if self.output_schema is not None and self.output_schema_dialect is None:
            raise ValueError("output schema requires resolved dialect")
        return self


@_fact(
    "mcp_tool_discovery_attribution.v1",
    "attribution_fingerprint",
    "mcp-tool-discovery-attribution:v1",
)
class McpToolDiscoveryAttributionFact(FrozenFactBase):
    schema_version: Literal["mcp_tool_discovery_attribution.v1"]
    tool_semantic_fingerprint: Fingerprint
    source_page_receipt_fingerprint: Fingerprint
    sdk_conformance_contract_fingerprint: Fingerprint
    sdk_conformed_listing_generation_fingerprint: Fingerprint
    sdk_header_routing_contract_fingerprint: Fingerprint
    pulsara_output_validation_contract_fingerprint: Fingerprint
    attribution_fingerprint: Fingerprint


@_fact(
    "mcp_provider_schema_projection.v1",
    "projection_fingerprint",
    "mcp-provider-schema-projection:v1",
)
class McpProviderSchemaProjectionFact(FrozenFactBase):
    schema_version: Literal["mcp_provider_schema_projection.v1"]
    tool_semantic_fingerprint: Fingerprint
    provider_schema_contract_fingerprint: Fingerprint
    disposition: McpProviderProjectionDisposition
    projected_schema: FrozenJsonObjectFact | None
    lossless_proof_fingerprint: Fingerprint | None
    reason_code: McpProviderProjectionRejectCode | None
    projection_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _projection_matrix(self) -> "McpProviderSchemaProjectionFact":
        if self.disposition is McpProviderProjectionDisposition.NOT_EXPOSABLE:
            if (
                self.projected_schema is not None
                or self.lossless_proof_fingerprint is not None
            ):
                raise ValueError("rejected provider projection cannot expose schema")
            if self.reason_code is None:
                raise ValueError("rejected provider projection requires reason")
        else:
            if self.projected_schema is None or self.reason_code is not None:
                raise ValueError("accepted provider projection shape mismatch")
            if (
                self.disposition is McpProviderProjectionDisposition.LOSSLESS_NORMALIZED
                and self.lossless_proof_fingerprint is None
            ):
                raise ValueError("normalized projection requires lossless proof")
        return self


@_fact("mcp_resource_semantic.v1", "semantic_fingerprint", "mcp-resource-semantic:v1")
class McpResourceSemanticFact(FrozenFactBase):
    schema_version: Literal["mcp_resource_semantic.v1"]
    server_id: str
    uri: str
    name: str
    title: str | None
    description: str | None
    mime_type: str | None
    size: int | None
    annotations: FrozenJsonObjectFact
    icons: tuple[FrozenJsonObjectFact, ...]
    protocol_meta: FrozenJsonObjectFact | None
    semantic_fingerprint: Fingerprint


@_fact(
    "mcp_resource_template_semantic.v1",
    "semantic_fingerprint",
    "mcp-resource-template-semantic:v1",
)
class McpResourceTemplateSemanticFact(FrozenFactBase):
    schema_version: Literal["mcp_resource_template_semantic.v1"]
    server_id: str
    uri_template: str
    name: str
    title: str | None
    description: str | None
    mime_type: str | None
    annotations: FrozenJsonObjectFact
    icons: tuple[FrozenJsonObjectFact, ...]
    protocol_meta: FrozenJsonObjectFact | None
    semantic_fingerprint: Fingerprint


@_fact(
    "mcp_prompt_argument_semantic.v1",
    "semantic_fingerprint",
    "mcp-prompt-argument-semantic:v1",
)
class McpPromptArgumentSemanticFact(FrozenFactBase):
    schema_version: Literal["mcp_prompt_argument_semantic.v1"]
    name: str
    title: str | None
    description: str | None
    required: bool
    semantic_fingerprint: Fingerprint


@_fact("mcp_prompt_semantic.v1", "semantic_fingerprint", "mcp-prompt-semantic:v1")
class McpPromptSemanticFact(FrozenFactBase):
    schema_version: Literal["mcp_prompt_semantic.v1"]
    server_id: str
    name: str
    title: str | None
    description: str | None
    arguments: tuple[McpPromptArgumentSemanticFact, ...]
    icons: tuple[FrozenJsonObjectFact, ...]
    protocol_meta: FrozenJsonObjectFact | None
    semantic_fingerprint: Fingerprint


@_fact(
    "mcp_server_surface_semantic.v1",
    "surface_semantic_fingerprint",
    "mcp-server-surface-semantic:v1",
)
class McpServerSurfaceSemanticFact(FrozenFactBase):
    schema_version: Literal["mcp_server_surface_semantic.v1"]
    server_id: str
    protocol_semantic: McpServerProtocolSemanticFact
    tools: tuple[McpToolSemanticFact, ...]
    resources: tuple[McpResourceSemanticFact, ...]
    resource_templates: tuple[McpResourceTemplateSemanticFact, ...]
    prompts: tuple[McpPromptSemanticFact, ...]
    instructions: str | None
    surface_semantic_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _ordered_surface(self) -> "McpServerSurfaceSemanticFact":
        for collection, identity in (
            (self.tools, lambda item: item.name),
            (self.resources, lambda item: item.uri),
            (self.resource_templates, lambda item: item.uri_template),
            (self.prompts, lambda item: item.name),
        ):
            keys = tuple(identity(item) for item in collection)
            if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
                raise ValueError("MCP surface collection must be ordered and unique")
            if any(item.server_id != self.server_id for item in collection):
                raise ValueError("MCP surface item server mismatch")
        return self


@_fact(
    "mcp_cache_page_attribution.v1",
    "page_receipt_fingerprint",
    "mcp-cache-page-attribution:v1",
)
class McpCachePageAttributionFact(FrozenFactBase):
    schema_version: Literal["mcp_cache_page_attribution.v1"]
    method: McpCacheableMethod
    request_params_fingerprint: Fingerprint
    request_cursor: str | None
    page_ordinal: int = Field(ge=0)
    received_at_utc: str
    raw_ttl_ms: int | None
    resolved_ttl_ms: int = Field(ge=0)
    raw_cache_scope: Literal["public", "private"] | None
    resolved_cache_scope: Literal["public", "private"]
    hint_disposition: Literal["exact", "absent_earlier_revision", "negative_normalized"]
    result_payload_fingerprint: Fingerprint
    next_cursor: str | None
    page_receipt_fingerprint: Fingerprint


@_fact(
    "mcp_discovery_page_set_attribution.v1",
    "page_set_fingerprint",
    "mcp-discovery-page-set-attribution:v1",
)
class McpDiscoveryPageSetAttributionFact(FrozenFactBase):
    schema_version: Literal["mcp_discovery_page_set_attribution.v1"]
    method: McpCacheableMethod
    started_from_cursor_none: bool
    ordered_pages: tuple[McpCachePageAttributionFact, ...]
    page_receipt_accumulator: Fingerprint
    common_resolved_cache_scope: Literal["public", "private"]
    complete_capture: bool
    page_set_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _cursor_chain(self) -> "McpDiscoveryPageSetAttributionFact":
        if not self.ordered_pages:
            raise ValueError("discovery page set cannot be empty")
        scopes = {page.resolved_cache_scope for page in self.ordered_pages}
        if len(scopes) != 1:
            raise ValueError("MCP discovery pages must use one cache scope")
        for ordinal, page in enumerate(self.ordered_pages):
            if page.method is not self.method or page.page_ordinal != ordinal:
                raise ValueError("MCP discovery page attribution mismatch")
            expected_cursor = (
                None if ordinal == 0 else self.ordered_pages[ordinal - 1].next_cursor
            )
            if page.request_cursor != expected_cursor:
                raise ValueError("MCP discovery cursor chain mismatch")
        if self.started_from_cursor_none != (
            self.ordered_pages[0].request_cursor is None
        ):
            raise ValueError("MCP page-set start attribution mismatch")
        if self.complete_capture != (self.ordered_pages[-1].next_cursor is None):
            raise ValueError("MCP page-set completion attribution mismatch")
        expected_scope = self.ordered_pages[0].resolved_cache_scope
        if self.common_resolved_cache_scope != expected_scope:
            raise ValueError("MCP page-set aggregate cache scope mismatch")
        return self


@_fact(
    "mcp_discovery_attribution.v1",
    "attribution_fingerprint",
    "mcp-discovery-attribution:v1",
)
class McpDiscoveryAttributionFact(FrozenFactBase):
    schema_version: Literal["mcp_discovery_attribution.v1"]
    snapshot_id: str
    config_epoch: int = Field(ge=0)
    discovery_generation: int = Field(ge=0)
    transport_generation: int = Field(ge=0)
    endpoint: McpEndpointAttributionFact
    auth: McpAuthAttributionFact
    negotiation: McpProtocolNegotiationAttributionFact
    page_set_receipts: tuple[McpDiscoveryPageSetAttributionFact, ...]
    ordered_tool_rejections: tuple[McpToolDiscoveryRejectionFact, ...]
    reconcile_attempt_id: str
    attribution_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _negotiation_join(self) -> "McpDiscoveryAttributionFact":
        if (
            self.negotiation.endpoint_attribution_fingerprint
            != self.endpoint.attribution_fingerprint
        ):
            raise ValueError("MCP negotiation endpoint attribution mismatch")
        if (
            self.negotiation.auth_attribution_fingerprint
            != self.auth.attribution_fingerprint
        ):
            raise ValueError("MCP negotiation auth attribution mismatch")
        methods = tuple(item.method.value for item in self.page_set_receipts)
        if methods != tuple(sorted(methods)) or len(methods) != len(set(methods)):
            raise ValueError("MCP discovery page sets must be ordered and unique")
        return self


@_fact(
    "mcp_server_snapshot_authority.v1",
    "authority_fingerprint",
    "mcp-server-snapshot-authority:v1",
)
class McpServerSnapshotAuthorityFact(FrozenFactBase):
    schema_version: Literal["mcp_server_snapshot_authority.v1"]
    surface_semantic: McpServerSurfaceSemanticFact
    discovery_attribution: McpDiscoveryAttributionFact
    ordered_provider_projections: tuple[McpProviderSchemaProjectionFact, ...]
    surface_semantic_fingerprint: Fingerprint
    projection_accumulator: Fingerprint
    authority_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _authority_join(self) -> "McpServerSnapshotAuthorityFact":
        if (
            self.surface_semantic_fingerprint
            != self.surface_semantic.surface_semantic_fingerprint
        ):
            raise ValueError("MCP surface semantic fingerprint mismatch")
        if self.discovery_attribution.snapshot_id == "":
            raise ValueError("MCP snapshot attribution requires identity")
        tool_fingerprints = {
            item.tool_semantic_fingerprint for item in self.surface_semantic.tools
        }
        projected = tuple(
            item.tool_semantic_fingerprint for item in self.ordered_provider_projections
        )
        if set(projected) != tool_fingerprints or len(projected) != len(set(projected)):
            raise ValueError("MCP provider projection coverage mismatch")
        return self


_McpFactT = TypeVar("_McpFactT", bound=FrozenFactBase)


def build_mcp_protocol_fact(fact_type: type[_McpFactT], /, **payload: Any) -> _McpFactT:
    return build_frozen_fact(fact_type, **payload)


def behavior_era_for_protocol_revision(revision: str) -> McpProtocolBehaviorEra:
    if revision == "2026-07-28":
        return McpProtocolBehaviorEra.STATELESS_PER_REQUEST
    if revision in SUPPORTED_MCP_PROTOCOL_REVISIONS[:-1]:
        return McpProtocolBehaviorEra.HANDSHAKE_SESSIONFUL
    raise ValueError(f"unsupported MCP protocol revision: {revision}")


__all__ = [name for name in globals() if name.startswith("Mcp")] + [
    "DEFAULT_MCP_JSON_SCHEMA_DIALECT",
    "SUPPORTED_MCP_PROTOCOL_REVISIONS",
    "behavior_era_for_protocol_revision",
    "build_mcp_protocol_fact",
]
