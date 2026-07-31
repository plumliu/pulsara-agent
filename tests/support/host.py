"""Component-test Host composition and factory."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from uuid import uuid4

from pulsara_agent.host.composition_contract import (
    HostAgentRuntimeWiringOutcome,
    HostProcessResourceLease,
    HostRuntimeBuildAdmission,
    HostRuntimeComposition,
    HostSessionManifestStorePort,
    build_host_wiring_outcome,
)
from pulsara_agent.host.core import HostCore
from pulsara_agent.host.identity import ResolvedWorkspace
from pulsara_agent.host.session_manifest import (
    ResumableSessionSummary,
    SessionManifest,
    utc_now_iso,
)
from pulsara_agent.llm import ModelRole
from pulsara_agent.runtime.permission import EffectivePermissionPolicy, mode_for_policy
from pulsara_agent.settings import PulsaraSettings
from tests.support.runtime_factory import build_component_agent_runtime_wiring


class _FakeProjectionService:
    def __init__(self) -> None:
        self.accepting = False

    async def start(self) -> None:
        self.accepting = True

    def wake(self, runtime_session_id: str | None = None) -> None:
        del runtime_session_id

    async def on_published_event(self, published: object) -> None:
        del published

    async def aclose(self, *, deadline_monotonic: float) -> None:
        if monotonic() > deadline_monotonic:
            raise TimeoutError("test projection close deadline exceeded")
        self.accepting = False


@dataclass(slots=True)
class TestHostProcessResourceLease:
    lease_id: str
    lease_generation: int
    projection_service: _FakeProjectionService
    schema_binding_fingerprint: str = "test-schema-binding:not-durable"
    resource_fingerprint: str = "test-host-resource:not-durable"
    durability_evidence: bool = False
    released: bool = False
    postgres_access_lease: object = None
    retrieval_resources: object = None
    governance_coordinator: object = None

    async def release(self, *, deadline_monotonic: float) -> None:
        await self.projection_service.aclose(deadline_monotonic=deadline_monotonic)
        if self.retrieval_resources is not None:
            if monotonic() >= deadline_monotonic:
                raise TimeoutError("test Host retrieval deadline exceeded")
            await self.retrieval_resources.aclose()
        self.released = True


class InMemorySessionManifestStore(HostSessionManifestStorePort):
    def __init__(self) -> None:
        self._records: dict[str, SessionManifest] = {}

    def get(self, runtime_session_id: str) -> SessionManifest | None:
        return self._records.get(runtime_session_id)

    def list_resumable(
        self,
        *,
        workspace_root: str | Path | None,
        memory_domain_id: str | None,
        include_closed: bool,
        limit: int,
    ) -> tuple[ResumableSessionSummary, ...]:
        records = tuple(reversed(tuple(self._records.values())))
        result: list[ResumableSessionSummary] = []
        for record in records:
            if workspace_root is not None and record.workspace_root != str(
                Path(workspace_root).expanduser().resolve()
            ):
                continue
            if (
                memory_domain_id is not None
                and record.memory_domain_id != memory_domain_id
            ):
                continue
            if not include_closed and not record.resumable:
                continue
            result.append(
                ResumableSessionSummary(
                    runtime_session_id=record.runtime_session_id,
                    conversation_id=record.conversation_id,
                    workspace_kind=record.workspace_kind,
                    workspace_root=record.workspace_root,
                    display_label=record.display_label,
                    memory_domain_id=record.memory_domain_id,
                    model_role=record.model_role,
                    permission_mode=record.permission_mode,
                    created_at=record.created_at,
                    last_active_at=record.last_active_at,
                    closed_at=record.closed_at,
                    archived=record.archived,
                    latest_run_status=None,
                    latest_run_id=None,
                )
            )
            if len(result) >= limit:
                break
        return tuple(result)

    def upsert_open_manifest(
        self,
        *,
        runtime_session_id: str,
        conversation_id: str,
        workspace: ResolvedWorkspace,
        model_role: ModelRole,
        permission_policy: EffectivePermissionPolicy,
        created_by: str,
    ) -> SessionManifest:
        now = utc_now_iso()
        existing = self._records.get(runtime_session_id)
        permission_mode = mode_for_policy(permission_policy)
        record = SessionManifest(
            runtime_session_id=runtime_session_id,
            conversation_id=conversation_id,
            workspace_kind=workspace.workspace_kind,
            workspace_root=str(workspace.workspace_root),
            display_label=workspace.display_label,
            memory_domain_id=workspace.memory_domain.memory_domain_id,
            model_role=model_role.value,
            permission_mode=(permission_mode.value if permission_mode else None),
            permission_policy=permission_policy.to_dict(),
            created_by=created_by,
            created_at=existing.created_at if existing is not None else now,
            last_active_at=now,
            closed_at=None,
            archived=False,
            metadata={"test_composition": True},
        )
        self._records[runtime_session_id] = record
        return record

    def touch(self, runtime_session_id: str) -> None:
        record = self._records[runtime_session_id]
        self._records[runtime_session_id] = _replace_manifest(
            record, last_active_at=utc_now_iso()
        )

    def mark_closed(self, runtime_session_id: str) -> None:
        record = self._records[runtime_session_id]
        self._records[runtime_session_id] = _replace_manifest(
            record, closed_at=utc_now_iso()
        )


class ComponentTestHostComposition(HostRuntimeComposition):
    def __init__(self) -> None:
        self._manifest = InMemorySessionManifestStore()
        self._lease: TestHostProcessResourceLease | None = None

    async def acquire_process_resources(
        self, *, deadline_monotonic: float
    ) -> HostProcessResourceLease:
        if monotonic() >= deadline_monotonic:
            raise TimeoutError("test Host resource deadline exceeded")
        if self._lease is None or self._lease.released:
            projection = _FakeProjectionService()
            await projection.start()
            self._lease = TestHostProcessResourceLease(
                lease_id=f"test-host-resource:{uuid4().hex}",
                lease_generation=1,
                projection_service=projection,
            )
        return self._lease

    def build_agent_runtime_wiring(
        self,
        *,
        admission: HostRuntimeBuildAdmission,
        resources: HostProcessResourceLease,
    ) -> HostAgentRuntimeWiringOutcome:
        if resources is not self._lease or resources.released:
            raise RuntimeError("test Host resource lease mismatch")
        live = admission.live_bindings
        wiring = build_component_agent_runtime_wiring(
            live.settings,
            Path(admission.build_fact.workspace_root),
            model_role=admission.build_fact.model_role,
            options=live.llm_options,
            system_prompt=admission.build_fact.system_prompt,
            runtime_session_id=admission.build_fact.runtime_session_id,
            reopen_deadline_monotonic=admission.reopen_deadline_monotonic,
            graph_id=admission.build_fact.graph_id,
            memory_domain=live.memory_domain,
            memory_reflection=admission.build_fact.memory_reflection,
            memory_reflection_options=live.memory_reflection_options,
            terminal_binding=live.terminal_binding,
            capability_runtime=live.capability_runtime_override,
            enable_workspace_skills=admission.build_fact.enable_workspace_skills,
            permission_policy=live.permission_policy,
            mcp_supervisor=live.mcp_supervisor,
            mcp_installation=live.mcp_installation,
        )
        return build_host_wiring_outcome(wiring=wiring, resource_lease=resources)

    def session_manifest_store(
        self, *, resources: HostProcessResourceLease
    ) -> HostSessionManifestStorePort:
        if resources is not self._lease or resources.released:
            raise RuntimeError("test Host manifest lease mismatch")
        return self._manifest


def component_test_host_core(
    settings: PulsaraSettings,
    *,
    scratch_root: Path | None = None,
) -> HostCore:
    return HostCore._from_component_test_composition(
        settings=settings,
        scratch_root=scratch_root,
        composition=ComponentTestHostComposition(),
    )


def replace_component_manifest_store(
    core: HostCore,
    manifest_store: HostSessionManifestStorePort,
) -> None:
    composition = core._composition
    if not isinstance(composition, ComponentTestHostComposition):
        raise TypeError("manifest replacement requires component test composition")
    composition._manifest = manifest_store


def component_manifest_store(core: HostCore) -> InMemorySessionManifestStore:
    composition = core._composition
    if not isinstance(composition, ComponentTestHostComposition):
        raise TypeError("manifest access requires component test composition")
    return composition._manifest


async def host_process_resource_lease(core: HostCore) -> HostProcessResourceLease:
    """Return the exact process lease for durable Host integration assertions."""

    return await core._get_process_resource_lease()


def _replace_manifest(record: SessionManifest, **updates: object) -> SessionManifest:
    values = {name: getattr(record, name) for name in record.__dataclass_fields__}
    values.update(updates)
    return SessionManifest(**values)


__all__ = [
    "component_manifest_store",
    "component_test_host_core",
    "replace_component_manifest_store",
]
