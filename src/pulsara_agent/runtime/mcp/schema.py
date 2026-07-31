"""Bounded SDK-conformed MCP tool schema and result validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from jsonschema import Draft7Validator, Draft201909Validator, Draft202012Validator
from jsonschema.exceptions import SchemaError

from pulsara_agent.primitives._context_base import (
    FrozenJsonObjectFact,
    context_fingerprint,
    freeze_json,
    thaw_json,
)
from pulsara_agent.primitives.mcp_protocol import (
    DEFAULT_MCP_JSON_SCHEMA_DIALECT,
    McpProviderProjectionDisposition,
    McpProviderSchemaProjectionFact,
    McpToolSemanticFact,
    McpToolWireRejectionCode,
    build_mcp_protocol_fact,
)


MAX_MCP_SCHEMA_BYTES = 256 * 1024
MAX_MCP_SCHEMA_NODES = 4_096
MAX_MCP_SCHEMA_DEPTH = 64
MCP_SDK_CONFORMANCE_CONTRACT_FINGERPRINT = context_fingerprint(
    "mcp-sdk-conformance-contract:v1",
    {
        "sdk": "2.0.0",
        "listing": "sdk-list-tools-conformed",
        "header_routing": "sdk-owned-x-mcp-header-v1",
    },
)
MCP_OUTPUT_VALIDATION_CONTRACT_FINGERPRINT = context_fingerprint(
    "mcp-output-validation-contract:v1",
    {"dialects": ("draft-07", "2019-09", "2020-12"), "external_refs": False},
)
MCP_PROVIDER_SCHEMA_CONTRACT_FINGERPRINT = context_fingerprint(
    "mcp-provider-schema-contract:v1",
    {"projection": "identity-json-schema-object-v1"},
)


_DIALECTS = {
    "http://json-schema.org/draft-07/schema#": Draft7Validator,
    "https://json-schema.org/draft/2019-09/schema": Draft201909Validator,
    "https://json-schema.org/draft/2020-12/schema": Draft202012Validator,
}


class McpSchemaContractError(ValueError):
    def __init__(self, code: McpToolWireRejectionCode, message: str) -> None:
        self.code = code
        super().__init__(message)


class McpOutputSchemaMismatch(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class McpConformedToolSchema:
    semantic: McpToolSemanticFact
    provider_projection: McpProviderSchemaProjectionFact


def build_conformed_tool_schema(
    *,
    server_id: str,
    name: str,
    title: str | None,
    description: str | None,
    input_schema: Mapping[str, object],
    output_schema: Mapping[str, object] | None,
    annotations: Mapping[str, object] | None,
    icons: tuple[Mapping[str, object], ...] = (),
    execution: Mapping[str, object] | None = None,
    protocol_meta: Mapping[str, object] | None = None,
) -> McpConformedToolSchema:
    input_frozen, input_dialect = _validate_schema(input_schema, input_schema=True)
    output_frozen: FrozenJsonObjectFact | None = None
    output_dialect: str | None = None
    if output_schema is not None:
        output_frozen, output_dialect = _validate_schema(
            output_schema,
            input_schema=False,
        )
    semantic = build_mcp_protocol_fact(
        McpToolSemanticFact,
        schema_version="mcp_tool_semantic.v1",
        server_id=server_id,
        name=name,
        title=title,
        description=(description or "").strip() or f"MCP tool {name}",
        input_schema=input_frozen,
        input_schema_dialect=input_dialect,
        output_schema=output_frozen,
        output_schema_dialect=output_dialect,
        annotations=_freeze_object(annotations or {}),
        icons=tuple(_freeze_object(item) for item in icons),
        execution=_freeze_object(execution) if execution is not None else None,
        protocol_meta=_freeze_object(protocol_meta) if protocol_meta is not None else None,
    )
    projection = build_mcp_protocol_fact(
        McpProviderSchemaProjectionFact,
        schema_version="mcp_provider_schema_projection.v1",
        tool_semantic_fingerprint=semantic.tool_semantic_fingerprint,
        provider_schema_contract_fingerprint=MCP_PROVIDER_SCHEMA_CONTRACT_FINGERPRINT,
        disposition=McpProviderProjectionDisposition.EXACTLY_SUPPORTED,
        projected_schema=input_frozen,
        lossless_proof_fingerprint=context_fingerprint(
            "mcp-provider-schema-lossless-proof:v1",
            {
                "tool": semantic.tool_semantic_fingerprint,
                "schema": thaw_json(input_frozen),
            },
        ),
        reason_code=None,
    )
    return McpConformedToolSchema(semantic=semantic, provider_projection=projection)


def validate_structured_tool_result(
    *,
    tool: McpToolSemanticFact,
    structured_content_present: bool,
    structured_content: object,
) -> None:
    if tool.output_schema is None:
        return
    if not structured_content_present:
        raise McpOutputSchemaMismatch(
            "tool declared outputSchema but structuredContent was absent"
        )
    schema = thaw_json(tool.output_schema)
    validator = _validator_for_dialect(tool.output_schema_dialect)(schema)
    errors = tuple(validator.iter_errors(structured_content))
    if errors:
        raise McpOutputSchemaMismatch(errors[0].message)


def _validate_schema(
    schema: Mapping[str, object],
    *,
    input_schema: bool,
) -> tuple[FrozenJsonObjectFact, str]:
    if not isinstance(schema, Mapping):
        raise McpSchemaContractError(
            McpToolWireRejectionCode.INVALID_INPUT_SCHEMA
            if input_schema
            else McpToolWireRejectionCode.INVALID_OUTPUT_SCHEMA,
            "MCP schema container must be a JSON object",
        )
    payload = dict(schema)
    if input_schema and payload.get("type") != "object":
        raise McpSchemaContractError(
            McpToolWireRejectionCode.INVALID_INPUT_SCHEMA,
            'MCP inputSchema must declare root type "object"',
        )
    try:
        frozen = _freeze_object(payload)
        _validate_bounds(payload)
        _reject_external_references(payload, input_schema=input_schema)
        dialect = _resolved_dialect(payload)
        _validator_for_dialect(dialect).check_schema(payload)
    except McpSchemaContractError:
        raise
    except (SchemaError, TypeError, ValueError) as exc:
        raise McpSchemaContractError(
            McpToolWireRejectionCode.INVALID_INPUT_SCHEMA
            if input_schema
            else McpToolWireRejectionCode.INVALID_OUTPUT_SCHEMA,
            str(exc),
        ) from exc
    return frozen, dialect


def _resolved_dialect(schema: Mapping[str, object]) -> str:
    raw = schema.get("$schema", DEFAULT_MCP_JSON_SCHEMA_DIALECT)
    if not isinstance(raw, str) or raw not in _DIALECTS:
        raise McpSchemaContractError(
            McpToolWireRejectionCode.UNSUPPORTED_DIALECT,
            f"unsupported MCP JSON Schema dialect: {raw!r}",
        )
    return raw


def _validator_for_dialect(dialect: str | None):
    if dialect is None:
        raise ValueError("schema dialect is required")
    try:
        return _DIALECTS[dialect]
    except KeyError as exc:
        raise McpSchemaContractError(
            McpToolWireRejectionCode.UNSUPPORTED_DIALECT,
            f"unsupported MCP JSON Schema dialect: {dialect!r}",
        ) from exc


def _validate_bounds(value: object) -> None:
    from pulsara_agent.primitives._context_base import canonical_json_bytes

    if len(canonical_json_bytes(value)) > MAX_MCP_SCHEMA_BYTES:
        raise McpSchemaContractError(
            McpToolWireRejectionCode.SCHEMA_BOUNDS_EXCEEDED,
            "MCP schema exceeds byte bound",
        )
    nodes = 0
    stack: list[tuple[object, int]] = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > MAX_MCP_SCHEMA_NODES or depth > MAX_MCP_SCHEMA_DEPTH:
            raise McpSchemaContractError(
                McpToolWireRejectionCode.SCHEMA_BOUNDS_EXCEEDED,
                "MCP schema exceeds structural bounds",
            )
        if isinstance(item, Mapping):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend((child, depth + 1) for child in item)


def _reject_external_references(value: object, *, input_schema: bool) -> None:
    if isinstance(value, Mapping):
        ref = value.get("$ref")
        if isinstance(ref, str) and not ref.startswith("#"):
            raise McpSchemaContractError(
                McpToolWireRejectionCode.INVALID_INPUT_SCHEMA
                if input_schema
                else McpToolWireRejectionCode.INVALID_OUTPUT_SCHEMA,
                "external MCP schema references are disabled",
            )
        for child in value.values():
            _reject_external_references(child, input_schema=input_schema)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _reject_external_references(child, input_schema=input_schema)


def _freeze_object(value: Mapping[str, object]) -> FrozenJsonObjectFact:
    frozen = freeze_json(dict(value))
    if not isinstance(frozen, FrozenJsonObjectFact):
        raise TypeError("expected frozen JSON object")
    return frozen


__all__ = [
    "MCP_OUTPUT_VALIDATION_CONTRACT_FINGERPRINT",
    "MCP_PROVIDER_SCHEMA_CONTRACT_FINGERPRINT",
    "MCP_SDK_CONFORMANCE_CONTRACT_FINGERPRINT",
    "McpConformedToolSchema",
    "McpOutputSchemaMismatch",
    "McpSchemaContractError",
    "build_conformed_tool_schema",
    "validate_structured_tool_result",
]
