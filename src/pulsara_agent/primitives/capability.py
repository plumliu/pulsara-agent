"""Event-safe capability execution-surface and projection contracts."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pulsara_agent.primitives.model_call import sha256_fingerprint
from pulsara_agent.primitives.run_entry import CapabilityExposureOwnerFact


MAX_CAPABILITY_AUTHORIZATION_ENTRIES = 512


class CapabilityDescriptorBindingIdentityFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    capability_name: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    descriptor_id: str = Field(min_length=1)
    descriptor_fingerprint: str = Field(min_length=1)
    descriptor_artifact_id: str = Field(min_length=1)
    binding_fingerprint: str | None
    binding_contract_id: str | None
    binding_contract_version: str | None

    @model_validator(mode="after")
    def _validate_binding_fields(self) -> "CapabilityDescriptorBindingIdentityFact":
        binding_fields = (
            self.binding_fingerprint,
            self.binding_contract_id,
            self.binding_contract_version,
        )
        if any(value is None for value in binding_fields) and any(
            value is not None for value in binding_fields
        ):
            raise ValueError("capability binding identity fields must be all-or-none")
        return self


def capability_descriptor_set_fingerprint(
    entries: tuple[CapabilityDescriptorBindingIdentityFact, ...],
) -> str:
    return sha256_fingerprint(
        "capability-descriptor-set:v1",
        [
            [
                entry.capability_name,
                entry.provider_id,
                entry.descriptor_id,
                entry.descriptor_fingerprint,
            ]
            for entry in entries
        ],
    )


def capability_binding_set_fingerprint(
    entries: tuple[CapabilityDescriptorBindingIdentityFact, ...],
) -> str:
    return sha256_fingerprint(
        "capability-binding-set:v1",
        [
            [
                entry.capability_name,
                entry.binding_fingerprint,
                entry.binding_contract_id,
                entry.binding_contract_version,
            ]
            for entry in entries
        ],
    )


def build_capability_execution_surface_identity(
    *,
    surface_contract_version: str,
    entries: tuple[CapabilityDescriptorBindingIdentityFact, ...],
    mcp_installation_id: str,
) -> "CapabilityExecutionSurfaceIdentityFact":
    descriptor_fp = capability_descriptor_set_fingerprint(entries)
    binding_fp = capability_binding_set_fingerprint(entries)
    return CapabilityExecutionSurfaceIdentityFact(
        surface_contract_version=surface_contract_version,
        entries=entries,
        descriptor_set_fingerprint=descriptor_fp,
        execution_binding_set_fingerprint=binding_fp,
        execution_surface_fingerprint=sha256_fingerprint(
            "capability-execution-surface:v1",
            [surface_contract_version, descriptor_fp, binding_fp],
        ),
        mcp_installation_id=mcp_installation_id,
    )


class CapabilityExecutionSurfaceIdentityFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    surface_contract_version: str = Field(min_length=1)
    entries: tuple[CapabilityDescriptorBindingIdentityFact, ...]
    descriptor_set_fingerprint: str
    execution_binding_set_fingerprint: str
    execution_surface_fingerprint: str
    mcp_installation_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_surface(self) -> "CapabilityExecutionSurfaceIdentityFact":
        names = [entry.capability_name for entry in self.entries]
        descriptor_ids = [entry.descriptor_id for entry in self.entries]
        if names != sorted(names) or len(names) != len(set(names)):
            raise ValueError("capability surface entries must be name-sorted and unique")
        if len(descriptor_ids) != len(set(descriptor_ids)):
            raise ValueError("capability descriptor ids must be unique")
        descriptor_fp = capability_descriptor_set_fingerprint(self.entries)
        binding_fp = capability_binding_set_fingerprint(self.entries)
        surface_fp = sha256_fingerprint(
            "capability-execution-surface:v1",
            [
                self.surface_contract_version,
                descriptor_fp,
                binding_fp,
            ],
        )
        if self.descriptor_set_fingerprint != descriptor_fp:
            raise ValueError("capability descriptor set fingerprint mismatch")
        if self.execution_binding_set_fingerprint != binding_fp:
            raise ValueError("capability binding set fingerprint mismatch")
        if self.execution_surface_fingerprint != surface_fp:
            raise ValueError("capability execution surface fingerprint mismatch")
        return self


def capability_projection_entry_id(
    *,
    projection_kind: str,
    provider_id: str,
    source_kind: str,
    stable_name: str,
) -> str:
    return sha256_fingerprint(
        "capability-projection-entry:v1",
        [projection_kind, provider_id, source_kind, stable_name],
    )


class CapabilityProjectionEntryFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    projection_entry_id: str
    projection_kind: Literal[
        "catalog_entry", "active_skill_injection", "provider_prompt_fragment"
    ]
    stable_name: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    source_kind: Literal["builtin", "mcp", "workspace", "user", "bundled", "custom"]
    content_fingerprint: str = Field(min_length=1)
    content_artifact_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_id(self) -> "CapabilityProjectionEntryFact":
        expected = capability_projection_entry_id(
            projection_kind=self.projection_kind,
            provider_id=self.provider_id,
            source_kind=self.source_kind,
            stable_name=self.stable_name,
        )
        if self.projection_entry_id != expected:
            raise ValueError("capability projection entry id mismatch")
        return self


class CapabilityRenderedProjectionFragmentFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    fragment_id: str = Field(min_length=1)
    container_id: str = Field(min_length=1)
    fragment_role: Literal["prefix", "entry", "suffix", "static"]
    static_scope: Literal["container_wrapper", "projection_wrapper"] | None
    source_entry_id: str | None
    source_content_fingerprint: str | None
    fragment_fingerprint: str = Field(min_length=1)
    fragment_artifact_id: str = Field(min_length=1)
    order_index: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_role(self) -> "CapabilityRenderedProjectionFragmentFact":
        if self.fragment_role == "entry":
            if self.source_entry_id is None or self.source_content_fingerprint is None:
                raise ValueError("entry fragment requires a source entry")
            if self.static_scope is not None:
                raise ValueError("entry fragment cannot set static_scope")
        else:
            if self.source_entry_id is not None or self.source_content_fingerprint is not None:
                raise ValueError("non-entry fragment cannot reference a source entry")
            if self.fragment_role == "static":
                if self.static_scope is None:
                    raise ValueError("static fragment requires static_scope")
            elif self.static_scope is not None:
                raise ValueError("only static fragment can set static_scope")
        return self


class CapabilityProjectionFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    visible_source_entries: tuple[CapabilityProjectionEntryFact, ...]
    rendered_fragments: tuple[CapabilityRenderedProjectionFragmentFact, ...]
    source_entry_count: int = Field(ge=0)
    rendered_entry_count: int = Field(ge=0)
    omitted_entry_count: int = Field(ge=0)
    projection_semantic_fingerprint: str
    rendered_prompt_fingerprint: str | None
    rendered_prompt_artifact_id: str | None
    rendered_prompt_chars: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_projection(self) -> "CapabilityProjectionFact":
        entry_ids = [entry.projection_entry_id for entry in self.visible_source_entries]
        if entry_ids != sorted(entry_ids) or len(entry_ids) != len(set(entry_ids)):
            raise ValueError("projection entries must be id-sorted and unique")
        fragment_ids = [fragment.fragment_id for fragment in self.rendered_fragments]
        if len(fragment_ids) != len(set(fragment_ids)):
            raise ValueError("projection fragment ids must be unique")
        if [fragment.order_index for fragment in self.rendered_fragments] != list(
            range(len(self.rendered_fragments))
        ):
            raise ValueError("projection fragment order must be contiguous")
        entries_by_id = {
            entry.projection_entry_id: entry for entry in self.visible_source_entries
        }
        referenced: set[str] = set()
        entries_by_container: dict[str, int] = {}
        for fragment in self.rendered_fragments:
            if fragment.fragment_role == "entry":
                entry = entries_by_id.get(str(fragment.source_entry_id))
                if entry is None:
                    raise ValueError("projection fragment references unknown entry")
                if fragment.source_content_fingerprint != entry.content_fingerprint:
                    raise ValueError("projection fragment content fingerprint mismatch")
                referenced.add(entry.projection_entry_id)
                entries_by_container[fragment.container_id] = (
                    entries_by_container.get(fragment.container_id, 0) + 1
                )
        if referenced != set(entry_ids):
            raise ValueError("visible projection entries must be exactly rendered entries")
        for fragment in self.rendered_fragments:
            if fragment.fragment_role in {"prefix", "suffix"} or (
                fragment.fragment_role == "static"
                and fragment.static_scope == "container_wrapper"
            ):
                if entries_by_container.get(fragment.container_id, 0) == 0:
                    raise ValueError("orphan projection container wrapper")
            if (
                fragment.fragment_role == "static"
                and fragment.static_scope == "projection_wrapper"
                and not referenced
            ):
                raise ValueError("orphan projection wrapper")
        if self.rendered_entry_count != len(entry_ids):
            raise ValueError("rendered entry count mismatch")
        if self.source_entry_count != (
            self.rendered_entry_count + self.omitted_entry_count
        ):
            raise ValueError("projection source count mismatch")
        aggregate_fields = (
            self.rendered_prompt_fingerprint,
            self.rendered_prompt_artifact_id,
        )
        if self.rendered_fragments:
            if any(value is None for value in aggregate_fields):
                raise ValueError("non-empty projection requires rendered prompt artifact")
        elif (
            self.visible_source_entries
            or any(value is not None for value in aggregate_fields)
            or self.rendered_prompt_chars != 0
        ):
            raise ValueError("empty projection cannot carry rendered prompt facts")
        expected_semantic = sha256_fingerprint(
            "capability-projection-semantic:v1",
            {
                "visible_source_entries": [
                    _projection_entry_semantic_payload(entry)
                    for entry in self.visible_source_entries
                ],
                "rendered_fragments": [
                    _projection_fragment_semantic_payload(fragment)
                    for fragment in self.rendered_fragments
                ],
                "source_entry_count": self.source_entry_count,
                "rendered_entry_count": self.rendered_entry_count,
                "omitted_entry_count": self.omitted_entry_count,
                "rendered_prompt_fingerprint": self.rendered_prompt_fingerprint,
                "rendered_prompt_chars": self.rendered_prompt_chars,
            },
        )
        if self.projection_semantic_fingerprint != expected_semantic:
            raise ValueError("capability projection semantic fingerprint mismatch")
        return self


def capability_projection_semantic_fingerprint(
    *,
    visible_source_entries: tuple[CapabilityProjectionEntryFact, ...],
    rendered_fragments: tuple[CapabilityRenderedProjectionFragmentFact, ...],
    source_entry_count: int,
    rendered_entry_count: int,
    omitted_entry_count: int,
    rendered_prompt_fingerprint: str | None,
    rendered_prompt_chars: int,
) -> str:
    return sha256_fingerprint(
        "capability-projection-semantic:v1",
        {
            "visible_source_entries": [
                _projection_entry_semantic_payload(entry)
                for entry in visible_source_entries
            ],
            "rendered_fragments": [
                _projection_fragment_semantic_payload(fragment)
                for fragment in rendered_fragments
            ],
            "source_entry_count": source_entry_count,
            "rendered_entry_count": rendered_entry_count,
            "omitted_entry_count": omitted_entry_count,
            "rendered_prompt_fingerprint": rendered_prompt_fingerprint,
            "rendered_prompt_chars": rendered_prompt_chars,
        },
    )


def _projection_entry_semantic_payload(
    entry: CapabilityProjectionEntryFact,
) -> dict[str, object]:
    """Return only model-visible/source identity fields.

    Artifact IDs are exposure-scoped persistence locations.  Including them in
    the semantic hash makes identical provider output look different on every
    exposure and defeats exact continuation reuse.
    """

    return entry.model_dump(mode="json", exclude={"content_artifact_id"})


def _projection_fragment_semantic_payload(
    fragment: CapabilityRenderedProjectionFragmentFact,
) -> dict[str, object]:
    return fragment.model_dump(mode="json", exclude={"fragment_artifact_id"})


def empty_capability_projection() -> CapabilityProjectionFact:
    return CapabilityProjectionFact(
        visible_source_entries=(),
        rendered_fragments=(),
        source_entry_count=0,
        rendered_entry_count=0,
        omitted_entry_count=0,
        projection_semantic_fingerprint=capability_projection_semantic_fingerprint(
            visible_source_entries=(),
            rendered_fragments=(),
            source_entry_count=0,
            rendered_entry_count=0,
            omitted_entry_count=0,
            rendered_prompt_fingerprint=None,
            rendered_prompt_chars=0,
        ),
        rendered_prompt_fingerprint=None,
        rendered_prompt_artifact_id=None,
        rendered_prompt_chars=0,
    )


class CapabilityExposureSemanticFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    execution_surface: CapabilityExecutionSurfaceIdentityFact
    catalog_projection: CapabilityProjectionFact
    active_skill_projection: CapabilityProjectionFact
    authorization_fingerprint: str
    exposure_semantic_fingerprint: str

    @model_validator(mode="after")
    def _validate_fingerprint(self) -> "CapabilityExposureSemanticFact":
        entry_ids = [
            entry.projection_entry_id
            for projection in (self.catalog_projection, self.active_skill_projection)
            for entry in projection.visible_source_entries
        ]
        fragment_ids = [
            fragment.fragment_id
            for projection in (self.catalog_projection, self.active_skill_projection)
            for fragment in projection.rendered_fragments
        ]
        if len(entry_ids) != len(set(entry_ids)):
            raise ValueError("capability projection entry ids must be exposure-unique")
        if len(fragment_ids) != len(set(fragment_ids)):
            raise ValueError("capability fragment ids must be exposure-unique")
        expected = sha256_fingerprint(
            "capability-exposure-semantic:v1",
            [
                self.execution_surface.execution_surface_fingerprint,
                self.catalog_projection.projection_semantic_fingerprint,
                self.catalog_projection.rendered_prompt_fingerprint,
                self.active_skill_projection.projection_semantic_fingerprint,
                self.active_skill_projection.rendered_prompt_fingerprint,
                self.authorization_fingerprint,
            ],
        )
        if self.exposure_semantic_fingerprint != expected:
            raise ValueError("capability exposure semantic fingerprint mismatch")
        return self


def build_capability_exposure_semantic(
    *,
    execution_surface: CapabilityExecutionSurfaceIdentityFact,
    catalog_projection: CapabilityProjectionFact,
    active_skill_projection: CapabilityProjectionFact,
    authorization_fingerprint: str,
) -> CapabilityExposureSemanticFact:
    semantic_fingerprint = sha256_fingerprint(
        "capability-exposure-semantic:v1",
        [
            execution_surface.execution_surface_fingerprint,
            catalog_projection.projection_semantic_fingerprint,
            catalog_projection.rendered_prompt_fingerprint,
            active_skill_projection.projection_semantic_fingerprint,
            active_skill_projection.rendered_prompt_fingerprint,
            authorization_fingerprint,
        ],
    )
    return CapabilityExposureSemanticFact(
        execution_surface=execution_surface,
        catalog_projection=catalog_projection,
        active_skill_projection=active_skill_projection,
        authorization_fingerprint=authorization_fingerprint,
        exposure_semantic_fingerprint=semantic_fingerprint,
    )


class CapabilityResolveBasisFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    basis_id: str = Field(min_length=1)
    basis_kind: Literal["initial", "continuation"]
    source_basis_id: str | None
    source_basis_fingerprint: str | None
    owner: CapabilityExposureOwnerFact
    workspace_identity_fingerprint: str = Field(min_length=1)
    memory_domain_id: str = Field(min_length=1)
    permission_snapshot_id: str = Field(min_length=1)
    plan_active: bool
    active_skill_names: tuple[str, ...]
    user_intent_fingerprint: str = Field(min_length=1)
    prior_transcript_fingerprint: str = Field(min_length=1)
    mcp_installation_id: str = Field(min_length=1)
    execution_surface_identity: CapabilityExecutionSurfaceIdentityFact
    basis_fingerprint: str

    @model_validator(mode="after")
    def _validate_basis(self) -> "CapabilityResolveBasisFact":
        if self.active_skill_names != tuple(sorted(set(self.active_skill_names))):
            raise ValueError("active skill names must be sorted and unique")
        if self.basis_kind == "initial":
            if self.source_basis_id is not None or self.source_basis_fingerprint is not None:
                raise ValueError("initial capability basis cannot reference a source")
        elif self.source_basis_id is None or self.source_basis_fingerprint is None:
            raise ValueError("continuation capability basis requires source attribution")
        if self.mcp_installation_id != self.execution_surface_identity.mcp_installation_id:
            raise ValueError("capability basis MCP installation mismatch")
        payload = self.model_dump(mode="json", exclude={"basis_fingerprint"})
        if self.basis_fingerprint != sha256_fingerprint(
            "capability-resolve-basis:v1", payload
        ):
            raise ValueError("capability basis fingerprint mismatch")
        return self


def build_capability_resolve_basis(
    *,
    basis_id: str,
    basis_kind: Literal["initial", "continuation"],
    source_basis_id: str | None,
    source_basis_fingerprint: str | None,
    owner: CapabilityExposureOwnerFact,
    workspace_identity_fingerprint: str,
    memory_domain_id: str,
    permission_snapshot_id: str,
    plan_active: bool,
    active_skill_names: tuple[str, ...],
    user_intent_fingerprint: str,
    prior_transcript_fingerprint: str,
    mcp_installation_id: str,
    execution_surface_identity: CapabilityExecutionSurfaceIdentityFact,
) -> CapabilityResolveBasisFact:
    payload = {
        "basis_id": basis_id,
        "basis_kind": basis_kind,
        "source_basis_id": source_basis_id,
        "source_basis_fingerprint": source_basis_fingerprint,
        "owner": owner.model_dump(mode="json"),
        "workspace_identity_fingerprint": workspace_identity_fingerprint,
        "memory_domain_id": memory_domain_id,
        "permission_snapshot_id": permission_snapshot_id,
        "plan_active": plan_active,
        "active_skill_names": list(active_skill_names),
        "user_intent_fingerprint": user_intent_fingerprint,
        "prior_transcript_fingerprint": prior_transcript_fingerprint,
        "mcp_installation_id": mcp_installation_id,
        "execution_surface_identity": execution_surface_identity.model_dump(
            mode="json"
        ),
    }
    return CapabilityResolveBasisFact(
        **payload,
        basis_fingerprint=sha256_fingerprint(
            "capability-resolve-basis:v1", payload
        ),
    )


class CapabilityAuthorizationEntryFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    capability_name: str = Field(min_length=1)
    descriptor_fingerprint: str = Field(min_length=1)
    binding_fingerprint: str | None
    disposition: Literal["direct", "deferred", "hidden"]
    callable: bool

    @model_validator(mode="after")
    def _validate_callable(self) -> "CapabilityAuthorizationEntryFact":
        if self.callable != (self.disposition == "direct"):
            raise ValueError("only direct capability authorization may be callable")
        return self


class CapabilityExposureDiagnosticFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(min_length=1, max_length=128)
    severity: Literal["info", "warning", "error"]
    stage: Literal["resolve", "projection", "rebind", "narrow"]
    message: str = Field(max_length=1024)


def capability_authorization_fingerprint(
    entries: tuple[CapabilityAuthorizationEntryFact, ...],
) -> str:
    return sha256_fingerprint(
        "capability-authorization:v1",
        [entry.model_dump(mode="json") for entry in entries],
    )


class CapabilityExposureSnapshotFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    exposure_id: str = Field(min_length=1)
    owner: CapabilityExposureOwnerFact
    resolution_kind: Literal[
        "initial", "continuation_reused", "continuation_narrowed"
    ]
    resolve_basis: CapabilityResolveBasisFact
    semantic: CapabilityExposureSemanticFact
    authorization_entries: tuple[CapabilityAuthorizationEntryFact, ...]
    source_exposure_id: str | None
    direct_names: tuple[str, ...]
    deferred_names: tuple[str, ...]
    hidden_names: tuple[str, ...]
    callable_names: tuple[str, ...]
    exposure_semantic_fingerprint: str
    exposure_fact_fingerprint: str
    diagnostics: tuple[CapabilityExposureDiagnosticFact, ...]

    @model_validator(mode="after")
    def _validate_exposure(self) -> "CapabilityExposureSnapshotFact":
        if len(self.authorization_entries) > MAX_CAPABILITY_AUTHORIZATION_ENTRIES:
            raise ValueError("capability authorization entries exceed safety cap")
        names = [entry.capability_name for entry in self.authorization_entries]
        if names != sorted(names) or len(names) != len(set(names)):
            raise ValueError("capability authorization entries must be name-sorted and unique")
        expected_by_disposition = {
            disposition: tuple(
                entry.capability_name
                for entry in self.authorization_entries
                if entry.disposition == disposition
            )
            for disposition in ("direct", "deferred", "hidden")
        }
        for field_name in ("direct_names", "deferred_names", "hidden_names", "callable_names"):
            values = getattr(self, field_name)
            if values != tuple(sorted(set(values))):
                raise ValueError(f"{field_name} must be sorted and unique")
        if self.direct_names != expected_by_disposition["direct"]:
            raise ValueError("direct capability names mismatch authorization entries")
        if self.deferred_names != expected_by_disposition["deferred"]:
            raise ValueError("deferred capability names mismatch authorization entries")
        if self.hidden_names != expected_by_disposition["hidden"]:
            raise ValueError("hidden capability names mismatch authorization entries")
        expected_callable = tuple(
            entry.capability_name for entry in self.authorization_entries if entry.callable
        )
        if self.callable_names != expected_callable:
            raise ValueError("callable capability names mismatch authorization entries")
        authorization_fp = capability_authorization_fingerprint(
            self.authorization_entries
        )
        if self.semantic.authorization_fingerprint != authorization_fp:
            raise ValueError("capability semantic authorization fingerprint mismatch")
        if self.exposure_semantic_fingerprint != self.semantic.exposure_semantic_fingerprint:
            raise ValueError("capability exposure semantic fingerprint mismatch")
        continuation = self.resolution_kind != "initial"
        if continuation != (self.source_exposure_id is not None):
            raise ValueError("capability continuation source attribution mismatch")
        if self.owner != self.resolve_basis.owner:
            raise ValueError("capability exposure owner must equal basis owner")
        payload = self.model_dump(mode="json", exclude={"exposure_fact_fingerprint"})
        if self.exposure_fact_fingerprint != sha256_fingerprint(
            "capability-exposure-fact:v1", payload
        ):
            raise ValueError("capability exposure fact fingerprint mismatch")
        return self


def build_capability_exposure_snapshot(
    *,
    exposure_id: str,
    owner: CapabilityExposureOwnerFact,
    resolution_kind: Literal[
        "initial", "continuation_reused", "continuation_narrowed"
    ],
    resolve_basis: CapabilityResolveBasisFact,
    semantic: CapabilityExposureSemanticFact,
    authorization_entries: tuple[CapabilityAuthorizationEntryFact, ...],
    source_exposure_id: str | None,
    diagnostics: tuple[CapabilityExposureDiagnosticFact, ...] = (),
) -> CapabilityExposureSnapshotFact:
    direct_names = tuple(
        entry.capability_name
        for entry in authorization_entries
        if entry.disposition == "direct"
    )
    deferred_names = tuple(
        entry.capability_name
        for entry in authorization_entries
        if entry.disposition == "deferred"
    )
    hidden_names = tuple(
        entry.capability_name
        for entry in authorization_entries
        if entry.disposition == "hidden"
    )
    callable_names = tuple(
        entry.capability_name for entry in authorization_entries if entry.callable
    )
    payload = {
        "exposure_id": exposure_id,
        "owner": owner.model_dump(mode="json"),
        "resolution_kind": resolution_kind,
        "resolve_basis": resolve_basis.model_dump(mode="json"),
        "semantic": semantic.model_dump(mode="json"),
        "authorization_entries": [
            entry.model_dump(mode="json") for entry in authorization_entries
        ],
        "source_exposure_id": source_exposure_id,
        "direct_names": list(direct_names),
        "deferred_names": list(deferred_names),
        "hidden_names": list(hidden_names),
        "callable_names": list(callable_names),
        "exposure_semantic_fingerprint": semantic.exposure_semantic_fingerprint,
        "diagnostics": [diagnostic.model_dump(mode="json") for diagnostic in diagnostics],
    }
    return CapabilityExposureSnapshotFact(
        **payload,
        exposure_fact_fingerprint=sha256_fingerprint(
            "capability-exposure-fact:v1", payload
        ),
    )


__all__ = [
    "MAX_CAPABILITY_AUTHORIZATION_ENTRIES",
    "CapabilityAuthorizationEntryFact",
    "CapabilityDescriptorBindingIdentityFact",
    "CapabilityExecutionSurfaceIdentityFact",
    "CapabilityExposureDiagnosticFact",
    "CapabilityExposureSemanticFact",
    "CapabilityExposureSnapshotFact",
    "CapabilityProjectionEntryFact",
    "CapabilityProjectionFact",
    "CapabilityRenderedProjectionFragmentFact",
    "CapabilityResolveBasisFact",
    "capability_authorization_fingerprint",
    "capability_binding_set_fingerprint",
    "capability_descriptor_set_fingerprint",
    "capability_projection_semantic_fingerprint",
    "capability_projection_entry_id",
    "build_capability_execution_surface_identity",
    "build_capability_exposure_semantic",
    "build_capability_exposure_snapshot",
    "build_capability_resolve_basis",
    "empty_capability_projection",
]
