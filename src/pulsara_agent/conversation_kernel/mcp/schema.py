"""Local-only MCP schema admission and validation.

jsonschema/referencing own dialects, pointers, anchors and embedded resource
bases. Pulsara admits only document-local references and disables retrieval.
"""

from __future__ import annotations

from jsonschema import validators
from referencing import Registry, Resource
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012, specification_with


# An explicit Registry uses referencing's no-retrieval default. Unlike the
# jsonschema default registry, it never opens URLs or local files.
LOCAL_MCP_SCHEMA_REGISTRY = Registry()
_REFERENCE_KEYWORDS = ("$ref", "$dynamicRef", "$recursiveRef")


class McpOutputSchemaMismatch(ValueError):
    """The SDK received a tool response whose successful output is invalid."""


class McpSchemaEvaluationFailure(RuntimeError):
    """An admitted schema could not be evaluated by the dependency."""


def require_local_mcp_schema_references(schema: dict[str, object]) -> None:
    """Resolve supported schema references within the supplied document only.

    referencing owns dialect-specific subresource traversal and $id base changes;
    data inside const/default/examples is not mistaken for a schema. Only
    fragment references are admitted, preserving the existing external-reference
    policy while also covering the newer dynamic and recursive vocabularies.
    """

    root = Resource.from_contents(schema, default_specification=DRAFT202012)
    registry = LOCAL_MCP_SCHEMA_REGISTRY.with_resource(root.id() or "", root).crawl()
    default_dialect = "https://json-schema.org/draft/2020-12/schema"
    pending = [(root, registry.resolver_with_root(root), default_dialect)]
    seen = set()
    while pending:
        resource, resolver, inherited_dialect = pending.pop()
        contents = resource.contents
        dialect = (
            contents.get("$schema", inherited_dialect)
            if isinstance(contents, dict)
            else inherited_dialect
        )
        identity = (id(contents), dialect)
        if identity in seen:
            continue
        seen.add(identity)
        if isinstance(contents, dict):
            for keyword in _REFERENCE_KEYWORDS:
                reference = contents.get(keyword)
                if not isinstance(reference, str):
                    continue
                if not reference.startswith("#"):
                    raise ValueError("external MCP schema references are unsupported")
                try:
                    resolved = resolver.lookup(reference)
                except Unresolvable as exc:
                    raise ValueError("MCP schema reference is not document-local") from exc
                # A JSON pointer may reach a schema in a custom container that
                # subresources() does not enumerate. Inspect that actual target
                # too; referencing still owns pointer and base resolution.
                target = Resource.from_contents(
                    resolved.contents,
                    default_specification=specification_with(dialect),
                )
                pending.append((target, resolved.resolver, dialect))
        pending.extend(
            (child, resolver.in_subresource(child), dialect)
            for child in resource.subresources()
        )


def mcp_schema_validator(schema: dict[str, object]):
    """Use jsonschema's validator with external retrieval disabled."""

    require_local_mcp_schema_references(schema)
    validator_type = validators.validator_for(schema)
    validator_type.check_schema(schema)
    return validator_type(schema, registry=LOCAL_MCP_SCHEMA_REGISTRY)


def validate_mcp_arguments(schema: dict[str, object], arguments: dict[str, object]) -> None:
    """Keep dependency evaluation failures local to the requested MCP tool.

    A document-local recursive schema is valid metadata, but non-progressing
    references can exhaust Python's recursion capacity during evaluation. This
    classifies that existing physical failure without restricting ordinary
    recursive schemas or adding a product depth/call bound.
    """

    try:
        mcp_schema_validator(schema).validate(arguments)
    except RecursionError as exc:
        raise McpSchemaEvaluationFailure(
            "MCP_SCHEMA_EVALUATION_FAILED: recursive schema could not be evaluated"
        ) from exc
