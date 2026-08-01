"""Run genesis and immutable authority-revision contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, TypeAlias

from pydantic import Field, PositiveInt, model_validator

from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.primitives.capability import (
    CapabilityExecutionSurfaceIdentityFact,
    CapabilityExposureSnapshotFact,
    CapabilityResolveBasisFact,
)
from pulsara_agent.primitives.context import (
    ContextEventReferenceFact,
    CurrentUserMessageFact,
    RunPermissionSnapshotFact,
)
from pulsara_agent.primitives.frozen import FrozenRuntimeStateBase
from pulsara_agent.primitives.host_ingress import (
    HostIngressAdmissionProofFact,
    HostRunIngressFact,
)
from pulsara_agent.primitives.long_horizon import (
    ChildRolloutSubaccountFact,
    RunLongHorizonContractFact,
    SubagentGraphReducerContractFact,
)
from pulsara_agent.primitives.model_call import ResolvedModelTargetFact
from pulsara_agent.primitives.run_boundary import (
    InteractionResumeBoundaryFact,
    NewRunBoundaryFact,
)
from pulsara_agent.primitives.run_entry import SubagentRunEntryFact
from pulsara_agent.primitives.transcript_projection import (
    RunTranscriptSeedReferenceFact,
    RunTranscriptSeedSemanticFact,
)
from pulsara_agent.ports.run_execution import RunOwnerIdentity


def _fingerprint(model: FrozenRuntimeStateBase, field: str, domain: str) -> None:
    expected = context_fingerprint(
        domain, model.model_dump(mode="json", exclude={field})
    )
    if getattr(model, field) != expected:
        raise ValueError(f"{field} mismatch")


class HostRunGenesisEntry(FrozenRuntimeStateBase):
    entry_kind: Literal["host"] = "host"
    new_run_boundary: NewRunBoundaryFact
    host_run_ingress: HostRunIngressFact
    host_ingress_admission_proof: HostIngressAdmissionProofFact

    @model_validator(mode="after")
    def _join(self) -> "HostRunGenesisEntry":
        if self.host_ingress_admission_proof.ingress_fact_fingerprint != (
            self.host_run_ingress.fact_fingerprint
        ):
            raise ValueError("Host genesis ingress proof mismatch")
        return self


class SubagentRunGenesisEntry(FrozenRuntimeStateBase):
    entry_kind: Literal["subagent"] = "subagent"
    subagent_run_entry: SubagentRunEntryFact
    child_rollout_subaccount: ChildRolloutSubaccountFact


RunGenesisEntry: TypeAlias = HostRunGenesisEntry | SubagentRunGenesisEntry


class RunGenesisAuthority(FrozenRuntimeStateBase):
    schema_version: Literal[1] = 1
    owner_identity: RunOwnerIdentity
    run_start_event_reference: ContextEventReferenceFact
    run_start_payload_fingerprint: str = Field(min_length=1)
    entry: RunGenesisEntry = Field(discriminator="entry_kind")
    current_user_message: CurrentUserMessageFact
    run_model_target: ResolvedModelTargetFact
    permission_snapshot: RunPermissionSnapshotFact
    subagent_graph_reducer_contract: SubagentGraphReducerContractFact
    long_horizon: RunLongHorizonContractFact
    mcp_installation_id: str = Field(min_length=1)
    mcp_installation_owner_runtime_session_id: str = Field(min_length=1)
    transcript_seed_semantic: RunTranscriptSeedSemanticFact
    transcript_seed_reference: RunTranscriptSeedReferenceFact
    terminal_run_end_event_id: str = Field(min_length=1)
    genesis_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_join(self) -> "RunGenesisAuthority":
        owner = self.owner_identity
        reference = self.run_start_event_reference
        if (
            reference.event_id != owner.run_start_event_id
            or reference.sequence != owner.run_start_sequence
            or reference.runtime_session_id != owner.runtime_session_id
        ):
            raise ValueError("genesis RunStart reference mismatch")
        if self.permission_snapshot.run_id != owner.run_id:
            raise ValueError("genesis permission run mismatch")
        if self.transcript_seed_reference.seed_semantic_fingerprint != (
            self.transcript_seed_semantic.seed_semantic_fingerprint
        ):
            raise ValueError("genesis transcript seed mismatch")
        if isinstance(self.entry, HostRunGenesisEntry):
            basis = self.entry.new_run_boundary.capability_basis
        else:
            basis = self.entry.subagent_run_entry.capability_basis
        if (
            basis.permission_snapshot_id != self.permission_snapshot.snapshot_id
            or basis.mcp_installation_id != self.mcp_installation_id
            or basis.owner.run_id != owner.run_id
        ):
            raise ValueError("genesis capability basis mismatch")
        if isinstance(self.entry, SubagentRunGenesisEntry) and (
            basis.owner.owner_id != owner.run_start_event_id
            or basis.owner.runtime_session_id != owner.runtime_session_id
        ):
            raise ValueError("child genesis capability basis envelope mismatch")
        _fingerprint(self, "genesis_fingerprint", "run-genesis-authority:v1")
        return self


class InitialRunAuthorityRevision(FrozenRuntimeStateBase):
    schema_version: Literal[1] = 1
    owner_identity: RunOwnerIdentity
    revision_kind: Literal["initial"] = "initial"
    revision: Literal[1] = 1
    source_exposure_event_reference: ContextEventReferenceFact
    source_exposure: CapabilityExposureSnapshotFact
    effective_model_target: ResolvedModelTargetFact
    effective_permission: RunPermissionSnapshotFact
    execution_surface_identity: CapabilityExecutionSurfaceIdentityFact
    authority_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_revision(self) -> "InitialRunAuthorityRevision":
        if self.source_exposure.resolution_kind != "initial":
            raise ValueError("initial revision requires initial exposure")
        if self.source_exposure.owner.run_id != self.owner_identity.run_id:
            raise ValueError("initial revision run mismatch")
        if self.source_exposure.semantic.execution_surface != (
            self.execution_surface_identity
        ):
            raise ValueError("initial revision execution surface mismatch")
        _fingerprint(self, "authority_fingerprint", "run-authority:initial:v1")
        return self


class ContinuationRunAuthorityRevision(FrozenRuntimeStateBase):
    schema_version: Literal[1] = 1
    owner_identity: RunOwnerIdentity
    revision_kind: Literal["continuation"] = "continuation"
    revision: int = Field(ge=2)
    predecessor_revision: PositiveInt
    predecessor_fingerprint: str = Field(min_length=1)
    source_resume_boundary_event_reference: ContextEventReferenceFact
    source_resume_boundary: InteractionResumeBoundaryFact
    source_exposure_event_reference: ContextEventReferenceFact
    source_exposure: CapabilityExposureSnapshotFact
    effective_model_target: ResolvedModelTargetFact
    effective_permission: RunPermissionSnapshotFact
    execution_surface_identity: CapabilityExecutionSurfaceIdentityFact
    authority_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_revision(self) -> "ContinuationRunAuthorityRevision":
        if self.predecessor_revision != self.revision - 1:
            raise ValueError("continuation predecessor revision mismatch")
        if self.source_exposure.resolution_kind not in {
            "continuation_reused",
            "continuation_narrowed",
        }:
            raise ValueError("continuation revision requires continuation exposure")
        if (
            self.source_resume_boundary.identity.run_id != self.owner_identity.run_id
            or self.source_exposure.owner.run_id != self.owner_identity.run_id
        ):
            raise ValueError("continuation revision run mismatch")
        if self.source_exposure.semantic.execution_surface != (
            self.execution_surface_identity
        ):
            raise ValueError("continuation execution surface mismatch")
        _fingerprint(self, "authority_fingerprint", "run-authority:continuation:v1")
        return self


RunAuthorityRevision: TypeAlias = (
    InitialRunAuthorityRevision | ContinuationRunAuthorityRevision
)


class AwaitingInitialRevision(FrozenRuntimeStateBase):
    head_kind: Literal["awaiting_initial_revision"] = "awaiting_initial_revision"
    owner_identity: RunOwnerIdentity
    source_run_start_event_reference: ContextEventReferenceFact
    capability_basis: CapabilityResolveBasisFact
    head_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_head(self) -> "AwaitingInitialRevision":
        if self.source_run_start_event_reference.event_id != (
            self.owner_identity.run_start_event_id
        ):
            raise ValueError("awaiting initial revision RunStart mismatch")
        _fingerprint(self, "head_fingerprint", "run-authority-head:awaiting:v1")
        return self


class InstalledRunAuthorityRevision(FrozenRuntimeStateBase):
    head_kind: Literal["installed_revision"] = "installed_revision"
    revision: RunAuthorityRevision = Field(discriminator="revision_kind")
    head_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _fingerprint(self) -> "InstalledRunAuthorityRevision":
        _fingerprint(self, "head_fingerprint", "run-authority-head:installed:v1")
        return self


RunAuthorityHead: TypeAlias = AwaitingInitialRevision | InstalledRunAuthorityRevision


@dataclass(frozen=True, slots=True)
class InitialAuthorityCommitRequest:
    owner_identity: RunOwnerIdentity
    capability_basis: CapabilityResolveBasisFact
    expected_termination_revision: int


@dataclass(frozen=True, slots=True)
class ContinuationAuthorityCommitRequest:
    owner_identity: RunOwnerIdentity
    predecessor_fingerprint: str
    resume_boundary: InteractionResumeBoundaryFact
    expected_termination_revision: int


class PreparedInitialAuthorityCommitHandle(Protocol):
    @property
    def candidate_id(self) -> str: ...

    @property
    def candidate_fingerprint(self) -> str: ...


class PreparedContinuationAuthorityCommitHandle(Protocol):
    @property
    def candidate_id(self) -> str: ...

    @property
    def candidate_fingerprint(self) -> str: ...


PreparedRunAuthorityCommitHandle: TypeAlias = (
    PreparedInitialAuthorityCommitHandle | PreparedContinuationAuthorityCommitHandle
)


class RunAuthorityCommitFull(FrozenRuntimeStateBase):
    disposition: Literal["full"] = "full"
    revision: RunAuthorityRevision = Field(discriminator="revision_kind")


class RunAuthorityCommitNone(FrozenRuntimeStateBase):
    disposition: Literal["none"] = "none"
    stable_candidate_id: str
    stable_candidate_fingerprint: str


class RunAuthorityCommitUntrusted(FrozenRuntimeStateBase):
    disposition: Literal["unknown", "conflict"]
    stable_candidate_id: str
    stable_candidate_fingerprint: str


RunAuthorityCommitOutcome: TypeAlias = (
    RunAuthorityCommitFull | RunAuthorityCommitNone | RunAuthorityCommitUntrusted
)


class RunAuthorityRevisionCommitPort(Protocol):
    def prepare_initial(
        self, request: InitialAuthorityCommitRequest
    ) -> PreparedInitialAuthorityCommitHandle: ...

    def prepare_continuation(
        self, request: ContinuationAuthorityCommitRequest
    ) -> PreparedContinuationAuthorityCommitHandle: ...

    async def commit(
        self,
        handle: PreparedRunAuthorityCommitHandle,
        *,
        attempt_generation: int,
        deadline_monotonic: float,
    ) -> RunAuthorityCommitOutcome: ...


class RunAuthorityReadPort(Protocol):
    async def hydrate_genesis(
        self,
        identity: RunOwnerIdentity,
        *,
        deadline_monotonic: float,
    ) -> RunGenesisAuthority: ...

    async def hydrate_latest_revision(
        self,
        identity: RunOwnerIdentity,
        *,
        through_sequence: int,
        deadline_monotonic: float,
    ) -> RunAuthorityRevision: ...

    async def fold_recovery_state(
        self,
        identity: RunOwnerIdentity,
        *,
        deadline_monotonic: float,
    ) -> object: ...

    async def hydrate_final_output_sources(
        self,
        identity: RunOwnerIdentity,
        *,
        run_end_event_reference: ContextEventReferenceFact,
        deadline_monotonic: float,
    ) -> object: ...


__all__ = [
    "AwaitingInitialRevision",
    "ContinuationAuthorityCommitRequest",
    "ContinuationRunAuthorityRevision",
    "HostRunGenesisEntry",
    "InitialAuthorityCommitRequest",
    "InitialRunAuthorityRevision",
    "InstalledRunAuthorityRevision",
    "PreparedContinuationAuthorityCommitHandle",
    "PreparedInitialAuthorityCommitHandle",
    "PreparedRunAuthorityCommitHandle",
    "RunAuthorityCommitOutcome",
    "RunAuthorityHead",
    "RunAuthorityReadPort",
    "RunAuthorityRevision",
    "RunAuthorityRevisionCommitPort",
    "RunGenesisAuthority",
    "RunGenesisEntry",
    "SubagentRunGenesisEntry",
]
