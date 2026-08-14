"""Formal structured-input test consumers; production never imports this module."""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Callable

from pulsara_agent.conversation_kernel.direct_model import (
    DirectKernelModelPort,
    KernelModelExecutionRequest,
    KernelModelPreparationRequest,
    PreparedKernelModelCall,
)
from pulsara_agent.conversation_kernel.input_continuity import (
    ProcessLocalProviderInputInstallAuthority,
)
from pulsara_agent.conversation_kernel.context_sources import (
    ContextSourceRegistry,
    FrozenNonTriggerContextSources,
)
from pulsara_agent.conversation_kernel.runner import KernelToolInvocationContext
from pulsara_agent.conversation_kernel.tool_surface import (
    BuiltinExecutionPolicyRef,
    PreparedKernelToolSurface,
    PreparedToolExecutionBinding,
    ProcessLocalToolSurfaceAccess,
    ProcessLocalToolSurfaceBorrow,
    tool_execution_surface_fingerprint,
)
from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
from pulsara_agent.model_input.contracts import (
    CanonicalModelInputSnapshot,
    CollectedContextSources,
    ContextBudgetClass,
    ContextChannel,
    ContextRenderMode,
    ContextRenderVariant,
    ContextSourceCandidate,
    ContextSourceAbsentFact,
    ContextSourceAbsenceKind,
    ContextSourceKind,
    ContextSourceLifecycle,
    ContextTrustClass,
    ContextBindingBaseKind,
    FrozenContextBindingCompileFact,
    FrozenModelToolSurface,
    FrozenCanonicalCompileSnapshot,
    FrozenToolSpec,
    ModelInputScopeKind,
    canonical_compile_snapshot_fingerprint,
    build_tool_observation_freshness_fact,
    context_binding_compile_fact_fingerprint,
    model_tool_surface_fingerprint,
)
from pulsara_agent.model_input.continuity import FULL_HISTORY_CONTEXT_BASE_IDENTITY
from pulsara_agent.model_input.continuity import ProcessLocalProviderInputInstallPermit
from pulsara_agent.primitives.context import context_fingerprint, freeze_json
from pulsara_agent.primitives.permission import DEFAULT_PERMISSION_MODE
from pulsara_agent.primitives.run_permission import (
    FrozenRunPermissionSnapshot,
    RunPermissionAdmissionSource,
    build_run_permission_snapshot,
)
from tests.support.model_config import test_llm_config


class ScriptedKernelModel:
    def __init__(self, calls: list[list[object]]) -> None:
        self._calls = calls
        self.requests: list[KernelModelExecutionRequest] = []
        self.preparation_requests: list[KernelModelPreparationRequest] = []
        self._preparer = DirectKernelModelPort(
            config=test_llm_config(
                api_key="test",
                base_url="https://example.invalid/v1",
                pro_model="test-pro",
                flash_model="test-flash",
                api="openai_chat_completions",
            )
        )

    def prepare_call(
        self, request: KernelModelPreparationRequest
    ) -> PreparedKernelModelCall:
        self.preparation_requests.append(request)
        return self._preparer.prepare_call(request)

    def preflight_execution(
        self,
        request: KernelModelExecutionRequest,
        *,
        expected_append_candidate_fingerprint: str,
        install_authority: ProcessLocalProviderInputInstallAuthority,
    ) -> "_ScriptedPreparedExecution":
        for tool in request.compiled_input.tools:
            binding = request.surface_borrow.execution_binding(tool.name)
            if binding.descriptor_fingerprint != tool.descriptor_fingerprint:
                raise RuntimeError("scripted tool binding was revoked")
        self.requests.append(request)
        return _ScriptedPreparedExecution(
            request=request,
            items=self._calls.pop(0),
            expected_candidate_fingerprint=expected_append_candidate_fingerprint,
            install_authority=install_authority,
        )


class CallbackScriptedKernelModel:
    """Round 3.1 model double with the production preflight/open boundary."""

    def __init__(
        self,
        stream_factory: Callable[
            [KernelModelExecutionRequest], AsyncIterator[object]
        ],
    ) -> None:
        self._stream_factory = stream_factory
        self.requests: list[KernelModelExecutionRequest] = []
        self._preparer = DirectKernelModelPort(
            config=test_llm_config(
                api_key="test",
                base_url="https://example.invalid/v1",
                pro_model="test-pro",
                flash_model="test-flash",
                api="openai_chat_completions",
            )
        )

    def prepare_call(
        self, request: KernelModelPreparationRequest
    ) -> PreparedKernelModelCall:
        return self._preparer.prepare_call(request)

    def preflight_execution(
        self,
        request: KernelModelExecutionRequest,
        *,
        expected_append_candidate_fingerprint: str,
        install_authority: ProcessLocalProviderInputInstallAuthority,
    ) -> "_CallbackPreparedExecution":
        self.requests.append(request)
        return _CallbackPreparedExecution(
            request=request,
            stream_factory=self._stream_factory,
            expected_candidate_fingerprint=expected_append_candidate_fingerprint,
            install_authority=install_authority,
        )


class _CallbackPreparedExecution:
    def __init__(
        self,
        *,
        request: KernelModelExecutionRequest,
        stream_factory: Callable[
            [KernelModelExecutionRequest], AsyncIterator[object]
        ],
        expected_candidate_fingerprint: str,
        install_authority: ProcessLocalProviderInputInstallAuthority,
    ) -> None:
        self._request = request
        self._stream_factory = stream_factory
        self._expected_candidate_fingerprint = expected_candidate_fingerprint
        self._install_authority = install_authority
        self._settled = False
        self.execution_fingerprint = context_fingerprint(
            "test-callback-prepared-execution:v1",
            {
                "compiled": request.compiled_input.compiled_semantic_fingerprint,
                "candidate": expected_candidate_fingerprint,
            },
        )

    def discard(self) -> None:
        if self._settled:
            raise RuntimeError("callback execution already settled")
        self._settled = True

    async def open_once(
        self, permit: ProcessLocalProviderInputInstallPermit
    ) -> AsyncIterator[object]:
        if self._settled:
            raise RuntimeError("callback execution already settled")
        if (
            permit.candidate_fingerprint
            != self._expected_candidate_fingerprint
            or permit.execution_fingerprint != self.execution_fingerprint
        ):
            raise RuntimeError("callback execution permit mismatch")
        self._install_authority.consume(
            permit,
            candidate_fingerprint=self._expected_candidate_fingerprint,
            execution_fingerprint=self.execution_fingerprint,
        )
        self._settled = True
        async for item in self._stream_factory(self._request):
            yield item


class _ScriptedPreparedExecution:
    def __init__(
        self,
        *,
        request: KernelModelExecutionRequest,
        items: list[object],
        expected_candidate_fingerprint: str,
        install_authority: ProcessLocalProviderInputInstallAuthority,
    ) -> None:
        self._request = request
        self._items = items
        self._expected_candidate_fingerprint = expected_candidate_fingerprint
        self._install_authority = install_authority
        self._opened = False
        self.execution_fingerprint = context_fingerprint(
            "test-scripted-prepared-execution:v1",
            {
                "compiled": request.compiled_input.compiled_semantic_fingerprint,
                "candidate": expected_candidate_fingerprint,
            },
        )

    def discard(self) -> None:
        if self._opened:
            raise RuntimeError("scripted execution already opened")
        self._opened = True

    async def open_once(
        self, permit: ProcessLocalProviderInputInstallPermit
    ) -> AsyncIterator[object]:
        if self._opened:
            raise RuntimeError("scripted execution already opened")
        if (
            permit.candidate_fingerprint
            != self._expected_candidate_fingerprint
            or permit.execution_fingerprint != self.execution_fingerprint
        ):
            raise RuntimeError("scripted execution permit mismatch")
        self._install_authority.consume(
            permit,
            candidate_fingerprint=self._expected_candidate_fingerprint,
            execution_fingerprint=self.execution_fingerprint,
        )
        self._opened = True
        for item in self._items:
            yield item


@dataclass(frozen=True, slots=True)
class _Cwd:
    value: str = "/test/workspace"


class StaticContextSourceCollector:
    """Required first-party facts with the production contracts."""

    @property
    def registry_fingerprint(self) -> str:
        return ContextSourceRegistry().fingerprint

    def collect(self, **_kwargs: object) -> CollectedContextSources:
        canonical_facts = _kwargs["canonical_facts"]
        permission = canonical_facts.run_permission_snapshot  # type: ignore[union-attr]
        candidates: tuple[ContextSourceCandidate, ...] = (
            _candidate(
                kind=ContextSourceKind.BASE_SYSTEM,
                version="pulsara.base-system.prefix-continuity.v3",
                channel=ContextChannel.SYSTEM,
                trust=ContextTrustClass.ROOT_INSTRUCTION,
                budget=ContextBudgetClass.MUST_KEEP,
                placement=0,
                degradation=0,
                variants=((ContextRenderMode.FULL, "ROOT SYSTEM"),),
            ),
            _candidate(
                kind=ContextSourceKind.RUNTIME_ENVIRONMENT,
                version="pulsara.runtime-environment.v2",
                channel=ContextChannel.RUNTIME_OBSERVATION,
                trust=ContextTrustClass.TRUSTED_RUNTIME_FACT,
                budget=ContextBudgetClass.MUST_KEEP,
                placement=10,
                degradation=10,
                variants=(
                    (ContextRenderMode.FULL, "runtime workspace=/test/workspace"),
                    (ContextRenderMode.COMPACT, "runtime=/test/workspace"),
                ),
            ),
            _candidate(
                kind=ContextSourceKind.RUN_PERMISSION,
                version="pulsara.run-permission.v2",
                channel=ContextChannel.RUNTIME_OBSERVATION,
                trust=ContextTrustClass.AUTHORIZED_RUNTIME_GUIDANCE,
                budget=ContextBudgetClass.MUST_KEEP,
                placement=20,
                degradation=12,
                variants=(
                    (
                        ContextRenderMode.FULL,
                        f"permission={permission.effective_mode.value}",
                    ),
                    (
                        ContextRenderMode.COMPACT,
                        f"permission={permission.effective_mode.value}",
                    ),
                ),
            ),
        )
        if canonical_facts.plan_handoff_fact is not None:  # type: ignore[union-attr]
            candidates += (
                _candidate(
                    kind=ContextSourceKind.PLAN_HANDOFF,
                    version="pulsara.plan-handoff.v2",
                    channel=ContextChannel.RUNTIME_OBSERVATION,
                    trust=ContextTrustClass.AUTHORIZED_RUNTIME_GUIDANCE,
                    budget=ContextBudgetClass.MUST_KEEP,
                    placement=30,
                    degradation=11,
                    variants=(
                        (ContextRenderMode.FULL, "plan handoff full"),
                        (ContextRenderMode.COMPACT, "plan handoff"),
                    ),
                ),
            )
        if canonical_facts.plan_workflow_fact is not None:  # type: ignore[union-attr]
            candidates += (
                _candidate(
                    kind=ContextSourceKind.PLAN_WORKFLOW,
                    version="pulsara.plan-workflow.v2",
                    channel=ContextChannel.RUNTIME_OBSERVATION,
                    trust=ContextTrustClass.AUTHORIZED_RUNTIME_GUIDANCE,
                    budget=ContextBudgetClass.MUST_KEEP,
                    placement=40,
                    degradation=10,
                    variants=(
                        (ContextRenderMode.FULL, "plan workflow full"),
                        (ContextRenderMode.COMPACT, "plan workflow"),
                    ),
                ),
            )
        freshness = canonical_facts.tool_observation_freshness_fact  # type: ignore[union-attr]
        candidates += (
            _candidate(
                kind=ContextSourceKind.TOOL_OBSERVATION_FRESHNESS,
                version="pulsara.tool-observation-freshness.v1",
                channel=ContextChannel.RUNTIME_OBSERVATION,
                trust=ContextTrustClass.TRUSTED_RUNTIME_FACT,
                budget=ContextBudgetClass.MUST_KEEP,
                placement=70,
                degradation=10,
                variants=((ContextRenderMode.FULL, freshness.current_turn_ref),),
            ),
        )
        present = {item.source_kind for item in candidates}
        absence_kinds = {
            ContextSourceKind.RUNTIME_CLOCK: ContextSourceAbsenceKind.UNAVAILABLE,
            ContextSourceKind.PLAN_HANDOFF: ContextSourceAbsenceKind.NOT_APPLICABLE,
            ContextSourceKind.PLAN_WORKFLOW: ContextSourceAbsenceKind.EXPLICIT_EMPTY,
            ContextSourceKind.CAPABILITY_CATALOG: ContextSourceAbsenceKind.EXPLICIT_EMPTY,
            ContextSourceKind.MCP_CATALOG: ContextSourceAbsenceKind.NOT_APPLICABLE,
            ContextSourceKind.ACTIVE_SKILL: ContextSourceAbsenceKind.EXPLICIT_EMPTY,
            ContextSourceKind.PREVIOUS_TURN_OUTCOME: (
                ContextSourceAbsenceKind.EXPLICIT_EMPTY
            ),
        }
        absent = tuple(
            _absent_source(kind, absence)
            for kind, absence in absence_kinds.items()
            if kind not in present
        )
        registry = self.registry_fingerprint
        collection = context_fingerprint(
            "collected-context-sources:v1",
            {
                "registry_fingerprint": registry,
                "candidates": tuple(
                    item.source_semantic_fingerprint for item in candidates
                ),
                "diagnostics": (),
                "absent": tuple(
                    (
                        item.source_kind.value,
                        item.lifecycle.value,
                        item.absence_kind.value,
                        item.domain_semantic_fingerprint,
                    )
                    for item in absent
                ),
            },
        )
        return CollectedContextSources(candidates, (), registry, collection, absent)

    def freeze_non_trigger_sources(
        self, **kwargs: object
    ) -> FrozenNonTriggerContextSources:
        collection = self.collect(
            activation_subject=None,
            activation_text="",
            **kwargs,
        )
        return FrozenNonTriggerContextSources(
            candidates=collection.candidates,
            absent_facts=collection.absent_facts,
            diagnostics=collection.diagnostics,
            available_tool_names=frozenset(
                tool.name
                for tool in kwargs["tool_surface"].tool_specs  # type: ignore[union-attr]
            ),
            registry_fingerprint=collection.registry_fingerprint,
            freeze_fingerprint=context_fingerprint(
                "test:frozen-non-trigger-sources:v1",
                collection.collection_fingerprint,
            ),
        )

    def complete_frozen_sources(
        self,
        frozen: FrozenNonTriggerContextSources,
        **_kwargs: object,
    ) -> CollectedContextSources:
        registry = self.registry_fingerprint
        if registry != frozen.registry_fingerprint:
            raise ValueError("context source registry changed after source freeze")
        collection = context_fingerprint(
            "collected-context-sources:v1",
            {
                "registry_fingerprint": registry,
                "candidates": tuple(
                    item.source_semantic_fingerprint for item in frozen.candidates
                ),
                "diagnostics": (),
                "absent": tuple(
                    (
                        item.source_kind.value,
                        item.lifecycle.value,
                        item.absence_kind.value,
                        item.domain_semantic_fingerprint,
                    )
                    for item in frozen.absent_facts
                ),
            },
        )
        return CollectedContextSources(
            frozen.candidates,
            frozen.diagnostics,
            registry,
            collection,
            frozen.absent_facts,
        )


def _absent_source(
    kind: ContextSourceKind,
    absence: ContextSourceAbsenceKind,
) -> ContextSourceAbsentFact:
    binding = ContextSourceRegistry().binding(kind)
    return ContextSourceAbsentFact(
        source_kind=kind,
        lifecycle=binding.lifecycle,
        absence_kind=absence,
        source_contract_version=binding.contract_version,
        source_contract_fingerprint=binding.contract_fingerprint,
        trust_class=binding.trust,
        budget_class=binding.budget,
        placement_ordinal=binding.placement,
        degradation_priority=binding.degradation,
        domain_semantic_fingerprint=context_fingerprint(
            "pulsara:context-source-absence:v1",
            {
                "kind": kind.value,
                "absence": absence.value,
                "contract": binding.contract_fingerprint,
            },
        ),
    )


def static_canonical_compile_facts(
    canonical_input: CanonicalModelInputSnapshot,
) -> FrozenCanonicalCompileSnapshot:
    """Build the final no-Plan compile fact for isolated adapter tests."""

    permission = build_run_permission_snapshot(
        snapshot_id="permission:test",
        requested_mode=DEFAULT_PERMISSION_MODE,
        effective_mode=DEFAULT_PERMISSION_MODE,
        admission_source=RunPermissionAdmissionSource.USER_SUBMISSION,
    )
    binding_values = {
        "binding_revision_id": canonical_input.identity.context_binding_revision_id,
        "revision_ordinal": 0,
        "base_kind": ContextBindingBaseKind.FULL_HISTORY,
        "context_snapshot_id": None,
        "source_through_sequence": 0,
        "context_base_semantic_identity": FULL_HISTORY_CONTEXT_BASE_IDENTITY,
    }
    provisional_binding = FrozenContextBindingCompileFact.__new__(
        FrozenContextBindingCompileFact
    )
    for name, value in binding_values.items():
        object.__setattr__(provisional_binding, name, value)
    object.__setattr__(provisional_binding, "fact_fingerprint", "")
    binding = FrozenContextBindingCompileFact(
        **binding_values,
        fact_fingerprint=context_binding_compile_fact_fingerprint(
            provisional_binding
        ),
    )
    provisional = FrozenCanonicalCompileSnapshot.__new__(
        FrozenCanonicalCompileSnapshot
    )
    object.__setattr__(provisional, "canonical_input", canonical_input)
    object.__setattr__(provisional, "context_binding_fact", binding)
    object.__setattr__(provisional, "run_permission_snapshot", permission)
    object.__setattr__(provisional, "plan_workflow_fact", None)
    object.__setattr__(provisional, "plan_handoff_fact", None)
    object.__setattr__(provisional, "approved_plan_materialization_fact", None)
    freshness = build_tool_observation_freshness_fact(
        session_id=canonical_input.identity.session_id,
        workspace_id="workspace:test",
        current_turn_id=canonical_input.identity.turn_id,
        current_scope_kind=canonical_input.identity.conversation_scope_kind,
        scope_subagent_task_id=canonical_input.identity.scope_subagent_task_id,
        current_initial_entry_sequence=1,
        immediate_predecessor_turn_id=None,
    )
    object.__setattr__(provisional, "previous_turn_outcome_fact", None)
    object.__setattr__(provisional, "tool_observation_freshness_fact", freshness)
    object.__setattr__(provisional, "canonical_read_cut_fingerprint", "")
    return FrozenCanonicalCompileSnapshot(
        canonical_input=canonical_input,
        context_binding_fact=binding,
        run_permission_snapshot=permission,
        plan_workflow_fact=None,
        plan_handoff_fact=None,
        approved_plan_materialization_fact=None,
        previous_turn_outcome_fact=None,
        tool_observation_freshness_fact=freshness,
        canonical_read_cut_fingerprint=canonical_compile_snapshot_fingerprint(
            provisional
        ),
    )


class StructuredToolPort:
    def __init__(
        self,
        delegate: object,
        *,
        tool_names: tuple[str, ...] = ("terminal", "test_tool"),
    ) -> None:
        self.delegate = delegate
        self._authority = object()
        self._active: set[str] = set()
        specs = tuple(
            FrozenToolSpec(
                name=name,
                description=f"Test tool {name}",
                parameters=freeze_json(
                    {"type": "object", "additionalProperties": True}
                ),
                descriptor_fingerprint=context_fingerprint(
                    "test-tool-descriptor:v1", name
                ),
            )
            for name in sorted(tool_names)
        )
        self._surfaces = {
            scope: FrozenModelToolSurface(
                scope,
                specs,
                model_tool_surface_fingerprint(scope, specs),
            )
            for scope in ModelInputScopeKind
        }

    def snapshot_tool_surface(
        self,
        *,
        conversation_scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
    ) -> PreparedKernelToolSurface:
        surface = self._surfaces[conversation_scope_kind]
        bindings = tuple(
            PreparedToolExecutionBinding(
                tool_name=item.name,
                descriptor_fingerprint=item.descriptor_fingerprint,
                executor_binding_fingerprint=context_fingerprint(
                    "test-tool-binding:v1", item.name
                ),
                execution_policy=BuiltinExecutionPolicyRef(
                    tool_name=item.name,
                    catalog_entry_fingerprint=context_fingerprint(
                        "test-tool-catalog:v1", item.name
                    ),
                    policy_fingerprint=context_fingerprint(
                        "builtin-execution-policy-ref:v1",
                        {
                            "tool_name": item.name,
                            "catalog_entry_fingerprint": context_fingerprint(
                                "test-tool-catalog:v1", item.name
                            ),
                        },
                    ),
                ),
            )
            for item in surface.tool_specs
        )
        execution_fingerprint = tool_execution_surface_fingerprint(
            owner_epoch=1,
            surface_generation=1,
            semantic_surface_fingerprint=surface.surface_fingerprint,
            bindings=bindings,
        )
        access = ProcessLocalToolSurfaceAccess(
            owner_epoch=1,
            surface_generation=1,
            conversation_scope_kind=conversation_scope_kind,
            scope_subagent_task_id=scope_subagent_task_id,
            semantic_surface_fingerprint=surface.surface_fingerprint,
            execution_surface_fingerprint=execution_fingerprint,
            _authority=self._authority,
        )
        return PreparedKernelToolSurface(
            model_surface=surface,
            execution_bindings=bindings,
            execution_surface_fingerprint=execution_fingerprint,
            access=access,
        )

    def borrow_tool_surface(
        self, prepared: PreparedKernelToolSurface
    ) -> ProcessLocalToolSurfaceBorrow:
        borrow_id = f"test-borrow:{len(self._active) + 1}"
        self._active.add(borrow_id)

        def validate(
            borrow: ProcessLocalToolSurfaceBorrow, tool_name: str
        ) -> PreparedToolExecutionBinding:
            if borrow.borrow_id not in self._active:
                raise RuntimeError("test tool surface borrow is inactive")
            for item in prepared.execution_bindings:
                if item.tool_name == tool_name:
                    return item
            raise RuntimeError("test tool was not advertised")

        def release(borrow: ProcessLocalToolSurfaceBorrow) -> None:
            self._active.discard(borrow.borrow_id)

        return ProcessLocalToolSurfaceBorrow(
            prepared=prepared,
            borrow_id=borrow_id,
            _authority=self._authority,
            _validate=validate,
            _release=release,
        )

    def validate_tool_surface_borrow(
        self,
        borrow: ProcessLocalToolSurfaceBorrow,
        prepared: PreparedKernelToolSurface,
    ) -> None:
        if (
            borrow._closed
            or borrow.borrow_id not in self._active
            or not borrow.exactly_joins(prepared)
        ):
            raise RuntimeError("test tool surface borrow is inactive")
        if prepared.model_surface.tool_specs:
            borrow.binding_fingerprint(prepared.model_surface.tool_specs[0].name)

    async def authorize(self, **kwargs: object):
        kwargs.pop("surface_borrow")
        permission = kwargs.pop("permission_snapshot")
        if not isinstance(permission, FrozenRunPermissionSnapshot):
            raise TypeError("test tool authorization lacks a permission snapshot")
        return await self.delegate.authorize(**kwargs)

    async def request_confirmation(self, **kwargs: object):
        permission = kwargs.pop("permission_snapshot")
        if not isinstance(permission, FrozenRunPermissionSnapshot):
            raise TypeError("test confirmation lacks a permission snapshot")
        return await self.delegate.request_confirmation(**kwargs)

    async def invoke(self, **kwargs: object):
        return await self.delegate.invoke(**kwargs)

    async def settle_process_local_effect(self, *args: object) -> None:
        method = getattr(self.delegate, "settle_process_local_effect", None)
        if method is not None:
            await method(*args)


def direct_tool_invocation_context(
    port: DirectKernelToolPort,
    *,
    session_id: str,
    tool_name: str,
    tool_call_id: str,
    attempt_id: str,
    turn_id: str,
    assistant_entry_id: str,
    workspace_id: str = "workspace:test",
    conversation_scope_kind: ModelInputScopeKind = ModelInputScopeKind.ROOT,
    scope_subagent_task_id: str | None = None,
    permission_snapshot: FrozenRunPermissionSnapshot | None = None,
) -> tuple[ProcessLocalToolSurfaceBorrow, KernelToolInvocationContext]:
    """Acquire the same narrow surface authority used by the runner."""

    prepared = port.snapshot_tool_surface(
        conversation_scope_kind=conversation_scope_kind,
        scope_subagent_task_id=scope_subagent_task_id,
    )
    borrow = port.borrow_tool_surface(prepared)
    try:
        permission = permission_snapshot or build_run_permission_snapshot(
            snapshot_id=f"permission:{turn_id}",
            requested_mode=DEFAULT_PERMISSION_MODE,
            effective_mode=DEFAULT_PERMISSION_MODE,
            admission_source=RunPermissionAdmissionSource.USER_SUBMISSION,
        )
        binding_fingerprint = borrow.binding_fingerprint(tool_name)
        return borrow, KernelToolInvocationContext(
            session_id=session_id,
            workspace_id=workspace_id,
            turn_id=turn_id,
            assistant_entry_id=assistant_entry_id,
            tool_call_id=tool_call_id,
            attempt_id=attempt_id,
            result_entry_id=f"result:{attempt_id}",
            conversation_scope_kind=conversation_scope_kind.value,
            scope_subagent_task_id=scope_subagent_task_id,
            host_owner_epoch=1,
            authorization_reference="test:authorized",
            permission_snapshot_fingerprint=permission.snapshot_fingerprint,
            attempt_permission_snapshot_fingerprint=(
                permission.snapshot_fingerprint
            ),
            tool_surface_fingerprint=prepared.model_surface.surface_fingerprint,
            executor_binding_fingerprint=binding_fingerprint,
            surface_borrow=borrow,
        )
    except BaseException:
        borrow.close()
        raise


async def invoke_direct_tool(
    port: DirectKernelToolPort,
    *,
    session_id: str,
    tool_name: str,
    arguments: dict[str, object],
    tool_call_id: str,
    attempt_id: str,
    turn_id: str,
    assistant_entry_id: str,
    workspace_id: str = "workspace:test",
    conversation_scope_kind: ModelInputScopeKind = ModelInputScopeKind.ROOT,
    scope_subagent_task_id: str | None = None,
    **kwargs: object,
):
    """Invoke through the same short-lived binding borrow as production."""

    borrow, invocation_context = direct_tool_invocation_context(
        port,
        session_id=session_id,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        attempt_id=attempt_id,
        turn_id=turn_id,
        assistant_entry_id=assistant_entry_id,
        workspace_id=workspace_id,
        conversation_scope_kind=conversation_scope_kind,
        scope_subagent_task_id=scope_subagent_task_id,
    )
    try:
        return await port.invoke(
            tool_name=tool_name,
            arguments=arguments,
            tool_call_id=tool_call_id,
            attempt_id=attempt_id,
            turn_id=turn_id,
            assistant_entry_id=assistant_entry_id,
            invocation_context=invocation_context,
            **kwargs,
        )
    finally:
        borrow.close()


async def authorize_direct_tool(
    port: DirectKernelToolPort,
    *,
    session_id: str,
    tool_name: str,
    arguments: dict[str, object],
    tool_call_id: str,
    turn_id: str,
    assistant_entry_id: str,
    conversation_scope_kind: ModelInputScopeKind = ModelInputScopeKind.ROOT,
    scope_subagent_task_id: str | None = None,
    permission_snapshot: FrozenRunPermissionSnapshot | None = None,
):
    """Authorize under a formally acquired immutable surface borrow."""

    prepared = port.snapshot_tool_surface(
        conversation_scope_kind=conversation_scope_kind,
        scope_subagent_task_id=scope_subagent_task_id,
    )
    borrow = port.borrow_tool_surface(prepared)
    try:
        permission = permission_snapshot or build_run_permission_snapshot(
            snapshot_id=f"permission:{turn_id}",
            requested_mode=DEFAULT_PERMISSION_MODE,
            effective_mode=DEFAULT_PERMISSION_MODE,
            admission_source=RunPermissionAdmissionSource.USER_SUBMISSION,
        )
        return await port.authorize(
            tool_name=tool_name,
            arguments=arguments,
            tool_call_id=tool_call_id,
            turn_id=turn_id,
            assistant_entry_id=assistant_entry_id,
            surface_borrow=borrow,
            permission_snapshot=permission,
        )
    finally:
        borrow.close()


def _candidate(
    *,
    kind: ContextSourceKind,
    version: str,
    channel: ContextChannel,
    trust: ContextTrustClass,
    budget: ContextBudgetClass,
    placement: int,
    degradation: int,
    variants: tuple[tuple[ContextRenderMode, str], ...],
) -> ContextSourceCandidate:
    lifecycle = {
        ContextSourceKind.BASE_SYSTEM: ContextSourceLifecycle.EPOCH_ROOT,
        ContextSourceKind.RUNTIME_ENVIRONMENT: ContextSourceLifecycle.SNAPSHOT_ON_CHANGE,
        ContextSourceKind.RUNTIME_CLOCK: ContextSourceLifecycle.CALL_APPEND,
        ContextSourceKind.RUN_PERMISSION: ContextSourceLifecycle.TURN_APPEND,
        ContextSourceKind.PLAN_HANDOFF: ContextSourceLifecycle.ONE_SHOT,
        ContextSourceKind.PLAN_WORKFLOW: ContextSourceLifecycle.SNAPSHOT_ON_CHANGE,
        ContextSourceKind.CAPABILITY_CATALOG: ContextSourceLifecycle.SNAPSHOT_ON_CHANGE,
        ContextSourceKind.MCP_CATALOG: ContextSourceLifecycle.SNAPSHOT_ON_CHANGE,
        ContextSourceKind.ACTIVE_SKILL: ContextSourceLifecycle.ACTIVATION_SNAPSHOT,
        ContextSourceKind.PREVIOUS_TURN_OUTCOME: ContextSourceLifecycle.TURN_APPEND,
        ContextSourceKind.TOOL_OBSERVATION_FRESHNESS: ContextSourceLifecycle.TURN_APPEND,
    }[kind]
    modes = tuple(mode for mode, _text in variants)
    contract = context_fingerprint(
        "context-source-contract:v1",
        {
            "kind": kind.value,
            "version": version,
            "channel": channel.value,
            "trust": trust.value,
            "budget": budget.value,
            "placement": placement,
            "degradation": degradation,
            "modes": tuple(mode.value for mode in modes),
            "lifecycle": lifecycle.value,
        },
    )
    rendered = tuple(
        ContextRenderVariant(
            mode,
            text,
            len(text.encode("utf-8")),
            context_fingerprint(
                "context-render-variant:v1", {"mode": mode.value, "text": text}
            ),
        )
        for mode, text in variants
    )
    instance = f"context-source:{kind.value.lower()}"
    semantic = context_fingerprint(
        "context-source-candidate:v1",
        {
            "source_kind": kind.value,
            "source_instance_id": instance,
            "source_contract_fingerprint": contract,
            "variants": tuple(item.semantic_fingerprint for item in rendered),
        },
    )
    return ContextSourceCandidate(
        kind,
        instance,
        version,
        contract,
        semantic,
        channel,
        trust,
        budget,
        placement,
        degradation,
        rendered,
        lifecycle,
        semantic,
    )


__all__ = [
    "ScriptedKernelModel",
    "StaticContextSourceCollector",
    "StructuredToolPort",
    "direct_tool_invocation_context",
    "static_canonical_compile_facts",
]
