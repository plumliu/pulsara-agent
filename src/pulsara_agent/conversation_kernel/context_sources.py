"""First-party process-local source collection for structured model input."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
import json
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Protocol

from pulsara_agent.capability.provider import CapabilityProjectionOutput
from pulsara_agent.capability.types import CapabilityDiagnostic
from pulsara_agent.conversation_kernel.capability import (
    FrozenKernelCapabilityProjectionInput,
    KernelCapabilityComposer,
)
from pulsara_agent.model_input.contracts import (
    CapabilityActivationSubjectKind,
    CollectedContextSources,
    ContextBudgetClass,
    ContextChannel,
    ContextPublicDiagnosticCode,
    ContextRenderMode,
    ContextRenderVariant,
    ContextSourceAbsentFact,
    ContextSourceAbsenceKind,
    ContextSourceCandidate,
    ContextSourceCollectionDiagnostic,
    ContextSourceKind,
    ContextSourceLifecycle,
    ContextTrustClass,
    FrozenModelToolSurface,
    FrozenCanonicalCompileSnapshot,
    RuntimeClockSnapshot,
    RuntimeEnvironmentSnapshot,
    RuntimeTemporalCapture,
)
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.permission import preset_permission_payload

if TYPE_CHECKING:
    from pulsara_agent.conversation_kernel.mcp.contracts import McpCatalogSnapshot


class TerminalCurrentCwdSnapshotPort(Protocol):
    def snapshot_terminal_cwd(self) -> Path: ...


class McpCatalogSnapshotPort(Protocol):
    def catalog_snapshot(self) -> "McpCatalogSnapshot": ...


class ContextSourceCollectorPort(Protocol):
    @property
    def registry_fingerprint(self) -> str: ...

    def collect(
        self,
        *,
        activation_subject: CapabilityActivationSubjectKind,
        activation_text: str,
        tool_surface: FrozenModelToolSurface,
        canonical_facts: FrozenCanonicalCompileSnapshot,
        deadline_monotonic: float | None = None,
    ) -> CollectedContextSources: ...

    def freeze_non_trigger_sources(
        self,
        *,
        tool_surface: FrozenModelToolSurface,
        canonical_facts: FrozenCanonicalCompileSnapshot,
        deadline_monotonic: float | None = None,
    ) -> "FrozenNonTriggerContextSources": ...

    def complete_frozen_sources(
        self,
        frozen: "FrozenNonTriggerContextSources",
        *,
        activation_subject: CapabilityActivationSubjectKind | None,
        activation_text: str,
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
    lifecycle: ContextSourceLifecycle

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
                "lifecycle": self.lifecycle.value,
            },
        )


@dataclass(frozen=True, slots=True)
class FrozenNonTriggerContextSources:
    candidates: tuple[ContextSourceCandidate, ...]
    absent_facts: tuple[ContextSourceAbsentFact, ...]
    diagnostics: tuple[ContextSourceCollectionDiagnostic, ...]
    available_tool_names: frozenset[str]
    registry_fingerprint: str
    freeze_fingerprint: str
    capability_projection_input: FrozenKernelCapabilityProjectionInput | None = field(
        default=None, repr=False
    )


_BINDINGS = (
    _SourceBinding(
        ContextSourceKind.BASE_SYSTEM,
        "pulsara.base-system.prefix-continuity.v2",
        ContextChannel.SYSTEM,
        ContextTrustClass.ROOT_INSTRUCTION,
        ContextBudgetClass.MUST_KEEP,
        0,
        0,
        (ContextRenderMode.FULL,),
        "pulsara.base-system-collector.v1",
        ContextSourceLifecycle.EPOCH_ROOT,
    ),
    _SourceBinding(
        ContextSourceKind.RUNTIME_ENVIRONMENT,
        "pulsara.runtime-environment.v1",
        ContextChannel.RUNTIME_OBSERVATION,
        ContextTrustClass.TRUSTED_RUNTIME_FACT,
        ContextBudgetClass.MUST_KEEP,
        10,
        10,
        (ContextRenderMode.FULL, ContextRenderMode.COMPACT),
        "pulsara.runtime-environment-collector.v1",
        ContextSourceLifecycle.SNAPSHOT_ON_CHANGE,
    ),
    _SourceBinding(
        ContextSourceKind.RUNTIME_CLOCK,
        "pulsara.runtime-clock.v1",
        ContextChannel.RUNTIME_OBSERVATION,
        ContextTrustClass.TRUSTED_RUNTIME_FACT,
        ContextBudgetClass.OPTIONAL,
        90,
        80,
        (ContextRenderMode.FULL, ContextRenderMode.COMPACT),
        "pulsara.runtime-clock-collector.v1",
        ContextSourceLifecycle.CALL_APPEND,
    ),
    _SourceBinding(
        ContextSourceKind.RUN_PERMISSION,
        "pulsara.run-permission.v1",
        ContextChannel.RUNTIME_OBSERVATION,
        ContextTrustClass.AUTHORIZED_RUNTIME_GUIDANCE,
        ContextBudgetClass.MUST_KEEP,
        20,
        12,
        (ContextRenderMode.FULL, ContextRenderMode.COMPACT),
        "pulsara.run-permission-collector.v1",
        ContextSourceLifecycle.TURN_APPEND,
    ),
    _SourceBinding(
        ContextSourceKind.PLAN_HANDOFF,
        "pulsara.plan-handoff.v1",
        ContextChannel.RUNTIME_OBSERVATION,
        ContextTrustClass.AUTHORIZED_RUNTIME_GUIDANCE,
        ContextBudgetClass.MUST_KEEP,
        30,
        11,
        (ContextRenderMode.FULL, ContextRenderMode.COMPACT),
        "pulsara.plan-handoff-collector.v1",
        ContextSourceLifecycle.ONE_SHOT,
    ),
    _SourceBinding(
        ContextSourceKind.PLAN_WORKFLOW,
        "pulsara.plan-workflow.v1",
        ContextChannel.RUNTIME_OBSERVATION,
        ContextTrustClass.AUTHORIZED_RUNTIME_GUIDANCE,
        ContextBudgetClass.MUST_KEEP,
        40,
        10,
        (ContextRenderMode.FULL, ContextRenderMode.COMPACT),
        "pulsara.plan-workflow-collector.v1",
        ContextSourceLifecycle.SNAPSHOT_ON_CHANGE,
    ),
    _SourceBinding(
        ContextSourceKind.CAPABILITY_CATALOG,
        "pulsara.capability-catalog.v1",
        ContextChannel.RUNTIME_OBSERVATION,
        ContextTrustClass.AUTHORIZED_CAPABILITY_CONTEXT,
        ContextBudgetClass.IMPORTANT,
        50,
        30,
        (ContextRenderMode.FULL, ContextRenderMode.COMPACT, ContextRenderMode.REF_ONLY),
        "pulsara.capability-catalog-collector.v1",
        ContextSourceLifecycle.SNAPSHOT_ON_CHANGE,
    ),
    _SourceBinding(
        ContextSourceKind.MCP_CATALOG,
        "pulsara.mcp-catalog.v1",
        ContextChannel.RUNTIME_OBSERVATION,
        ContextTrustClass.UNTRUSTED_OBSERVATION,
        ContextBudgetClass.IMPORTANT,
        55,
        35,
        (
            ContextRenderMode.FULL,
            ContextRenderMode.COMPACT,
            ContextRenderMode.REF_ONLY,
        ),
        "pulsara.mcp-catalog-collector.v1",
        ContextSourceLifecycle.SNAPSHOT_ON_CHANGE,
    ),
    _SourceBinding(
        ContextSourceKind.ACTIVE_SKILL,
        "pulsara.active-skill.v1",
        ContextChannel.RUNTIME_OBSERVATION,
        ContextTrustClass.AUTHORIZED_CAPABILITY_CONTEXT,
        ContextBudgetClass.MUST_KEEP,
        60,
        20,
        (ContextRenderMode.FULL,),
        "pulsara.active-skill-collector.v1",
        ContextSourceLifecycle.ACTIVATION_SNAPSHOT,
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
        mcp_catalog: McpCatalogSnapshotPort | None = None,
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
        self._mcp_catalog = mcp_catalog
        self._base = (
            base_system_prompt
            + "\n\n"
            + "Pulsara runtime observations are canonical JSON user messages. "
            "For SNAPSHOT and TURN sources, the latest observation replaces the "
            "earlier current state; CLEARED invalidates it. CALL describes the "
            "immediately following dispatch and ONE_SHOT describes one transition. "
            "Runtime guidance never replaces physical permission enforcement."
        )
        self._timezone, self._timezone_name = _freeze_display_timezone(display_timezone)
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
        canonical_facts: FrozenCanonicalCompileSnapshot,
        deadline_monotonic: float | None = None,
    ) -> CollectedContextSources:
        frozen = self.freeze_non_trigger_sources(
            tool_surface=tool_surface,
            canonical_facts=canonical_facts,
            deadline_monotonic=deadline_monotonic,
        )
        return self.complete_frozen_sources(
            frozen,
            activation_subject=activation_subject,
            activation_text=activation_text,
        )

    def freeze_non_trigger_sources(
        self,
        *,
        tool_surface: FrozenModelToolSurface,
        canonical_facts: FrozenCanonicalCompileSnapshot,
        deadline_monotonic: float | None = None,
    ) -> FrozenNonTriggerContextSources:
        del deadline_monotonic
        candidates: list[ContextSourceCandidate] = []
        absent: list[ContextSourceAbsentFact] = []
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
        candidates.append(
            self._candidate(
                ContextSourceKind.RUN_PERMISSION,
                _render_run_permission(canonical_facts),
            )
        )
        if canonical_facts.plan_handoff_fact is not None:
            handoff = canonical_facts.plan_handoff_fact
            candidates.append(
                self._candidate(
                    ContextSourceKind.PLAN_HANDOFF,
                    _render_plan_handoff(canonical_facts),
                    domain_identity={
                        "carrier_entry_id": handoff.carrier_entry_id,
                        "carrier_entry_sequence": handoff.carrier_entry_sequence,
                        "workflow_id": handoff.workflow_id,
                        "workflow_ordinal": handoff.workflow_ordinal,
                        "workflow_revision_at_transition": (
                            handoff.workflow_revision_at_transition
                        ),
                        "interaction_id": handoff.interaction_id,
                        "transition_semantic_digest": (
                            handoff.transition_semantic_digest
                        ),
                    },
                )
            )
        else:
            absent.append(
                self._absent(
                    ContextSourceKind.PLAN_HANDOFF,
                    ContextSourceAbsenceKind.NOT_APPLICABLE,
                )
            )
        if canonical_facts.plan_workflow_fact is not None:
            candidates.append(
                self._candidate(
                    ContextSourceKind.PLAN_WORKFLOW,
                    _render_plan_workflow(canonical_facts),
                )
            )
        else:
            absent.append(
                self._absent(
                    ContextSourceKind.PLAN_WORKFLOW,
                    ContextSourceAbsenceKind.EXPLICIT_EMPTY,
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
        else:
            absent.append(
                self._absent(
                    ContextSourceKind.RUNTIME_CLOCK,
                    ContextSourceAbsenceKind.UNAVAILABLE,
                )
            )

        tool_names = frozenset(tool.name for tool in tool_surface.tool_specs)
        capability_projection_input = self._capability.freeze_projection_input(
            available_tool_names=tool_names
        )
        if self._mcp_catalog is None:
            absent.append(
                self._absent(
                    ContextSourceKind.MCP_CATALOG,
                    ContextSourceAbsenceKind.NOT_APPLICABLE,
                )
            )
        else:
            catalog = self._mcp_catalog.catalog_snapshot().for_scope(
                canonical_facts.canonical_input.identity.conversation_scope_kind
            )
            if catalog.servers:
                candidates.append(
                    self._candidate(
                        ContextSourceKind.MCP_CATALOG,
                        _render_mcp_catalog(catalog),
                        domain_identity=catalog.semantic_fingerprint,
                    )
                )
            else:
                absent.append(
                    self._absent(
                        ContextSourceKind.MCP_CATALOG,
                        ContextSourceAbsenceKind.NOT_APPLICABLE,
                    )
                )
        fingerprint = context_fingerprint(
            "pulsara:frozen-non-trigger-context-sources:v1",
            {
                "candidates": tuple(
                    item.source_semantic_fingerprint for item in candidates
                ),
                "absent": tuple(item.domain_semantic_fingerprint for item in absent),
                "diagnostics": tuple(
                    (item.code.value, item.severity) for item in diagnostics
                ),
                "tools": tuple(sorted(tool_names)),
                "capability_projection_input": (
                    capability_projection_input.snapshot_fingerprint
                ),
                "registry": self._registry.fingerprint,
            },
        )
        return FrozenNonTriggerContextSources(
            candidates=tuple(candidates),
            absent_facts=tuple(absent),
            diagnostics=tuple(diagnostics),
            available_tool_names=tool_names,
            registry_fingerprint=self._registry.fingerprint,
            freeze_fingerprint=fingerprint,
            capability_projection_input=capability_projection_input,
        )

    def complete_frozen_sources(
        self,
        frozen: FrozenNonTriggerContextSources,
        *,
        activation_subject: CapabilityActivationSubjectKind | None,
        activation_text: str,
        deadline_monotonic: float | None = None,
    ) -> CollectedContextSources:
        del deadline_monotonic
        if frozen.registry_fingerprint != self._registry.fingerprint:
            raise ValueError("context source registry changed after source freeze")
        candidates = list(frozen.candidates)
        absent = list(frozen.absent_facts)
        diagnostics = list(frozen.diagnostics)
        user_input = (
            activation_text
            if activation_subject is CapabilityActivationSubjectKind.ROOT_HUMAN_PROMPT
            else ""
        )
        if frozen.capability_projection_input is None:
            raise ValueError("capability projection input was not frozen")
        output = self._capability.resolve_projection_from_frozen(
            frozen.capability_projection_input,
            user_input=user_input,
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
        else:
            absent.append(
                self._absent(
                    ContextSourceKind.CAPABILITY_CATALOG,
                    ContextSourceAbsenceKind.EXPLICIT_EMPTY,
                )
            )
        if activation_subject is None:
            # A same-turn tool/result follow-up is not a new activation
            # boundary.  Keep the installed ACTIVE_SKILL head unchanged while
            # still allowing the capability catalog to advance.
            absent.append(
                self._absent(
                    ContextSourceKind.ACTIVE_SKILL,
                    ContextSourceAbsenceKind.NOT_APPLICABLE,
                )
            )
        elif output.active_skill_prompt:
            candidates.append(
                self._candidate(
                    ContextSourceKind.ACTIVE_SKILL,
                    (output.active_skill_prompt,),
                )
            )
        else:
            absent.append(
                self._absent(
                    ContextSourceKind.ACTIVE_SKILL,
                    ContextSourceAbsenceKind.EXPLICIT_EMPTY,
                )
            )
        return _collected(
            candidates=tuple(candidates),
            absent_facts=tuple(absent),
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
        *,
        domain_identity: object | None = None,
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
        domain_semantic_fingerprint = (
            semantic
            if domain_identity is None
            else context_fingerprint(
                "context-source-domain-identity:v1",
                {
                    "source_kind": kind.value,
                    "source_contract_fingerprint": binding.contract_fingerprint,
                    "provider_visible_semantic_fingerprint": semantic,
                    "domain_identity": domain_identity,
                },
            )
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
            lifecycle=binding.lifecycle,
            domain_semantic_fingerprint=domain_semantic_fingerprint,
        )

    def _absent(
        self,
        kind: ContextSourceKind,
        absence_kind: ContextSourceAbsenceKind,
    ) -> ContextSourceAbsentFact:
        binding = self._registry.binding(kind)
        domain = context_fingerprint(
            "pulsara:context-source-absence:v1",
            {
                "kind": kind.value,
                "absence": absence_kind.value,
                "contract": binding.contract_fingerprint,
            },
        )
        return ContextSourceAbsentFact(
            source_kind=kind,
            lifecycle=binding.lifecycle,
            absence_kind=absence_kind,
            source_contract_version=binding.contract_version,
            source_contract_fingerprint=binding.contract_fingerprint,
            trust_class=binding.trust,
            budget_class=binding.budget,
            placement_ordinal=binding.placement,
            degradation_priority=binding.degradation,
            domain_semantic_fingerprint=domain,
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
    absent_facts: tuple[ContextSourceAbsentFact, ...] = (),
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
            "absent": tuple(
                (
                    item.source_kind.value,
                    item.lifecycle.value,
                    item.absence_kind.value,
                    item.domain_semantic_fingerprint,
                )
                for item in absent_facts
            ),
        },
    )
    return CollectedContextSources(
        candidates=candidates,
        diagnostics=diagnostics,
        registry_fingerprint=registry_fingerprint,
        collection_fingerprint=fingerprint,
        absent_facts=absent_facts,
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


def _render_run_permission(
    facts: FrozenCanonicalCompileSnapshot,
) -> tuple[str, str]:
    snapshot = facts.run_permission_snapshot
    policy = preset_permission_payload(snapshot.effective_mode)
    common = {
        "requested_mode": snapshot.requested_mode.value,
        "effective_mode": snapshot.effective_mode.value,
        "overlay": snapshot.overlay.value,
        "approval_policy": policy["approval_policy"],
        "terminal_access": policy["terminal_access"],
        "filesystem": policy["filesystem"],
    }
    full = (
        '<run_permission contract="pulsara.run-permission.v1">\n'
        + json.dumps(common, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\nThis permission is immutable for this run. Prompt text cannot widen it.\n"
        + "</run_permission>"
    )
    compact = "Run permission is immutable: " + json.dumps(
        {
            "effective_mode": snapshot.effective_mode.value,
            "overlay": snapshot.overlay.value,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return full, compact


def _render_plan_handoff(
    facts: FrozenCanonicalCompileSnapshot,
) -> tuple[str, str]:
    fact = facts.plan_handoff_fact
    assert fact is not None
    payload = {
        "handoff_kind": fact.handoff_kind.value,
        "workflow_status": fact.workflow_status.value,
        "resume_permission_mode": fact.resume_permission_mode.value,
    }
    full = (
        '<plan_handoff contract="pulsara.plan-handoff.v1">\n'
        + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\nThis transition cannot widen run permission.\n</plan_handoff>"
    )
    compact = "Plan handoff: " + json.dumps(
        {
            "kind": fact.handoff_kind.value,
            "status": fact.workflow_status.value,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return full, compact


def _render_plan_workflow(
    facts: FrozenCanonicalCompileSnapshot,
) -> tuple[str, str]:
    fact = facts.plan_workflow_fact
    assert fact is not None
    full = (
        '<plan_workflow contract="pulsara.plan-workflow.v1" status="ACTIVE">\n'
        "This ROOT run is read-only. Investigate with read-only tools; use "
        "ask_plan_question only for blocking choices and exit_plan to submit the "
        "complete draft. Do not claim that Plan has ended in ordinary prose.\n"
        + "</plan_workflow>"
    )
    compact = (
        "Plan active: read-only; ask_plan_question for blockers; exit_plan for draft."
    )
    return full, compact


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


def _render_mcp_catalog(catalog: "McpCatalogSnapshot") -> tuple[str, str, str]:
    servers = [
        {
            "server_id": item.server_id,
            "display_name": item.display_name,
            "status": item.status.value,
            "required": item.required,
            "tools": item.bounded_tool_name_overview,
            "resource_count": item.resource_count,
            "resource_template_count": item.resource_template_count,
            "prompt_count": item.prompt_count,
            "instructions": item.sanitized_instructions,
            "failure_category": item.stable_failure_category,
        }
        for item in catalog.servers
    ]
    full = _bounded_mcp_catalog_json(
        base={
            "source": "MCP_CATALOG",
            "trust": "UNTRUSTED_OBSERVATION",
            "permission_note": (
                "Availability does not grant physical permission; local run policy "
                "and the exact execution generation remain authoritative."
            ),
        },
        servers=servers,
        maximum_bytes=32 * 1024,
    )
    compact = _bounded_mcp_catalog_json(
        base={
            "source": "MCP_CATALOG",
            "trust": "UNTRUSTED_OBSERVATION",
        },
        servers=[
            {
                "server_id": item["server_id"],
                "status": item["status"],
                "tools": item["tools"],
                "resource_count": item["resource_count"],
                "prompt_count": item["prompt_count"],
            }
            for item in servers
        ],
        maximum_bytes=8 * 1024,
    )
    reference = _bounded_mcp_catalog_json(
        base={
            "source": "MCP_CATALOG",
            "trust": "UNTRUSTED_OBSERVATION",
            "catalog_fingerprint": catalog.semantic_fingerprint,
            "read_more": "Call list_mcp_servers for the bounded current catalog.",
        },
        servers=[
            {"server_id": item["server_id"], "status": item["status"]}
            for item in servers
        ],
        maximum_bytes=2 * 1024,
    )
    return full, compact, reference


def _bounded_catalog_json(value: object, maximum_bytes: int) -> str:
    text = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    if len(text.encode("utf-8")) > maximum_bytes:
        raise ValueError("MCP catalog context source exceeds its bound")
    return text


def _bounded_mcp_catalog_json(
    *,
    base: dict[str, object],
    servers: list[dict[str, object]],
    maximum_bytes: int,
) -> str:
    included: list[dict[str, object]] = []
    for server in servers:
        candidate = {
            **base,
            "servers": [*included, server],
            "omitted_server_count": len(servers) - len(included) - 1,
        }
        encoded = json.dumps(
            candidate, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > maximum_bytes:
            break
        included.append(server)
    final = {**base, "servers": included}
    omitted = len(servers) - len(included)
    if omitted:
        final["omitted_server_count"] = omitted
    return _bounded_catalog_json(final, maximum_bytes)


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
    "McpCatalogSnapshotPort",
    "TerminalCurrentCwdSnapshotPort",
]
