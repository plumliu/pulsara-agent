"""Runtime capability and local skill resolution.

The compatibility exports are resolved lazily so importing a focused
capability submodule does not construct the legacy tool/runtime surface.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any


_LAZY_MODULES = (
    "pulsara_agent.capability.bundled_skills",
    "pulsara_agent.capability.local_skills",
    "pulsara_agent.capability.builtin_provider",
    "pulsara_agent.capability.render",
    "pulsara_agent.capability.resolver",
    "pulsara_agent.capability.skill_health",
    "pulsara_agent.capability.call_classifier",
    "pulsara_agent.capability.descriptor",
    "pulsara_agent.ports.artifact",
    "pulsara_agent.capability.exposure",
    "pulsara_agent.capability.provider",
    "pulsara_agent.capability.types",
    "pulsara_agent.capability.result_semantics",
)


def __getattr__(name: str) -> Any:
    if name not in __all__:
        raise AttributeError(name)
    for module_name in _LAZY_MODULES:
        module = import_module(module_name)
        try:
            value = getattr(module, name)
        except AttributeError:
            continue
        globals()[name] = value
        return value
    raise AttributeError(name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))

__all__ = [
    "ActiveSkillInjection",
    "BUNDLED_MANIFEST_FILE_NAME",
    "BUNDLED_OPT_OUT_MARKER_NAME",
    "BundledSkillResetResult",
    "BundledSkillStatus",
    "BundledSkillStatusResult",
    "BundledSkillSyncItem",
    "BundledSkillSyncResult",
    "BuiltinToolCapabilityProvider",
    "CapabilityDiagnostic",
    "CapabilityAdvertisePolicy",
    "CapabilityAvailability",
    "CapabilityCallClassification",
    "CapabilityDescriptor",
    "CapabilityExposurePlan",
    "CapabilityProviderKind",
    "CapabilityProvenance",
    "CapabilityDescriptorSnapshotOutput",
    "CapabilityExecutionSurfaceProvider",
    "CapabilityExecutionSurfaceSnapshotContext",
    "CapabilityProjectionOutput",
    "CapabilityProjectionProvider",
    "CapabilityProviderComponent",
    "CapabilityProjectionResolveContext",
    "DefaultCapabilityCallClassifier",
    "LocalSkillManifest",
    "LocalSkillCapabilityProvider",
    "LocalSkillProvider",
    "RenderedCapabilityPrompt",
    "ResolvedSkillCatalogEntry",
    "SkillHealthResolver",
    "SkillBinaryLookupPath",
    "ToolResultSemanticsBuilder",
    "ToolResultSemanticsBuilderBinding",
    "ToolResultSemanticsBuilderRegistry",
    "ToolResultSemanticsRuntimeInput",
    "ToolArtifactMode",
    "bundled_skills_status",
    "default_pulsara_home",
    "render_active_skill_prompt",
    "render_catalog_prompt",
    "reset_bundled_skill",
    "sync_bundled_skills",
    "user_product_skills_root",
]
