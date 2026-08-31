"""Effective Skill source owner and Round 9 sibling-view composer."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Callable

from pulsara_agent.capability.bundled_skills import (
    BundledSkillDefinitionProducer,
    BundledSkillDistributionBindingOwner,
    EXPECTED_BUNDLED_SKILL_NAMES,
)
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
)
from pulsara_agent.capability.local_skills import (
    AGENT_SKILLS_CONTRACT_ID,
    FrozenLooseSkillDefinitions,
    LOOSE_SKILL_ROOT_ORDER,
    SKILL_PLACEMENT_CONTRACT_ID,
    LooseSkillDefinitionsDisposition,
    LooseSkillDefinitionProducer,
    check_skill_deadline,
)
from pulsara_agent.capability.provider import SkillProjectionOutput
from pulsara_agent.capability.plugin_skill_contracts import (
    FrozenPluginSkillDefinitions,
)
from pulsara_agent.capability.resolver import (
    CompleteEffectiveSkillCatalogInspection,
    SkillCatalogCapabilityProvider,
    SkillCatalogResolver,
)
from pulsara_agent.capability.types import (
    LooseSkillOrigin,
    SkillManifest,
    SkillProjectionResolveContext,
)
from pulsara_agent.capability.user_skill_config import UserSkillConfigSnapshot
from pulsara_agent.conversation_kernel.capability_composition import (
    PreparedSkillCatalogSourceSnapshot,
    issue_skill_catalog_source_snapshot,
)
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.primitives.context import context_fingerprint


_SKILL_SOURCE_ID = "pulsara-local-skill-catalog"


class KernelSkillProjectionComposer:
    """Freeze one effective loose+Plugin+bundled Skill source."""

    def __init__(
        self,
        *,
        workspace_root: Path,
        bundled_binding_owner: BundledSkillDistributionBindingOwner,
        plugin_definitions_provider: Callable[[], FrozenPluginSkillDefinitions],
        configured_active_skill_names: frozenset[str] = frozenset(),
        user_skill_config_provider: Callable[[], UserSkillConfigSnapshot] | None = None,
        loose_producer: LooseSkillDefinitionProducer | None = None,
        catalog_resolver: SkillCatalogResolver | None = None,
        projection_provider: SkillCatalogCapabilityProvider | None = None,
    ) -> None:
        self._workspace_root = workspace_root
        self._configured = configured_active_skill_names
        self._loose_producer = loose_producer or LooseSkillDefinitionProducer()
        self._bundled_producer = BundledSkillDefinitionProducer(
            bundled_binding_owner
        )
        self._plugin_definitions_provider = plugin_definitions_provider
        self._user_skill_config_provider = user_skill_config_provider or (
            lambda: UserSkillConfigSnapshot(config_path=Path("skills.yaml"))
        )
        self._resolver = catalog_resolver or SkillCatalogResolver()
        self._projection_provider = (
            projection_provider or SkillCatalogCapabilityProvider()
        )
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
    ) -> PreparedSkillCatalogSourceSnapshot:
        check_skill_deadline(deadline_monotonic)
        root_policy = self._loose_producer.prepare_root_policy(self._workspace_root)
        bundled = self._bundled_producer.observe(
            deadline_monotonic=deadline_monotonic
        )
        check_skill_deadline(deadline_monotonic)
        loose = self._loose_producer.observe(
            root_policy, deadline_monotonic=deadline_monotonic
        )
        if loose.root_policy is not root_policy:
            raise ValueError("loose definitions do not join their physical policy")
        user_skill_config = self._user_skill_config_provider()
        if not isinstance(user_skill_config, UserSkillConfigSnapshot):
            raise TypeError("user Skill config provider returned a foreign snapshot")
        loose = _apply_user_skill_config(loose, user_skill_config)
        check_skill_deadline(deadline_monotonic)
        plugin = self._plugin_definitions_provider()
        if not isinstance(plugin, FrozenPluginSkillDefinitions):
            raise TypeError("Plugin Skill definition provider returned a foreign batch")
        inspection = self._resolver.resolve(loose, plugin, bundled)
        check_skill_deadline(deadline_monotonic)
        source = capability_source_ref(
            CapabilitySourceKind.LOCAL_SKILL_CATALOG,
            _SKILL_SOURCE_ID,
        )
        registration = capability_source_registration(
            source=source,
            refresh_mode=CapabilitySourceRefreshMode.SAFE_POINT_REFRESHABLE,
            source_contract_fingerprint=context_fingerprint(
                "skill-source-contract:v5-user-enablement",
                {
                    "parser_contract": AGENT_SKILLS_CONTRACT_ID,
                    "placement_contract": SKILL_PLACEMENT_CONTRACT_ID,
                    "producer_kinds": ("LOOSE", "PLUGIN", "BUNDLED"),
                    "precedence": (
                        *(item.value for item in LOOSE_SKILL_ROOT_ORDER),
                        "WORKSPACE_PLUGIN",
                        "USER_PLUGIN",
                        "BUNDLED",
                    ),
                    "bundled_names": EXPECTED_BUNDLED_SKILL_NAMES,
                    "user_enablement": "exact-path-user-config",
                },
            ),
        )
        complete = isinstance(inspection, CompleteEffectiveSkillCatalogInspection)
        disposition = (
            CapabilitySourceSnapshotDisposition.COMPLETE
            if complete
            else CapabilitySourceSnapshotDisposition.UNAVAILABLE
        )
        facts = (
            tuple(_skill_fact(source, item) for item in inspection.winners)
            if complete
            else ()
        )
        snapshot = freeze_capability_source_snapshot(
            registration=registration,
            conversation_scope_kind=conversation_scope_kind,
            scope_subagent_task_id=scope_subagent_task_id,
            disposition=disposition,
            facts=facts,
        )
        return issue_skill_catalog_source_snapshot(
            conversation_scope_kind=conversation_scope_kind,
            scope_subagent_task_id=scope_subagent_task_id,
            source_snapshot=snapshot,
            inspection=inspection,
            owner_authenticity=self._owner_authenticity,
        )

    def freeze_projection_input(
        self, owner: PreparedSkillCatalogSourceSnapshot
    ) -> FrozenSkillProjectionInput:
        if owner.owner_authenticity is not self._owner_authenticity:
            raise ValueError("foreign Skill source snapshot")
        return FrozenSkillProjectionInput(
            source_snapshot=owner.source_snapshot,
            inspection=owner.inspection,
        )

    def compose(
        self,
        *,
        view: FrozenSkillCapabilityDispatchView,
        owner: PreparedSkillCatalogSourceSnapshot,
        activation_subject: SkillProjectionResolveContext,
    ) -> SkillProjectionOutput:
        if owner.owner_authenticity is not self._owner_authenticity:
            raise ValueError("foreign Skill source snapshot")
        frozen = view.projection_input
        if (
            owner.source_snapshot is not frozen.source_snapshot
            or owner.inspection is not frozen.inspection
        ):
            raise ValueError("Skill projection owner does not exact-join sibling view")
        expected_facts = tuple(
            sorted(
                (
                    _skill_fact(
                        capability_source_ref(
                            CapabilitySourceKind.LOCAL_SKILL_CATALOG,
                            _SKILL_SOURCE_ID,
                        ),
                        item,
                    )
                    for item in owner.inspection.winners
                ),
                key=lambda fact: (
                    fact.identity.kind.value,
                    fact.identity.identity_fingerprint,
                    fact.fact_semantic_fingerprint,
                ),
            )
        )
        if tuple(item.fact_semantic_fingerprint for item in expected_facts) != tuple(
            item.fact_semantic_fingerprint for item in view.registry_skill_facts
        ):
            raise ValueError("Skill projection inspection does not join registry")
        return self._projection_provider.resolve_projection_from_snapshot(
            activation_subject,
            inspection=owner.inspection,
        )

    def activation_context(self, *, user_input: str) -> SkillProjectionResolveContext:
        return SkillProjectionResolveContext(
            user_input=user_input,
            active_skill_names=self._configured,
        )


def _skill_fact(source, skill: SkillManifest) -> FrozenSkillCapabilityFact:
    identity = capability_identity(
        kind=CapabilityKind.SKILL,
        source=source,
        stable_name=skill.name,
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
        origin=skill.origin,
    )
    return FrozenSkillCapabilityFact(
        identity=identity,
        public_name=skill.name,
        description=skill.description,
        location=skill.location,
        origin=skill.origin,
        catalog_semantic_fingerprint=catalog,
        activation_semantic_fingerprint=activation,
        fact_semantic_fingerprint=fact_fingerprint,
    )


def _apply_user_skill_config(
    loose: FrozenLooseSkillDefinitions,
    config: UserSkillConfigSnapshot,
) -> FrozenLooseSkillDefinitions:
    if loose.disposition is LooseSkillDefinitionsDisposition.UNAVAILABLE:
        return loose

    def admitted(path: Path, origin: object) -> bool:
        return not (
            isinstance(origin, LooseSkillOrigin)
            and origin.root_kind
            in {LocalSkillRootKind.USER_PULSARA, LocalSkillRootKind.USER_AGENTS}
            and not config.enabled_for(path)
        )

    return FrozenLooseSkillDefinitions(
        root_policy=loose.root_policy,
        disposition=loose.disposition,
        candidates=tuple(
            item for item in loose.candidates if admitted(item.path, item.origin)
        ),
        invalid_issues=tuple(
            item
            for item in loose.invalid_issues
            if admitted(item.path, item.origin)
        ),
    )


__all__ = ["KernelSkillProjectionComposer"]
