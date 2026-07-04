"""Capability runtime facade that resolves descriptors and prompts once per turn."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pulsara_agent.capability.builtin_provider import BuiltinToolCapabilityProvider
from pulsara_agent.capability.exposure import CapabilityExposurePlan, build_exposure_plan
from pulsara_agent.capability.provider import CapabilityProvider, CapabilityProviderOutput
from pulsara_agent.capability.registry import CapabilityRegistry
from pulsara_agent.capability.types import CapabilityDiagnostic, CapabilityResolveContext

if TYPE_CHECKING:
    from pulsara_agent.runtime.permission import EffectivePermissionPolicy
    from pulsara_agent.tools.registry import ToolRegistry


@dataclass(slots=True)
class CapabilityRuntime:
    providers: tuple[CapabilityProvider, ...] = (BuiltinToolCapabilityProvider(),)

    @classmethod
    def with_default_providers(cls, *providers: CapabilityProvider) -> "CapabilityRuntime":
        return cls(providers=(BuiltinToolCapabilityProvider(), *providers))

    def resolve_for_turn(
        self,
        context: CapabilityResolveContext,
        *,
        tool_registry: ToolRegistry,
        permission_policy: EffectivePermissionPolicy | None = None,
        plan_active: bool = False,
    ) -> CapabilityExposurePlan:
        del permission_policy, plan_active
        bound_tool_names = frozenset(tool_registry.names())
        outputs = tuple(
            provider.resolve(context, bound_tool_names=bound_tool_names)
            for provider in self.providers
        )
        registry = CapabilityRegistry()
        for output in outputs:
            for descriptor in output.descriptors:
                registry.register(descriptor)
        snapshot = registry.snapshot()
        descriptor_names = frozenset(descriptor.name for descriptor in snapshot.descriptors)
        binding_diagnostics = tuple(
            CapabilityDiagnostic(
                severity="error",
                code="capability_missing_descriptor",
                message=f"Execution binding has no explicit capability descriptor: {name}",
            )
            for name in sorted(bound_tool_names.difference(descriptor_names))
        )
        provider_output = _merge_provider_outputs(outputs, extra_diagnostics=binding_diagnostics)
        return build_exposure_plan(
            snapshot,
            provider_output=provider_output,
            bound_tool_names=bound_tool_names,
        )


def _merge_provider_outputs(
    outputs: tuple[CapabilityProviderOutput, ...],
    *,
    extra_diagnostics: tuple[CapabilityDiagnostic, ...] = (),
) -> CapabilityProviderOutput:
    catalog_entries = tuple(entry for output in outputs for entry in output.catalog_entries)
    active_injections = tuple(injection for output in outputs for injection in output.active_injections)
    diagnostics = (
        *(diagnostic for output in outputs for diagnostic in output.diagnostics),
        *extra_diagnostics,
    )
    catalog_prompt = "\n\n".join(output.catalog_prompt for output in outputs if output.catalog_prompt) or None
    active_skill_prompt = "\n\n".join(
        output.active_skill_prompt for output in outputs if output.active_skill_prompt
    ) or None
    return CapabilityProviderOutput(
        catalog_entries=catalog_entries,
        active_injections=active_injections,
        diagnostics=diagnostics,
        catalog_prompt=catalog_prompt,
        active_skill_prompt=active_skill_prompt,
    )
