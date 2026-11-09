"""Composition, preparation, borrowing, and retirement of host tool surfaces."""

from __future__ import annotations

from pulsara_agent.identity import random_id
from dataclasses import replace
from pulsara_agent.capability.builtin_catalog import (
    builtin_tool_catalog_entry,
    execution_catalog_entry,
    builtin_tool_descriptor,
)
from pulsara_agent.capability.mcp_compatibility import (
    NativeMcpCompatibility,
    native_mcp_compatibility,
)
from pulsara_agent.capability.contracts import (
    CapabilityKind,
    FrozenCapabilityDispatchCut,
    CapabilitySourceKind,
    CapabilitySourceRefreshMode,
    CapabilitySourceSnapshotDisposition,
    FrozenToolCapabilityExposurePlan,
    ToolCapabilityRouteKind,
    ToolCapabilityOrigin,
    capability_identity,
    capability_source_ref,
    capability_source_registration,
    freeze_capability_source_snapshot,
    freeze_tool_capability_fact,
    tool_capability_version_ref,
)
from pulsara_agent.model_input.contracts import FrozenToolSpec, ModelInputScopeKind
from pulsara_agent.ports.tool_execution import ToolInvocationOwnerKind
from pulsara_agent.ports.tool_registry import tool_binding_contract_identity_fingerprint
from pulsara_agent.tools.builtins.media import MEDIA_TOOL_NAMES
from pulsara_agent.conversation_kernel.capability_composition import (
    BuiltinCompositionState,
    SealedBuiltinCapabilitySnapshot,
    issue_sealed_builtin_capability_snapshot,
)
from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    context_fingerprint,
    freeze_json,
)
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.conversation_kernel.tool_surface import (
    CODE_MODE_SHARED_TOOL_NAMES,
    CodeModeToolDirectory,
    BuiltinExecutionPolicyRef,
    DirectToolAccessLeaf,
    McpToolExecutionPolicyFact,
    PreparedKernelToolSurface,
    PreparedToolExecutionBinding,
    PreparedUnavailableDirectMcpGate,
    ProcessLocalToolSurfaceAccess,
    ProcessLocalToolSurfaceBorrow,
)
from .mcp.supervisor import McpBoundToolExecutor
from .mcp.contracts import McpDiscoveryCatalogInspection
from .tool_runtime_bindings import (
    ProductionBuiltinExecutorBinding,
    _qualified_executor_identity,
    _production_executor_binding,
    production_builtin_executor_binding_identity_fingerprint,
)
from .tool_runtime_schema import _json_schema_value


def _mcp_executor_capability_version(executor: McpBoundToolExecutor):
    semantic = executor.semantic
    source = capability_source_ref(
        CapabilitySourceKind.MCP_SERVER,
        semantic.server_id,
    )
    fact = freeze_tool_capability_fact(
        identity=capability_identity(
            kind=CapabilityKind.TOOL,
            source=source,
            stable_name=semantic.remote_tool_name,
        ),
        origin=ToolCapabilityOrigin.MCP,
        canonical_tool_spec=semantic.provider_spec(),
    )
    return tool_capability_version_ref(fact)


class _ToolSurfaceOperations:
    """Surface operations on the direct tool owner's single state and lock."""

    def _require_builtin_composition_preparing_locked(self) -> None:
        if self._builtin_composition_state is not BuiltinCompositionState.PREPARING:
            raise RuntimeError("builtin composition is already sealed or closed")


    def seal_builtin_composition(self) -> object:
        with self._surface_lock:
            if self._builtin_composition_state is BuiltinCompositionState.CLOSED:
                raise RuntimeError("builtin composition is closed")
            if self._builtin_composition_state is BuiltinCompositionState.SEALED:
                assert self._builtin_composition_seal is not None
                return self._builtin_composition_seal
            if any(
                item is None
                for item in (
                    self._interaction,
                    self._subagent,
                    self._memory,
                    self._decision,
                    self._mcp_supervisor,
                    self._capability_reload,
                )
            ):
                raise RuntimeError("builtin composition required ports are incomplete")
            self._sealed_builtin_bindings = self._executor_bindings_locked()
            self._builtin_composition_seal = object()
            self._builtin_composition_state = BuiltinCompositionState.SEALED
            return self._builtin_composition_seal


    def sealed_builtin_capability_snapshot(
        self,
        *,
        conversation_scope_kind: ModelInputScopeKind,
        effective_permission_mode: PermissionMode,
    ) -> SealedBuiltinCapabilitySnapshot:
        if not isinstance(effective_permission_mode, PermissionMode):
            raise TypeError("builtin selection requires the actual turn permission mode")
        with self._surface_lock:
            if self._builtin_composition_state is not BuiltinCompositionState.SEALED:
                raise RuntimeError("builtin composition is not sealed")
            assert self._sealed_builtin_bindings is not None
            assert self._builtin_composition_seal is not None
            bindings = self._sealed_builtin_bindings
            # Provider selection is distinct from the sealed execution catalog.
            # Only the actual role's native definitions enter provider selection.
            invocation_owner = (ToolInvocationOwnerKind.HOST_MAIN_RUN
                if conversation_scope_kind is ModelInputScopeKind.ROOT else ToolInvocationOwnerKind.SUBAGENT)
            bindings = tuple(item for item in bindings
                if item.catalog_entry.descriptor.is_model_callable
                and invocation_owner in item.catalog_entry.availability_requirement.allowed_invocation_owners)
            if conversation_scope_kind is ModelInputScopeKind.SUBAGENT:
                bindings = tuple(
                    item
                    for item in bindings
                    if item.tool_name
                    not in {
                        *MEDIA_TOOL_NAMES, "open_in_pulsara", "search_sessions", "search_session_content",
                        "read_session_content", "query_schedules", "manage_schedule", "search_memory", "get_memory",
                        "remember", "mark_memory_relation", "enter_plan", "ask_plan_question",
                        "exit_plan", "reload_capabilities", "manage_capability",
                        "run_agent", "stop_agent",
                        "exec_cell", "query_cells", "manage_cell",
                    }
                )
            source = capability_source_ref(
                CapabilitySourceKind.BUILTIN_REGISTRY,
                "pulsara-builtin-tools",
            )
            registration = capability_source_registration(
                source=source,
                refresh_mode=CapabilitySourceRefreshMode.IMMUTABLE,
                source_contract_fingerprint=context_fingerprint(
                    "builtin-capability-source-contract:v1",
                    tuple(
                        (
                            item.tool_name,
                            item.catalog_entry.descriptor.fingerprint(),
                            tool_binding_contract_identity_fingerprint(
                                item.catalog_entry.binding_contract
                            ),
                        )
                        for item in bindings
                    ),
                ),
            )
            facts = []
            for binding in bindings:
                entry = builtin_tool_catalog_entry(binding.tool_name)
                if entry is not binding.catalog_entry:
                    raise RuntimeError("builtin catalog/executor binding drifted")
                descriptor = builtin_tool_descriptor(binding.tool_name)
                schema = freeze_json(_json_schema_value(descriptor.input_schema))
                if not isinstance(schema, FrozenJsonObjectFact):
                    raise TypeError("builtin tool schema did not freeze to an object")
                spec = FrozenToolSpec(
                    name=binding.tool_name,
                    description=descriptor.description,
                    parameters=schema,
                    descriptor_fingerprint=descriptor.fingerprint(),
                )
                facts.append(
                    freeze_tool_capability_fact(
                        identity=capability_identity(
                            kind=CapabilityKind.TOOL,
                            source=source,
                            stable_name=binding.tool_name,
                        ),
                        origin=ToolCapabilityOrigin.BUILTIN,
                        canonical_tool_spec=spec,
                    )
                )
            source_snapshot = freeze_capability_source_snapshot(
                registration=registration,
                conversation_scope_kind=conversation_scope_kind,
                disposition=CapabilitySourceSnapshotDisposition.COMPLETE,
                facts=tuple(facts),
            )
            return issue_sealed_builtin_capability_snapshot(
                conversation_scope_kind=conversation_scope_kind,
                source_snapshot=source_snapshot,
                executor_bindings=bindings,
                builtin_composition_seal=self._builtin_composition_seal,
            )


    def prepare_tool_surface_safe_point(self) -> None:
        supervisor = self._mcp_supervisor
        if supervisor is None:
            return
        installed = supervisor.install_pending_at_safe_point()
        if installed is None:
            return
        with self._surface_lock:
            previous_generation = self._surface_generation
            previous = self._mcp_current
            self._surface_generation += 1
            self._mcp_current = installed
            self._mcp_runtime_by_surface_generation[self._surface_generation] = (
                installed
            )
            if (
                previous is not None
                and previous_generation not in self._surface_borrows.values()
            ):
                for key in tuple(self._prepared_surfaces):
                    if key[0] == previous_generation:
                        self._prepared_surfaces.pop(key, None)
                self._mcp_runtime_by_surface_generation.pop(previous_generation, None)
                previous.release()


    def freeze_mcp_capability_source_snapshot_set(
        self,
        *,
        conversation_scope_kind: ModelInputScopeKind,
    ):
        supervisor = self._mcp_supervisor
        if supervisor is None:
            raise RuntimeError("MCP supervisor is not bound")
        return supervisor.freeze_capability_source_snapshot_set(
            conversation_scope_kind=conversation_scope_kind,
        )


    def observe_hook_review_tools(self):
        """Disposable observation; never installs provider tools or grants authority."""
        from pulsara_agent.hooks.matcher import tool_matcher_subject

        with self._surface_lock:
            if self._sealed_builtin_bindings is None:
                raise RuntimeError("builtin composition is not sealed")
            subjects = tuple(
                tool_matcher_subject(binding.tool_name)
                for binding in self._sealed_builtin_bindings
            )
            supervisor = self._mcp_supervisor
        if supervisor is None:
            return subjects, False
        inspection = supervisor.inspect_discovery_catalog()
        subjects += tuple(
            tool_matcher_subject(
                item.semantic.provider_tool_name,
                resolved_remote_identity=item.semantic.provider_tool_name,
            )
            for item in inspection.tools
        )
        complete = all(
            server.tool_surface_semantic_fingerprint is not None
            for server in inspection.catalog_snapshot.servers
        )
        return subjects, complete


    def inspect_mcp_discovery_catalog(self) -> McpDiscoveryCatalogInspection:
        supervisor = self._mcp_supervisor
        if supervisor is None:
            raise RuntimeError("MCP supervisor is not bound")
        return supervisor.inspect_discovery_catalog(live=True)


    def freeze_mcp_capability_projection_input(self, owner):
        supervisor = self._mcp_supervisor
        if supervisor is None:
            raise RuntimeError("MCP supervisor is not bound")
        return supervisor.freeze_capability_projection_input(owner)


    def _executor_bindings_locked(self) -> tuple[ProductionBuiltinExecutorBinding, ...]:
        if self._sealed_builtin_bindings is not None:
            return self._sealed_builtin_bindings
        identities = {
            name: _qualified_executor_identity(tool, name)
            for name, tool in self._tools.items()
        }
        if self._subagent is not None:
            for name in self._subagent.tool_names:
                if name in identities:
                    raise RuntimeError("production builtin has multiple executors")
                identities[name] = _qualified_executor_identity(self._subagent, name)
        if self._memory is not None:
            for name in self._memory.tool_names:
                if name in identities:
                    raise RuntimeError("production builtin has multiple executors")
                identities[name] = _qualified_executor_identity(self._memory, name)
        if self._decision is not None:
            for name in self._decision.tool_names:
                if name in identities:
                    raise RuntimeError("production builtin has multiple executors")
                identities[name] = _qualified_executor_identity(self._decision, name)
        return tuple(
            _production_executor_binding(name, identities[name])
            for name in sorted(identities)
        )


    @property
    def executor_bindings(self) -> tuple[ProductionBuiltinExecutorBinding, ...]:
        with self._surface_lock:
            return self._executor_bindings_locked()


    def freeze_code_mode_tool_directory(
        self, prepared: PreparedKernelToolSurface, *, effective_permission_mode: PermissionMode,
    ) -> CodeModeToolDirectory:
        """Freeze Cell capabilities from sealed owners, retaining only authorized MCP leaves."""
        if (prepared.access.conversation_scope_kind is not ModelInputScopeKind.ROOT
            or effective_permission_mode is not PermissionMode.BYPASS_PERMISSIONS):
            raise PermissionError("Code Mode requires ROOT bypass-permissions mode")
        with self._surface_lock:
            self._require_prepared_surface_locked(prepared)
            if self._builtin_composition_state is not BuiltinCompositionState.SEALED:
                raise RuntimeError("Code Mode requires sealed execution capabilities")
            assert self._sealed_builtin_bindings is not None
            specs, bindings = [], []
            for owner in self._sealed_builtin_bindings:
                if owner.tool_name not in CODE_MODE_SHARED_TOOL_NAMES:
                    continue
                entry = execution_catalog_entry(owner.tool_name, code_mode=True)
                descriptor = entry.descriptor
                owner = replace(owner, catalog_entry=entry)
                spec = FrozenToolSpec(name=owner.tool_name, description=descriptor.description,
                    parameters=freeze_json(_json_schema_value(descriptor.input_schema)),
                    descriptor_fingerprint=descriptor.fingerprint())
                specs.append(spec)
                bindings.append(PreparedToolExecutionBinding(tool_name=spec.name,
                    descriptor_fingerprint=spec.descriptor_fingerprint,
                    executor_binding_fingerprint=production_builtin_executor_binding_identity_fingerprint(owner),
                    execution_policy=BuiltinExecutionPolicyRef(tool_name=spec.name,
                        catalog_entry_fingerprint=entry.entry_fingerprint)))
            # MCP discovery/authorization remains owned by the prepared capability cut.
            # Never enumerate the supervisor's entire inventory to build a Cell catalog.
            for spec in prepared.model_surface.tool_specs:
                binding = prepared.binding(spec.name)
                if (isinstance(binding, PreparedToolExecutionBinding)
                    and isinstance(binding.execution_policy, McpToolExecutionPolicyFact)):
                    specs.append(spec)
                    bindings.append(binding)
            return CodeModeToolDirectory(tuple(specs), tuple(bindings), prepared.access)


    def prepare_planned_tool_surface(
        self,
        *,
        plan: FrozenToolCapabilityExposurePlan,
        builtin: SealedBuiltinCapabilitySnapshot,
    ) -> PreparedKernelToolSurface:
        """Exact-join one semantic plan to this Host's physical owners."""

        surface = plan.direct_tool_surface
        scope = surface.conversation_scope_kind
        if (
            builtin.conversation_scope_kind is not scope
            or plan.dispatch_view.parent_dispatch_cut.conversation_scope_kind
            is not scope
        ):
            raise ValueError("planned tool surface scope is invalid")
        with self._surface_lock:
            if self._closed:
                raise RuntimeError("tool surface is closed")
            if (
                self._builtin_composition_state is not BuiltinCompositionState.SEALED
                or builtin.builtin_composition_seal
                is not self._builtin_composition_seal
            ):
                raise RuntimeError("foreign or unsealed builtin snapshot")
            # Bind current-contract frozen specs to the sealed execution owners.
            builtin_by_name = {
                item.tool_name: item for item in self._sealed_builtin_bindings
            }
            current_mcp = self._mcp_current
            mcp_binding_by_name = (
                {}
                if current_mcp is None
                else {item.tool_name: item for item in current_mcp.execution_bindings}
            )
            mcp_executor_by_name = {} if current_mcp is None else current_mcp.executors
            route_by_name = {item.version.provider_name: item for item in plan.mcp_catalog_route_projection.routes}
            version_by_name = {
                item.provider_name: item
                for item in plan.direct_projection_set.tool_versions
            }
            leaves: list[DirectToolAccessLeaf] = []
            for spec in surface.tool_specs:
                builtin_binding = builtin_by_name.get(spec.name)
                if builtin_binding is not None:
                    entry = builtin_binding.catalog_entry
                    if (not entry.descriptor.is_model_callable
                        or builtin_tool_descriptor(spec.name).fingerprint() != spec.descriptor_fingerprint):
                        raise RuntimeError("planned builtin descriptor drifted")
                    policy = BuiltinExecutionPolicyRef(
                        tool_name=spec.name,
                        catalog_entry_fingerprint=entry.entry_fingerprint,
                    )
                    leaves.append(
                        PreparedToolExecutionBinding(
                            tool_name=spec.name,
                            descriptor_fingerprint=spec.descriptor_fingerprint,
                            executor_binding_fingerprint=(
                                production_builtin_executor_binding_identity_fingerprint(
                                    builtin_binding
                                )
                            ),
                            execution_policy=policy,
                        )
                    )
                    continue
                version = version_by_name.get(spec.name)
                mcp_binding = mcp_binding_by_name.get(spec.name)
                mcp_executor = mcp_executor_by_name.get(spec.name)
                route = route_by_name.get(spec.name)
                if (
                    version is not None
                    and mcp_binding is not None
                    and mcp_executor is not None
                    and route is not None
                    and route.route is ToolCapabilityRouteKind.DIRECT
                    and route.version == _mcp_executor_capability_version(mcp_executor)
                    and mcp_binding.descriptor_fingerprint == mcp_executor.semantic.descriptor_fingerprint
                    and native_mcp_compatibility(spec, mcp_executor.semantic.provider_spec()) is NativeMcpCompatibility.COMPATIBLE
                    and mcp_executor.policy is mcp_binding.execution_policy
                ):
                    leaves.append(replace(mcp_binding,
                        descriptor_fingerprint=spec.descriptor_fingerprint,
                        current_mcp_spec=mcp_executor.semantic.provider_spec()))
                    continue
                if version is None:
                    raise RuntimeError("planned MCP version is absent")
                if route is None:
                    raise RuntimeError("planned native MCP route is absent")
                reason = (route.public_reason_code.value
                          if route.route is ToolCapabilityRouteKind.UNAVAILABLE else
                          "NATIVE_CONTRACT_REQUIRES_META" if mcp_binding is not None
                          else "MCP_DIRECT_CURRENTLY_UNAVAILABLE")
                leaves.append(
                    PreparedUnavailableDirectMcpGate(
                        capability_identity=(version.identity),
                        tool_semantic_fingerprint=spec.descriptor_fingerprint,
                        provider_tool_name=spec.name,
                        unavailable_reason_code=reason,
                        target=route.target,
                        supervisor_authority_identity=self._mcp_supervisor,
                    )
                )
            frozen_leaves = tuple(leaves)
            access = ProcessLocalToolSurfaceAccess(
                owner_epoch=self._surface_owner_epoch,
                surface_generation=self._surface_generation,
                conversation_scope_kind=scope,
                _authority=self._surface_authority,
                session_id=self._session_id,
            )
            prepared = PreparedKernelToolSurface(
                model_surface=surface,
                execution_bindings=frozen_leaves,
                access=access,
                capability_exposure_plan=plan,
            )
            self._prepared_surfaces[(self._surface_generation, scope)] = (
                prepared
            )
            return prepared


    def borrow_tool_surface(
        self, prepared: PreparedKernelToolSurface
    ) -> ProcessLocalToolSurfaceBorrow:
        with self._surface_lock:
            self._require_prepared_surface_locked(prepared)
            if prepared.access.surface_generation != self._surface_generation:
                raise RuntimeError("retiring tool surface refuses new borrows")
            borrow_id = random_id("borrow")
            while borrow_id in self._surface_borrows or borrow_id in self._surface_borrow_owners:
                borrow_id = random_id("borrow")
            borrow = ProcessLocalToolSurfaceBorrow(
                prepared=prepared,
                borrow_id=borrow_id,
                _authority=self._surface_authority,
                _validate=self._validate_surface_borrow,
                _release=self._release_surface_borrow,
            )
            self._surface_borrows[borrow_id] = prepared.access.surface_generation
            self._surface_borrow_owners[borrow_id] = borrow
            return borrow


    def borrow_code_mode_surface(
        self, prepared: CodeModeToolDirectory, *, cell_id: str
    ) -> ProcessLocalToolSurfaceBorrow:
        """Borrow a physical invocation without pinning a provider epoch."""
        self.prepare_tool_surface_safe_point()
        with self._surface_lock:
            if self._closed or prepared.access._authority is not self._surface_authority:
                raise RuntimeError("Code Mode tool owner is unavailable")
            access = replace(prepared.access, surface_generation=self._surface_generation)
            physical_surface = replace(prepared, access=access)
            borrow = ProcessLocalToolSurfaceBorrow(
                prepared=physical_surface, borrow_id=random_id("borrow"),
                _authority=self._surface_authority,
                _validate=self._validate_surface_borrow,
                _release=self._release_surface_borrow, code_cell_id=cell_id,
            )
            self._execution_borrows[borrow.borrow_id] = borrow
            return borrow


    def validate_tool_surface_borrow(
        self,
        borrow: ProcessLocalToolSurfaceBorrow,
        prepared: PreparedKernelToolSurface,
    ) -> None:
        """Revalidate one pinned surface without selecting an arbitrary tool."""

        with self._surface_lock:
            if (
                borrow._closed
                or borrow._authority is not self._surface_authority
                or borrow.borrow_id not in self._surface_borrows
                or self._surface_borrow_owners.get(borrow.borrow_id) is not borrow
                or not borrow.exactly_joins(prepared)
            ):
                raise RuntimeError("tool surface borrow is not active")
            self._require_prepared_surface_locked(prepared)


    def assert_no_tool_surface_borrows(
        self,
        *,
        scope_kind: ModelInputScopeKind,
    ) -> None:
        """Prove all process-local surface borrows for this scope are closed."""

        with self._surface_lock:
            if any(
                borrow.prepared.access.conversation_scope_kind is scope_kind
                for borrow in self._surface_borrow_owners.values()
            ):
                raise RuntimeError("no-continuation left a tool surface borrow active")


    def issue_capability_dispatch_observation(
        self,
        *,
        capability_dispatch_cut: FrozenCapabilityDispatchCut,
        prepared_surface: PreparedKernelToolSurface,
        surface_borrow: ProcessLocalToolSurfaceBorrow,
    ) -> object:
        """Issue an exact one-shot carrier only for a registry-current borrow."""

        from pulsara_agent.conversation_kernel.tool_surface import (
            _issue_capability_dispatch_observation,
        )

        with self._surface_lock:
            if (
                surface_borrow._closed
                or surface_borrow._authority is not self._surface_authority
                or surface_borrow.borrow_id not in self._surface_borrows
                or self._surface_borrow_owners.get(surface_borrow.borrow_id)
                is not surface_borrow
                or not surface_borrow.exactly_joins(prepared_surface)
            ):
                raise RuntimeError("tool surface borrow is not active")
            self._require_prepared_surface_locked(prepared_surface)
            access = prepared_surface.access
            if (
                access.conversation_scope_kind
                is not capability_dispatch_cut.conversation_scope_kind
            ):
                raise RuntimeError(
                    "capability dispatch observation does not exact-join"
                )
            return _issue_capability_dispatch_observation(
                capability_dispatch_cut=capability_dispatch_cut,
                prepared_surface=prepared_surface,
                surface_borrow=surface_borrow,
                owner=self,
            )


    def _require_prepared_surface_locked(
        self, prepared: PreparedKernelToolSurface
    ) -> None:
        access = prepared.access
        key = (
            access.surface_generation,
            access.conversation_scope_kind,
        )
        retained = self._prepared_surfaces.get(key)
        if (
            self._closed
            or access._authority is not self._surface_authority
            or access.owner_epoch != self._surface_owner_epoch
            or retained is None
            or retained is not prepared
        ):
            raise RuntimeError("prepared tool surface is revoked")


    def _validate_surface_borrow(
        self, borrow: ProcessLocalToolSurfaceBorrow, tool_name: str
    ) -> DirectToolAccessLeaf:
        with self._surface_lock:
            if borrow.code_cell_id is not None:
                if (self._closed or borrow._closed
                    or borrow._authority is not self._surface_authority
                    or self._execution_borrows.get(borrow.borrow_id) is not borrow):
                    raise RuntimeError("Code Mode invocation resource is unavailable")
                return borrow.prepared.binding(tool_name)
            if (
                borrow._closed
                or borrow._authority is not self._surface_authority
                or borrow.borrow_id not in self._surface_borrows
                or self._surface_borrow_owners.get(borrow.borrow_id) is not borrow
            ):
                raise RuntimeError("tool surface borrow is not active")
            self._require_prepared_surface_locked(borrow.prepared)
            for binding in borrow.prepared.execution_bindings:
                if binding.tool_name == tool_name:
                    return binding
        raise RuntimeError("tool was not advertised by the prepared surface")


    def _release_surface_borrow(self, borrow: ProcessLocalToolSurfaceBorrow) -> None:
        with self._surface_condition:
            if borrow.code_cell_id is not None:
                if self._execution_borrows.pop(borrow.borrow_id, None) is not borrow:
                    raise RuntimeError("Code Mode invocation resource is not current")
                self._surface_condition.notify_all()
                return
            if borrow._authority is not self._surface_authority:
                raise RuntimeError("tool surface borrow authority conflicts")
            if self._surface_borrow_owners.get(borrow.borrow_id) is not borrow:
                raise RuntimeError("tool surface borrow is not registry-current")
            generation = self._surface_borrows.pop(borrow.borrow_id, None)
            self._surface_borrow_owners.pop(borrow.borrow_id, None)
            self._installed_epoch_by_borrow.pop(borrow.borrow_id, None)
            if generation is not None and generation != self._surface_generation:
                if generation not in self._surface_borrows.values():
                    for key in tuple(self._prepared_surfaces):
                        if key[0] == generation:
                            self._prepared_surfaces.pop(key, None)
                    runtime = self._mcp_runtime_by_surface_generation.pop(
                        generation, None
                    )
                    if runtime is not None:
                        runtime.release()
            self._surface_condition.notify_all()
