"""Typed Host composition boundary.

The Host owns lifecycle coordination.  Resource construction is delegated to a
single composition implementation so product code cannot select an in-memory
runtime through a boolean flag.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from pulsara_agent.capability.runtime import CapabilityRuntime
from pulsara_agent.workspace_identity import ResolvedWorkspace
from pulsara_agent.host.session_manifest import (
    ResumableSessionSummary,
    SessionManifest,
)
from pulsara_agent.llm import ModelRole
from pulsara_agent.llm.request import LLMOptions
from pulsara_agent.memory.reflection.engine import MemoryReflectionOptions
from pulsara_agent.memory.scope import MemoryDomainContext
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.runtime.mcp.supervisor import McpServerSupervisor
from pulsara_agent.runtime.mcp.types import McpInstalledCapabilitySnapshot
from pulsara_agent.runtime.permission import EffectivePermissionPolicy
from pulsara_agent.runtime.terminal import TerminalRuntimeBinding
from pulsara_agent.runtime.wiring import AgentRuntimeWiring
from pulsara_agent.settings import PulsaraSettings


HostRuntimeLiveBindingKind = Literal[
    "settings",
    "memory_domain",
    "llm_options",
    "memory_reflection_options",
    "capability_runtime",
    "terminal_runtime",
    "permission_policy",
    "mcp_supervisor",
    "mcp_installation",
]

_BINDING_KIND_ORDER: tuple[HostRuntimeLiveBindingKind, ...] = (
    "settings",
    "memory_domain",
    "llm_options",
    "memory_reflection_options",
    "capability_runtime",
    "terminal_runtime",
    "permission_policy",
    "mcp_supervisor",
    "mcp_installation",
)


@dataclass(frozen=True, slots=True)
class HostRuntimeBuildFact:
    resolved_settings_semantic_fingerprint: str
    workspace_root: str
    workspace_identity_fingerprint: str
    runtime_session_id: str | None
    graph_id: str | None
    memory_domain_id: str | None
    memory_domain_semantic_fingerprint: str | None
    model_role: ModelRole
    llm_options_semantic_fingerprint: str | None
    system_prompt: str | None
    system_prompt_fingerprint: str | None
    memory_reflection: bool
    memory_reflection_options_fingerprint: str | None
    enable_workspace_skills: bool
    capability_runtime_semantic_fingerprint: str | None
    terminal_binding_semantic_fingerprint: str
    permission_policy_semantic_fingerprint: str
    mcp_supervisor_contract_fingerprint: str
    mcp_installation_semantic_fingerprint: str
    fact_fingerprint: str

    def __post_init__(self) -> None:
        if not self.workspace_root:
            raise ValueError("Host runtime build workspace root is required")
        expected = _host_runtime_build_fact_fingerprint(self)
        if self.fact_fingerprint != expected:
            raise ValueError("Host runtime build fact fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class HostRuntimeLiveBindingIdentity:
    binding_kind: HostRuntimeLiveBindingKind
    process_owner_id: str
    binding_generation: int
    semantic_contract_fingerprint: str
    identity_fingerprint: str

    def __post_init__(self) -> None:
        if self.binding_kind not in _BINDING_KIND_ORDER:
            raise ValueError("unknown Host runtime live binding kind")
        if not self.process_owner_id or self.binding_generation < 1:
            raise ValueError("Host live binding owner and generation are required")
        expected = context_fingerprint(
            "host-runtime-live-binding-identity:v1",
            {
                "binding_kind": self.binding_kind,
                "process_owner_id": self.process_owner_id,
                "binding_generation": self.binding_generation,
                "semantic_contract_fingerprint": self.semantic_contract_fingerprint,
            },
        )
        if self.identity_fingerprint != expected:
            raise ValueError("Host live binding identity fingerprint mismatch")


class _ProcessLocalCarrier:
    """Make accidental pickle use fail closed for live composition carriers."""

    __slots__ = ()

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise TypeError(f"{type(self).__name__} is immutable and process-local")

    def __copy__(self):
        raise TypeError(f"{type(self).__name__} is process-local")

    def __deepcopy__(self, memo: object):
        del memo
        raise TypeError(f"{type(self).__name__} is process-local")

    def __reduce__(self):
        raise TypeError(f"{type(self).__name__} is process-local")

    def __reduce_ex__(self, protocol: int):  # pragma: no cover - pickle protocol hook
        del protocol
        raise TypeError(f"{type(self).__name__} is process-local")


class HostRuntimeLiveBindings(_ProcessLocalCarrier):
    """Read-only live objects joined to an immutable build fact.

    This deliberately is not a dataclass.  Besides pickle rejection, that makes
    generic ``dataclasses.asdict()`` serializers fail before traversing secret-
    bearing settings or process-owned resources.
    """

    __slots__ = (
        "_settings",
        "_settings_identity",
        "_memory_domain",
        "_memory_domain_identity",
        "_llm_options",
        "_llm_options_identity",
        "_memory_reflection_options",
        "_memory_reflection_options_identity",
        "_capability_runtime_override",
        "_capability_runtime_identity",
        "_terminal_binding",
        "_terminal_binding_identity",
        "_permission_policy",
        "_permission_policy_identity",
        "_mcp_supervisor",
        "_mcp_supervisor_identity",
        "_mcp_installation",
        "_mcp_installation_identity",
        "_ordered_binding_identity_fingerprints",
    )

    def __init__(
        self,
        *,
        settings: PulsaraSettings,
        settings_identity: HostRuntimeLiveBindingIdentity,
        memory_domain: MemoryDomainContext | None,
        memory_domain_identity: HostRuntimeLiveBindingIdentity | None,
        llm_options: LLMOptions | None,
        llm_options_identity: HostRuntimeLiveBindingIdentity | None,
        memory_reflection_options: MemoryReflectionOptions | None,
        memory_reflection_options_identity: HostRuntimeLiveBindingIdentity | None,
        capability_runtime_override: CapabilityRuntime | None,
        capability_runtime_identity: HostRuntimeLiveBindingIdentity | None,
        terminal_binding: TerminalRuntimeBinding,
        terminal_binding_identity: HostRuntimeLiveBindingIdentity,
        permission_policy: EffectivePermissionPolicy,
        permission_policy_identity: HostRuntimeLiveBindingIdentity,
        mcp_supervisor: McpServerSupervisor,
        mcp_supervisor_identity: HostRuntimeLiveBindingIdentity,
        mcp_installation: McpInstalledCapabilitySnapshot,
        mcp_installation_identity: HostRuntimeLiveBindingIdentity,
        ordered_binding_identity_fingerprints: tuple[str, ...],
    ) -> None:
        values = {
            "settings": settings,
            "settings_identity": settings_identity,
            "memory_domain": memory_domain,
            "memory_domain_identity": memory_domain_identity,
            "llm_options": llm_options,
            "llm_options_identity": llm_options_identity,
            "memory_reflection_options": memory_reflection_options,
            "memory_reflection_options_identity": memory_reflection_options_identity,
            "capability_runtime_override": capability_runtime_override,
            "capability_runtime_identity": capability_runtime_identity,
            "terminal_binding": terminal_binding,
            "terminal_binding_identity": terminal_binding_identity,
            "permission_policy": permission_policy,
            "permission_policy_identity": permission_policy_identity,
            "mcp_supervisor": mcp_supervisor,
            "mcp_supervisor_identity": mcp_supervisor_identity,
            "mcp_installation": mcp_installation,
            "mcp_installation_identity": mcp_installation_identity,
            "ordered_binding_identity_fingerprints": (
                ordered_binding_identity_fingerprints
            ),
        }
        for name, value in values.items():
            object.__setattr__(self, f"_{name}", value)

        pairs = (
            ("settings", self.settings, self.settings_identity),
            ("memory_domain", self.memory_domain, self.memory_domain_identity),
            ("llm_options", self.llm_options, self.llm_options_identity),
            (
                "memory_reflection_options",
                self.memory_reflection_options,
                self.memory_reflection_options_identity,
            ),
            (
                "capability_runtime",
                self.capability_runtime_override,
                self.capability_runtime_identity,
            ),
            ("terminal_runtime", self.terminal_binding, self.terminal_binding_identity),
            (
                "permission_policy",
                self.permission_policy,
                self.permission_policy_identity,
            ),
            ("mcp_supervisor", self.mcp_supervisor, self.mcp_supervisor_identity),
            ("mcp_installation", self.mcp_installation, self.mcp_installation_identity),
        )
        identities: list[HostRuntimeLiveBindingIdentity] = []
        for expected_kind, value, identity in pairs:
            if (value is None) != (identity is None):
                raise ValueError(f"{expected_kind} object/identity presence mismatch")
            if identity is not None:
                if identity.binding_kind != expected_kind:
                    raise ValueError(f"{expected_kind} live binding kind mismatch")
                identities.append(identity)
        expected_order = tuple(
            identity.identity_fingerprint
            for kind in _BINDING_KIND_ORDER
            for identity in identities
            if identity.binding_kind == kind
        )
        if self.ordered_binding_identity_fingerprints != expected_order:
            raise ValueError("Host live binding identity order mismatch")

    @property
    def settings(self) -> PulsaraSettings:
        return self._settings

    @property
    def settings_identity(self) -> HostRuntimeLiveBindingIdentity:
        return self._settings_identity

    @property
    def memory_domain(self) -> MemoryDomainContext | None:
        return self._memory_domain

    @property
    def memory_domain_identity(self) -> HostRuntimeLiveBindingIdentity | None:
        return self._memory_domain_identity

    @property
    def llm_options(self) -> LLMOptions | None:
        return self._llm_options

    @property
    def llm_options_identity(self) -> HostRuntimeLiveBindingIdentity | None:
        return self._llm_options_identity

    @property
    def memory_reflection_options(self) -> MemoryReflectionOptions | None:
        return self._memory_reflection_options

    @property
    def memory_reflection_options_identity(
        self,
    ) -> HostRuntimeLiveBindingIdentity | None:
        return self._memory_reflection_options_identity

    @property
    def capability_runtime_override(self) -> CapabilityRuntime | None:
        return self._capability_runtime_override

    @property
    def capability_runtime_identity(self) -> HostRuntimeLiveBindingIdentity | None:
        return self._capability_runtime_identity

    @property
    def terminal_binding(self) -> TerminalRuntimeBinding:
        return self._terminal_binding

    @property
    def terminal_binding_identity(self) -> HostRuntimeLiveBindingIdentity:
        return self._terminal_binding_identity

    @property
    def permission_policy(self) -> EffectivePermissionPolicy:
        return self._permission_policy

    @property
    def permission_policy_identity(self) -> HostRuntimeLiveBindingIdentity:
        return self._permission_policy_identity

    @property
    def mcp_supervisor(self) -> McpServerSupervisor:
        return self._mcp_supervisor

    @property
    def mcp_supervisor_identity(self) -> HostRuntimeLiveBindingIdentity:
        return self._mcp_supervisor_identity

    @property
    def mcp_installation(self) -> McpInstalledCapabilitySnapshot:
        return self._mcp_installation

    @property
    def mcp_installation_identity(self) -> HostRuntimeLiveBindingIdentity:
        return self._mcp_installation_identity

    @property
    def ordered_binding_identity_fingerprints(self) -> tuple[str, ...]:
        return self._ordered_binding_identity_fingerprints


class HostRuntimeBuildAdmission(_ProcessLocalCarrier):
    __slots__ = (
        "_build_fact",
        "_live_bindings",
        "_reopen_deadline_monotonic",
        "_admission_generation",
        "_admission_fingerprint",
    )

    def __init__(
        self,
        *,
        build_fact: HostRuntimeBuildFact,
        live_bindings: HostRuntimeLiveBindings,
        reopen_deadline_monotonic: float | None,
        admission_generation: int,
        admission_fingerprint: str,
    ) -> None:
        object.__setattr__(self, "_build_fact", build_fact)
        object.__setattr__(self, "_live_bindings", live_bindings)
        object.__setattr__(
            self, "_reopen_deadline_monotonic", reopen_deadline_monotonic
        )
        object.__setattr__(self, "_admission_generation", admission_generation)
        object.__setattr__(self, "_admission_fingerprint", admission_fingerprint)
        if self.admission_generation < 1:
            raise ValueError("Host build admission generation must be positive")
        _validate_fact_live_binding_join(self.build_fact, self.live_bindings)
        expected = context_fingerprint(
            "host-runtime-build-admission:v1",
            {
                "build_fact_fingerprint": self.build_fact.fact_fingerprint,
                "ordered_binding_identity_fingerprints": (
                    self.live_bindings.ordered_binding_identity_fingerprints
                ),
                "reopen_deadline_monotonic": self.reopen_deadline_monotonic,
                "admission_generation": self.admission_generation,
            },
        )
        if self.admission_fingerprint != expected:
            raise ValueError("Host build admission fingerprint mismatch")

    @property
    def build_fact(self) -> HostRuntimeBuildFact:
        return self._build_fact

    @property
    def live_bindings(self) -> HostRuntimeLiveBindings:
        return self._live_bindings

    @property
    def reopen_deadline_monotonic(self) -> float | None:
        return self._reopen_deadline_monotonic

    @property
    def admission_generation(self) -> int:
        return self._admission_generation

    @property
    def admission_fingerprint(self) -> str:
        return self._admission_fingerprint


@runtime_checkable
class RuntimeProjectionServicePort(Protocol):
    @property
    def accepting(self) -> bool: ...

    async def start(self) -> None: ...

    def wake(self, runtime_session_id: str | None = None) -> None: ...

    async def aclose(self, *, deadline_monotonic: float) -> None: ...


@runtime_checkable
class HostProcessResourceLease(Protocol):
    @property
    def lease_id(self) -> str: ...

    @property
    def postgres_access_lease(self) -> object: ...

    @property
    def retrieval_resources(self) -> object: ...

    @property
    def projection_service(self) -> RuntimeProjectionServicePort: ...

    @property
    def governance_coordinator(self) -> object: ...

    @property
    def schema_binding_fingerprint(self) -> str: ...

    @property
    def lease_generation(self) -> int: ...

    @property
    def resource_fingerprint(self) -> str: ...

    @property
    def released(self) -> bool: ...

    @property
    def durability_evidence(self) -> bool: ...

    async def release(self, *, deadline_monotonic: float) -> None: ...


@dataclass(frozen=True, slots=True)
class HostAgentRuntimeWiringOutcome:
    agent_runtime_wiring: AgentRuntimeWiring
    runtime_session_id: str
    process_resource_lease_id: str
    schema_binding_fingerprint: str
    outcome_fingerprint: str

    def __post_init__(self) -> None:
        actual_session_id = (
            self.agent_runtime_wiring.runtime_wiring.runtime_session.runtime_session_id
        )
        if self.runtime_session_id != actual_session_id:
            raise ValueError("Host wiring outcome runtime session mismatch")
        expected = context_fingerprint(
            "host-agent-runtime-wiring-outcome:v1",
            {
                "runtime_session_id": self.runtime_session_id,
                "process_resource_lease_id": self.process_resource_lease_id,
                "schema_binding_fingerprint": self.schema_binding_fingerprint,
            },
        )
        if self.outcome_fingerprint != expected:
            raise ValueError("Host wiring outcome fingerprint mismatch")


@runtime_checkable
class HostSessionManifestStorePort(Protocol):
    def get(self, runtime_session_id: str) -> SessionManifest | None: ...

    def list_resumable(
        self,
        *,
        workspace_root: str | Path | None,
        memory_domain_id: str | None,
        include_closed: bool,
        limit: int,
    ) -> tuple[ResumableSessionSummary, ...] | list[ResumableSessionSummary]: ...

    def upsert_open_manifest(
        self,
        *,
        runtime_session_id: str,
        conversation_id: str,
        workspace: ResolvedWorkspace,
        model_role: ModelRole,
        permission_policy: EffectivePermissionPolicy,
        created_by: str,
    ) -> SessionManifest: ...

    def touch(self, runtime_session_id: str) -> None: ...

    def mark_closed(self, runtime_session_id: str) -> None: ...


@runtime_checkable
class HostRuntimeComposition(Protocol):
    async def acquire_process_resources(
        self, *, deadline_monotonic: float
    ) -> HostProcessResourceLease: ...

    def build_agent_runtime_wiring(
        self,
        *,
        admission: HostRuntimeBuildAdmission,
        resources: HostProcessResourceLease,
    ) -> HostAgentRuntimeWiringOutcome: ...

    def session_manifest_store(
        self, *, resources: HostProcessResourceLease
    ) -> HostSessionManifestStorePort: ...


def build_host_runtime_admission(
    *,
    settings: PulsaraSettings,
    workspace: ResolvedWorkspace,
    runtime_session_id: str | None,
    graph_id: str | None,
    model_role: ModelRole,
    options: LLMOptions | None,
    system_prompt: str | None,
    memory_reflection: bool,
    memory_reflection_options: MemoryReflectionOptions | None,
    enable_workspace_skills: bool,
    capability_runtime_override: CapabilityRuntime | None,
    terminal_binding: TerminalRuntimeBinding,
    permission_policy: EffectivePermissionPolicy,
    mcp_supervisor: McpServerSupervisor,
    mcp_installation: McpInstalledCapabilitySnapshot,
    reopen_deadline_monotonic: float | None,
    process_owner_id: str,
    admission_generation: int,
) -> HostRuntimeBuildAdmission:
    if (
        mcp_installation.config_epoch != mcp_supervisor.config_epoch
        or mcp_installation.event_safe_config_set_fingerprint
        != mcp_supervisor.event_safe_config_set_fingerprint
    ):
        raise ValueError("Host MCP installation/supervisor identity mismatch")
    settings_fp = context_fingerprint(
        "host-settings-semantic:v1", settings.redacted_dict()
    )
    workspace_fp = context_fingerprint(
        "host-workspace-semantic:v1",
        {
            "workspace_kind": workspace.workspace_kind,
            "workspace_root": str(workspace.workspace_root),
            "workspace_key": workspace.workspace_key,
            "memory_domain_id": workspace.memory_domain.memory_domain_id,
        },
    )
    memory_domain_fp = context_fingerprint(
        "host-memory-domain-semantic:v1",
        {
            "memory_domain_id": workspace.memory_domain.memory_domain_id,
            "graph_id": workspace.memory_domain.graph_id,
            "workspace_kind": workspace.memory_domain.workspace_kind,
        },
    )
    options_fp = (
        context_fingerprint(
            "host-llm-options-semantic:v1",
            {"reasoning_effort": options.reasoning_effort},
        )
        if options is not None
        else None
    )
    reflection_options_fp = (
        context_fingerprint(
            "host-memory-reflection-options:v1",
            {
                "model_role": memory_reflection_options.model_role.value,
                "reasoning_effort": (
                    memory_reflection_options.llm_options.reasoning_effort
                ),
                "max_summary_chars": memory_reflection_options.max_summary_chars,
                "tool_call_threshold": memory_reflection_options.tool_call_threshold,
                "turn_threshold": memory_reflection_options.turn_threshold,
                "token_delta_threshold": (
                    memory_reflection_options.token_delta_threshold
                ),
                "min_runs_between_reflections": (
                    memory_reflection_options.min_runs_between_reflections
                ),
            },
        )
        if memory_reflection_options is not None
        else None
    )
    capability_fp = (
        context_fingerprint(
            "host-capability-runtime-semantic:v1",
            tuple(
                str(getattr(provider, "provider_id", type(provider).__name__))
                for provider in capability_runtime_override.providers
            ),
        )
        if capability_runtime_override is not None
        else None
    )
    terminal_fp = context_fingerprint(
        "host-terminal-binding-contract:v1", type(terminal_binding).__name__
    )
    permission_fp = context_fingerprint(
        "host-permission-policy-semantic:v1", permission_policy.to_dict()
    )
    mcp_supervisor_fp = context_fingerprint(
        "host-mcp-supervisor-contract:v1", type(mcp_supervisor).__name__
    )
    mcp_installation_fp = context_fingerprint(
        "host-mcp-installation-semantic:v1",
        {
            "installation_id": mcp_installation.installation_id,
            "config_epoch": mcp_installation.config_epoch,
            "event_safe_config_set_fingerprint": (
                mcp_installation.event_safe_config_set_fingerprint
            ),
        },
    )
    fact_fields = dict(
        resolved_settings_semantic_fingerprint=settings_fp,
        workspace_root=str(workspace.workspace_root),
        workspace_identity_fingerprint=workspace_fp,
        runtime_session_id=runtime_session_id,
        graph_id=graph_id,
        memory_domain_id=workspace.memory_domain.memory_domain_id,
        memory_domain_semantic_fingerprint=memory_domain_fp,
        model_role=model_role,
        llm_options_semantic_fingerprint=options_fp,
        system_prompt=system_prompt,
        system_prompt_fingerprint=(
            context_fingerprint("host-system-prompt:v1", system_prompt)
            if system_prompt is not None
            else None
        ),
        memory_reflection=memory_reflection,
        memory_reflection_options_fingerprint=reflection_options_fp,
        enable_workspace_skills=enable_workspace_skills,
        capability_runtime_semantic_fingerprint=capability_fp,
        terminal_binding_semantic_fingerprint=terminal_fp,
        permission_policy_semantic_fingerprint=permission_fp,
        mcp_supervisor_contract_fingerprint=mcp_supervisor_fp,
        mcp_installation_semantic_fingerprint=mcp_installation_fp,
    )
    fact_fingerprint = context_fingerprint(
        "host-runtime-build-fact:v1",
        {
            name: (value.value if isinstance(value, ModelRole) else value)
            for name, value in fact_fields.items()
        },
    )
    fact = HostRuntimeBuildFact(
        **fact_fields,
        fact_fingerprint=fact_fingerprint,
    )

    live_values: tuple[
        tuple[HostRuntimeLiveBindingKind, object | None, str | None], ...
    ] = (
        ("settings", settings, settings_fp),
        ("memory_domain", workspace.memory_domain, memory_domain_fp),
        ("llm_options", options, options_fp),
        ("memory_reflection_options", memory_reflection_options, reflection_options_fp),
        ("capability_runtime", capability_runtime_override, capability_fp),
        ("terminal_runtime", terminal_binding, terminal_fp),
        ("permission_policy", permission_policy, permission_fp),
        ("mcp_supervisor", mcp_supervisor, mcp_supervisor_fp),
        ("mcp_installation", mcp_installation, mcp_installation_fp),
    )
    identities = {
        kind: _build_live_identity(
            kind=kind,
            process_owner_id=process_owner_id,
            generation=admission_generation,
            semantic_contract_fingerprint=semantic_fp,
        )
        for kind, value, semantic_fp in live_values
        if value is not None and semantic_fp is not None
    }
    live = HostRuntimeLiveBindings(
        settings=settings,
        settings_identity=identities["settings"],
        memory_domain=workspace.memory_domain,
        memory_domain_identity=identities["memory_domain"],
        llm_options=options,
        llm_options_identity=identities.get("llm_options"),
        memory_reflection_options=memory_reflection_options,
        memory_reflection_options_identity=identities.get("memory_reflection_options"),
        capability_runtime_override=capability_runtime_override,
        capability_runtime_identity=identities.get("capability_runtime"),
        terminal_binding=terminal_binding,
        terminal_binding_identity=identities["terminal_runtime"],
        permission_policy=permission_policy,
        permission_policy_identity=identities["permission_policy"],
        mcp_supervisor=mcp_supervisor,
        mcp_supervisor_identity=identities["mcp_supervisor"],
        mcp_installation=mcp_installation,
        mcp_installation_identity=identities["mcp_installation"],
        ordered_binding_identity_fingerprints=tuple(
            identities[kind].identity_fingerprint
            for kind in _BINDING_KIND_ORDER
            if kind in identities
        ),
    )
    admission_fp = context_fingerprint(
        "host-runtime-build-admission:v1",
        {
            "build_fact_fingerprint": fact.fact_fingerprint,
            "ordered_binding_identity_fingerprints": (
                live.ordered_binding_identity_fingerprints
            ),
            "reopen_deadline_monotonic": reopen_deadline_monotonic,
            "admission_generation": admission_generation,
        },
    )
    return HostRuntimeBuildAdmission(
        build_fact=fact,
        live_bindings=live,
        reopen_deadline_monotonic=reopen_deadline_monotonic,
        admission_generation=admission_generation,
        admission_fingerprint=admission_fp,
    )


def build_host_wiring_outcome(
    *,
    wiring: AgentRuntimeWiring,
    resource_lease: HostProcessResourceLease,
) -> HostAgentRuntimeWiringOutcome:
    runtime_session_id = wiring.runtime_wiring.runtime_session.runtime_session_id
    fingerprint = context_fingerprint(
        "host-agent-runtime-wiring-outcome:v1",
        {
            "runtime_session_id": runtime_session_id,
            "process_resource_lease_id": resource_lease.lease_id,
            "schema_binding_fingerprint": resource_lease.schema_binding_fingerprint,
        },
    )
    return HostAgentRuntimeWiringOutcome(
        agent_runtime_wiring=wiring,
        runtime_session_id=runtime_session_id,
        process_resource_lease_id=resource_lease.lease_id,
        schema_binding_fingerprint=resource_lease.schema_binding_fingerprint,
        outcome_fingerprint=fingerprint,
    )


def _build_live_identity(
    *,
    kind: HostRuntimeLiveBindingKind,
    process_owner_id: str,
    generation: int,
    semantic_contract_fingerprint: str,
) -> HostRuntimeLiveBindingIdentity:
    fingerprint = context_fingerprint(
        "host-runtime-live-binding-identity:v1",
        {
            "binding_kind": kind,
            "process_owner_id": process_owner_id,
            "binding_generation": generation,
            "semantic_contract_fingerprint": semantic_contract_fingerprint,
        },
    )
    return HostRuntimeLiveBindingIdentity(
        binding_kind=kind,
        process_owner_id=process_owner_id,
        binding_generation=generation,
        semantic_contract_fingerprint=semantic_contract_fingerprint,
        identity_fingerprint=fingerprint,
    )


def _host_runtime_build_fact_fingerprint(value: HostRuntimeBuildFact) -> str:
    return context_fingerprint(
        "host-runtime-build-fact:v1",
        {
            name: (
                getattr(value, name).value
                if isinstance(getattr(value, name), ModelRole)
                else getattr(value, name)
            )
            for name in value.__dataclass_fields__
            if name != "fact_fingerprint"
        },
    )


def _validate_fact_live_binding_join(
    fact: HostRuntimeBuildFact, live: HostRuntimeLiveBindings
) -> None:
    joins = (
        (fact.resolved_settings_semantic_fingerprint, live.settings_identity),
        (fact.memory_domain_semantic_fingerprint, live.memory_domain_identity),
        (fact.llm_options_semantic_fingerprint, live.llm_options_identity),
        (
            fact.memory_reflection_options_fingerprint,
            live.memory_reflection_options_identity,
        ),
        (
            fact.capability_runtime_semantic_fingerprint,
            live.capability_runtime_identity,
        ),
        (
            fact.terminal_binding_semantic_fingerprint,
            live.terminal_binding_identity,
        ),
        (
            fact.permission_policy_semantic_fingerprint,
            live.permission_policy_identity,
        ),
        (
            fact.mcp_supervisor_contract_fingerprint,
            live.mcp_supervisor_identity,
        ),
        (
            fact.mcp_installation_semantic_fingerprint,
            live.mcp_installation_identity,
        ),
    )
    for expected, identity in joins:
        actual = (
            identity.semantic_contract_fingerprint if identity is not None else None
        )
        if expected != actual:
            raise ValueError("Host build fact/live binding semantic mismatch")


__all__ = [
    "HostAgentRuntimeWiringOutcome",
    "HostProcessResourceLease",
    "HostRuntimeBuildAdmission",
    "HostRuntimeBuildFact",
    "HostRuntimeComposition",
    "HostRuntimeLiveBindingIdentity",
    "HostRuntimeLiveBindings",
    "HostSessionManifestStorePort",
    "RuntimeProjectionServicePort",
    "build_host_runtime_admission",
    "build_host_wiring_outcome",
]
