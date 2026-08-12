"""First-party process-local source collection for structured model input."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
import json
from pathlib import Path
from typing import Callable, Protocol

from pulsara_agent.capability.provider import CapabilityProjectionOutput
from pulsara_agent.capability.types import CapabilityDiagnostic
from pulsara_agent.conversation_kernel.capability import KernelCapabilityComposer
from pulsara_agent.model_input.contracts import (
    CapabilityActivationSubjectKind,
    CollectedContextSources,
    ContextBudgetClass,
    ContextChannel,
    ContextPublicDiagnosticCode,
    ContextRenderMode,
    ContextRenderVariant,
    ContextSourceCandidate,
    ContextSourceCollectionDiagnostic,
    ContextSourceKind,
    ContextTrustClass,
    FrozenModelToolSurface,
    RuntimeClockSnapshot,
    RuntimeEnvironmentSnapshot,
    RuntimeTemporalCapture,
)
from pulsara_agent.primitives.context import context_fingerprint


class TerminalCurrentCwdSnapshotPort(Protocol):
    def snapshot_terminal_cwd(self) -> Path: ...


class ContextSourceCollectorPort(Protocol):
    @property
    def registry_fingerprint(self) -> str: ...

    def collect(
        self,
        *,
        activation_subject: CapabilityActivationSubjectKind,
        activation_text: str,
        tool_surface: FrozenModelToolSurface,
        deadline_monotonic: float | None = None,
    ) -> CollectedContextSources: ...


@dataclass(frozen=True, slots=True)
class _SourceBinding:
    source_kind: ContextSourceKind
    contract_version: str
    channel: ContextChannel
    trust: ContextTrustClass
    budget: ContextBudgetClass
    placement: int
    degradation: int
    modes: tuple[ContextRenderMode, ...]
    implementation_contract_version: str

    @property
    def contract_fingerprint(self) -> str:
        return context_fingerprint(
            "context-source-contract:v1",
            {
                "kind": self.source_kind.value,
                "version": self.contract_version,
                "channel": self.channel.value,
                "trust": self.trust.value,
                "budget": self.budget.value,
                "placement": self.placement,
                "degradation": self.degradation,
                "modes": tuple(mode.value for mode in self.modes),
            },
        )


_BINDINGS = (
    _SourceBinding(
        ContextSourceKind.BASE_SYSTEM,
        "pulsara.base-system.v1",
        ContextChannel.SYSTEM,
        ContextTrustClass.ROOT_INSTRUCTION,
        ContextBudgetClass.MUST_KEEP,
        0,
        0,
        (ContextRenderMode.FULL,),
        "pulsara.base-system-collector.v1",
    ),
    _SourceBinding(
        ContextSourceKind.RUNTIME_ENVIRONMENT,
        "pulsara.runtime-environment.v1",
        ContextChannel.SYSTEM,
        ContextTrustClass.TRUSTED_RUNTIME_FACT,
        ContextBudgetClass.MUST_KEEP,
        10,
        10,
        (ContextRenderMode.FULL, ContextRenderMode.COMPACT),
        "pulsara.runtime-environment-collector.v1",
    ),
    _SourceBinding(
        ContextSourceKind.RUNTIME_CLOCK,
        "pulsara.runtime-clock.v1",
        ContextChannel.LEADING_OBSERVATION,
        ContextTrustClass.TRUSTED_RUNTIME_FACT,
        ContextBudgetClass.OPTIONAL,
        0,
        80,
        (ContextRenderMode.FULL, ContextRenderMode.COMPACT),
        "pulsara.runtime-clock-collector.v1",
    ),
    _SourceBinding(
        ContextSourceKind.CAPABILITY_CATALOG,
        "pulsara.capability-catalog.v1",
        ContextChannel.SYSTEM,
        ContextTrustClass.AUTHORIZED_CAPABILITY_CONTEXT,
        ContextBudgetClass.IMPORTANT,
        20,
        30,
        (ContextRenderMode.FULL, ContextRenderMode.COMPACT, ContextRenderMode.REF_ONLY),
        "pulsara.capability-catalog-collector.v1",
    ),
    _SourceBinding(
        ContextSourceKind.ACTIVE_SKILL,
        "pulsara.active-skill.v1",
        ContextChannel.SYSTEM,
        ContextTrustClass.AUTHORIZED_CAPABILITY_CONTEXT,
        ContextBudgetClass.MUST_KEEP,
        30,
        20,
        (ContextRenderMode.FULL,),
        "pulsara.active-skill-collector.v1",
    ),
)


class ContextSourceRegistry:
    """Closed first-party binding set, never a domain truth registry."""

    def __init__(self) -> None:
        kinds = tuple(binding.source_kind for binding in _BINDINGS)
        if len(kinds) != len(set(kinds)):
            raise RuntimeError("first-party context source bindings are duplicated")
        self._by_kind = {binding.source_kind: binding for binding in _BINDINGS}
        self.fingerprint = context_fingerprint(
            "context-source-registry:v1",
            tuple(
                (
                    binding.source_kind.value,
                    binding.contract_fingerprint,
                    binding.implementation_contract_version,
                )
                for binding in _BINDINGS
            ),
        )

    def binding(self, kind: ContextSourceKind) -> _SourceBinding:
        return self._by_kind[kind]


class KernelContextSourceCollector:
    def __init__(
        self,
        *,
        workspace_kind: str,
        workspace_root: Path,
        terminal_cwd: TerminalCurrentCwdSnapshotPort,
        capability_composer: KernelCapabilityComposer,
        base_system_prompt: str,
        display_timezone: tzinfo,
        clock: Callable[[], datetime] | None = None,
        registry: ContextSourceRegistry | None = None,
    ) -> None:
        if workspace_kind not in {"project", "transient"}:
            raise ValueError("context source workspace kind is invalid")
        root = workspace_root.expanduser().resolve()
        base_system_prompt.encode("utf-8")
        if not base_system_prompt:
            raise ValueError("base system prompt is empty")
        self._workspace_kind = workspace_kind
        self._workspace_root = root
        self._terminal_cwd = terminal_cwd
        self._capability = capability_composer
        self._base = base_system_prompt
        self._timezone, self._timezone_name = _freeze_display_timezone(
            display_timezone
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._registry = registry or ContextSourceRegistry()

    @property
    def registry_fingerprint(self) -> str:
        return self._registry.fingerprint

    def collect(
        self,
        *,
        activation_subject: CapabilityActivationSubjectKind,
        activation_text: str,
        tool_surface: FrozenModelToolSurface,
        deadline_monotonic: float | None = None,
    ) -> CollectedContextSources:
        del deadline_monotonic
        candidates: list[ContextSourceCandidate] = []
        diagnostics: list[ContextSourceCollectionDiagnostic] = []
        candidates.append(self._candidate(ContextSourceKind.BASE_SYSTEM, (self._base,)))

        temporal: RuntimeTemporalCapture | None
        try:
            temporal = self._capture_temporal()
        except Exception:
            temporal = None
            diagnostics.append(
                ContextSourceCollectionDiagnostic(
                    ContextPublicDiagnosticCode.RUNTIME_CLOCK_UNAVAILABLE,
                    "WARNING",
                    ContextSourceKind.RUNTIME_CLOCK,
                )
            )
        environment = self._environment_snapshot(temporal)
        candidates.append(
            self._candidate(
                ContextSourceKind.RUNTIME_ENVIRONMENT,
                (
                    _render_environment(environment, compact=False),
                    _render_environment(environment, compact=True),
                ),
            )
        )
        if temporal is not None:
            clock = RuntimeClockSnapshot(
                observed_at_utc=temporal.observed_at_utc,
                local_date=temporal.local_date,
                timezone_name=temporal.timezone_name,
                utc_offset_minutes=temporal.utc_offset_minutes,
                temporal_capture_fingerprint=temporal.capture_fingerprint,
            )
            candidates.append(
                self._candidate(
                    ContextSourceKind.RUNTIME_CLOCK,
                    (
                        _render_clock(clock, compact=False),
                        _render_clock(clock, compact=True),
                    ),
                )
            )

        user_input = (
            activation_text
            if activation_subject is CapabilityActivationSubjectKind.ROOT_HUMAN_PROMPT
            else ""
        )
        output = self._capability.resolve_projection(
            user_input=user_input,
            available_tool_names=frozenset(
                tool.name for tool in tool_surface.tool_specs
            ),
        )
        diagnostics.extend(_public_capability_diagnostics(output.diagnostics))
        if output.catalog_prompt:
            compact_catalog = _compact_catalog(
                output,
                maximum_characters=len(output.catalog_prompt),
            )
            candidates.append(
                self._candidate(
                    ContextSourceKind.CAPABILITY_CATALOG,
                    (
                        output.catalog_prompt,
                        compact_catalog,
                        _reference_catalog(
                            output,
                            maximum_characters=len(compact_catalog),
                        ),
                    ),
                )
            )
        if output.active_skill_prompt:
            candidates.append(
                self._candidate(
                    ContextSourceKind.ACTIVE_SKILL,
                    (output.active_skill_prompt,),
                )
            )
        return _collected(
            candidates=tuple(candidates),
            diagnostics=tuple(diagnostics),
            registry_fingerprint=self._registry.fingerprint,
        )

    def _capture_temporal(self) -> RuntimeTemporalCapture:
        observed = self._clock()
        if observed.tzinfo is None or observed.utcoffset() is None:
            raise ValueError("runtime clock must be timezone-aware")
        observed_utc = observed.astimezone(timezone.utc)
        local = observed_utc.astimezone(self._timezone)
        offset = local.utcoffset()
        if offset is None:
            raise ValueError("runtime timezone has no UTC offset")
        offset_minutes = int(offset.total_seconds() // 60)
        fingerprint = context_fingerprint(
            "runtime-temporal-capture:v1",
            {
                "observed_at_utc": observed_utc.isoformat(),
                "local_date": local.date().isoformat(),
                "timezone_name": self._timezone_name,
                "utc_offset_minutes": offset_minutes,
            },
        )
        return RuntimeTemporalCapture(
            observed_at_utc=observed_utc,
            local_date=local.date(),
            timezone_name=self._timezone_name,
            utc_offset_minutes=offset_minutes,
            capture_fingerprint=fingerprint,
        )

    def _environment_snapshot(
        self, temporal: RuntimeTemporalCapture | None
    ) -> RuntimeEnvironmentSnapshot:
        cwd = self._terminal_cwd.snapshot_terminal_cwd().expanduser().resolve()
        if cwd != self._workspace_root and self._workspace_root not in cwd.parents:
            raise ValueError("Terminal cwd is outside the active workspace")
        payload = {
            "workspace_kind": self._workspace_kind,
            "workspace_root": str(self._workspace_root),
            "terminal_current_cwd": str(cwd),
            "timezone_name": self._timezone_name,
            "utc_offset_minutes": (
                None if temporal is None else temporal.utc_offset_minutes
            ),
        }
        return RuntimeEnvironmentSnapshot(
            **payload,
            snapshot_fingerprint=context_fingerprint(
                "runtime-environment-snapshot:v1", payload
            ),
        )

    def _candidate(
        self,
        kind: ContextSourceKind,
        texts: tuple[str, ...],
    ) -> ContextSourceCandidate:
        binding = self._registry.binding(kind)
        if len(texts) != len(binding.modes):
            raise ValueError("source renderer mode count differs from binding")
        variants = tuple(
            _variant(mode, text)
            for mode, text in zip(binding.modes, texts, strict=True)
        )
        instance_id = f"context-source:{kind.value.lower()}"
        semantic = context_fingerprint(
            "context-source-candidate:v1",
            {
                "source_kind": kind.value,
                "source_instance_id": instance_id,
                "source_contract_fingerprint": binding.contract_fingerprint,
                "variants": tuple(item.semantic_fingerprint for item in variants),
            },
        )
        return ContextSourceCandidate(
            source_kind=kind,
            source_instance_id=instance_id,
            source_contract_version=binding.contract_version,
            source_contract_fingerprint=binding.contract_fingerprint,
            source_semantic_fingerprint=semantic,
            channel=binding.channel,
            trust_class=binding.trust,
            budget_class=binding.budget,
            placement_ordinal=binding.placement,
            degradation_priority=binding.degradation,
            variants=variants,
        )


def _variant(mode: ContextRenderMode, text: str) -> ContextRenderVariant:
    encoded = text.encode("utf-8")
    return ContextRenderVariant(
        mode=mode,
        text=text,
        utf8_bytes=len(encoded),
        semantic_fingerprint=context_fingerprint(
            "context-render-variant:v1", {"mode": mode.value, "text": text}
        ),
    )


def _collected(
    *,
    candidates: tuple[ContextSourceCandidate, ...],
    diagnostics: tuple[ContextSourceCollectionDiagnostic, ...],
    registry_fingerprint: str,
) -> CollectedContextSources:
    fingerprint = context_fingerprint(
        "collected-context-sources:v1",
        {
            "registry_fingerprint": registry_fingerprint,
            "candidates": tuple(
                item.source_semantic_fingerprint for item in candidates
            ),
            "diagnostics": tuple(
                (
                    item.code.value,
                    item.severity,
                    None if item.source_kind is None else item.source_kind.value,
                )
                for item in diagnostics
            ),
        },
    )
    return CollectedContextSources(
        candidates=candidates,
        diagnostics=diagnostics,
        registry_fingerprint=registry_fingerprint,
        collection_fingerprint=fingerprint,
    )


def _render_environment(snapshot: RuntimeEnvironmentSnapshot, *, compact: bool) -> str:
    values = {
        "workspace_kind": snapshot.workspace_kind,
        "workspace_root": snapshot.workspace_root,
        "terminal_current_cwd": snapshot.terminal_current_cwd,
        "timezone": snapshot.timezone_name,
        "utc_offset_minutes": snapshot.utc_offset_minutes,
    }
    lines = [
        '<runtime_environment contract="pulsara.runtime-environment.v1">',
        *(
            f"{key}={json.dumps(value, ensure_ascii=False)}"
            for key, value in values.items()
        ),
    ]
    if not compact:
        lines.append('relative_workdir_base="terminal_current_cwd"')
    lines.append("</runtime_environment>")
    return "\n".join(lines)


def _render_clock(snapshot: RuntimeClockSnapshot, *, compact: bool) -> str:
    payload = {
        "local_date": snapshot.local_date.isoformat(),
        "timezone": snapshot.timezone_name,
        "utc_offset_minutes": snapshot.utc_offset_minutes,
    }
    if not compact:
        payload["observed_at_utc"] = snapshot.observed_at_utc.isoformat().replace(
            "+00:00", "Z"
        )
    return (
        '<runtime_clock contract="pulsara.runtime-clock.v1" '
        'authority="runtime observation, not human instruction">\n'
        + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n</runtime_clock>"
    )


def _compact_catalog(
    output: CapabilityProjectionOutput,
    *,
    maximum_characters: int,
) -> str:
    """Render the richest compact catalog that cannot exceed FULL by chars.

    The source collector is provider-neutral and therefore cannot use the
    prepared target estimator.  Bounding the candidate against its own FULL
    carrier prevents the common large-catalog inversion; the compiler still
    performs the authoritative estimator monotonicity check and fails closed
    for any future estimator whose cost is not monotonic with this bound.
    """

    ordered = tuple(sorted(output.catalog_entries, key=lambda item: item.name))

    def render(description_characters: int) -> str:
        entries = [
            (
                {"name": item.name}
                if description_characters == 0
                else {
                    "name": item.name,
                    "description": item.description[:description_characters],
                }
            )
            for item in ordered
        ]
        return (
            "<available_skills_compact>\n"
            + json.dumps(
                entries,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n</available_skills_compact>"
        )

    low = 0
    high = 160
    winner: str | None = None
    while low <= high:
        middle = (low + high) // 2
        candidate = render(middle)
        if len(candidate) <= maximum_characters:
            winner = candidate
            low = middle + 1
        else:
            high = middle - 1
    # FULL is already a valid, bounded catalog.  Equal adjacent variants are
    # legal and are deterministically skipped by the allocator as non-progress.
    return output.catalog_prompt if winner is None else winner


def _reference_catalog(
    output: CapabilityProjectionOutput,
    *,
    maximum_characters: int,
) -> str:
    names = tuple(sorted(item.name for item in output.catalog_entries))
    candidate = (
        "Available skills: "
        + ",".join(names)
        + ". Read the selected SKILL.md before use."
    )
    if len(candidate) <= maximum_characters:
        return candidate
    # A pathological name set may already consume the whole FULL/COMPACT
    # carrier.  Preserve the exact catalog rather than inventing a truncated
    # authority; the compiler can move past this non-progressing variant.
    return _compact_catalog(output, maximum_characters=maximum_characters)


def _public_capability_diagnostics(
    diagnostics: tuple[CapabilityDiagnostic, ...],
) -> tuple[ContextSourceCollectionDiagnostic, ...]:
    result: list[ContextSourceCollectionDiagnostic] = []
    for item in diagnostics:
        if item.code == "skill_catalog_budget_truncated":
            code = ContextPublicDiagnosticCode.CATALOG_TRUNCATED
            kind = ContextSourceKind.CAPABILITY_CATALOG
        elif item.code in {
            "active_skill_not_found",
            "skill_not_found",
        }:
            code = ContextPublicDiagnosticCode.ACTIVE_SKILL_NOT_FOUND
            kind = ContextSourceKind.ACTIVE_SKILL
        elif item.code.startswith("active_skill") or item.code.startswith("skill_body"):
            code = ContextPublicDiagnosticCode.ACTIVE_SKILL_UNAVAILABLE
            kind = ContextSourceKind.ACTIVE_SKILL
        else:
            code = ContextPublicDiagnosticCode.CAPABILITY_DISCOVERY_INCOMPLETE
            kind = ContextSourceKind.CAPABILITY_CATALOG
        severity = {
            "info": "INFO",
            "warning": "WARNING",
            "error": "ERROR",
        }[item.severity]
        result.append(ContextSourceCollectionDiagnostic(code, severity, kind))
    return tuple(result)


def _freeze_display_timezone(value: tzinfo) -> tuple[tzinfo, str]:
    """Keep IANA rules, but freeze an unkeyed zone to its opening offset."""

    key = getattr(value, "key", None)
    if isinstance(key, str) and key:
        return value, key
    now = datetime.now(timezone.utc).astimezone(value)
    offset = now.utcoffset()
    if offset is None:
        raise ValueError("session display timezone has no UTC offset")
    seconds = offset.total_seconds()
    if not seconds.is_integer() or int(seconds) % 60:
        raise ValueError("session display timezone offset is not minute-aligned")
    minutes = int(seconds) // 60
    sign = "+" if minutes >= 0 else "-"
    hours, remainder = divmod(abs(minutes), 60)
    name = f"UTC{sign}{hours:02d}:{remainder:02d}"
    return timezone(timedelta(minutes=minutes), name), name


__all__ = [
    "ContextSourceCollectorPort",
    "ContextSourceRegistry",
    "KernelContextSourceCollector",
    "TerminalCurrentCwdSnapshotPort",
]
