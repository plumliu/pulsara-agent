"""Production-only durable Host composition."""

from __future__ import annotations

import asyncio
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
from pulsara_agent.host.session_manifest import SessionManifestStore
from pulsara_agent.memory.governance.coordinator import MemoryGovernanceCoordinator
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.retrieval.runtime import (
    RetrievalRuntimeResources,
    build_retrieval_runtime_resources,
)
from pulsara_agent.runtime.projection_jobs.projection_handlers import (
    projection_executables,
)
from pulsara_agent.runtime.projection_jobs.registry import (
    build_projection_executable_registry,
)
from pulsara_agent.runtime.projection_jobs.service import DurableProjectionJobService
from pulsara_agent.runtime.projection_jobs.surface_handlers import surface_handlers
from pulsara_agent.runtime.wiring import build_agent_runtime_wiring
from pulsara_agent.settings import PulsaraSettings
from pulsara_agent.storage.schema_verification_service import (
    VerifiedPostgresAccessLease,
    process_postgres_schema_verification_service,
)


class ProductionHostProcessResourceLease:
    """One borrower-scoped owner for all process-wide durable Host resources."""

    durability_evidence = True

    def __init__(
        self,
        *,
        lease_id: str,
        lease_generation: int,
        postgres_access_lease: VerifiedPostgresAccessLease,
        retrieval_resources: RetrievalRuntimeResources,
        projection_service: DurableProjectionJobService,
        governance_coordinator: MemoryGovernanceCoordinator,
    ) -> None:
        self._lease_id = lease_id
        self._lease_generation = lease_generation
        self._postgres_access_lease = postgres_access_lease
        self._retrieval_resources = retrieval_resources
        self._projection_service = projection_service
        self._governance_coordinator = governance_coordinator
        self._released = False
        self._release_lock = asyncio.Lock()
        self._schema_binding_fingerprint = (
            postgres_access_lease.schema_binding.binding_fingerprint
        )
        self._resource_fingerprint = context_fingerprint(
            "production-host-process-resource-lease:v1",
            {
                "lease_id": lease_id,
                "lease_generation": lease_generation,
                "schema_binding_fingerprint": self._schema_binding_fingerprint,
            },
        )

    @property
    def lease_id(self) -> str:
        return self._lease_id

    @property
    def postgres_access_lease(self) -> VerifiedPostgresAccessLease:
        self._require_active()
        return self._postgres_access_lease

    @property
    def retrieval_resources(self) -> RetrievalRuntimeResources:
        self._require_active()
        return self._retrieval_resources

    @property
    def projection_service(self) -> DurableProjectionJobService:
        self._require_active()
        return self._projection_service

    @property
    def governance_coordinator(self) -> MemoryGovernanceCoordinator:
        self._require_active()
        return self._governance_coordinator

    @property
    def schema_binding_fingerprint(self) -> str:
        return self._schema_binding_fingerprint

    @property
    def lease_generation(self) -> int:
        return self._lease_generation

    @property
    def resource_fingerprint(self) -> str:
        return self._resource_fingerprint

    @property
    def released(self) -> bool:
        return self._released

    async def release(self, *, deadline_monotonic: float) -> None:
        async with self._release_lock:
            if self._released:
                return
            remaining = deadline_monotonic - monotonic()
            if remaining <= 0:
                raise TimeoutError("Host process resource release deadline exceeded")
            # Projection physical ownership must drain before dependencies close.
            await self._projection_service.aclose(deadline_monotonic=deadline_monotonic)
            remaining = deadline_monotonic - monotonic()
            if remaining <= 0:
                raise TimeoutError("Host retrieval release deadline exceeded")
            await asyncio.wait_for(
                self._retrieval_resources.aclose(),
                timeout=remaining,
            )
            self._postgres_access_lease.release()
            self._released = True

    def _require_active(self) -> None:
        if self._released:
            raise RuntimeError("production Host process resource lease is released")

    def __reduce_ex__(self, protocol: int):  # pragma: no cover - pickle hook
        del protocol
        raise TypeError("production Host process resource lease is process-local")


class ProductionHostComposition(HostRuntimeComposition):
    """The only product Host composition: PostgreSQL + durable projections."""

    def __init__(self, settings: PulsaraSettings) -> None:
        self._settings = settings
        self._resource_lock = asyncio.Lock()
        self._resource_lease: ProductionHostProcessResourceLease | None = None
        self._resource_generation = 0
        self._manifest_store: SessionManifestStore | None = None

    async def acquire_process_resources(
        self, *, deadline_monotonic: float
    ) -> HostProcessResourceLease:
        async with self._resource_lock:
            existing = self._resource_lease
            if existing is not None and not existing.released:
                return existing
            access_lease: VerifiedPostgresAccessLease | None = None
            retrieval: RetrievalRuntimeResources | None = None
            projection: DurableProjectionJobService | None = None
            try:
                access_lease = (
                    await process_postgres_schema_verification_service().acquire(
                        self._settings.storage.postgres_dsn,
                        deadline_monotonic=deadline_monotonic,
                    )
                )
                if monotonic() >= deadline_monotonic:
                    raise TimeoutError("Host resource acquisition deadline exceeded")
                retrieval = build_retrieval_runtime_resources(self._settings.retrieval)
                governance = MemoryGovernanceCoordinator()
                retrieval.attach_worker(governance)
                retrieval.start()
                projection = DurableProjectionJobService(
                    connection_provider=access_lease.connection_provider,
                    executable_registry=build_projection_executable_registry(
                        projection_executables(access_lease.connection_provider)
                    ),
                    surface_handlers=surface_handlers(
                        connection_provider=access_lease.connection_provider,
                        oxigraph_url=self._settings.storage.oxigraph_url,
                        embedding=retrieval.embedding,
                        embedding_provider_name=(
                            self._settings.retrieval.embedding.provider
                        ),
                    ),
                )
                await projection.start()
                governance.on_commit = projection.wake
                self._resource_generation += 1
                result = ProductionHostProcessResourceLease(
                    lease_id=f"host-resource:{uuid4().hex}",
                    lease_generation=self._resource_generation,
                    postgres_access_lease=access_lease,
                    retrieval_resources=retrieval,
                    projection_service=projection,
                    governance_coordinator=governance,
                )
                self._resource_lease = result
                self._manifest_store = SessionManifestStore(
                    access_lease.connection_provider
                )
                return result
            except BaseException:
                if projection is not None:
                    try:
                        await projection.aclose(deadline_monotonic=deadline_monotonic)
                    except BaseException:
                        pass
                if retrieval is not None:
                    try:
                        await retrieval.aclose()
                    except BaseException:
                        pass
                if access_lease is not None:
                    access_lease.release()
                raise

    def build_agent_runtime_wiring(
        self,
        *,
        admission: HostRuntimeBuildAdmission,
        resources: HostProcessResourceLease,
    ) -> HostAgentRuntimeWiringOutcome:
        if not isinstance(resources, ProductionHostProcessResourceLease):
            raise TypeError("production Host requires a production resource lease")
        if resources.released or not resources.durability_evidence:
            raise RuntimeError("production Host resource lease is not active")
        live = admission.live_bindings
        if live.settings is not self._settings:
            raise ValueError("Host composition settings binding mismatch")
        wiring = build_agent_runtime_wiring(
            live.settings,
            Path(admission.build_fact.workspace_root),
            postgres_access_lease=resources.postgres_access_lease,
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
            retrieval_resources=resources.retrieval_resources,
            governance_coordinator=resources.governance_coordinator,
            mcp_supervisor=live.mcp_supervisor,
            mcp_installation=live.mcp_installation,
        )
        return build_host_wiring_outcome(
            wiring=wiring,
            resource_lease=resources,
        )

    def session_manifest_store(
        self, *, resources: HostProcessResourceLease
    ) -> HostSessionManifestStorePort:
        if resources is not self._resource_lease or resources.released:
            raise RuntimeError("manifest store resource lease mismatch")
        if self._manifest_store is None:
            raise RuntimeError("Host manifest store has not been initialized")
        return self._manifest_store


__all__ = [
    "ProductionHostComposition",
    "ProductionHostProcessResourceLease",
]
