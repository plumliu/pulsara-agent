"""MCP catalog discovery, bounded normalization, and installation candidates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Mapping

from pulsara_agent.identity import derive_id
from pulsara_agent.mcp_config import (
    McpConfiguredEffect,
    McpInvalidToolPolicy,
    McpScopePolicy,
    McpServerConfig,
)
from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    canonical_json_bytes,
    context_fingerprint,
    freeze_json,
)

from ..tool_surface import (
    McpEffectKind,
    McpPolicyClassificationSource,
    McpToolExecutionPolicyFact,
    execution_policy_fingerprint,
)
from .contracts import (
    MAXIMUM_DISCOVERED_TOOLS_PER_SERVER,
    MAXIMUM_DISCOVERY_ITEMS_PER_SERVER,
    MAXIMUM_DISCOVERY_PAGES_PER_METHOD,
    MAXIMUM_MCP_CATALOG_RESULT_BYTES,
    MAXIMUM_MCP_INSTRUCTIONS_BYTES,
    McpAdvertisedCapabilities,
    McpDiscoverySnapshot,
    McpInstallationCandidate,
    McpPromptSemanticFact,
    McpProtocolConformanceError,
    McpResourceSemanticFact,
    McpResourceTemplateSemanticFact,
    McpToolSemanticFact,
    mcp_prompt_public_item,
    mcp_resource_public_item,
    mcp_resource_template_public_item,
)
from .naming import mangle_mcp_tool_names
from .schema import mcp_schema_validator
from .slots import McpSlotLease
from .wire import DEFAULT_MCP_WIRE_BOUNDS, validate_schema

if TYPE_CHECKING:
    from .sdk_facade import BoundedMcpSdkClient, types


_DEFAULT_MCP_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"


_SUPPORTED_MCP_SCHEMA_DIALECTS = frozenset(
    {
        "http://json-schema.org/draft-07/schema#",
        "https://json-schema.org/draft/2019-09/schema",
        _DEFAULT_MCP_SCHEMA_DIALECT,
    }
)


async def discover_mcp_catalog(
    client: BoundedMcpSdkClient,
    config: McpServerConfig,
) -> tuple[McpDiscoverySnapshot, tuple[McpToolExecutionPolicyFact, ...]]:
    budget = _DiscoveryBudget(
        remaining_items=MAXIMUM_DISCOVERY_ITEMS_PER_SERVER,
        remaining_bytes=(
            DEFAULT_MCP_WIRE_BOUNDS.maximum_discovery_candidate_bytes_per_server
        ),
    )
    advertised = client.advertised_capabilities
    if not isinstance(advertised, McpAdvertisedCapabilities):
        raise McpProtocolConformanceError("MCP_SERVER_CAPABILITIES_MISSING")
    tools = (
        await _list_pages(client, client.session.list_tools, "tools", budget=budget)
        if advertised.tools
        else ()
    )
    resources = (
        await _list_pages(
            client, client.session.list_resources, "resources", budget=budget
        )
        if advertised.resources
        else ()
    )
    templates = (
        await _list_optional_resource_templates(client, budget=budget)
        if advertised.resources
        else ()
    )
    prompts = (
        await _list_pages(
            client, client.session.list_prompts, "prompts", budget=budget
        )
        if advertised.prompts
        else ()
    )
    if len(tools) > MAXIMUM_DISCOVERED_TOOLS_PER_SERVER:
        raise ValueError("MCP discovered tool bound exceeded")
    if len(tools) + len(resources) + len(templates) + len(prompts) > MAXIMUM_DISCOVERY_ITEMS_PER_SERVER:
        raise ValueError("MCP discovery item bound exceeded")
    remote_names = tuple(item.name for item in tools)
    names = mangle_mcp_tool_names(config.server_id, remote_names)
    include = config.exposure_policy.include_tool_names
    excluded = set(config.exposure_policy.exclude_tool_names)
    selected = tuple(
        item
        for item in tools
        if (include is None or item.name in include) and item.name not in excluded
    )
    unknown_effect = set(dict(config.effect_policy.tool_effect_overrides)) - set(remote_names)
    unknown_timeout = set(dict(config.per_tool_timeout_ms)) - set(remote_names)
    unknown_include = (set(include) - set(remote_names)) if include is not None else set()
    if unknown_effect or unknown_timeout or unknown_include:
        raise ValueError("MCP config references an unknown remote tool")
    semantics: list[McpToolSemanticFact] = []
    policies: list[McpToolExecutionPolicyFact] = []
    invalid_tool_count = 0
    for item in selected:
        try:
            validate_schema(item.input_schema, DEFAULT_MCP_WIRE_BOUNDS)
            if item.input_schema.get("type") != "object":
                raise ValueError("MCP tool input schema is not an object")
            input_dialect = _mcp_schema_dialect(item.input_schema)
            mcp_schema_validator(item.input_schema)
            frozen = freeze_json(item.input_schema)
            if not isinstance(frozen, FrozenJsonObjectFact):
                raise TypeError("MCP tool schema did not freeze to an object")
            output_schema = None
            if item.output_schema is not None:
                validate_schema(item.output_schema, DEFAULT_MCP_WIRE_BOUNDS)
                _mcp_schema_dialect(item.output_schema)
                mcp_schema_validator(item.output_schema)
                output_schema = freeze_json(item.output_schema)
                if not isinstance(output_schema, FrozenJsonObjectFact):
                    raise TypeError("MCP output schema did not freeze to an object")
        except Exception:
            if (
                config.exposure_policy.invalid_tool_policy
                is McpInvalidToolPolicy.FAIL_SERVER
            ):
                raise
            invalid_tool_count += 1
            continue
        provider_name = names[item.name]
        public_description = _sanitize_text(item.description or "", 8 * 1024)
        descriptor = context_fingerprint(
            "mcp-tool-semantic:v1",
            {
                "server_id": config.server_id,
                "remote_tool_name": item.name,
                "provider_tool_name": provider_name,
                "description": public_description,
                "input_schema": frozen,
                "output_schema": output_schema,
                "schema_dialect": input_dialect,
                "scope_policy": config.scope_policy.value,
            },
        )
        semantic = McpToolSemanticFact(
            server_id=config.server_id,
            remote_tool_name=item.name,
            provider_tool_name=provider_name,
            description=public_description,
            input_schema=frozen,
            output_schema=output_schema,
            schema_dialect=input_dialect,
            descriptor_fingerprint=descriptor,
            root_visible=True,
            subagent_visible=(
                config.scope_policy is McpScopePolicy.ROOT_AND_SUBAGENTS
            ),
        )
        semantics.append(semantic)
        effect, source = _effect(config, item)
        timeout = dict(config.per_tool_timeout_ms).get(
            item.name, config.default_tool_timeout_ms
        )
        policies.append(
            McpToolExecutionPolicyFact(
                server_id=config.server_id,
                remote_tool_name=item.name,
                provider_tool_name=provider_name,
                tool_semantic_fingerprint=descriptor,
                effect_kind=effect,
                timeout_ms=timeout,
                parallel_safe=config.supports_parallel_tool_calls,
                classification_source=source,
            )
        )
    semantic_tuple = tuple(sorted(semantics, key=lambda item: item.provider_tool_name))
    policy_tuple = tuple(sorted(policies, key=lambda item: item.provider_tool_name))
    resource_facts = tuple(
        _resource(config.server_id, item) for item in sorted(resources, key=lambda item: str(item.uri))
    )
    template_facts = tuple(
        _resource_template(config.server_id, item)
        for item in sorted(templates, key=lambda item: item.uri_template)
    )
    prompt_facts = tuple(
        _prompt(config.server_id, item) for item in sorted(prompts, key=lambda item: item.name)
    )
    instructions = _sanitize_text(client.server_instructions, MAXIMUM_MCP_INSTRUCTIONS_BYTES)
    tool_fp = context_fingerprint(
        "mcp-server-tool-surface:v1",
        tuple(item.descriptor_fingerprint for item in semantic_tuple),
    )
    catalog_fp = context_fingerprint(
        "mcp-server-catalog:v1",
        {
            "server_id": config.server_id,
            "display_name": config.display_name,
            "instructions": instructions,
            "tools": tuple(item.descriptor_fingerprint for item in semantic_tuple),
            "resources": tuple(item.semantic_fingerprint for item in resource_facts),
            "templates": tuple(item.semantic_fingerprint for item in template_facts),
            "prompts": tuple(item.semantic_fingerprint for item in prompt_facts),
        },
    )
    snapshot = McpDiscoverySnapshot(
        server_id=config.server_id,
        display_name=config.display_name,
        protocol_version=client.protocol_version,
        sanitized_instructions=instructions,
        discovered_tool_count=len(tools),
        invalid_tool_count=invalid_tool_count,
        tools=semantic_tuple,
        resources=resource_facts,
        resource_templates=template_facts,
        prompts=prompt_facts,
        tool_surface_semantic_fingerprint=tool_fp,
        catalog_semantic_fingerprint=catalog_fp,
        presentation_fingerprint=context_fingerprint(
            "mcp-server-presentation:v1",
            (config.server_id, config.display_name, len(semantic_tuple)),
        ),
        sdk_conformance_contract_fingerprint=context_fingerprint(
            "mcp-sdk-conformance:v2",
            (
                "mcp==2.1.0",
                "mcp-types==2.1.0",
                "modern-explicit-result-type",
                "negotiated-legacy-implicit-complete",
            ),
        ),
    )
    if _discovery_snapshot_physical_bytes(snapshot) > (
        DEFAULT_MCP_WIRE_BOUNDS.maximum_discovery_candidate_bytes_per_server
    ):
        raise ValueError("MCP discovery candidate byte bound exceeded")
    client.complete_tool_catalog(tools)
    return snapshot, policy_tuple


@dataclass(slots=True)
class _DiscoveryBudget:
    remaining_items: int
    remaining_bytes: int

    def charge(self, item: object) -> None:
        if self.remaining_items <= 0:
            raise ValueError("MCP discovery item bound exceeded")
        model_dump = getattr(item, "model_dump", None)
        if not callable(model_dump):
            raise ValueError("MCP discovery item carrier is invalid")
        physical_bytes = len(
            canonical_json_bytes(
                model_dump(by_alias=True, mode="json", exclude_none=True)
            )
        )
        if physical_bytes > self.remaining_bytes:
            raise ValueError("MCP discovery candidate byte bound exceeded")
        self.remaining_items -= 1
        self.remaining_bytes -= physical_bytes


async def _list_pages(
    client: BoundedMcpSdkClient,
    method: object,
    field_name: str,
    *,
    budget: _DiscoveryBudget,
    optional_method: bool = False,
) -> tuple[object, ...]:
    from .sdk_facade import MCPError, types

    cursor: str | None = None
    items: list[object] = []
    seen: set[str] = set()
    for _ in range(MAXIMUM_DISCOVERY_PAGES_PER_METHOD):
        params = (
            types.PaginatedRequestParams(cursor=cursor)
            if cursor is not None
            else None
        )
        try:
            result = await method(params=params)  # type: ignore[operator]
        except MCPError as exc:
            # A server can expose concrete resources without a template-list
            # method in either protocol era. Only its first-page -32601 proves
            # absence; all other errors (including mid-pagination) must fail.
            if optional_method and cursor is None and exc.code == types.METHOD_NOT_FOUND:
                return ()
            raise
        if client.require_closed_result_type(result) != "complete":
            raise RuntimeError("MCP listing cannot require input")
        page = getattr(result, field_name)
        if not isinstance(page, list):
            raise ValueError("MCP discovery page shape is invalid")
        for item in page:
            budget.charge(item)
        items.extend(page)
        cursor = result.next_cursor
        if cursor is None:
            return tuple(items)
        if cursor in seen or len(cursor.encode("utf-8")) > 4096:
            raise ValueError("MCP discovery cursor is invalid")
        seen.add(cursor)
    raise ValueError("MCP discovery page bound exceeded")


async def _list_optional_resource_templates(
    client: BoundedMcpSdkClient,
    *,
    budget: _DiscoveryBudget,
) -> tuple[object, ...]:
    return await _list_pages(
        client,
        client.session.list_resource_templates,
        "resource_templates",
        budget=budget,
        optional_method=True,
    )


def _candidate(
    *,
    config: McpServerConfig,
    epoch: str,
    attempt_generation: int,
    lease: McpSlotLease,
    snapshot: McpDiscoverySnapshot,
    policies: tuple[McpToolExecutionPolicyFact, ...],
) -> McpInstallationCandidate:
    normalized_physical_bytes = _discovery_snapshot_physical_bytes(snapshot)
    fingerprint = context_fingerprint(
        "mcp-installation-candidate:v1",
        {
            "server_id": config.server_id,
            "epoch": epoch,
            "semantic_config": config.semantic_config_fingerprint,
            "runtime_config": config.runtime_config_fingerprint,
            "resolved_config": config.resolved_config_identity,
            "attempt_generation": attempt_generation,
            "slot_lease_identity": lease.lease_identity,
            "tool_surface": snapshot.tool_surface_semantic_fingerprint,
            "catalog": snapshot.catalog_semantic_fingerprint,
            "conformance": snapshot.sdk_conformance_contract_fingerprint,
            "policies": tuple(execution_policy_fingerprint(item) for item in policies),
            "standard_read_timeout_ms": config.default_tool_timeout_ms,
            "normalized_physical_bytes": normalized_physical_bytes,
        },
    )
    return McpInstallationCandidate(
        candidate_id=derive_id("mcp-candidate", "installation", fingerprint),
        server_id=config.server_id,
        expected_supervisor_epoch=epoch,
        expected_semantic_config_fingerprint=config.semantic_config_fingerprint,
        expected_runtime_config_fingerprint=config.runtime_config_fingerprint,
        expected_resolved_config_identity=config.resolved_config_identity,
        attempt_generation=attempt_generation,
        slot_lease=lease,
        discovery_snapshot=snapshot,
        ordered_tool_execution_policies=policies,
        standard_read_timeout_ms=config.default_tool_timeout_ms,
        normalized_physical_bytes=normalized_physical_bytes,
    )


def _effect(
    config: McpServerConfig, tool: types.Tool
) -> tuple[McpEffectKind, McpPolicyClassificationSource]:
    overrides = dict(config.effect_policy.tool_effect_overrides)
    if tool.name in overrides:
        return McpEffectKind(overrides[tool.name].value), McpPolicyClassificationSource.TOOL_OVERRIDE
    if config.effect_policy.default_effect is not McpConfiguredEffect.AUTO:
        return McpEffectKind(config.effect_policy.default_effect.value), McpPolicyClassificationSource.SERVER_OVERRIDE
    # Server hints describe tools; they do not grant Host execution permission.
    # With no explicit Host override, AUTO remains conservatively external.
    return McpEffectKind.EXTERNAL_EFFECT, McpPolicyClassificationSource.HOST_DEFAULT


def _resource(server_id: str, item: types.Resource) -> McpResourceSemanticFact:
    payload = {
        "server_id": server_id,
        "uri": _bounded_public_identity(str(item.uri), 32 * 1024),
        "name": _bounded_public_identity(item.name, 8 * 1024),
        "description": _sanitize_text(item.description or "", 8 * 1024),
        "mime_type": (
            _bounded_public_identity(item.mime_type, 1024)
            if item.mime_type is not None
            else None
        ),
    }
    fact = McpResourceSemanticFact(
        **payload,
        semantic_fingerprint=context_fingerprint("mcp-resource:v1", payload),
    )
    _assert_catalog_item_bound(mcp_resource_public_item(server_id, fact))
    return fact


def _resource_template(
    server_id: str, item: types.ResourceTemplate
) -> McpResourceTemplateSemanticFact:
    payload = {
        "server_id": server_id,
        "uri_template": _bounded_public_identity(item.uri_template, 32 * 1024),
        "name": _bounded_public_identity(item.name, 8 * 1024),
        "description": _sanitize_text(item.description or "", 8 * 1024),
        "mime_type": (
            _bounded_public_identity(item.mime_type, 1024)
            if item.mime_type is not None
            else None
        ),
    }
    fact = McpResourceTemplateSemanticFact(
        **payload,
        semantic_fingerprint=context_fingerprint("mcp-resource-template:v1", payload),
    )
    _assert_catalog_item_bound(mcp_resource_template_public_item(server_id, fact))
    return fact


def _prompt(server_id: str, item: types.Prompt) -> McpPromptSemanticFact:
    arguments = tuple(
        (
            _bounded_public_identity(arg.name, 8 * 1024),
            _sanitize_text(arg.description or "", 8 * 1024),
            bool(arg.required),
        )
        for arg in (item.arguments or [])
    )
    payload = {
        "server_id": server_id,
        "name": _bounded_public_identity(item.name, 8 * 1024),
        "description": _sanitize_text(item.description or "", 8 * 1024),
        "arguments": arguments,
    }
    fact = McpPromptSemanticFact(
        **payload,
        semantic_fingerprint=context_fingerprint("mcp-prompt:v1", payload),
    )
    _assert_catalog_item_bound(mcp_prompt_public_item(server_id, fact))
    return fact


def _assert_catalog_item_bound(item: object) -> None:
    if len(canonical_json_bytes({"items": [item]})) > (
        MAXIMUM_MCP_CATALOG_RESULT_BYTES
    ):
        raise ValueError("MCP catalog item exceeds the product result bound")


def _bounded_public_identity(value: str, maximum_bytes: int) -> str:
    if not value or len(value.encode("utf-8")) > maximum_bytes:
        raise ValueError("MCP public identity exceeds its product bound")
    if any(
        ord(character) < 0x20
        or 0x7F <= ord(character) <= 0x9F
        for character in value
    ):
        raise ValueError("MCP public identity contains terminal controls")
    return value


def _mcp_schema_dialect(value: Mapping[str, object]) -> str:
    raw = value.get("$schema", _DEFAULT_MCP_SCHEMA_DIALECT)
    if not isinstance(raw, str) or raw not in _SUPPORTED_MCP_SCHEMA_DIALECTS:
        raise ValueError("unsupported MCP JSON Schema dialect")
    return raw


def _sanitize_text(value: str, maximum_bytes: int) -> str:
    safe = "".join(
        character
        if character in {"\n", "\t"}
        or (ord(character) >= 0x20 and not 0x7F <= ord(character) <= 0x9F)
        else "�"
        for character in value
    )
    encoded = safe.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return safe
    return encoded[:maximum_bytes].decode("utf-8", "ignore")


def _discovery_snapshot_physical_bytes(snapshot: McpDiscoverySnapshot) -> int:
    """Quote the immutable normalized candidate before installation."""

    return len(
        canonical_json_bytes(
            {
                "server_id": snapshot.server_id,
                "display_name": snapshot.display_name,
                "protocol_version": snapshot.protocol_version,
                "instructions": snapshot.sanitized_instructions,
                "discovered_tool_count": snapshot.discovered_tool_count,
                "invalid_tool_count": snapshot.invalid_tool_count,
                "tools": tuple(
                    {
                        "server_id": item.server_id,
                        "remote_name": item.remote_tool_name,
                        "provider_name": item.provider_tool_name,
                        "description": item.description,
                        "input_schema": item.input_schema,
                        "output_schema": item.output_schema,
                        "descriptor": item.descriptor_fingerprint,
                    }
                    for item in snapshot.tools
                ),
                "resources": tuple(
                    (
                        item.server_id,
                        item.uri,
                        item.name,
                        item.description,
                        item.mime_type,
                        item.semantic_fingerprint,
                    )
                    for item in snapshot.resources
                ),
                "resource_templates": tuple(
                    (
                        item.server_id,
                        item.uri_template,
                        item.name,
                        item.description,
                        item.mime_type,
                        item.semantic_fingerprint,
                    )
                    for item in snapshot.resource_templates
                ),
                "prompts": tuple(
                    (
                        item.server_id,
                        item.name,
                        item.description,
                        item.arguments,
                        item.semantic_fingerprint,
                    )
                    for item in snapshot.prompts
                ),
                "tool_surface": snapshot.tool_surface_semantic_fingerprint,
                "catalog": snapshot.catalog_semantic_fingerprint,
                "conformance": snapshot.sdk_conformance_contract_fingerprint,
            }
        )
    )
