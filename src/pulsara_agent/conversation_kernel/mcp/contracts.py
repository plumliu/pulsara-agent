"""Immutable discovery, catalog, and installation facts for MCP."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping

from pulsara_agent.model_input.contracts import FrozenToolSpec, ModelInputScopeKind
from pulsara_agent.primitives.context import FrozenJsonObjectFact, context_fingerprint

from ..tool_surface import McpToolExecutionPolicyFact


MAXIMUM_CONFIGURED_MCP_SERVERS = 64
MAXIMUM_DISCOVERED_TOOLS_PER_SERVER = 512
MAXIMUM_DISCOVERY_PAGES_PER_METHOD = 20
MAXIMUM_DISCOVERY_ITEMS_PER_SERVER = 2_000
MAXIMUM_MCP_REMOTE_BODY_BYTES = 16 * 1024 * 1024
MAXIMUM_MCP_INSTRUCTIONS_BYTES = 8 * 1024
MAXIMUM_MCP_CATALOG_FULL_BYTES = 32 * 1024
MAXIMUM_MCP_CATALOG_COMPACT_BYTES = 8 * 1024
MAXIMUM_MCP_CATALOG_REF_BYTES = 2 * 1024
MAXIMUM_MCP_CATALOG_RESULT_BYTES = 64 * 1024
MAXIMUM_MCP_HOST_IN_FLIGHT = 16


class McpServerState(StrEnum):
    DISABLED = "DISABLED"
    CONNECTING = "CONNECTING"
    DISCOVERING = "DISCOVERING"
    READY = "READY"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_TERMINAL = "FAILED_TERMINAL"
    RETIRING = "RETIRING"
    CLOSED = "CLOSED"


class McpPhysicalConcurrencyKind(StrEnum):
    SERIAL_SESSION = "SERIAL_SESSION"
    BOUNDED_STATELESS_HTTP = "BOUNDED_STATELESS_HTTP"


@dataclass(frozen=True, slots=True)
class McpToolSemanticFact:
    server_id: str
    remote_tool_name: str
    provider_tool_name: str
    description: str
    input_schema: FrozenJsonObjectFact = field(repr=False)
    output_schema: FrozenJsonObjectFact | None = field(default=None, repr=False)
    schema_dialect: str = "json-schema-draft-2020-12"
    descriptor_fingerprint: str = ""
    root_visible: bool = True
    subagent_visible: bool = False

    def __post_init__(self) -> None:
        if not all(
            (
                self.server_id,
                self.remote_tool_name,
                self.provider_tool_name,
                self.descriptor_fingerprint,
            )
        ):
            raise ValueError("MCP tool semantic identity is incomplete")

    def provider_spec(self) -> FrozenToolSpec:
        return FrozenToolSpec(
            name=self.provider_tool_name,
            description=self.description,
            parameters=self.input_schema,
            descriptor_fingerprint=self.descriptor_fingerprint,
        )


@dataclass(frozen=True, slots=True)
class McpResourceSemanticFact:
    server_id: str
    uri: str
    name: str
    description: str
    mime_type: str | None
    semantic_fingerprint: str


@dataclass(frozen=True, slots=True)
class McpResourceTemplateSemanticFact:
    server_id: str
    uri_template: str
    name: str
    description: str
    mime_type: str | None
    semantic_fingerprint: str


@dataclass(frozen=True, slots=True)
class McpPromptSemanticFact:
    server_id: str
    name: str
    description: str
    arguments: tuple[tuple[str, str, bool], ...]
    semantic_fingerprint: str


@dataclass(frozen=True, slots=True)
class McpDiscoverySnapshot:
    server_id: str
    display_name: str
    protocol_version: str
    sanitized_instructions: str
    discovered_tool_count: int
    invalid_tool_count: int
    tools: tuple[McpToolSemanticFact, ...]
    resources: tuple[McpResourceSemanticFact, ...]
    resource_templates: tuple[McpResourceTemplateSemanticFact, ...]
    prompts: tuple[McpPromptSemanticFact, ...]
    tool_surface_semantic_fingerprint: str
    catalog_semantic_fingerprint: str
    presentation_fingerprint: str
    sdk_conformance_contract_fingerprint: str


@dataclass(frozen=True, slots=True)
class McpServerCatalogEntry:
    server_id: str
    display_name: str
    status: McpServerState
    required: bool
    exposed_tool_count: int
    discovered_tool_count: int
    resource_count: int
    resource_template_count: int
    prompt_count: int
    bounded_tool_name_overview: tuple[str, ...]
    sanitized_instructions: str
    stable_failure_category: str | None
    tool_surface_semantic_fingerprint: str | None
    catalog_semantic_fingerprint: str
    scope_subagents: bool


@dataclass(frozen=True, slots=True)
class McpCatalogSnapshot:
    owner_epoch: int
    catalog_revision: int
    servers: tuple[McpServerCatalogEntry, ...]
    semantic_fingerprint: str
    presentation_fingerprint: str

    def for_scope(
        self,
        scope: ModelInputScopeKind,
    ) -> McpCatalogSnapshot:
        if scope is ModelInputScopeKind.ROOT:
            return self
        servers = tuple(item for item in self.servers if item.scope_subagents)
        semantic = context_fingerprint(
            "mcp-catalog-scope:v1",
            tuple(_catalog_semantic_payload(item) for item in servers),
        )
        presentation = context_fingerprint(
            "mcp-catalog-presentation-scope:v1",
            tuple((item.server_id, item.status.value) for item in servers),
        )
        return McpCatalogSnapshot(
            owner_epoch=self.owner_epoch,
            catalog_revision=self.catalog_revision,
            servers=servers,
            semantic_fingerprint=semantic,
            presentation_fingerprint=presentation,
        )


@dataclass(frozen=True, slots=True)
class McpInstallationCandidate:
    candidate_id: str
    server_id: str
    expected_supervisor_epoch: int
    expected_semantic_config_fingerprint: str
    expected_runtime_config_fingerprint: str
    expected_resolved_config_identity: str
    attempt_generation: int
    slot_lease: object = field(repr=False, compare=False)
    discovery_snapshot: McpDiscoverySnapshot
    ordered_tool_execution_policies: tuple[McpToolExecutionPolicyFact, ...]
    standard_read_timeout_ms: int
    normalized_physical_bytes: int
    candidate_fingerprint: str

    def __post_init__(self) -> None:
        if not 1_000 <= self.standard_read_timeout_ms <= 600_000:
            raise ValueError("MCP standard read timeout is out of range")


def build_catalog_snapshot(
    *,
    owner_epoch: int,
    catalog_revision: int,
    entries: tuple[McpServerCatalogEntry, ...],
) -> McpCatalogSnapshot:
    ordered = tuple(sorted(entries, key=lambda item: item.server_id))
    semantic = context_fingerprint(
        "mcp-catalog-semantic:v1",
        tuple(_catalog_semantic_payload(item) for item in ordered),
    )
    presentation = context_fingerprint(
        "mcp-catalog-presentation:v1",
        tuple(
            (
                item.server_id,
                item.status.value,
                item.stable_failure_category,
            )
            for item in ordered
        ),
    )
    return McpCatalogSnapshot(
        owner_epoch=owner_epoch,
        catalog_revision=catalog_revision,
        servers=ordered,
        semantic_fingerprint=semantic,
        presentation_fingerprint=presentation,
    )


def _catalog_semantic_payload(item: McpServerCatalogEntry) -> Mapping[str, object]:
    return {
        "server_id": item.server_id,
        "display_name": item.display_name,
        "status": item.status.value,
        "required": item.required,
        "exposed_tool_count": item.exposed_tool_count,
        "discovered_tool_count": item.discovered_tool_count,
        "resource_count": item.resource_count,
        "resource_template_count": item.resource_template_count,
        "prompt_count": item.prompt_count,
        "bounded_tool_name_overview": item.bounded_tool_name_overview,
        "sanitized_instructions": item.sanitized_instructions,
        "stable_failure_category": item.stable_failure_category,
        "tool_surface_semantic_fingerprint": (
            item.tool_surface_semantic_fingerprint
        ),
        "catalog_semantic_fingerprint": item.catalog_semantic_fingerprint,
    }


__all__ = [name for name in globals() if name.startswith("Mcp") or name.startswith("MAXIMUM_")]
