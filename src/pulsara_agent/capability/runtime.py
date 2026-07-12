"""Capability runtime facade that resolves descriptors and prompts once per turn."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

from pulsara_agent.capability.builtin_provider import BuiltinToolCapabilityProvider
from pulsara_agent.capability.exposure import CapabilityExposurePlan, build_exposure_plan
from pulsara_agent.capability.facts import (
    ProviderProjectionResult,
    build_capability_projection_fact,
    narrow_capability_projection_fact,
)
from pulsara_agent.capability.provider import (
    CapabilityProviderComponent,
    CapabilityProjectionOutput,
)
from pulsara_agent.capability.registry import CapabilityRegistry
from pulsara_agent.capability.types import (
    CapabilityDiagnostic,
    CapabilityExecutionSurfaceSnapshotContext,
    CapabilityProjectionResolveContext,
)
from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.primitives.capability import (
    CapabilityDescriptorBindingIdentityFact,
    CapabilityExecutionSurfaceIdentityFact,
    CapabilityAuthorizationEntryFact,
    CapabilityExposureDiagnosticFact,
    CapabilityExposureSnapshotFact,
    CapabilityResolveBasisFact,
    build_capability_execution_surface_identity,
    build_capability_exposure_semantic,
    build_capability_exposure_snapshot,
    capability_authorization_fingerprint,
)
from pulsara_agent.primitives.run_entry import CapabilityExposureOwnerFact
from pulsara_agent.primitives.model_call import canonical_json_bytes, sha256_fingerprint

if TYPE_CHECKING:
    from pulsara_agent.tools.registry import ToolRegistry


@dataclass(frozen=True, slots=True)
class FrozenCapabilityExecutionSurface:
    identity: CapabilityExecutionSurfaceIdentityFact
    descriptors: tuple[object, ...]
    diagnostics: tuple[CapabilityDiagnostic, ...]


@dataclass(frozen=True, slots=True)
class ResolvedCapabilityExposure:
    plan: CapabilityExposurePlan
    fact: CapabilityExposureSnapshotFact


@dataclass(slots=True)
class CapabilityRuntime:
    providers: tuple[CapabilityProviderComponent, ...] = (
        BuiltinToolCapabilityProvider(),
    )

    @classmethod
    def with_default_providers(
        cls, *providers: CapabilityProviderComponent
    ) -> "CapabilityRuntime":
        return cls(providers=(BuiltinToolCapabilityProvider(), *providers))

    def freeze_execution_surface(
        self,
        context: CapabilityExecutionSurfaceSnapshotContext,
        *,
        tool_registry: ToolRegistry,
        archive: ArtifactStore,
        runtime_session_id: str,
        owner_id: str,
    ) -> FrozenCapabilityExecutionSurface:
        descriptors = []
        diagnostics: list[CapabilityDiagnostic] = []
        for provider in self.providers:
            snapshot = getattr(provider, "snapshot_descriptors", None)
            if snapshot is None:
                if hasattr(provider, "resolve_projection"):
                    continue
                raise ValueError(
                    f"capability provider {provider.provider_id!r} uses the removed "
                    "mixed resolve contract"
                )
            output = snapshot(context)
            descriptors.extend(output.descriptors)
            diagnostics.extend(output.diagnostics)

        descriptors.sort(key=lambda descriptor: descriptor.name)
        names = [descriptor.name for descriptor in descriptors]
        ids = [descriptor.id for descriptor in descriptors]
        if len(names) != len(set(names)) or len(ids) != len(set(ids)):
            raise ValueError("capability execution surface has duplicate descriptors")
        descriptor_names = set(names)
        unowned_bindings = set(tool_registry.names()).difference(descriptor_names)
        if unowned_bindings:
            raise ValueError(
                "execution bindings lack capability descriptors: "
                + ", ".join(sorted(unowned_bindings))
            )

        entries: list[CapabilityDescriptorBindingIdentityFact] = []
        for descriptor in descriptors:
            payload = descriptor.to_event_payload()
            descriptor_fingerprint = descriptor.fingerprint()
            artifact_digest = sha256_fingerprint(
                "capability-descriptor-artifact-id:v1",
                [descriptor_fingerprint, owner_id],
            ).removeprefix("sha256:")
            artifact_id = f"artifact:capability_descriptor:{artifact_digest}"
            archive.put_text_if_absent_or_confirm_identical(
                artifact_id,
                canonical_json_bytes(payload).decode("utf-8"),
                session_id=runtime_session_id,
                run_id=None,
                media_type="application/vnd.pulsara.capability-descriptor+json",
                semantic_metadata={
                    "artifact_kind": "capability_descriptor",
                    "descriptor_id": descriptor.id,
                    "descriptor_fingerprint": descriptor_fingerprint,
                    "owner_id": owner_id,
                },
            )
            binding = tool_registry.binding_contract(descriptor.name)
            if descriptor.is_model_callable and binding is None:
                raise ValueError(
                    f"model-callable capability {descriptor.name!r} has no stable "
                    "binding contract"
                )
            if not descriptor.is_model_callable and binding is not None:
                raise ValueError(
                    f"non-callable capability {descriptor.name!r} has an execution binding"
                )
            entries.append(
                CapabilityDescriptorBindingIdentityFact(
                    capability_name=descriptor.name,
                    provider_id=descriptor.provider_id,
                    descriptor_id=descriptor.id,
                    descriptor_fingerprint=descriptor_fingerprint,
                    descriptor_artifact_id=artifact_id,
                    binding_fingerprint=(
                        binding.binding_fingerprint if binding is not None else None
                    ),
                    binding_contract_id=(
                        binding.contract_id if binding is not None else None
                    ),
                    binding_contract_version=(
                        binding.contract_version if binding is not None else None
                    ),
                )
            )
        identity = build_capability_execution_surface_identity(
            surface_contract_version="capability-execution-surface:v1",
            entries=tuple(entries),
            mcp_installation_id=context.mcp_installation_id,
        )
        return FrozenCapabilityExecutionSurface(
            identity=identity,
            descriptors=tuple(descriptors),
            diagnostics=tuple(diagnostics),
        )

    def resolve_exposure_projection(
        self,
        context: CapabilityProjectionResolveContext,
        *,
        frozen_surface: FrozenCapabilityExecutionSurface,
        archive: ArtifactStore,
        runtime_session_id: str,
        owner: CapabilityExposureOwnerFact,
        resolve_basis: CapabilityResolveBasisFact,
        exposure_id: str,
        resolution_kind: str = "initial",
        source_exposure_id: str | None = None,
        persist_artifacts: bool = True,
    ) -> ResolvedCapabilityExposure:
        provider_results, merged, plan = self._resolve_projection_plan(
            context,
            frozen_surface=frozen_surface,
        )
        catalog_fact = build_capability_projection_fact(
            projection_type="catalog",
            provider_results=tuple(provider_results),
            archive=archive,
            runtime_session_id=runtime_session_id,
            owner_id=owner.owner_id,
            exposure_id=exposure_id,
            persist_artifacts=persist_artifacts,
        )
        active_fact = build_capability_projection_fact(
            projection_type="active_skill",
            provider_results=tuple(provider_results),
            archive=archive,
            runtime_session_id=runtime_session_id,
            owner_id=owner.owner_id,
            exposure_id=exposure_id,
            persist_artifacts=persist_artifacts,
        )
        surface_by_name = {
            entry.capability_name: entry for entry in frozen_surface.identity.entries
        }
        authorization_entries = tuple(
            CapabilityAuthorizationEntryFact(
                capability_name=name,
                descriptor_fingerprint=surface_by_name[name].descriptor_fingerprint,
                binding_fingerprint=surface_by_name[name].binding_fingerprint,
                disposition=(
                    "direct"
                    if name in plan.direct_names
                    else "deferred"
                    if name in plan.deferred_names
                    else "hidden"
                ),
                callable=name in plan.callable_names,
            )
            for name in sorted(surface_by_name)
        )
        authorization_fp = capability_authorization_fingerprint(
            authorization_entries
        )
        semantic = build_capability_exposure_semantic(
            execution_surface=frozen_surface.identity,
            catalog_projection=catalog_fact,
            active_skill_projection=active_fact,
            authorization_fingerprint=authorization_fp,
        )
        diagnostics = tuple(
            CapabilityExposureDiagnosticFact(
                code=diagnostic.code,
                severity=diagnostic.severity,
                stage="projection",
                message=diagnostic.message[:1024],
            )
            for diagnostic in plan.diagnostics
        )
        fact = build_capability_exposure_snapshot(
            exposure_id=exposure_id,
            owner=owner,
            resolution_kind=resolution_kind,  # type: ignore[arg-type]
            resolve_basis=resolve_basis,
            semantic=semantic,
            authorization_entries=authorization_entries,
            source_exposure_id=source_exposure_id,
            diagnostics=diagnostics,
        )
        return ResolvedCapabilityExposure(plan=plan, fact=fact)

    def preview_exposure_plan(
        self,
        context: CapabilityProjectionResolveContext,
        *,
        frozen_surface: FrozenCapabilityExecutionSurface,
    ) -> CapabilityExposurePlan:
        """Resolve a non-durable static preview through the split contracts.

        This is used by the CLI workspace inspector. It cannot create a run or
        capability fact and therefore does not accept a mutable turn context.
        """

        _results, _merged, plan = self._resolve_projection_plan(
            context,
            frozen_surface=frozen_surface,
        )
        return plan

    def _resolve_projection_plan(
        self,
        context: CapabilityProjectionResolveContext,
        *,
        frozen_surface: FrozenCapabilityExecutionSurface,
    ) -> tuple[
        tuple[ProviderProjectionResult, ...],
        CapabilityProjectionOutput,
        CapabilityExposurePlan,
    ]:
        provider_results: list[ProviderProjectionResult] = []
        for provider in self.providers:
            resolve_projection = getattr(provider, "resolve_projection", None)
            if resolve_projection is None:
                if hasattr(provider, "snapshot_descriptors"):
                    continue
                raise ValueError(
                    f"capability provider {getattr(provider, 'provider_id', '<unknown>')!r} "
                    "implements neither split capability provider contract"
                )
            provider_results.append(
                ProviderProjectionResult(
                    provider_id=str(provider.provider_id),
                    output=resolve_projection(
                        context,
                        execution_surface=frozen_surface.identity,
                    ),
                )
            )

        registry = CapabilityRegistry()
        for descriptor in frozen_surface.descriptors:
            registry.register(descriptor)
        snapshot = registry.snapshot()
        merged = _merge_projection_outputs(tuple(provider_results))
        plan = build_exposure_plan(
            snapshot,
            provider_output=merged,
            bound_tool_names=frozenset(
                entry.capability_name for entry in frozen_surface.identity.entries
            ),
        )
        return tuple(provider_results), merged, plan

    def resolve_continuation_exposure(
        self,
        context: CapabilityProjectionResolveContext,
        *,
        frozen_surface: FrozenCapabilityExecutionSurface,
        original_plan: CapabilityExposurePlan,
        original_fact: CapabilityExposureSnapshotFact,
        archive: ArtifactStore,
        runtime_session_id: str,
        owner: CapabilityExposureOwnerFact,
        resolve_basis: CapabilityResolveBasisFact,
        exposure_id: str,
    ) -> ResolvedCapabilityExposure:
        """Resolve one continuation as exact reuse or monotonic narrowing.

        The current provider projection is a process-local comparison candidate:
        its artifact IDs are deterministic, but no candidate artifact is written.
        A narrowed exposure persists only a new aggregate prompt built from exact
        original fragment artifacts.
        """

        candidate = self.resolve_exposure_projection(
            context,
            frozen_surface=frozen_surface,
            archive=archive,
            runtime_session_id=runtime_session_id,
            owner=owner,
            resolve_basis=resolve_basis,
            exposure_id=f"candidate:{exposure_id}",
            resolution_kind="continuation_narrowed",
            source_exposure_id=original_fact.exposure_id,
            persist_artifacts=False,
        )
        if (
            candidate.fact.exposure_semantic_fingerprint
            == original_fact.exposure_semantic_fingerprint
        ):
            fact = build_capability_exposure_snapshot(
                exposure_id=exposure_id,
                owner=owner,
                resolution_kind="continuation_reused",
                resolve_basis=resolve_basis,
                semantic=original_fact.semantic,
                authorization_entries=original_fact.authorization_entries,
                source_exposure_id=original_fact.exposure_id,
                diagnostics=(),
            )
            return ResolvedCapabilityExposure(plan=candidate.plan, fact=fact)

        original_auth = {
            entry.capability_name: entry
            for entry in original_fact.authorization_entries
        }
        current_auth = {
            entry.capability_name: entry
            for entry in candidate.fact.authorization_entries
        }
        original_surface = {
            entry.capability_name: entry
            for entry in original_fact.semantic.execution_surface.entries
        }
        current_surface = {
            entry.capability_name: entry
            for entry in frozen_surface.identity.entries
        }
        rank = {"hidden": 0, "deferred": 1, "direct": 2}
        authorization_entries: list[CapabilityAuthorizationEntryFact] = []
        retained_surface_names: set[str] = set()
        reason_codes: set[str] = set()
        for name in sorted(original_auth):
            original_entry = original_auth[name]
            current_entry = current_auth.get(name)
            original_identity = original_surface.get(name)
            current_identity = current_surface.get(name)
            identity_matches = (
                original_identity is not None
                and current_identity is not None
                and original_identity.provider_id == current_identity.provider_id
                and original_identity.descriptor_id == current_identity.descriptor_id
                and original_identity.descriptor_fingerprint
                == current_identity.descriptor_fingerprint
                and original_identity.binding_fingerprint
                == current_identity.binding_fingerprint
                and original_identity.binding_contract_id
                == current_identity.binding_contract_id
                and original_identity.binding_contract_version
                == current_identity.binding_contract_version
            )
            if (
                identity_matches
                and current_entry is not None
                and current_entry.descriptor_fingerprint
                == original_entry.descriptor_fingerprint
                and current_entry.binding_fingerprint
                == original_entry.binding_fingerprint
                and rank[current_entry.disposition] <= rank[original_entry.disposition]
            ):
                disposition = current_entry.disposition
                retained_surface_names.add(name)
            else:
                disposition = "hidden"
                reason_codes.add(f"capability_revoked_or_changed:{name}")
            authorization_entries.append(
                CapabilityAuthorizationEntryFact(
                    capability_name=name,
                    descriptor_fingerprint=original_entry.descriptor_fingerprint,
                    binding_fingerprint=original_entry.binding_fingerprint,
                    disposition=disposition,
                    callable=disposition == "direct",
                )
            )

        effective_surface = build_capability_execution_surface_identity(
            surface_contract_version=(
                original_fact.semantic.execution_surface.surface_contract_version
            ),
            entries=tuple(
                current_surface[name]
                for name in sorted(retained_surface_names)
            ),
            mcp_installation_id=frozen_surface.identity.mcp_installation_id,
        )
        catalog_fact, catalog_prompt = narrow_capability_projection_fact(
            projection_type="catalog",
            original=original_fact.semantic.catalog_projection,
            current_candidate=candidate.fact.semantic.catalog_projection,
            archive=archive,
            runtime_session_id=runtime_session_id,
            owner_id=owner.owner_id,
            exposure_id=exposure_id,
        )
        active_fact, active_prompt = narrow_capability_projection_fact(
            projection_type="active_skill",
            original=original_fact.semantic.active_skill_projection,
            current_candidate=candidate.fact.semantic.active_skill_projection,
            archive=archive,
            runtime_session_id=runtime_session_id,
            owner_id=owner.owner_id,
            exposure_id=exposure_id,
        )
        authorization_tuple = tuple(authorization_entries)
        semantic = build_capability_exposure_semantic(
            execution_surface=effective_surface,
            catalog_projection=catalog_fact,
            active_skill_projection=active_fact,
            authorization_fingerprint=capability_authorization_fingerprint(
                authorization_tuple
            ),
        )
        diagnostics = tuple(
            CapabilityExposureDiagnosticFact(
                code="capability_continuation_narrowed",
                severity="warning",
                stage="narrow",
                message=reason[:1024],
            )
            for reason in sorted(reason_codes)
        )
        fact = build_capability_exposure_snapshot(
            exposure_id=exposure_id,
            owner=owner,
            resolution_kind="continuation_narrowed",
            resolve_basis=resolve_basis,
            semantic=semantic,
            authorization_entries=authorization_tuple,
            source_exposure_id=original_fact.exposure_id,
            diagnostics=diagnostics,
        )
        direct_names = frozenset(fact.direct_names)
        retained_catalog_names = {
            entry.stable_name for entry in catalog_fact.visible_source_entries
        }
        retained_active_names = {
            entry.stable_name for entry in active_fact.visible_source_entries
        }
        descriptors = {
            name: candidate.plan.descriptors_by_name[name]
            for name in retained_surface_names
            if name in candidate.plan.descriptors_by_name
        }
        plan = CapabilityExposurePlan(
            registry_generation=candidate.plan.registry_generation,
            direct_tool_specs=tuple(
                spec
                for spec in original_plan.direct_tool_specs
                if spec.name in direct_names
            ),
            direct_names=direct_names,
            deferred_names=frozenset(fact.deferred_names),
            hidden_names=frozenset(fact.hidden_names),
            callable_names=frozenset(fact.callable_names),
            descriptors_by_name=MappingProxyType(descriptors),
            catalog_entries=tuple(
                entry
                for entry in candidate.plan.catalog_entries
                if entry.name in retained_catalog_names
            ),
            active_injections=tuple(
                entry
                for entry in candidate.plan.active_injections
                if entry.name in retained_active_names
            ),
            catalog_prompt=catalog_prompt,
            active_skill_prompt=active_prompt,
            diagnostics=candidate.plan.diagnostics,
        )
        return ResolvedCapabilityExposure(plan=plan, fact=fact)


def _merge_projection_outputs(
    results: tuple[ProviderProjectionResult, ...],
) -> CapabilityProjectionOutput:
    outputs = tuple(result.output for result in results)
    return CapabilityProjectionOutput(
        catalog_entries=tuple(
            entry for output in outputs for entry in output.catalog_entries
        ),
        active_injections=tuple(
            injection for output in outputs for injection in output.active_injections
        ),
        diagnostics=tuple(
            diagnostic for output in outputs for diagnostic in output.diagnostics
        ),
        catalog_prompt=(
            "\n\n".join(
                output.catalog_prompt for output in outputs if output.catalog_prompt
            )
            or None
        ),
        active_skill_prompt=(
            "\n\n".join(
                output.active_skill_prompt
                for output in outputs
                if output.active_skill_prompt
            )
            or None
        ),
    )
