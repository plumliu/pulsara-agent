"""Pure, bounded Tool capability exposure planning for Round 9."""

from __future__ import annotations

from pulsara_agent.capability.contracts import (
    CapabilityRouteReasonCode,
    EmptyCapabilityEpochPredecessor,
    FrozenMcpRouteProjection,
    FrozenMcpInspectabilityFact,
    FrozenMcpToolExposure,
    FrozenNativeToolProjectionSet,
    FrozenNativeToolWireEligibilityQuote,
    FrozenNativeToolWireIncompatibility,
    FrozenToolCapabilityDispatchView,
    FrozenToolCapabilityExposureSelection,
    FrozenToolCapabilityExposurePlan,
    FrozenToolCapabilityFact,
    InstalledCapabilityEpochPredecessor,
    McpInspectDeliveryDisposition,
    MAXIMUM_NATIVE_TOOL_BYTES,
    MAXIMUM_NATIVE_TOOL_COUNT,
    McpToolCapabilityRef,
    ToolCapabilityVersionRef,
    ToolCapabilityOrigin,
    ToolCapabilityRouteKind,
    tool_capability_version_identity_digest,
    tool_capability_version_ref,
)
from pulsara_agent.model_input.contracts import (
    FrozenModelToolSurface,
    model_tool_surface_fingerprint,
)
from pulsara_agent.primitives.context import context_fingerprint


class CapabilityPlanningError(RuntimeError):
    """The frozen capability cut cannot produce a safe exposure plan."""


def _route(
    *,
    fact: FrozenToolCapabilityFact,
    kind: ToolCapabilityRouteKind,
    reason: CapabilityRouteReasonCode,
) -> FrozenMcpToolExposure:
    target = McpToolCapabilityRef(
        server_id=fact.identity.source.stable_source_id,
        remote_tool_name=fact.identity.stable_name,
    )
    version = tool_capability_version_ref(fact)
    fingerprint = context_fingerprint(
        "mcp-tool-exposure-route:v1",
        {
            "server_id": target.server_id,
            "remote_tool_name": target.remote_tool_name,
            "version": tool_capability_version_identity_digest(version),
            "route": kind.value,
            "reason": reason.value,
        },
    )
    return FrozenMcpToolExposure(
        target=target,
        version=version,
        route=kind,
        public_reason_code=reason,
        route_fingerprint=fingerprint,
    )


def _direct_surface(
    *,
    view: FrozenToolCapabilityDispatchView,
    selected: tuple[FrozenToolCapabilityFact, ...],
) -> tuple[FrozenModelToolSurface, tuple[ToolCapabilityVersionRef, ...]]:
    ordered = tuple(
        sorted(selected, key=lambda item: item.canonical_tool_spec.name)
    )
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
    """Select the immutable native cohort and current MCP route projection."""

    def select(
        self, *, view: FrozenToolCapabilityDispatchView
    ) -> FrozenToolCapabilityExposureSelection:
        facts = view.registry_tool_facts
        eligibility = {
            item.version: item for item in view.planning_input.native_wire.entries
        }
        if len(eligibility) != len(view.planning_input.native_wire.entries):
            raise CapabilityPlanningError("native eligibility versions conflict")
        inspectability = {
            (item.target.server_id, item.target.remote_tool_name): item
            for item in view.planning_input.mcp.inspectability_facts
        }

        builtin: list[FrozenToolCapabilityFact] = []
        mcp_eligible: list[FrozenToolCapabilityFact] = []
        mcp_incompatible: list[FrozenToolCapabilityFact] = []
        for fact in facts:
            item = eligibility.get(tool_capability_version_ref(fact))
            if item is None:
                raise CapabilityPlanningError("tool eligibility coverage is incomplete")
            if fact.origin is ToolCapabilityOrigin.BUILTIN:
                if not isinstance(item, FrozenNativeToolWireEligibilityQuote):
                    raise CapabilityPlanningError(
                        "execution-backed builtin is not native-wire compatible"
                    )
                builtin.append(fact)
            elif isinstance(item, FrozenNativeToolWireEligibilityQuote):
                mcp_eligible.append(fact)
            elif isinstance(item, FrozenNativeToolWireIncompatibility):
                mcp_incompatible.append(fact)
            else:  # pragma: no cover - closed union
                raise TypeError("native tool eligibility union is open")

        if (
            len(builtin) > MAXIMUM_NATIVE_TOOL_COUNT
            or sum(
                len(item.canonical_tool_spec.canonical_bytes)
                for item in builtin
            )
            > MAXIMUM_NATIVE_TOOL_BYTES
            or sum(
                eligibility[
                    tool_capability_version_ref(item)
                ].wire_utf8_bytes
                for item in builtin
            )
            > MAXIMUM_NATIVE_TOOL_BYTES
        ):
            # Builtins are execution-backed Pulsara product surface: unlike
            # MCP leaves they cannot be skipped or diverted through the meta
            # gateway merely to make a provider request fit.
            raise CapabilityPlanningError(
                "execution-backed builtin surface exceeds native bounds"
            )

        predecessor = view.planning_input.predecessor
        routes: list[FrozenMcpToolExposure] = []
        reusable_direct_projection_set: FrozenNativeToolProjectionSet | None = None
        if isinstance(predecessor, EmptyCapabilityEpochPredecessor):
            candidate = (*builtin, *mcp_eligible)
            all_direct = (
                len(candidate) <= MAXIMUM_NATIVE_TOOL_COUNT
                and sum(
                    len(item.canonical_tool_spec.canonical_bytes)
                    for item in candidate
                )
                <= MAXIMUM_NATIVE_TOOL_BYTES
                and sum(
                    eligibility[
                        tool_capability_version_ref(item)
                    ].wire_utf8_bytes
                    for item in candidate
                )
                <= MAXIMUM_NATIVE_TOOL_BYTES
            )
            selected = tuple(candidate if all_direct else builtin)
            direct_tool_surface, direct_tool_versions = _direct_surface(
                view=view, selected=selected
            )
            direct_versions = set(direct_tool_versions)
            for fact in mcp_eligible:
                version = tool_capability_version_ref(fact)
                if version in direct_versions:
                    routes.append(
                        _route(
                            fact=fact,
                            kind=ToolCapabilityRouteKind.DIRECT,
                            reason=CapabilityRouteReasonCode.DIRECT_NATIVE_SURFACE,
                        )
                    )
                else:
                    routes.append(
                        self._meta_or_unavailable(
                            fact,
                            inspectability[
                                (
                                    fact.identity.source.stable_source_id,
                                    fact.identity.stable_name,
                                )
                            ],
                            CapabilityRouteReasonCode.NEW_COLD_COHORT_META_FALLBACK,
                        )
                    )
        elif isinstance(predecessor, InstalledCapabilityEpochPredecessor):
            # The existing continuity reset classifier owns whether these wire
            # projections are reused byte-for-byte or reprojected.  The planner
            # never promotes current late MCP facts into this cohort.
            direct_tool_surface = predecessor.tool_surface
            direct_tool_versions = predecessor.direct_projection_set.tool_versions
            if (
                predecessor.direct_projection_set.native_function_tool_wire_contract_fingerprint
                == view.planning_input.native_wire.native_function_tool_wire_contract_fingerprint
            ):
                reusable_direct_projection_set = predecessor.direct_projection_set
            else:
                for version in predecessor.direct_projection_set.tool_versions:
                    item = eligibility.get(version)
                    if not isinstance(item, FrozenNativeToolWireEligibilityQuote):
                        raise CapabilityPlanningError(
                            "installed direct cohort cannot be fully reprojected"
                        )
            current_by_identity = {
                fact.identity.identity_fingerprint: fact
                for fact in facts
                if fact.origin is ToolCapabilityOrigin.MCP
            }
            for version in direct_tool_versions:
                current = current_by_identity.get(version.identity_fingerprint)
                if current is None:
                    # A retained direct version remains provider-visible but
                    # has no current clean executor fact.
                    continue
                if current.fact_semantic_fingerprint == version.semantic_fingerprint:
                    routes.append(
                        _route(
                            fact=current,
                            kind=ToolCapabilityRouteKind.DIRECT,
                            reason=CapabilityRouteReasonCode.DIRECT_NATIVE_SURFACE,
                        )
                    )
                else:
                    routes.append(
                        _route(
                            fact=current,
                            kind=ToolCapabilityRouteKind.UNAVAILABLE,
                            reason=CapabilityRouteReasonCode.SCHEMA_REPLACED,
                        )
                    )
            direct_identities = {
                item.identity_fingerprint
                for item in direct_tool_versions
            }
            previous_meta_routes = {
                (item.target.server_id, item.target.remote_tool_name): item
                for item in predecessor.mcp_route_projection.routes
                if item.route is ToolCapabilityRouteKind.NEW_MCP_META_ONLY
            }
            for fact in mcp_eligible:
                if fact.identity.identity_fingerprint not in direct_identities:
                    key = (
                        fact.identity.source.stable_source_id,
                        fact.identity.stable_name,
                    )
                    previous = previous_meta_routes.get(key)
                    if (
                        previous is not None
                        and previous.version == tool_capability_version_ref(fact)
                    ):
                        # A policy-bound ref issued from the previous call must
                        # retain its exact route identity throughout the same
                        # process-local continuity epoch.  Reclassifying an
                        # unchanged cold-fallback leaf as merely "late" would
                        # invalidate the ref before the model can use it.
                        routes.append(previous)
                    else:
                        routes.append(
                            self._meta_or_unavailable(
                                fact,
                                inspectability[key],
                                CapabilityRouteReasonCode.NEW_NOT_IN_NATIVE_SURFACE,
                            )
                        )
        else:  # pragma: no cover - closed union
            raise TypeError("capability epoch predecessor union is open")

        installed_direct_identities = (
            {
                item.identity_fingerprint
                for item in direct_tool_versions
            }
            if isinstance(predecessor, InstalledCapabilityEpochPredecessor)
            else set()
        )
        for fact in mcp_incompatible:
            if fact.identity.identity_fingerprint in installed_direct_identities:
                # An installed direct identity that changed its descriptor is
                # already represented by SCHEMA_REPLACED above.  Its new
                # version must never bypass the old native contract via meta.
                continue
            key = (
                fact.identity.source.stable_source_id,
                fact.identity.stable_name,
            )
            previous = (
                previous_meta_routes.get(key)
                if isinstance(predecessor, InstalledCapabilityEpochPredecessor)
                else None
            )
            if (
                previous is not None
                and previous.version == tool_capability_version_ref(fact)
            ):
                routes.append(previous)
            else:
                routes.append(
                    self._meta_or_unavailable(
                        fact,
                        inspectability[key],
                        CapabilityRouteReasonCode.NATIVE_WIRE_INCOMPATIBLE,
                    )
                )

        ordered_routes = tuple(
            sorted(routes, key=lambda item: (item.target.server_id, item.target.remote_tool_name))
        )
        mcp_projection = FrozenMcpRouteProjection(
            routes=ordered_routes,
            joined_catalog_semantic_fingerprint=(
                view.planning_input.mcp.catalog_semantic_fingerprint
            ),
            projection_fingerprint=context_fingerprint(
                "mcp-route-projection:v1",
                {
                    "routes": tuple(item.route_fingerprint for item in ordered_routes),
                    "catalog": view.planning_input.mcp.catalog_semantic_fingerprint,
                },
            ),
        )
        return FrozenToolCapabilityExposureSelection(
            dispatch_view=view,
            direct_tool_surface=direct_tool_surface,
            direct_tool_versions=direct_tool_versions,
            mcp_catalog_route_projection=mcp_projection,
            reusable_direct_projection_set=reusable_direct_projection_set,
        )

    @staticmethod
    def finalize(
        *,
        selection: FrozenToolCapabilityExposureSelection,
        direct_projection_set: FrozenNativeToolProjectionSet,
    ) -> FrozenToolCapabilityExposurePlan:
        if (
            direct_projection_set.conversation_scope_kind
            is not selection.conversation_scope_kind
            or direct_projection_set.scope_subagent_task_id
            != selection.scope_subagent_task_id
            or direct_projection_set.native_function_tool_wire_contract_fingerprint
            != selection.native_function_tool_wire_contract_fingerprint
            or direct_projection_set.tool_versions
            != selection.direct_tool_versions
        ):
            raise CapabilityPlanningError(
                "materialized native projection set does not join selection"
            )
        return FrozenToolCapabilityExposurePlan(
            selection=selection,
            direct_projection_set=direct_projection_set,
        )

    @staticmethod
    def _meta_or_unavailable(
        fact: FrozenToolCapabilityFact,
        inspectability: FrozenMcpInspectabilityFact,
        meta_reason: CapabilityRouteReasonCode,
    ) -> FrozenMcpToolExposure:
        if (
            inspectability.target.server_id
            != fact.identity.source.stable_source_id
            or inspectability.target.remote_tool_name
            != fact.identity.stable_name
            or inspectability.version != tool_capability_version_ref(fact)
        ):
            raise CapabilityPlanningError(
                "MCP inspectability proof does not exact-join Tool fact"
            )
        if (
            inspectability.disposition
            is McpInspectDeliveryDisposition.FULL_ELIGIBLE
        ):
            return _route(
                fact=fact,
                kind=ToolCapabilityRouteKind.NEW_MCP_META_ONLY,
                reason=meta_reason,
            )
        return _route(
            fact=fact,
            kind=ToolCapabilityRouteKind.UNAVAILABLE,
            reason=CapabilityRouteReasonCode.DESCRIPTOR_OVERBOUND,
        )


__all__ = [
    "CapabilityPlanningError",
    "KernelToolCapabilityPlanner",
]
