"""Agent Skills source owner and Round 9 sibling-view composer."""

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
    skill_capability_fact_fingerprint,
    skill_projection_input_fingerprint,
)
from pulsara_agent.capability.local_skills import (
    AGENT_SKILLS_CONTRACT_ID,
    LocalSkillDiscovery,
    SkillDiscoveryDisposition,
)
from pulsara_agent.capability.provider import SkillProjectionOutput
from pulsara_agent.capability.resolver import LocalSkillCapabilityProvider
from pulsara_agent.capability.types import (
    LocalSkillManifest,
    SkillProjectionResolveContext,
)
from pulsara_agent.conversation_kernel.capability_composition import (
    PreparedLocalSkillCatalogSourceSnapshot,
    issue_local_skill_catalog_source_snapshot,
)
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.primitives.context import context_fingerprint


_LOCAL_SKILL_SOURCE_ID = "pulsara-local-skill-catalog"
_ROOT_ORDER = (
    LocalSkillRootKind.WORKSPACE_PULSARA,
    LocalSkillRootKind.WORKSPACE_AGENTS,
    LocalSkillRootKind.USER_PULSARA,
    LocalSkillRootKind.USER_AGENTS,
)
_ROOT_PREFIX = {
    LocalSkillRootKind.WORKSPACE_PULSARA: ".pulsara/skills",
    LocalSkillRootKind.WORKSPACE_AGENTS: ".agents/skills",
    LocalSkillRootKind.USER_PULSARA: "${PULSARA_HOME}/skills",
    LocalSkillRootKind.USER_AGENTS: "~/.agents/skills",
}


class KernelSkillProjectionComposer:
    """Freeze one aggregate Skill source and compose from its sibling view."""

    def __init__(
        self,
        *,
        workspace_root: Path,
        configured_active_skill_names: frozenset[str] = frozenset(),
        provider: LocalSkillCapabilityProvider | None = None,
    ) -> None:
        self._workspace_root = workspace_root
        self._configured = configured_active_skill_names
        self._provider = provider or LocalSkillCapabilityProvider()
        self._owner_authenticity = object()

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
        root_policy = self._provider.provider.prepare_root_policy(
            self._workspace_root,
            conversation_scope_kind=conversation_scope_kind,
            scope_subagent_task_id=scope_subagent_task_id,
        )
        discovery = self._provider.snapshot_projection_input(
            root_policy=root_policy,
            deadline_monotonic=deadline_monotonic,
        )
        if discovery.root_policy_fingerprint != root_policy.root_policy_fingerprint:
            raise ValueError("Skill discovery does not join its physical root policy")
        source = capability_source_ref(
            CapabilitySourceKind.LOCAL_SKILL_CATALOG,
            _LOCAL_SKILL_SOURCE_ID,
        )
        registration = capability_source_registration(
            source=source,
            refresh_mode=CapabilitySourceRefreshMode.SAFE_POINT_REFRESHABLE,
            source_contract_fingerprint=context_fingerprint(
                "local-skill-source-contract:v2-agent-skills",
                {"parser": AGENT_SKILLS_CONTRACT_ID},
            ),
        )
        disposition = (
            CapabilitySourceSnapshotDisposition.COMPLETE
            if discovery.disposition is SkillDiscoveryDisposition.COMPLETE
            else CapabilitySourceSnapshotDisposition.UNAVAILABLE
        )
        facts = (
            tuple(_skill_fact(source, item) for item in discovery.skills)
            if disposition is CapabilitySourceSnapshotDisposition.COMPLETE
            else ()
        )
        snapshot = freeze_capability_source_snapshot(
            registration=registration,
            conversation_scope_kind=conversation_scope_kind,
            scope_subagent_task_id=scope_subagent_task_id,
            disposition=disposition,
            facts=facts,
        )
        return issue_local_skill_catalog_source_snapshot(
            conversation_scope_kind=conversation_scope_kind,
            scope_subagent_task_id=scope_subagent_task_id,
            source_snapshot=snapshot,
            root_policy_fingerprint=root_policy.root_policy_fingerprint,
            discovery=discovery,
            owner_authenticity=self._owner_authenticity,
        )

    def freeze_projection_input(
        self, owner: PreparedLocalSkillCatalogSourceSnapshot
    ) -> FrozenSkillProjectionInput:
        if owner.owner_authenticity is not self._owner_authenticity:
            raise ValueError("foreign Skill source snapshot")
        if (
            owner.root_policy_fingerprint
            != owner.discovery.root_policy_fingerprint
        ):
            raise ValueError("Skill owner carrier root policy drifted")
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
            or owner.root_policy_fingerprint
            != owner.discovery.root_policy_fingerprint
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
        return self._provider.resolve_projection_from_snapshot(
            activation_subject,
            discovery=owner.discovery,
        )

    def activation_context(
        self, *, user_input: str
    ) -> SkillProjectionResolveContext:
        return SkillProjectionResolveContext(
            user_input=user_input,
            active_skill_names=self._configured,
        )


def skill_discovery_semantic_fingerprint(
    discovery: LocalSkillDiscovery,
) -> str:
    if not isinstance(discovery, LocalSkillDiscovery):
        raise TypeError("Skill discovery carrier is not frozen")
    return context_fingerprint(
        "local-skill-discovery-semantic:v2-agent-skills",
        {
            "disposition": discovery.disposition.value,
            "unavailable_reason": (
                None
                if discovery.unavailable_reason is None
                else discovery.unavailable_reason.value
            ),
            "root_policy": discovery.root_policy_fingerprint,
            "skills": tuple(
                {
                    "manifest": skill.manifest_semantic_fingerprint,
                    "raw_document": skill.raw_document_digest,
                    "location": skill.location,
                    "root_kind": skill.root_kind.value,
                }
                for skill in discovery.skills
            ),
            "diagnostics": tuple(
                (item.severity.value, item.code) for item in discovery.diagnostics
            ),
        },
    )


def _skill_fact(
    source, skill: LocalSkillManifest
) -> FrozenSkillCapabilityFact:
    kind = skill.root_kind
    ordinal = _ROOT_ORDER.index(kind)
    prefix = _ROOT_PREFIX[kind]
    identity = capability_identity(
        kind=CapabilityKind.SKILL,
        source=source,
        stable_name=skill.name,
    )
    root_fingerprint = context_fingerprint(
        "local-skill-winning-root-provenance:v2-agent-skills",
        {
            "root_kind": kind.value,
            "precedence_ordinal": ordinal,
            "stable_location_prefix": prefix,
        },
    )
    catalog = context_fingerprint(
        "local-skill-catalog-semantic:v2-agent-skills",
        {
            "name": skill.name,
            "description": skill.description,
            "location": skill.location,
        },
    )
    activation = context_fingerprint(
        "local-skill-activation-semantic:v2-agent-skills",
        {
            "name": skill.name,
            "location": skill.location,
            "source": skill.source.value,
            "body_digest": "sha256:"
            + sha256(skill.body.encode("utf-8")).hexdigest(),
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


__all__ = [
    "KernelSkillProjectionComposer",
    "skill_discovery_semantic_fingerprint",
]
