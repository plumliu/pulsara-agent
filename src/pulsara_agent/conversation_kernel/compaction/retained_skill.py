"""Carry previously read, still enabled Skill identities beside TODO/PCA."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pulsara_agent.capability.contracts import LocalSkillRootKind
from pulsara_agent.capability.resolver import (
    CompleteEffectiveSkillCatalogInspection,
    EffectiveSkillCatalogInspection,
)
from pulsara_agent.capability.types import MinimalSkillRef
from pulsara_agent.conversation_kernel.compaction.contracts import FrozenCompactionCanonicalRead
from pulsara_agent.model_input.contracts import ModelInputTokenEstimator
from pulsara_agent.model_input.continuity import FrozenProviderInputEpochView
from pulsara_agent.primitives.context import canonical_json_bytes


@dataclass(frozen=True, slots=True)
class FrozenRetainedSkillContextSelection:
    ordered_items: tuple[MinimalSkillRef, ...] = field(repr=False)
    rendered_body: str = field(repr=False)
    estimated_tokens: int


def freeze_retained_skill_context(
    *, canonical_read: FrozenCompactionCanonicalRead,
    predecessor_epoch: FrozenProviderInputEpochView | None,
    inspection: EffectiveSkillCatalogInspection,
    estimator: ModelInputTokenEstimator,
) -> FrozenRetainedSkillContextSelection:
    """Successful partial and JS reads count; no FULL-page or body-copy ledger."""
    canonical = canonical_read.dispatch_read.compile_snapshot.canonical_input
    refs = {} if predecessor_epoch is None else {
        item.skill_path: item for item in predecessor_epoch.observation_state.reread_skills
    }
    if predecessor_epoch is not None and (
        predecessor_epoch.scope.session_id != canonical.identity.session_id
        or predecessor_epoch.scope.scope_kind is not canonical.identity.conversation_scope_kind
    ):
        raise ValueError("Skill handoff predecessor belongs to another scope")
    if isinstance(inspection, CompleteEffectiveSkillCatalogInspection):
        available = {str(item.path): MinimalSkillRef(item.name, str(item.path))
                     for item in inspection.readable_skills}
        # Source owners already filtered disabled and removed physical copies.
        refs = {path: available[path] for path in refs if path in available}
        result_paths = {_public_path(path, inspection): item for path, item in available.items()}
        for path in canonical_read.successful_file_read_paths:
            ref = result_paths.get(path)
            if ref is not None:
                refs[ref.skill_path] = ref
    ordered = tuple(refs.values())
    body = _render(ordered, unavailable=not isinstance(inspection, CompleteEffectiveSkillCatalogInspection))
    return FrozenRetainedSkillContextSelection(ordered, body, estimator.estimate_text(body) if body else 0)


def _public_path(path: str, inspection: CompleteEffectiveSkillCatalogInspection) -> str:
    workspace = next((root.containment_root for root in inspection.root_policy.roots
                      if root.root_kind is LocalSkillRootKind.WORKSPACE_PULSARA), None)
    value = Path(path)
    return value.relative_to(workspace).as_posix() if workspace is not None and value.is_relative_to(workspace) else path


def _render(items: tuple[MinimalSkillRef, ...], *, unavailable: bool) -> str:
    if not items:
        return ""
    hint = ("当前技能来源暂不可用；以下是此前读过的技能，来源恢复后必须优先使用 read_file 重新读取说明，再继续相关工作。"
            if unavailable else "继续相关工作前，必须优先使用 read_file 重新读取以下此前读过的技能说明。")
    return hint + "\n" + canonical_json_bytes(tuple({
        "text": item.name, "target": {"kind": "SKILL", "skill_path": item.skill_path},
    } for item in items)).decode("utf-8")


__all__ = ["FrozenRetainedSkillContextSelection", "freeze_retained_skill_context"]
