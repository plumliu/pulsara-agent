"""Shared public projection of native inspection diagnostics, formerly in CLI."""

from pulsara_agent.capability.types import (
    ProducerUnavailableCause,
    ResolutionUnavailableCause,
    InvalidSkillCandidateIssue,
    ShadowedSkillCandidateIssue,
    ConflictingSkillCandidateIssue,
    skill_origin_label,
)


def diagnostic_payload(item) -> dict[str, object]:
    if isinstance(item, ProducerUnavailableCause):
        return {
            "kind": "SKILL_PRODUCER_UNAVAILABLE",
            "producer_kind": item.producer_kind.value,
            "reason": item.reason.value,
            "diagnostics": [value.to_dict() for value in item.diagnostics],
        }
    if isinstance(item, ResolutionUnavailableCause):
        return {
            "kind": "SKILL_RESOLUTION_UNAVAILABLE",
            "reason": item.reason.value,
            "diagnostics": [item.diagnostic.to_dict()],
        }
    if isinstance(item, InvalidSkillCandidateIssue):
        return {
            "kind": item.kind.value,
            "path": str(item.path),
            "origin_label": skill_origin_label(item.origin),
            "declared_name": item.declared_name,
            "diagnostics": [value.to_dict() for value in item.diagnostics],
        }
    if isinstance(item, ShadowedSkillCandidateIssue):
        return {
            "kind": item.kind.value,
            "path": str(item.path),
            "origin_label": skill_origin_label(item.origin),
            "name": item.name,
            "winner_origin_label": skill_origin_label(item.winner_origin),
            "winner_path": str(item.winner_path),
            "diagnostic_codes": [value.value for value in item.diagnostic_codes],
        }
    if isinstance(item, ConflictingSkillCandidateIssue):
        return {
            "kind": item.kind.value,
            "name": item.name,
            "tier": item.tier.value,
            "candidates": [
                {
                    "path": str(candidate.path),
                    "origin_label": skill_origin_label(candidate.origin),
                }
                for candidate in item.candidates
            ],
            "diagnostic_codes": [item.diagnostic_code.value],
        }
    if hasattr(item, "to_dict"):
        return item.to_dict()
    value: dict[str, object] = {
        "code": getattr(
            getattr(item, "code", None),
            "value",
            getattr(item, "code", type(item).__name__),
        ),
        "message": getattr(item, "message", type(item).__name__),
    }
    for name in ("severity", "path", "component", "source_label"):
        field = getattr(item, name, None)
        if field is not None:
            value[name] = getattr(field, "value", field)
    return value
