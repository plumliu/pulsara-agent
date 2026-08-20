"""One provider-neutral OpenAI function-tool wire contract.

OpenAI-compatible endpoints do not all make the same implicit choice when the
``strict`` member is omitted.  In particular, Responses implementations may
attempt Structured Outputs normalization even though Chat Completions treats
the same JSON Schema as non-strict by default.  Pulsara keeps its exact input
schema and validation authority locally, and explicitly requests the OpenAI
documented non-strict function-calling mode on both wire APIs.
"""

from __future__ import annotations

from copy import deepcopy
from time import monotonic
from typing import Any

from pulsara_agent.capability.contracts import (
    FrozenNativeToolProjectionSet,
    FrozenNativeToolWireEligibilityQuote,
    FrozenNativeToolWireEligibilitySet,
    FrozenNativeToolWireIncompatibility,
    FrozenNativeToolWireProjection,
    FrozenToolCapabilityFact,
    ToolCapabilityVersionRef,
    NativeToolWireIncompatibilityReason,
    canonical_tool_spec_fingerprint,
    frozen_tool_spec_fingerprint,
    native_tool_projection_set_fingerprint,
    tool_capability_version_ref,
)
from pulsara_agent.llm.input import ToolSpec
from pulsara_agent.model_input.contracts import FrozenToolSpec, ModelInputScopeKind
from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    canonical_json_bytes,
    context_fingerprint,
    freeze_json,
    thaw_json,
)


OPENAI_FUNCTION_TOOL_WIRE_CONTRACT_VERSION = (
    "v2-explicit-non-strict-prevalidated-lowering"
)


class OpenAIFunctionSchemaIncompatible(ValueError):
    """A canonical object schema has no honest portable wire projection."""


_ROOT_UNION_BRANCH_KEYS = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "description",
        "title",
        "$comment",
        "deprecated",
        "readOnly",
        "writeOnly",
        "examples",
        "default",
    }
)


def _same_json(left: object, right: object) -> bool:
    return canonical_json_bytes(left) == canonical_json_bytes(right)


def _merge_property_schema(
    left: dict[str, Any], right: dict[str, Any]
) -> dict[str, Any]:
    if _same_json(left, right):
        return left
    left_without_enum = {key: value for key, value in left.items() if key != "enum"}
    right_without_enum = {
        key: value for key, value in right.items() if key != "enum"
    }
    left_enum = left.get("enum")
    right_enum = right.get("enum")
    if (
        isinstance(left_enum, list)
        and isinstance(right_enum, list)
        and _same_json(left_without_enum, right_without_enum)
    ):
        merged = deepcopy(left_without_enum)
        merged["enum"] = []
        for value in (*left_enum, *right_enum):
            if not any(_same_json(value, item) for item in merged["enum"]):
                merged["enum"].append(deepcopy(value))
        return merged

    alternatives: list[dict[str, Any]] = []
    for value in (left, right):
        nested = value.get("anyOf")
        candidates = nested if set(value) == {"anyOf"} and isinstance(nested, list) else [value]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise OpenAIFunctionSchemaIncompatible(
                    "OpenAI function property union is not an object"
                )
            if not any(_same_json(candidate, item) for item in alternatives):
                alternatives.append(deepcopy(candidate))
    return {"anyOf": alternatives}


def _lower_schema_node(value: object) -> object:
    if isinstance(value, list):
        return [_lower_schema_node(item) for item in value]
    if not isinstance(value, dict):
        return value
    if "oneOf" in value and "anyOf" in value:
        raise OpenAIFunctionSchemaIncompatible(
            "OpenAI function schema cannot combine oneOf and anyOf at one node"
        )
    result: dict[str, Any] = {}
    constant_present = "const" in value
    constant = value.get("const")
    for key, nested in value.items():
        if key in {"discriminator", "const"}:
            continue
        if key in {"oneOf", "anyOf"} and (
            not isinstance(nested, list) or not nested
        ):
            raise OpenAIFunctionSchemaIncompatible(
                "OpenAI function schema union must be a non-empty array"
            )
        target_key = "anyOf" if key == "oneOf" else key
        result[target_key] = _lower_schema_node(nested)
    if constant_present:
        existing = result.get("enum")
        if existing is not None and (
            not isinstance(existing, list)
            or not any(_same_json(constant, item) for item in existing)
        ):
            raise OpenAIFunctionSchemaIncompatible(
                "OpenAI function schema const conflicts with enum"
            )
        result["enum"] = [deepcopy(constant)]
    return result


def _root_union_requirement_description(
    *, branches: list[dict[str, Any]], discriminator: str | None
) -> str | None:
    if discriminator is None:
        return None
    clauses: list[str] = []
    for branch in branches:
        properties = branch.get("properties")
        if not isinstance(properties, dict):
            return None
        discriminator_schema = properties.get(discriminator)
        if not isinstance(discriminator_schema, dict):
            return None
        values = discriminator_schema.get("enum")
        if not isinstance(values, list) or len(values) != 1:
            return None
        required = branch.get("required", [])
        if not isinstance(required, list) or any(
            not isinstance(item, str) for item in required
        ):
            return None
        action_requirements = [item for item in required if item != discriminator]
        requirement = ", ".join(action_requirements) if action_requirements else "none"
        clauses.append(
            f"When {discriminator} is {values[0]!r}, required fields: {requirement}."
        )
    return " ".join(clauses)


def _lower_root_object_union(parameters: dict[str, Any]) -> dict[str, Any]:
    raw_one_of = parameters.get("oneOf")
    raw_any_of = parameters.get("anyOf")
    if raw_one_of is not None and raw_any_of is not None:
        raise OpenAIFunctionSchemaIncompatible(
            "OpenAI function root cannot combine oneOf and anyOf"
        )
    raw_branches = raw_one_of if raw_one_of is not None else raw_any_of
    if raw_branches is None:
        lowered = _lower_schema_node(parameters)
        if not isinstance(lowered, dict):  # pragma: no cover - input is closed above
            raise TypeError("OpenAI function schema did not remain an object")
        return lowered
    if not isinstance(raw_branches, list) or not raw_branches:
        raise OpenAIFunctionSchemaIncompatible(
            "OpenAI function root union must have object branches"
        )

    lowered_branches: list[dict[str, Any]] = []
    for raw_branch in raw_branches:
        lowered = _lower_schema_node(raw_branch)
        if not isinstance(lowered, dict) or lowered.get("type") not in {
            None,
            "object",
        }:
            raise OpenAIFunctionSchemaIncompatible(
                "OpenAI function root union branches must constrain objects"
            )
        unsupported_branch_keys = set(lowered).difference(
            _ROOT_UNION_BRANCH_KEYS
        )
        if unsupported_branch_keys:
            raise OpenAIFunctionSchemaIncompatible(
                "OpenAI function root union branch has non-portable constraints"
            )
        if not isinstance(lowered.get("properties", {}), dict):
            raise OpenAIFunctionSchemaIncompatible(
                "OpenAI function root union properties must be an object"
            )
        required = lowered.get("required", [])
        if not isinstance(required, list) or any(
            not isinstance(item, str) for item in required
        ):
            raise OpenAIFunctionSchemaIncompatible(
                "OpenAI function root union required list is invalid"
            )
        additional = lowered.get("additionalProperties", True)
        if not isinstance(additional, bool):
            raise OpenAIFunctionSchemaIncompatible(
                "OpenAI function root union additionalProperties must be boolean"
            )
        lowered_branches.append(lowered)

    base = {
        key: nested
        for key, nested in parameters.items()
        if key
        not in {
            "oneOf",
            "anyOf",
            "discriminator",
            "properties",
            "required",
            "additionalProperties",
        }
    }
    lowered_base = _lower_schema_node(base)
    if not isinstance(lowered_base, dict):  # pragma: no cover - base is an object
        raise TypeError("OpenAI function schema base did not remain an object")
    result: dict[str, Any] = lowered_base
    result["type"] = "object"

    properties: dict[str, Any] = {}
    raw_base_properties = parameters.get("properties", {})
    if not isinstance(raw_base_properties, dict):
        raise OpenAIFunctionSchemaIncompatible(
            "OpenAI function root properties must be an object"
        )
    lowered_base_properties = _lower_schema_node(raw_base_properties)
    if not isinstance(lowered_base_properties, dict):
        raise TypeError("OpenAI function properties did not remain an object")
    properties.update(lowered_base_properties)
    for branch in lowered_branches:
        branch_properties = branch.get("properties", {})
        for name, schema in branch_properties.items():
            if not isinstance(name, str) or not isinstance(schema, dict):
                raise OpenAIFunctionSchemaIncompatible(
                    "OpenAI function property schema is invalid"
                )
            existing = properties.get(name)
            properties[name] = (
                deepcopy(schema)
                if existing is None
                else _merge_property_schema(existing, schema)
            )
    result["properties"] = properties

    base_required = parameters.get("required", [])
    if not isinstance(base_required, list) or any(
        not isinstance(item, str) for item in base_required
    ):
        raise OpenAIFunctionSchemaIncompatible(
            "OpenAI function root required list is invalid"
        )
    common_required = set(lowered_branches[0].get("required", []))
    for branch in lowered_branches[1:]:
        common_required.intersection_update(branch.get("required", []))
    required_set = {*base_required, *common_required}
    result["required"] = [name for name in properties if name in required_set]

    base_additional = parameters.get("additionalProperties", True)
    if not isinstance(base_additional, bool):
        raise OpenAIFunctionSchemaIncompatible(
            "OpenAI function root additionalProperties must be boolean"
        )
    branch_additional = tuple(
        branch.get("additionalProperties", True) for branch in lowered_branches
    )
    # The provider schema may be a superset because Pulsara's immutable local
    # descriptor remains the exact argument authority.  It must never be a
    # narrower set than the canonical MCP/builtin schema: preserve an open
    # root whenever any canonical branch admits unknown properties.
    result["additionalProperties"] = not (
        base_additional is False
        or all(value is False for value in branch_additional)
    )

    raw_discriminator = parameters.get("discriminator")
    discriminator = (
        raw_discriminator.get("propertyName")
        if isinstance(raw_discriminator, dict)
        and isinstance(raw_discriminator.get("propertyName"), str)
        else None
    )
    requirements = _root_union_requirement_description(
        branches=lowered_branches, discriminator=discriminator
    )
    if requirements:
        existing_description = result.get("description")
        result["description"] = (
            f"{existing_description} {requirements}"
            if isinstance(existing_description, str) and existing_description
            else requirements
        )
    return result


def lower_openai_function_parameters(parameters: dict[str, Any]) -> dict[str, Any]:
    """Lower one function schema to OpenAI's portable root-object shape.

    OpenAI's documented Structured Outputs subset permits nested ``anyOf`` but
    forbids a root union.  Root object branches are therefore merged into one
    best-effort provider schema while their exact discriminated-union semantics
    stay in Pulsara's local parser.  No provider name participates in this
    deterministic transformation.
    """

    cloned = deepcopy(parameters)
    declared_type = cloned.get("type")
    if declared_type not in {None, "object"}:
        raise OpenAIFunctionSchemaIncompatible(
            "OpenAI function parameters must have an object root"
        )
    lowered = _lower_root_object_union(cloned)
    lowered.setdefault("type", "object")
    return lowered


def openai_function_definition(tool: ToolSpec) -> dict[str, Any]:
    """Return the shared function definition used by Chat and Responses.

    Function arguments are always JSON objects.  The wire schema may be a
    deterministic superset where OpenAI's portable shape cannot express a
    canonical root union; the immutable local descriptor and strict argument
    parser remain the exact execution truth.
    """

    parameters = lower_openai_function_parameters(tool.parameters)
    return {
        "name": tool.name,
        "description": tool.description,
        "parameters": parameters,
        # OpenAI documents that Responses otherwise attempts strict-schema
        # normalization while Chat remains non-strict by default.  Make the
        # shared behavior explicit and deterministic for every function tool.
        "strict": False,
    }


def openai_chat_function_tool(tool: ToolSpec) -> dict[str, Any]:
    return {"type": "function", "function": openai_function_definition(tool)}


def openai_responses_function_tool(tool: ToolSpec) -> dict[str, Any]:
    return {"type": "function", **openai_function_definition(tool)}


def openai_native_function_tool_contract_fingerprint(wire_api: str) -> str:
    if wire_api not in {"openai_chat_completions", "openai_responses"}:
        raise ValueError("OpenAI function tool wire API is unsupported")
    return context_fingerprint(
        "openai-native-function-tool-contract:v1",
        {
            "wire_api": wire_api,
            "contract_version": OPENAI_FUNCTION_TOOL_WIRE_CONTRACT_VERSION,
        },
    )


def freeze_openai_native_tool_eligibility(
    *,
    conversation_scope_kind: ModelInputScopeKind,
    scope_subagent_task_id: str | None,
    wire_api: str,
    tool_facts: tuple[FrozenToolCapabilityFact, ...],
    retained_direct_inputs: tuple[
        tuple[ToolCapabilityVersionRef, FrozenToolSpec], ...
    ] = (),
    deadline_monotonic: float | None = None,
) -> FrozenNativeToolWireEligibilitySet:
    """Quote every canonical tool without retaining its complete wire schema.

    The generic capability registry never sees this projection.  It remains a
    process-local adapter fact and therefore cannot replace the canonical MCP
    schema or local argument validator.  Full wire values are materialized
    only after the pure planner selects the bounded DIRECT cohort.
    """

    contract = openai_native_function_tool_contract_fingerprint(wire_api)
    entries: list[
        FrozenNativeToolWireEligibilityQuote | FrozenNativeToolWireIncompatibility
    ] = []
    inputs = [
        (
            tool_capability_version_ref(fact),
            fact.canonical_tool_spec,
            canonical_tool_spec_fingerprint(fact),
        )
        for fact in tool_facts
    ]
    seen_versions = {item[0].version_fingerprint: item[2] for item in inputs}
    for version, raw_spec in retained_direct_inputs:
        if not isinstance(version, ToolCapabilityVersionRef) or not isinstance(
            raw_spec, FrozenToolSpec
        ):
            raise TypeError("retained direct tool input is not frozen")
        if version.provider_name != raw_spec.name:
            raise ValueError("retained direct tool version/spec name drifted")
        spec_fingerprint = frozen_tool_spec_fingerprint(raw_spec)
        existing = seen_versions.get(version.version_fingerprint)
        if existing is not None:
            if existing != spec_fingerprint:
                raise ValueError("retained direct tool schema conflicts")
            continue
        seen_versions[version.version_fingerprint] = spec_fingerprint
        inputs.append((version, raw_spec, spec_fingerprint))

    for version, canonical_spec, spec_fingerprint in inputs:
        _require_native_planning_deadline(deadline_monotonic)
        try:
            frozen_wire = _materialize_openai_function_wire(
                wire_api=wire_api, canonical_spec=canonical_spec
            )
            wire_bytes = len(canonical_json_bytes(frozen_wire))
            if wire_bytes > 1024 * 1024:
                raise OverflowError("native function tool projection is overbound")
            wire_fingerprint = context_fingerprint(
                "native-tool-wire-value:v1", frozen_wire
            )
            payload = {
                "capability_version_fingerprint": version.version_fingerprint,
                "canonical_tool_spec_fingerprint": spec_fingerprint,
                "native_function_tool_wire_contract_fingerprint": contract,
                "wire_tool_fingerprint": wire_fingerprint,
                "wire_utf8_bytes": wire_bytes,
            }
            entries.append(
                FrozenNativeToolWireEligibilityQuote(
                    **payload,
                    eligibility_fingerprint=context_fingerprint(
                        "native-tool-wire-eligibility-quote:v1", payload
                    ),
                )
            )
        except (OpenAIFunctionSchemaIncompatible, OverflowError) as exc:
            if isinstance(exc, OverflowError):
                reason = NativeToolWireIncompatibilityReason.PROJECTION_OVERBOUND
            elif "root" in str(exc).lower():
                reason = NativeToolWireIncompatibilityReason.ROOT_SHAPE_UNSUPPORTED
            elif "union" in str(exc).lower() or "combine" in str(exc).lower():
                reason = NativeToolWireIncompatibilityReason.COMPOSITION_UNSUPPORTED
            else:
                reason = NativeToolWireIncompatibilityReason.CONSTRAINT_UNSUPPORTED
            payload = {
                "capability_version_fingerprint": version.version_fingerprint,
                "canonical_tool_spec_fingerprint": spec_fingerprint,
                "native_function_tool_wire_contract_fingerprint": contract,
                "reason": reason.value,
            }
            entries.append(
                FrozenNativeToolWireIncompatibility(
                    capability_version_fingerprint=version.version_fingerprint,
                    canonical_tool_spec_fingerprint=spec_fingerprint,
                    native_function_tool_wire_contract_fingerprint=contract,
                    reason=reason,
                    decision_fingerprint=context_fingerprint(
                        "native-tool-wire-incompatibility:v1", payload
                    ),
                )
            )
        _require_native_planning_deadline(deadline_monotonic)
    ordered = tuple(sorted(entries, key=lambda item: item.capability_version_fingerprint))
    fingerprint = context_fingerprint(
        "native-tool-wire-eligibility-set:v1",
        {
            "scope": conversation_scope_kind.value,
            "scope_subagent_task_id": scope_subagent_task_id,
            "contract": contract,
            "entries": tuple(
                item.eligibility_fingerprint
                if isinstance(item, FrozenNativeToolWireEligibilityQuote)
                else item.decision_fingerprint
                for item in ordered
            ),
        },
    )
    return FrozenNativeToolWireEligibilitySet(
        conversation_scope_kind=conversation_scope_kind,
        scope_subagent_task_id=scope_subagent_task_id,
        native_function_tool_wire_contract_fingerprint=contract,
        entries=ordered,
        eligibility_set_fingerprint=fingerprint,
    )


def materialize_openai_native_tool_projection_set(
    *,
    conversation_scope_kind: ModelInputScopeKind,
    scope_subagent_task_id: str | None,
    wire_api: str,
    tool_versions: tuple[ToolCapabilityVersionRef, ...],
    tool_specs: tuple[FrozenToolSpec, ...],
    eligibility: FrozenNativeToolWireEligibilitySet,
    deadline_monotonic: float | None = None,
) -> FrozenNativeToolProjectionSet:
    """Materialize only the exact bounded cohort selected by the planner."""

    contract = openai_native_function_tool_contract_fingerprint(wire_api)
    if (
        eligibility.conversation_scope_kind is not conversation_scope_kind
        or eligibility.scope_subagent_task_id != scope_subagent_task_id
        or eligibility.native_function_tool_wire_contract_fingerprint != contract
        or len(tool_versions) != len(tool_specs)
    ):
        raise ValueError("native materialization input does not exact-join quote")
    quoted = {
        item.capability_version_fingerprint: item
        for item in eligibility.entries
    }
    projections: list[FrozenNativeToolWireProjection] = []
    for version, spec in zip(tool_versions, tool_specs, strict=True):
        _require_native_planning_deadline(deadline_monotonic)
        item = quoted.get(version.version_fingerprint)
        spec_fingerprint = frozen_tool_spec_fingerprint(spec)
        if (
            not isinstance(item, FrozenNativeToolWireEligibilityQuote)
            or version.provider_name != spec.name
            or item.canonical_tool_spec_fingerprint != spec_fingerprint
        ):
            raise ValueError("selected native Tool is not eligible")
        frozen_wire = _materialize_openai_function_wire(
            wire_api=wire_api, canonical_spec=spec
        )
        wire_bytes = len(canonical_json_bytes(frozen_wire))
        wire_fingerprint = context_fingerprint(
            "native-tool-wire-value:v1", frozen_wire
        )
        if (
            wire_bytes != item.wire_utf8_bytes
            or wire_fingerprint != item.wire_tool_fingerprint
        ):
            raise ValueError("native Tool materialization drifted from quote")
        payload = {
            "capability_version_fingerprint": version.version_fingerprint,
            "canonical_tool_spec_fingerprint": spec_fingerprint,
            "native_function_tool_wire_contract_fingerprint": contract,
            "wire_tool": frozen_wire,
        }
        projections.append(
            FrozenNativeToolWireProjection(
                **payload,
                projection_fingerprint=context_fingerprint(
                    "native-tool-wire-projection:v1", payload
                ),
            )
        )
        _require_native_planning_deadline(deadline_monotonic)
    projection_tuple = tuple(projections)
    return FrozenNativeToolProjectionSet(
        conversation_scope_kind=conversation_scope_kind,
        scope_subagent_task_id=scope_subagent_task_id,
        native_function_tool_wire_contract_fingerprint=contract,
        tool_versions=tool_versions,
        projections=projection_tuple,
        projection_set_fingerprint=native_tool_projection_set_fingerprint(
            conversation_scope_kind=conversation_scope_kind,
            scope_subagent_task_id=scope_subagent_task_id,
            native_function_tool_wire_contract_fingerprint=contract,
            tool_versions=tool_versions,
            projections=projection_tuple,
        ),
    )


def _materialize_openai_function_wire(
    *, wire_api: str, canonical_spec: FrozenToolSpec
) -> FrozenJsonObjectFact:
    parameters = thaw_json(canonical_spec.parameters)
    if not isinstance(parameters, dict):
        raise OpenAIFunctionSchemaIncompatible(
            "OpenAI function parameters are not an object"
        )
    tool = ToolSpec(
        name=canonical_spec.name,
        description=canonical_spec.description,
        parameters=parameters,
    )
    wire_value = (
        openai_chat_function_tool(tool)
        if wire_api == "openai_chat_completions"
        else openai_responses_function_tool(tool)
    )
    frozen_wire = freeze_json(wire_value)
    if not isinstance(frozen_wire, FrozenJsonObjectFact):
        raise TypeError("native function tool wire did not freeze to an object")
    return frozen_wire


def _require_native_planning_deadline(deadline_monotonic: float | None) -> None:
    if deadline_monotonic is not None and monotonic() >= deadline_monotonic:
        raise TimeoutError("native Tool planning deadline expired")


__all__ = [
    "OPENAI_FUNCTION_TOOL_WIRE_CONTRACT_VERSION",
    "OpenAIFunctionSchemaIncompatible",
    "freeze_openai_native_tool_eligibility",
    "materialize_openai_native_tool_projection_set",
    "lower_openai_function_parameters",
    "openai_native_function_tool_contract_fingerprint",
    "openai_chat_function_tool",
    "openai_function_definition",
    "openai_responses_function_tool",
]
