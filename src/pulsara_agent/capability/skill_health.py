"""Cheap diagnostics for active local skill CLI hints."""

from __future__ import annotations

import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass

from pulsara_agent.capability.types import ActiveSkillInjection, CapabilityDiagnostic


@dataclass(frozen=True, slots=True)
class _CachedBinaryHealth:
    expires_at: float
    found: bool
    path_source: str


@dataclass(frozen=True, slots=True)
class SkillBinaryLookupPath:
    path: str | None
    source: str


class SkillHealthResolver:
    def __init__(
        self,
        *,
        ttl_seconds: float = 30.0,
        which: Callable[[str], str | None] | None = None,
        path_supplier: Callable[[], SkillBinaryLookupPath] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self._which = which
        self._path_supplier = path_supplier
        self._monotonic = monotonic or time.monotonic
        self._binary_cache: dict[tuple[str, str | None, str], _CachedBinaryHealth] = {}

    def diagnostics_for_active_skills(
        self,
        injections: tuple[ActiveSkillInjection, ...],
    ) -> tuple[CapabilityDiagnostic, ...]:
        diagnostics: list[CapabilityDiagnostic] = []
        for injection in injections:
            for binary in injection.required_binaries:
                found, path_source, health_diagnostic = self._binary_found(binary)
                if health_diagnostic is not None:
                    diagnostics.append(_with_skill_path(health_diagnostic, injection))
                    continue
                if not found:
                    diagnostics.append(
                        CapabilityDiagnostic(
                            severity="warning",
                            code="skill_required_binary_missing",
                            message=f"Active skill requires CLI binary not found on {path_source}: {binary}",
                            path=injection.path,
                        )
                    )
            for binary in injection.optional_binaries:
                found, path_source, health_diagnostic = self._binary_found(binary)
                if health_diagnostic is not None:
                    diagnostics.append(_with_skill_path(health_diagnostic, injection))
                    continue
                if not found:
                    diagnostics.append(
                        CapabilityDiagnostic(
                            severity="info",
                            code="skill_optional_binary_missing",
                            message=f"Active skill optional CLI binary not found on {path_source}: {binary}",
                            path=injection.path,
                        )
                    )
            if injection.auth_required != "none":
                diagnostics.append(
                    CapabilityDiagnostic(
                        severity="info",
                        code="skill_auth_required",
                        message=f"Active skill declares auth_required={injection.auth_required}.",
                        path=injection.path,
                    )
                )
            if injection.network_required:
                diagnostics.append(
                    CapabilityDiagnostic(
                        severity="info",
                        code="skill_network_required",
                        message="Active skill declares network_required=true.",
                        path=injection.path,
                    )
                )
        return tuple(diagnostics)

    def _binary_found(self, binary: str) -> tuple[bool, str, CapabilityDiagnostic | None]:
        now = self._monotonic()
        lookup_path = self._lookup_path()
        cache_key = (binary, lookup_path.path, lookup_path.source)
        cached = self._binary_cache.get(cache_key)
        if cached is not None and cached.expires_at > now:
            return cached.found, cached.path_source, None
        try:
            if self._which is not None:
                found = self._which(binary) is not None
            else:
                found = shutil.which(binary, path=lookup_path.path) is not None
        except (OSError, RuntimeError) as exc:
            return False, lookup_path.source, CapabilityDiagnostic(
                severity="warning",
                code="skill_binary_health_check_failed",
                message=f"Could not check active skill CLI binary on {lookup_path.source}: {binary}: {exc}",
            )
        self._binary_cache[cache_key] = _CachedBinaryHealth(
            expires_at=now + self.ttl_seconds,
            found=found,
            path_source=lookup_path.source,
        )
        return found, lookup_path.source, None

    def _lookup_path(self) -> SkillBinaryLookupPath:
        if self._path_supplier is None:
            return SkillBinaryLookupPath(path=None, source="Pulsara process PATH")
        try:
            return self._path_supplier()
        except (OSError, RuntimeError) as exc:
            # The check is advisory only. Fall back to the process PATH rather
            # than failing capability resolution.
            del exc
            return SkillBinaryLookupPath(path=None, source="Pulsara process PATH")


def _with_skill_path(diagnostic: CapabilityDiagnostic, injection: ActiveSkillInjection) -> CapabilityDiagnostic:
    if diagnostic.path is not None:
        return diagnostic
    return CapabilityDiagnostic(
        severity=diagnostic.severity,
        code=diagnostic.code,
        message=diagnostic.message,
        path=injection.path,
    )
