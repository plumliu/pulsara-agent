"""Skill source owner and sibling-view composer for the canonical Kernel."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from pulsara_agent.capability.contracts import (
    CapabilityKind,
    CapabilitySourceKind,
    CapabilitySourceRefreshMode,
    CapabilitySourceSnapshotDisposition,
    FrozenSkillCapabilityDispatchView,
    FrozenSkillCapabilityFact,
    FrozenSkillProjectionInput,
    LocalSkillRootKind,
    capability_identity,
    capability_source_ref,
    capability_source_registration,
    freeze_capability_source_snapshot,
    skill_projection_input_fingerprint,
    skill_capability_fact_fingerprint,
)
from pulsara_agent.capability.resolver import LocalSkillCapabilityProvider
from pulsara_agent.capability.local_skills import LocalSkillDiscovery
from pulsara_agent.capability.provider import SkillProjectionOutput
from pulsara_agent.capability.types import (
    LocalSkillManifest,
    SkillDiagnostic,
    SkillProjectionResolveContext,
)
from pulsara_agent.conversation_kernel.capability_composition import (
    PreparedLocalSkillCatalogSourceSnapshot,
    issue_local_skill_catalog_source_snapshot,
)
from pulsara_agent.memory.scope import MemoryDomainContext
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.primitives.context import context_fingerprint


_LOCAL_SKILL_SOURCE_ID = "pulsara-local-skill-catalog"
_ROOT_POLICY = (
    LocalSkillRootKind.WORKSPACE_PULSARA,
    LocalSkillRootKind.WORKSPACE_AGENTS,
    LocalSkillRootKind.USER_PULSARA,
    LocalSkillRootKind.USER_AGENTS,
)


class KernelSkillProjectionComposer:
    """Own one bounded local scan; compose only from a parent-derived view."""

    def __init__(
        self,
        *,
        workspace_root: Path,
        workspace_kind: str,
        memory_domain: MemoryDomainContext,
        configured_active_skill_names: frozenset[str] = frozenset(),
        provider: LocalSkillCapabilityProvider | None = None,
    ) -> None:
        if workspace_kind not in {"project", "transient"}:
            raise ValueError("kernel Skill workspace kind is invalid")
        self._workspace_root = workspace_root
        self._workspace_kind = workspace_kind
        self._memory_domain = memory_domain
        self._configured = configured_active_skill_names
        self._provider = provider or LocalSkillCapabilityProvider()
        self._owner_authenticity = object()
        self._root_policy_fingerprint = context_fingerprint(
            "local-skill-root-policy:v1",
            tuple(item.value for item in _ROOT_POLICY),
        )

    @property
    def configured_active_skill_names(self) -> frozenset[str]:
        return self._configured

    def freeze_owner_snapshot(
        self,
        *,
        conversation_scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
        deadline_monotonic: float | None = None,
    ) -> PreparedLocalSkillCatalogSourceSnapshot:
        unavailable = False
        try:
            discovery = self._provider.snapshot_projection_input(
                workspace_root=self._workspace_root,
                available_tool_names=frozenset(),
                deadline_monotonic=deadline_monotonic,
            )
        except TimeoutError:
            raise
        except Exception:
            # A filesystem failure can make the global ordered scan
            # unknowable.  Publish one closed UNAVAILABLE source snapshot;
            # individual invalid manifests returned by a completed scan are
            # merely omitted leaves and do not poison the whole catalog.
            unavailable = True
            discovery = LocalSkillDiscovery(
                skills=(),
                diagnostics=(
                    SkillDiagnostic(
                        severity="error",
                        code="skill_catalog_unavailable",
                        message="Local Skill catalog is unavailable",
                    ),
                ),
            )
        source = capability_source_ref(
            CapabilitySourceKind.LOCAL_SKILL_CATALOG,
            _LOCAL_SKILL_SOURCE_ID,
        )
        registration = capability_source_registration(
            source=source,
            refresh_mode=CapabilitySourceRefreshMode.SAFE_POINT_REFRESHABLE,
            source_contract_fingerprint=context_fingerprint(
                "local-skill-source-contract:v1",
                {
                    "root_policy": self._root_policy_fingerprint,
                    "parser": "pulsara-local-skill-v1",
                },
            ),
        )
        facts = tuple(_skill_fact(source, item) for item in discovery.skills)
        disposition = (
            CapabilitySourceSnapshotDisposition.UNAVAILABLE
            if unavailable
            else CapabilitySourceSnapshotDisposition.COMPLETE
        )
        snapshot = freeze_capability_source_snapshot(
            registration=registration,
            conversation_scope_kind=conversation_scope_kind,
            scope_subagent_task_id=scope_subagent_task_id,
            disposition=disposition,
            facts=() if disposition is CapabilitySourceSnapshotDisposition.UNAVAILABLE else facts,
        )
        return issue_local_skill_catalog_source_snapshot(
            conversation_scope_kind=conversation_scope_kind,
            scope_subagent_task_id=scope_subagent_task_id,
            source_snapshot=snapshot,
            discovery=discovery,
            owner_authenticity=self._owner_authenticity,
        )

    def freeze_projection_input(
        self, owner: PreparedLocalSkillCatalogSourceSnapshot
    ) -> FrozenSkillProjectionInput:
        if owner.owner_authenticity is not self._owner_authenticity:
            raise ValueError("foreign Skill source snapshot")
        discovery_fingerprint = skill_discovery_semantic_fingerprint(
            owner.discovery
        )
        fingerprint = skill_projection_input_fingerprint(
            discovery_semantic_fingerprint=discovery_fingerprint,
            source_snapshot_fingerprint=(
                owner.source_snapshot.source_snapshot_fingerprint
            ),
        )
        return FrozenSkillProjectionInput(
            discovery_semantic_fingerprint=discovery_fingerprint,
            source_snapshot_fingerprint=(
                owner.source_snapshot.source_snapshot_fingerprint
            ),
            snapshot_fingerprint=fingerprint,
        )

    def compose(
        self,
        *,
        view: FrozenSkillCapabilityDispatchView,
        owner: PreparedLocalSkillCatalogSourceSnapshot,
        activation_subject: SkillProjectionResolveContext,
    ) -> SkillProjectionOutput:
        if owner.owner_authenticity is not self._owner_authenticity:
            raise ValueError("foreign Skill source snapshot")
        frozen = view.projection_input
        if (
            owner.source_snapshot.source_snapshot_fingerprint
            != frozen.source_snapshot_fingerprint
            or skill_discovery_semantic_fingerprint(owner.discovery)
            != frozen.discovery_semantic_fingerprint
        ):
            raise ValueError("Skill projection owner does not exact-join sibling view")
        expected_facts = tuple(
            sorted(
                (
                    _skill_fact(
                        capability_source_ref(
                            CapabilitySourceKind.LOCAL_SKILL_CATALOG,
                            _LOCAL_SKILL_SOURCE_ID,
                        ),
                        item,
                    )
                    for item in owner.discovery.skills
                ),
                key=lambda fact: (
                    fact.identity.kind.value,
                    fact.identity.identity_fingerprint,
                    fact.fact_semantic_fingerprint,
                ),
            )
        )
        if tuple(
            item.fact_semantic_fingerprint for item in expected_facts
        ) != tuple(
            item.fact_semantic_fingerprint for item in view.registry_skill_facts
        ):
            raise ValueError("Skill projection discovery does not join registry")
        if (
            owner.source_snapshot.disposition
            is CapabilitySourceSnapshotDisposition.UNAVAILABLE
        ):
            output = SkillProjectionOutput(diagnostics=owner.discovery.diagnostics)
        else:
            output = self._provider.resolve_projection_from_snapshot(
                activation_subject,
                available_tool_names=frozenset(),
                discovery=owner.discovery,
            )
        return output

    def activation_context(self, *, user_input: str) -> SkillProjectionResolveContext:
        return SkillProjectionResolveContext(
            workspace_root=self._workspace_root,
            workspace_kind=self._workspace_kind,  # type: ignore[arg-type]
            memory_domain=self._memory_domain,
            user_input=user_input,
            active_skill_names=self._configured,
        )


def _root_provenance(skill: LocalSkillManifest) -> tuple[LocalSkillRootKind, int, str]:
    location = skill.location
    if location.startswith(".pulsara/skills/"):
        kind = LocalSkillRootKind.WORKSPACE_PULSARA
        prefix = ".pulsara/skills"
    elif location.startswith(".agents/skills/"):
        kind = LocalSkillRootKind.WORKSPACE_AGENTS
        prefix = ".agents/skills"
    elif location.startswith("~/.pulsara/skills/"):
        kind = LocalSkillRootKind.USER_PULSARA
        prefix = "~/.pulsara/skills"
    elif location.startswith("~/.agents/skills/"):
        kind = LocalSkillRootKind.USER_AGENTS
        prefix = "~/.agents/skills"
    else:
        raise ValueError("Skill location escaped the four-root policy")
    return kind, _ROOT_POLICY.index(kind), prefix


def skill_discovery_semantic_fingerprint(
    discovery: LocalSkillDiscovery,
) -> str:
    """Freeze the source-specific parsed carrier outside pure capability DTOs."""

    if not isinstance(discovery, LocalSkillDiscovery):
        raise TypeError("Skill discovery carrier is not frozen")
    return context_fingerprint(
        "local-skill-discovery-semantic:v1",
        {
            "skills": tuple(
                {
                    "name": skill.name,
                    "description": skill.description,
                    "location": skill.location,
                    "content_digest": "sha256:"
                    + sha256(skill.content.encode("utf-8")).hexdigest(),
                    "source": skill.source,
                    "when_to_use": skill.when_to_use,
                    "body_too_large": skill.body_too_large,
                    "disable_model_invocation": skill.disable_model_invocation,
                    "user_invocable": skill.user_invocable,
                }
                for skill in discovery.skills
            ),
            "diagnostics": tuple(
                (item.severity, item.code) for item in discovery.diagnostics
            ),
        },
    )


def _skill_fact(source, skill: LocalSkillManifest) -> FrozenSkillCapabilityFact:
    kind, ordinal, prefix = _root_provenance(skill)
    identity = capability_identity(
        kind=CapabilityKind.SKILL,
        source=source,
        stable_name=skill.name,
    )
    root_fingerprint = context_fingerprint(
        "local-skill-winning-root-provenance:v1",
        {
            "root_kind": kind.value,
            "precedence_ordinal": ordinal,
            "stable_location_prefix": prefix,
        },
    )
    catalog = context_fingerprint(
        "local-skill-catalog-semantic:v1",
        {
            "name": skill.name,
            "description": skill.description,
            "location": skill.location,
            "when_to_use": skill.when_to_use,
            "source": skill.source,
        },
    )
    activation = context_fingerprint(
        "local-skill-activation-semantic:v1",
        {
            "name": skill.name,
            "content_digest": "sha256:"
            + sha256(skill.content.encode("utf-8")).hexdigest(),
            "body_too_large": skill.body_too_large,
            "disable_model_invocation": skill.disable_model_invocation,
            "user_invocable": skill.user_invocable,
        },
    )
    fact_fingerprint = skill_capability_fact_fingerprint(
        identity_fingerprint=identity.identity_fingerprint,
        catalog_semantic_fingerprint=catalog,
        activation_semantic_fingerprint=activation,
        winning_root_provenance_fingerprint=root_fingerprint,
    )
    return FrozenSkillCapabilityFact(
        identity=identity,
        public_name=skill.name,
        description=skill.description,
        location=skill.location,
        winning_root_provenance_fingerprint=root_fingerprint,
        catalog_semantic_fingerprint=catalog,
        activation_semantic_fingerprint=activation,
        fact_semantic_fingerprint=fact_fingerprint,
    )


__all__ = ["KernelSkillProjectionComposer", "skill_discovery_semantic_fingerprint"]
