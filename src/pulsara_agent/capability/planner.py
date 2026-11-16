"""Pure, bounded Tool capability exposure planning for Round 9."""

from __future__ import annotations


from pulsara_agent.capability.contracts import (
    EmptyCapabilityEpochPredecessor,
    FrozenNativeToolProjectionSet,
    FrozenNativeToolWireEligibilityQuote,
    FrozenToolCapabilityDispatchView,
    FrozenToolCapabilityExposureSelection,
    FrozenToolCapabilityExposurePlan,
    FrozenToolCapabilityFact,
    InstalledCapabilityEpochPredecessor,
    MAXIMUM_NATIVE_TOOL_BYTES,
    MAXIMUM_BUILTIN_NATIVE_TOOL_COUNT,
    ToolCapabilityVersionRef,
    ToolCapabilityOrigin,
    tool_capability_version_ref,
)
from pulsara_agent.model_input.contracts import (
    FrozenModelToolSurface,
    model_tool_surface_fingerprint,
)
from pulsara_agent.primitives.context import context_fingerprint


class CapabilityPlanningError(RuntimeError):
    """The frozen capability cut cannot produce a safe exposure plan."""






def _direct_surface(
    *,
    view: FrozenToolCapabilityDispatchView,
    selected: tuple[FrozenToolCapabilityFact, ...],
) -> tuple[FrozenModelToolSurface, tuple[ToolCapabilityVersionRef, ...]]:
    ordered = tuple(sorted(selected, key=lambda item: item.canonical_tool_spec.name))
    specs = tuple(item.canonical_tool_spec for item in ordered)
    return (
        FrozenModelToolSurface(
            conversation_scope_kind=(
                view.planning_input.native_wire.conversation_scope_kind
            ),
            tool_specs=specs,
            surface_fingerprint=model_tool_surface_fingerprint(
                view.planning_input.native_wire.conversation_scope_kind,
                specs,
            ),
        ),
        tuple(tool_capability_version_ref(item) for item in ordered),
    )


class KernelToolCapabilityPlanner:
    """Select the immutable builtin native cohort."""

    def select(self, *, view: FrozenToolCapabilityDispatchView) -> FrozenToolCapabilityExposureSelection:
        """Freeze builtins only; discovered MCP tools always use the meta entry."""
        facts = view.registry_tool_facts
        builtin = tuple(fact for fact in facts if fact.origin is ToolCapabilityOrigin.BUILTIN)
        eligibility = {item.version: item for item in view.planning_input.native_wire.entries}
        for fact in builtin:
            if not isinstance(eligibility.get(tool_capability_version_ref(fact)), FrozenNativeToolWireEligibilityQuote):
                raise CapabilityPlanningError("execution-backed builtin is not native-wire compatible")
        if (len(builtin) > MAXIMUM_BUILTIN_NATIVE_TOOL_COUNT
            or sum(len(item.canonical_tool_spec.canonical_bytes) for item in builtin) > MAXIMUM_NATIVE_TOOL_BYTES
            or sum(eligibility[tool_capability_version_ref(item)].wire_utf8_bytes for item in builtin) > MAXIMUM_NATIVE_TOOL_BYTES):
            raise CapabilityPlanningError("execution-backed builtin surface exceeds native bounds")
        predecessor = view.planning_input.predecessor
        reusable = None
        if isinstance(predecessor, EmptyCapabilityEpochPredecessor):
            surface, versions = _direct_surface(view=view, selected=builtin)
        elif isinstance(predecessor, InstalledCapabilityEpochPredecessor):
            surface = predecessor.tool_surface
            versions = predecessor.direct_projection_set.tool_versions
            if any(version.identity.source.kind.value == "MCP_SERVER" for version in versions):
                raise CapabilityPlanningError("provider tools must contain only Pulsara builtins")
            if all(isinstance(quote := eligibility.get(projection.version), FrozenNativeToolWireEligibilityQuote)
                   and quote.wire_utf8_bytes == projection.wire_utf8_bytes
                   and quote.wire_tool_fingerprint == context_fingerprint("native-tool-wire-value:v1", projection.wire_tool)
                   for projection in predecessor.direct_projection_set.projections):
                reusable = predecessor.direct_projection_set
        else:
            raise TypeError("capability epoch predecessor union is open")
        return FrozenToolCapabilityExposureSelection(dispatch_view=view,
            direct_tool_surface=surface, direct_tool_versions=versions,
            reusable_direct_projection_set=reusable)

    @staticmethod
    def finalize(
        *,
        selection: FrozenToolCapabilityExposureSelection,
        direct_projection_set: FrozenNativeToolProjectionSet,
    ) -> FrozenToolCapabilityExposurePlan:
        if (
            direct_projection_set.conversation_scope_kind
            is not selection.conversation_scope_kind
            or direct_projection_set.tool_versions != selection.direct_tool_versions
        ):
            raise CapabilityPlanningError(
                "materialized native projection set does not join selection"
            )
        return FrozenToolCapabilityExposurePlan(
            selection=selection,
            direct_projection_set=direct_projection_set,
        )



__all__ = [
    "CapabilityPlanningError",
    "KernelToolCapabilityPlanner",
]
