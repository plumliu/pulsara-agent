"""Provider-agnostic lifecycle policy for context sections."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

from pulsara_agent.runtime.context_engine.types import (
    ContextCompileRequest,
    ContextLifecycleDecisionDiagnostic,
    ContextSection,
)


class ContextLifecycleCoordinator:
    """Collect/reuse/invalidate section candidates without provider cache logic."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str, str, str, str], ContextSection] = {}

    def apply(
        self,
        request: ContextCompileRequest,
        sections: tuple[ContextSection, ...],
    ) -> tuple[tuple[ContextSection, ...], tuple[ContextLifecycleDecisionDiagnostic, ...]]:
        final_sections: list[ContextSection] = []
        decisions: list[ContextLifecycleDecisionDiagnostic] = []
        for section in sections:
            scoped = _scope_key(request, section)
            if section.stability == "ephemeral" or section.dependency_fingerprint is None:
                final_sections.append(
                    replace(
                        _copy_section(section),
                        lifecycle_status="not_cacheable",
                        lifecycle_reason=(
                            "ephemeral"
                            if section.stability == "ephemeral"
                            else "missing_dependency_fingerprint"
                        ),
                        cache_key_scope=scoped,
                    )
                )
                continue
            key = (
                request.runtime_session_id,
                section.source_id,
                section.id,
                section.stability,
                scoped,
            )
            existing = self._entries.get(key)
            if existing is not None and existing.dependency_fingerprint == section.dependency_fingerprint:
                final_sections.append(
                    replace(
                        _copy_section(existing),
                        lifecycle_status="reused",
                        lifecycle_reason="dependency_fingerprint_unchanged",
                        cache_key_scope=scoped,
                    )
                )
                continue
            if existing is not None:
                decisions.append(
                    ContextLifecycleDecisionDiagnostic(
                        source_id=section.source_id,
                        section_id=section.id,
                        old_cache_key_scope=scoped,
                        old_dependency_fingerprint=existing.dependency_fingerprint or "",
                        new_dependency_fingerprint=section.dependency_fingerprint,
                        decision="invalidated",
                        reason="dependency_fingerprint_changed",
                    )
                )
            fresh = replace(
                _copy_section(section),
                lifecycle_status="freshly_collected",
                lifecycle_reason="dependency_fingerprint_recorded",
                cache_key_scope=scoped,
            )
            self._entries[key] = _copy_section(fresh)
            final_sections.append(fresh)
        return tuple(final_sections), tuple(decisions)


def _scope_key(request: ContextCompileRequest, section: ContextSection) -> str:
    if section.stability == "stable":
        return request.runtime_session_id
    if section.stability == "turn":
        return f"{request.run_id}:{request.turn_id}:{request.current_user_anchor or 'no-anchor'}"
    if section.stability == "step":
        return f"{request.run_id}:{request.turn_id}:{request.reply_id}:{request.model_call_index}"
    return request.context_id


def _copy_section(section: ContextSection) -> ContextSection:
    return deepcopy(section)
