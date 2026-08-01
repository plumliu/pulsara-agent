from tests.support.runtime_owner import runtime_session_for_test

from tests.support.runtime_owner import build_test_agent_runtime


import asyncio
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import AsyncIterator

import pytest

from tests.support.runtime_session import in_memory_runtime_session

from tests.support.raw_provider import (
    RawProviderTextBlockEnd,
    RawProviderTextBlockStart,
    RawProviderTextDelta,
    RawProviderToolCallDelta,
    RawProviderToolCallEnd,
    RawProviderToolCallStart,
)

from pulsara_agent.capability import (
    LocalSkillCapabilityProvider,
    LocalSkillProvider,
    SkillHealthResolver,
)
from pulsara_agent.capability.descriptor import (
    CapabilityAdvertisePolicy,
    CapabilityAvailability,
    CapabilityDescriptor,
    CapabilityProviderKind,
)
from pulsara_agent.ports.artifact import ToolArtifactMode
from pulsara_agent.capability.builtin_provider import BuiltinToolCapabilityProvider
from pulsara_agent.capability.builtin_catalog import (
    builtin_tool_catalog,
    builtin_tool_catalog_entry,
)
from pulsara_agent.capability.exposure import build_exposure_plan
from pulsara_agent.capability.provider import (
    CapabilityDescriptorSnapshotOutput,
    CapabilityProjectionOutput,
)
from pulsara_agent.capability.render import render_active_skill_prompt
from pulsara_agent.capability.result_contracts import (
    result_render_contract_for_tool,
)
from pulsara_agent.capability.result_semantics import (
    FrozenToolResultSemanticsRuntimeInput,
    build_terminal_payload_timing,
)
from pulsara_agent.capability.registry import CapabilityRegistry
from pulsara_agent.capability.runtime import CapabilityRuntime
from pulsara_agent.capability.types import (
    ActiveSkillInjection,
    CapabilityExecutionSurfaceSnapshotContext,
    CapabilityProjectionResolveContext,
)
from pulsara_agent.event import (
    AgentEvent,
    CapabilityExposureResolvedEvent,
    CapabilityGateDecisionEvent,
    EventContext,
    PlanModeEnteredEvent,
    ToolResultEndEvent,
)
from pulsara_agent.llm import LLMRuntime
from tests.support import (
    resume_test_agent_after_approval,
    run_agent_task,
    test_llm_config,
)
from pulsara_agent.llm.registry import LLMTransportRegistry
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.memory.artifacts.archive import InMemoryArchiveStore
from pulsara_agent.message import ToolResultState
from pulsara_agent.runtime.agent import AgentRuntime
from pulsara_agent.runtime.approval import ApprovalResolution, ToolApprovalDecision
from pulsara_agent.runtime.state import LoopStatus
from pulsara_agent.runtime.permission import (
    AllowAllPermissionGate,
    ApprovalPolicy,
    EffectivePermissionPolicy,
    PermissionDecision,
    PermissionDecisionKind,
    PermissionProfile,
    PolicyPermissionGate,
    TerminalAccess,
    preset_to_policy,
)
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.capability import build_capability_resolve_basis
from pulsara_agent.primitives.run_entry import CapabilityExposureOwnerFact
from pulsara_agent.primitives.tool_result import (
    TerminalCommandDomainSubmissionFact,
    TerminalProcessInventoryDomainSubmissionFact,
    ToolResultRenderVariantCode,
)
from pulsara_agent.host.run_boundary import (
    CapabilityResolveBasis,
    derive_continuation_basis,
)
from pulsara_agent.capability.tool_action import builtin_tool_action_policy
from pulsara_agent.runtime.tool_artifacts import (
    ToolResultArtifactOptions,
    ToolResultArtifactService,
    artifact_processing_policy_for_descriptor,
)
from tests.support.artifacts import FakeToolResultArtifactIndex
from pulsara_agent.ports.tool_execution import ToolCall, ToolExecutionResult
from pulsara_agent.tools.registry import ToolRegistry, build_tool_binding_contract


@dataclass(slots=True)
class DummyTool:
    name: str
    is_read_only: bool
    is_concurrency_safe: bool
    description: str = "dummy"
    parameters: dict = field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )
    calls: list[str] = field(default_factory=list)

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        self.calls.append(call.id)
        timing = build_terminal_payload_timing(
            observed_at_utc="2026-01-01T00:00:00Z",
            duration_seconds=0,
            freshness="current_tool_observation",
            clock_source="tool_runtime_metadata",
        )
        semantics_input = None
        terminal_timing = None
        if self.name == "terminal":
            semantics_input = FrozenToolResultSemanticsRuntimeInput(
                semantics_input_kind=ToolResultRenderVariantCode.TERMINAL_COMMAND_EXECUTED,
                domain_submission=TerminalCommandDomainSubmissionFact(
                    command=str(call.arguments.get("command") or "test command"),
                    status="success",
                    exit_code=0,
                    cwd="/test",
                    timed_out=False,
                    output_truncated=False,
                    error=None,
                    process_id=None,
                    yielded_to_background=False,
                    terminal_session_id="test",
                    backend_type="test",
                    io_mode=None,
                    stdin_closed=None,
                    policy_code=None,
                    duration_seconds=0,
                ),
            )
            terminal_timing = timing
        elif self.name == "terminal_process":
            semantics_input = FrozenToolResultSemanticsRuntimeInput(
                semantics_input_kind=ToolResultRenderVariantCode.TERMINAL_PROCESS_INVENTORY,
                domain_submission=TerminalProcessInventoryDomainSubmissionFact(
                    status="success",
                    live_process_count=0,
                    finished_process_count=0,
                    process_summaries=(),
                    omitted_process_count=0,
                    summaries_truncated=False,
                ),
            )
            terminal_timing = timing
        return ToolExecutionResult(
            call_id=call.id,
            tool_name=call.name,
            status=ToolResultState.SUCCESS,
            output=f"ran:{call.id}",
            semantics_input=semantics_input,
            terminal_payload_timing=terminal_timing,
        )


@dataclass(frozen=True, slots=True)
class StaticCapabilityProvider:
    descriptors: tuple[CapabilityDescriptor, ...]
    active_injections: tuple[ActiveSkillInjection, ...] = ()
    provider_id: str = "static-test"

    def snapshot_descriptors(
        self, context: CapabilityExecutionSurfaceSnapshotContext
    ) -> CapabilityDescriptorSnapshotOutput:
        del context
        return CapabilityDescriptorSnapshotOutput(descriptors=self.descriptors)

    def resolve_projection(
        self,
        context: CapabilityProjectionResolveContext,
        *,
        execution_surface,
    ) -> CapabilityProjectionOutput:
        del context, execution_surface
        rendered = render_active_skill_prompt(self.active_injections)
        return CapabilityProjectionOutput(
            active_injections=self.active_injections,
            active_skill_prompt=rendered.text,
            active_skill_rendered=rendered,
            diagnostics=rendered.diagnostics,
        )


class CountingPermissionGate:
    def __init__(self) -> None:
        self.call_batches: list[list[str]] = []

    async def evaluate(self, calls: list[ToolCall]) -> PermissionDecision:
        self.call_batches.append([call.id for call in calls])
        return PermissionDecision.allow()


def _gate_decisions(agent: AgentRuntime) -> list[CapabilityGateDecisionEvent]:
    return [
        event
        for event in runtime_session_for_test(agent).event_log.iter()
        if isinstance(event, CapabilityGateDecisionEvent)
    ]


def _gate_decisions_by_call(
    agent: AgentRuntime,
) -> dict[str, CapabilityGateDecisionEvent]:
    return {event.tool_call_id: event for event in _gate_decisions(agent)}


def _descriptor(
    name: str,
    *,
    advertise_policy: CapabilityAdvertisePolicy = CapabilityAdvertisePolicy.DIRECT,
    availability: CapabilityAvailability = CapabilityAvailability.AVAILABLE,
    permission_category: str | None = None,
) -> CapabilityDescriptor:
    try:
        descriptor = builtin_tool_catalog_entry(name).descriptor
    except KeyError:
        descriptor = None
    if descriptor is not None:
        if advertise_policy is not CapabilityAdvertisePolicy.DIRECT:
            raise ValueError("built-in advertise policy must come from the catalog")
        if availability is not CapabilityAvailability.AVAILABLE:
            raise ValueError("built-in availability must come from the catalog")
        if (
            permission_category is not None
            and permission_category != descriptor.permission_category
        ):
            raise ValueError("built-in permission category must come from the catalog")
        return descriptor
    return CapabilityDescriptor(
        id=f"builtin:{name}",
        name=name,
        description=f"{name} tool",
        input_schema={"type": "object", "properties": {}},
        namespace=None,
        provider_kind=CapabilityProviderKind.BUILTIN,
        provider_id="builtin",
        is_model_callable=True,
        is_read_only=True,
        is_concurrency_safe=True,
        result_render_contract=result_render_contract_for_tool(name),
        long_horizon_policy=builtin_tool_action_policy(name),
        permission_category=permission_category or "general",
        advertise_policy=advertise_policy,
        availability=availability,
    )


def _exposure_for_descriptors(
    *descriptors: CapabilityDescriptor,
    bound_tool_names: frozenset[str] | None = None,
):
    registry = CapabilityRegistry()
    for descriptor in descriptors:
        registry.register(descriptor)
    return build_exposure_plan(
        registry.snapshot(),
        provider_output=CapabilityProjectionOutput(),
        bound_tool_names=bound_tool_names,
    )


def _runtime_for_descriptors(*descriptors: CapabilityDescriptor) -> CapabilityRuntime:
    return CapabilityRuntime(providers=(StaticCapabilityProvider(tuple(descriptors)),))


def _runtime_for_provider(provider: StaticCapabilityProvider) -> CapabilityRuntime:
    return CapabilityRuntime(providers=(provider,))


def _install_test_registry(agent: AgentRuntime, registry: ToolRegistry) -> None:
    descriptors_by_name = {
        entry.name: entry.descriptor
        for entry in builtin_tool_catalog()
        if entry.name in registry.names()
    }
    descriptors_by_name.update(
        {
            descriptor.name: descriptor
            for provider in agent.capability_runtime.providers
            for descriptor in getattr(provider, "descriptors", ())
            if descriptor.name in registry.names()
        }
    )
    agent.tool_executor.registry = registry
    agent.tool_executor.artifact_policies = {
        descriptor.name: artifact_processing_policy_for_descriptor(
            descriptor=descriptor,
            tool_call=ToolCall(
                id=f"call:artifact-policy:{descriptor.name}",
                name=descriptor.name,
            ),
            options=runtime_session_for_test(agent).artifact_service.options,
        )
        for descriptor in descriptors_by_name.values()
    }


def _firecrawl_active_injection(tmp_path: Path) -> ActiveSkillInjection:
    skill_path = tmp_path / ".agents/skills/firecrawl-search/SKILL.md"
    return ActiveSkillInjection(
        name="firecrawl-search",
        path=skill_path,
        base_dir=skill_path.parent,
        location=".agents/skills/firecrawl-search/SKILL.md",
        content="# Firecrawl Search",
        reason="explicit_user_mention",
        suggested_tools=("terminal",),
        required_binaries=("firecrawl",),
        optional_binaries=("npx",),
        external_services=("firecrawl",),
        network_required=True,
        auth_required="required",
        cli_usage_kind="read",
    )


def test_child_profile_filter_preserves_split_provider_protocol_shapes() -> None:
    from types import SimpleNamespace

    from pulsara_agent.capability.runtime import CapabilityRuntime
    from pulsara_agent.capability.profile_runtime import (
        profile_filtered_capability_runtime,
    )

    class ExecutionOnly:
        provider_id = "execution-only"

        def snapshot_descriptors(self, _context):
            return CapabilityDescriptorSnapshotOutput()

    class ProjectionOnly:
        provider_id = "projection-only"

        def resolve_projection(self, _context, *, execution_surface):
            del execution_surface
            return CapabilityProjectionOutput()

    child = profile_filtered_capability_runtime(
        CapabilityRuntime(providers=(ExecutionOnly(), ProjectionOnly())),
        SimpleNamespace(
            allowed_tool_names=("report_agent_result",),
            allowed_descriptor_ids=(),
            allowed_skill_names=("example-skill",),
        ),
    )

    assert len(child.providers) == 2
    execution, projection = child.providers
    assert hasattr(execution, "snapshot_descriptors")
    assert not hasattr(execution, "resolve_projection")
    assert hasattr(projection, "resolve_projection")
    assert not hasattr(projection, "snapshot_descriptors")


def test_continuation_narrows_revoked_active_skill_with_unchanged_mcp(
    tmp_path,
) -> None:
    descriptor = _descriptor("alpha")
    tool = DummyTool(name="alpha", is_read_only=True, is_concurrency_safe=True)
    registry = ToolRegistry()
    registry.register(
        tool,
        binding_contract=build_tool_binding_contract(
            tool_name="alpha",
            origin="builtin",
            contract_id="test.alpha",
            contract_version="v1",
        ),
    )
    active = _firecrawl_active_injection(tmp_path)
    initial_runtime = _runtime_for_provider(
        StaticCapabilityProvider((descriptor,), active_injections=(active,))
    )
    current_runtime = _runtime_for_provider(StaticCapabilityProvider((descriptor,)))
    archive = InMemoryArchiveStore()
    initial_owner = CapabilityExposureOwnerFact(
        owner_kind="host_boundary",
        owner_id="boundary:initial",
        host_boundary_kind="pre_run",
        runtime_session_id="runtime:1",
        run_id="run:1",
    )
    initial_surface = initial_runtime.freeze_execution_surface(
        CapabilityExecutionSurfaceSnapshotContext(
            workspace_root=tmp_path,
            workspace_kind="project",
            available_tool_names=frozenset({"alpha"}),
            mcp_installation_id="mcp:same",
        ),
        tool_registry=registry,
        archive=archive,
        runtime_session_id="runtime:1",
        owner_id=initial_owner.owner_id,
    )
    initial_basis_fact = build_capability_resolve_basis(
        basis_id="basis:initial",
        basis_kind="initial",
        source_basis_id=None,
        source_basis_fingerprint=None,
        owner=initial_owner,
        workspace_identity_fingerprint="workspace:1",
        memory_domain_id="memory:1",
        permission_snapshot_id="permission:1",
        plan_active=False,
        active_skill_names=(active.name,),
        user_intent_fingerprint="intent:1",
        prior_transcript_fingerprint="transcript:1",
        mcp_installation_id="mcp:same",
        execution_surface_identity=initial_surface.identity,
    )
    raw_basis = CapabilityResolveBasis(
        fact=initial_basis_fact,
        user_input="use firecrawl",
        prior_messages=(),
        active_skill_names=frozenset({active.name}),
        workspace_root=tmp_path,
        memory_domain_id="memory:1",
    )
    context = CapabilityProjectionResolveContext(
        workspace_root=tmp_path,
        workspace_kind="project",
        memory_domain=None,
        user_input=raw_basis.user_input,
        active_skill_names=raw_basis.active_skill_names,
    )
    initial = initial_runtime.resolve_exposure_projection(
        context,
        frozen_surface=initial_surface,
        archive=archive,
        runtime_session_id="runtime:1",
        owner=initial_owner,
        resolve_basis=initial_basis_fact,
        exposure_id="exposure:initial",
    )
    assert initial.fact.semantic.active_skill_projection.rendered_entry_count == 1

    resume_owner = CapabilityExposureOwnerFact(
        owner_kind="host_boundary",
        owner_id="boundary:resume",
        host_boundary_kind="pre_interaction_resume",
        runtime_session_id="runtime:1",
        run_id="run:1",
    )
    current_surface = current_runtime.freeze_execution_surface(
        CapabilityExecutionSurfaceSnapshotContext(
            workspace_root=tmp_path,
            workspace_kind="project",
            available_tool_names=frozenset({"alpha"}),
            mcp_installation_id="mcp:same",
        ),
        tool_registry=registry,
        archive=archive,
        runtime_session_id="runtime:1",
        owner_id=resume_owner.owner_id,
    )
    continuation_basis = derive_continuation_basis(
        raw_basis,
        continuation_owner=resume_owner,
        current_execution_surface=current_surface,
        basis_id="basis:resume",
    )
    continuation = current_runtime.resolve_continuation_exposure(
        context,
        frozen_surface=current_surface,
        original_plan=initial.plan,
        original_fact=initial.fact,
        archive=archive,
        runtime_session_id="runtime:1",
        owner=resume_owner,
        resolve_basis=continuation_basis.fact,
        exposure_id="exposure:resume",
    )
    assert continuation.fact.resolution_kind == "continuation_narrowed"
    assert continuation.fact.semantic.active_skill_projection.rendered_entry_count == 0
    assert continuation.plan.active_skill_prompt is None
    assert continuation.fact.direct_names == initial.fact.direct_names


def test_projection_semantic_fingerprint_ignores_exposure_scoped_artifact_ids(
    tmp_path,
) -> None:
    descriptor = _descriptor("alpha")
    provider = StaticCapabilityProvider(
        (descriptor,),
        active_injections=(_firecrawl_active_injection(tmp_path),),
    )
    runtime = _runtime_for_provider(provider)
    registry = ToolRegistry()
    registry.register(DummyTool("alpha", is_read_only=True, is_concurrency_safe=True))
    registry.bind_contract(
        build_tool_binding_contract(
            tool_name="alpha",
            origin="custom",
            contract_id="test.alpha",
            contract_version="v1",
        )
    )
    archive = InMemoryArchiveStore()

    def resolve(owner_id: str, exposure_id: str):
        owner = CapabilityExposureOwnerFact(
            owner_kind="host_boundary",
            owner_id=owner_id,
            host_boundary_kind="pre_run",
            runtime_session_id="runtime:semantic",
            run_id="run:semantic",
        )
        surface = runtime.freeze_execution_surface(
            CapabilityExecutionSurfaceSnapshotContext(
                workspace_root=tmp_path,
                workspace_kind="project",
                available_tool_names=frozenset({"alpha"}),
                mcp_installation_id="mcp:same",
            ),
            tool_registry=registry,
            archive=archive,
            runtime_session_id="runtime:semantic",
            owner_id=owner_id,
        )
        basis = build_capability_resolve_basis(
            basis_id=f"basis:{owner_id}",
            basis_kind="initial",
            source_basis_id=None,
            source_basis_fingerprint=None,
            owner=owner,
            workspace_identity_fingerprint="workspace:same",
            memory_domain_id="memory:same",
            permission_snapshot_id="permission:same",
            plan_active=False,
            active_skill_names=("firecrawl",),
            user_intent_fingerprint="intent:same",
            prior_transcript_fingerprint="transcript:same",
            mcp_installation_id="mcp:same",
            execution_surface_identity=surface.identity,
        )
        return runtime.resolve_exposure_projection(
            CapabilityProjectionResolveContext(
                workspace_root=tmp_path,
                workspace_kind="project",
                memory_domain=None,
                user_input="use firecrawl",
                active_skill_names=frozenset({"firecrawl"}),
            ),
            frozen_surface=surface,
            archive=archive,
            runtime_session_id="runtime:semantic",
            owner=owner,
            resolve_basis=basis,
            exposure_id=exposure_id,
        )

    first = resolve("boundary:first", "exposure:first")
    second = resolve("boundary:second", "exposure:second")
    first_projection = first.fact.semantic.active_skill_projection
    second_projection = second.fact.semantic.active_skill_projection
    assert first.plan.active_skill_prompt == second.plan.active_skill_prompt
    assert (
        first_projection.rendered_prompt_artifact_id
        != second_projection.rendered_prompt_artifact_id
    )
    assert (
        first.fact.exposure_semantic_fingerprint
        == second.fact.exposure_semantic_fingerprint
    )


def test_builtin_provider_uses_explicit_descriptor_truth_for_bound_core_tools() -> None:
    output = BuiltinToolCapabilityProvider().snapshot_descriptors(
        CapabilityExecutionSurfaceSnapshotContext(
            workspace_root=Path("."),
            workspace_kind="transient",
            available_tool_names=frozenset(
                {"artifact_read", "terminal_process", "exit_plan"}
            ),
            mcp_installation_id="mcp_installation:empty",
        ),
    )
    descriptors = {descriptor.name: descriptor for descriptor in output.descriptors}

    assert descriptors["artifact_read"].artifact_mode is ToolArtifactMode.NEVER
    assert descriptors["artifact_read"].permission_category == "artifact_read"
    assert descriptors["terminal_process"].permission_category == "terminal"
    assert descriptors["terminal_process"].is_open_world is True
    assert descriptors["exit_plan"].provider_kind is CapabilityProviderKind.WORKFLOW
    assert descriptors["exit_plan"].permission_category == "plan_workflow"


def test_cli_route_has_no_provider_kind_or_typed_cli_tool() -> None:
    assert "CLI" not in CapabilityProviderKind.__members__
    assert "cli" not in {kind.value for kind in CapabilityProviderKind}

    registry = ToolRegistry()
    assert "CliCapabilityTool" not in {type(tool).__name__ for tool in registry.all()}


def test_agent_runtime_requires_explicit_capability_runtime(tmp_path) -> None:
    with pytest.raises(ValueError, match="requires an explicit CapabilityRuntime"):
        build_test_agent_runtime(  # type: ignore[arg-type]
            runtime_session=in_memory_runtime_session(tmp_path),
            llm_runtime=_llm_runtime(_ScriptedTransport([{"text": "done"}])),
            capability_runtime=None,
        )


def test_exposure_plan_separates_direct_deferred_hidden_unavailable_and_callable() -> (
    None
):
    exposure = _exposure_for_descriptors(
        _descriptor("direct"),
        _descriptor("deferred", advertise_policy=CapabilityAdvertisePolicy.DEFERRED),
        _descriptor("hidden", advertise_policy=CapabilityAdvertisePolicy.HIDDEN),
        _descriptor("down", availability=CapabilityAvailability.UNAVAILABLE),
    )

    assert [spec.name for spec in exposure.direct_tool_specs] == ["direct"]
    assert exposure.direct_names == frozenset({"direct"})
    assert exposure.deferred_names == frozenset({"deferred"})
    assert exposure.hidden_names == frozenset({"hidden", "down"})
    assert exposure.callable_names == frozenset({"direct"})


def test_exposure_plan_hides_direct_descriptor_without_execution_binding() -> None:
    registry = CapabilityRegistry()
    registry.register(_descriptor("ghost"))

    exposure = build_exposure_plan(
        registry.snapshot(),
        provider_output=CapabilityProjectionOutput(),
        bound_tool_names=frozenset({"real_tool"}),
    )

    assert exposure.direct_tool_specs == ()
    assert exposure.direct_names == frozenset()
    assert exposure.callable_names == frozenset()
    assert exposure.hidden_names == frozenset({"ghost"})
    assert [diagnostic.code for diagnostic in exposure.diagnostics] == [
        "capability_missing_execution_binding"
    ]


def test_exposure_plan_diagnoses_non_direct_descriptor_without_execution_binding() -> (
    None
):
    exposure = _exposure_for_descriptors(
        _descriptor(
            "ghost_deferred", advertise_policy=CapabilityAdvertisePolicy.DEFERRED
        ),
        bound_tool_names=frozenset(),
    )

    assert exposure.deferred_names == frozenset({"ghost_deferred"})
    assert exposure.callable_names == frozenset()
    assert [diagnostic.code for diagnostic in exposure.diagnostics] == [
        "capability_missing_execution_binding"
    ]


def test_capability_gate_denies_hidden_unavailable_and_not_callable_calls() -> None:
    hidden = _descriptor("hidden", advertise_policy=CapabilityAdvertisePolicy.HIDDEN)
    unavailable = _descriptor("down", availability=CapabilityAvailability.UNAVAILABLE)
    deferred = _descriptor(
        "deferred", advertise_policy=CapabilityAdvertisePolicy.DEFERRED
    )
    exposure = _exposure_for_descriptors(hidden, unavailable, deferred)
    gate = PolicyPermissionGate(
        EffectivePermissionPolicy(
            profile=PermissionProfile.TRUSTED_HOST,
            approval=ApprovalPolicy.NEVER,
            terminal=TerminalAccess.ALLOW,
        ),
        inner=AllowAllPermissionGate(),
    )

    hidden_decision = asyncio.run(
        gate.evaluate([ToolCall(id="call:hidden", name="hidden")], exposure=exposure)
    )
    unavailable_decision = asyncio.run(
        gate.evaluate([ToolCall(id="call:down", name="down")], exposure=exposure)
    )
    deferred_decision = asyncio.run(
        gate.evaluate(
            [ToolCall(id="call:deferred", name="deferred")], exposure=exposure
        )
    )

    assert hidden_decision.kind is PermissionDecisionKind.DENY
    assert hidden_decision.reason == "capability_hidden_in_current_exposure: hidden"
    assert unavailable_decision.kind is PermissionDecisionKind.DENY
    assert unavailable_decision.reason == "capability_unavailable: down"
    assert deferred_decision.kind is PermissionDecisionKind.DENY
    assert (
        deferred_decision.reason
        == "capability_not_callable_in_current_exposure: deferred"
    )


def test_capability_gate_preserves_terminal_process_observe_contract_and_terminal_off() -> (
    None
):
    terminal_process = builtin_tool_catalog_entry("terminal_process").descriptor
    exposure = _exposure_for_descriptors(terminal_process)
    observe = ToolCall(
        id="call:observe", name="terminal_process", arguments={"action": "list"}
    )

    ask_gate = PolicyPermissionGate(
        EffectivePermissionPolicy(
            profile=PermissionProfile.TRUSTED_HOST,
            approval=ApprovalPolicy.ON_REQUEST,
            terminal=TerminalAccess.ASK,
        ),
        inner=AllowAllPermissionGate(),
    )
    off_gate = PolicyPermissionGate(
        EffectivePermissionPolicy(
            profile=PermissionProfile.TRUSTED_HOST,
            approval=ApprovalPolicy.NEVER,
            terminal=TerminalAccess.OFF,
        ),
        inner=AllowAllPermissionGate(),
    )

    assert (
        asyncio.run(ask_gate.evaluate([observe], exposure=exposure)).kind
        is PermissionDecisionKind.ALLOW
    )
    off_decision = asyncio.run(off_gate.evaluate([observe], exposure=exposure))
    assert off_decision.kind is PermissionDecisionKind.DENY
    assert (
        off_decision.reason
        == "tool 'terminal_process' is not allowed by permission policy"
    )


def test_terminal_process_observe_gate_event_records_effective_category(
    tmp_path,
) -> None:
    descriptor = _descriptor(
        "terminal_process",
        permission_category="terminal",
    )
    transport = _ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:observe",
                        "name": "terminal_process",
                        "arguments": json.dumps({"action": "list"}),
                    }
                ]
            },
            {"text": "done"},
        ]
    )
    agent = build_test_agent_runtime(
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=_llm_runtime(transport),
        capability_runtime=_runtime_for_descriptors(descriptor),
        permission_policy=preset_to_policy(PermissionMode.ASK_PERMISSIONS),
    )
    registry = ToolRegistry()
    registry.register(
        DummyTool("terminal_process", is_read_only=False, is_concurrency_safe=True)
    )
    _install_test_registry(agent, registry)
    agent.tool_executor.artifact_policies = {
        "terminal_process": artifact_processing_policy_for_descriptor(
            descriptor=descriptor,
            tool_call=ToolCall(
                id="call:observe",
                name="terminal_process",
                arguments={"action": "list"},
            ),
            options=runtime_session_for_test(agent).artifact_service.options,
        )
    }

    result = asyncio.run(run_agent_task(agent, "list processes"))

    assert result.status is LoopStatus.FINISHED
    gate_decision = _gate_decisions(agent)[0]
    assert gate_decision.decision == "allow"
    assert gate_decision.permission_category == "terminal"
    assert gate_decision.effective_permission_category == "terminal_process_observe"
    assert gate_decision.effective_read_only is True


def test_wait_for_user_batch_suspension_reason_code(tmp_path) -> None:
    transport = _ScriptedTransport(
        [
            {
                "tool_calls": [
                    {"id": "call:read", "name": "read_file", "arguments": "{}"},
                    {"id": "call:terminal", "name": "terminal", "arguments": "{}"},
                ]
            }
        ]
    )
    agent = build_test_agent_runtime(
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=_llm_runtime(transport),
        capability_runtime=_runtime_for_descriptors(
            _descriptor("read_file", permission_category="filesystem_read"),
            _descriptor("terminal", permission_category="terminal"),
        ),
        permission_policy=preset_to_policy(PermissionMode.ASK_PERMISSIONS),
    )
    registry = ToolRegistry()
    registry.register(
        DummyTool("read_file", is_read_only=True, is_concurrency_safe=True)
    )
    registry.register(
        DummyTool("terminal", is_read_only=False, is_concurrency_safe=True)
    )
    _install_test_registry(agent, registry)

    result = asyncio.run(run_agent_task(agent, "read and terminal"))

    assert result.status is LoopStatus.WAITING_USER
    gate_decisions = _gate_decisions_by_call(agent)
    assert gate_decisions["call:terminal"].decision == "wait_for_user"
    assert gate_decisions["call:terminal"].reason_code == "permission_wait_for_user"
    assert gate_decisions["call:read"].decision == "wait_for_user"
    assert (
        gate_decisions["call:read"].reason_code
        == "permission_wait_for_user_batch_suspension"
    )
    assert gate_decisions["call:read"].reason_message == (
        "terminal access requires user confirmation by permission policy"
    )


def test_multiple_independent_wait_calls_keep_own_reason_code(tmp_path) -> None:
    transport = _ScriptedTransport(
        [
            {
                "tool_calls": [
                    {"id": "call:write", "name": "write_file", "arguments": "{}"},
                    {"id": "call:terminal", "name": "terminal", "arguments": "{}"},
                ]
            }
        ]
    )
    agent = build_test_agent_runtime(
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=_llm_runtime(transport),
        capability_runtime=_runtime_for_descriptors(
            _descriptor("write_file", permission_category="filesystem_write"),
            _descriptor("terminal", permission_category="terminal"),
        ),
        permission_policy=preset_to_policy(PermissionMode.ASK_PERMISSIONS),
    )
    registry = ToolRegistry()
    registry.register(
        DummyTool("write_file", is_read_only=False, is_concurrency_safe=True)
    )
    registry.register(
        DummyTool("terminal", is_read_only=False, is_concurrency_safe=True)
    )
    _install_test_registry(agent, registry)

    result = asyncio.run(run_agent_task(agent, "write and terminal"))

    assert result.status is LoopStatus.WAITING_USER
    gate_decisions = _gate_decisions_by_call(agent)
    assert gate_decisions["call:write"].reason_code == "permission_wait_for_user"
    assert (
        gate_decisions["call:write"].reason_message
        == "file write tool requires user confirmation by approval policy"
    )
    assert gate_decisions["call:terminal"].reason_code == "permission_wait_for_user"
    assert gate_decisions["call:terminal"].reason_message == (
        "terminal access requires user confirmation by permission policy"
    )


def test_permission_prepass_does_not_invoke_inner_gate_per_call(tmp_path) -> None:
    transport = _ScriptedTransport(
        [
            {
                "tool_calls": [
                    {"id": "call:a", "name": "a", "arguments": "{}"},
                    {"id": "call:b", "name": "b", "arguments": "{}"},
                ]
            },
            {"text": "done"},
        ]
    )
    inner_gate = CountingPermissionGate()
    agent = build_test_agent_runtime(
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=_llm_runtime(transport),
        capability_runtime=_runtime_for_descriptors(_descriptor("a"), _descriptor("b")),
        permission_gate=inner_gate,
    )
    registry = ToolRegistry()
    registry.register(DummyTool("a", is_read_only=True, is_concurrency_safe=True))
    registry.register(DummyTool("b", is_read_only=True, is_concurrency_safe=True))
    _install_test_registry(agent, registry)

    result = asyncio.run(run_agent_task(agent, "call two tools"))

    assert result.status is LoopStatus.FINISHED
    assert inner_gate.call_batches == [["call:a", "call:b"]]


def test_workflow_control_emits_gate_decision_before_execution_and_suppresses_siblings(
    tmp_path,
) -> None:
    transport = _ScriptedTransport(
        [
            {
                "tool_calls": [
                    {"id": "call:plan", "name": "enter_plan", "arguments": "{}"},
                    {"id": "call:noop", "name": "noop", "arguments": "{}"},
                ]
            },
            {"text": "done"},
        ]
    )
    agent = build_test_agent_runtime(
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=_llm_runtime(transport),
        capability_runtime=_runtime_for_descriptors(
            _descriptor("enter_plan", permission_category="plan_workflow"),
            _descriptor("noop"),
        ),
    )
    registry = ToolRegistry()
    registry.register(
        DummyTool("enter_plan", is_read_only=False, is_concurrency_safe=True)
    )
    registry.register(DummyTool("noop", is_read_only=True, is_concurrency_safe=True))
    _install_test_registry(agent, registry)

    result = asyncio.run(run_agent_task(agent, "enter plan and noop"))

    assert result.status is LoopStatus.FINISHED
    gate_decisions = _gate_decisions_by_call(agent)
    assert gate_decisions["call:plan"].decision == "allow"
    assert gate_decisions["call:plan"].permission_category == "plan_workflow"
    assert gate_decisions["call:noop"].decision == "deny"
    assert gate_decisions["call:noop"].result_state is ToolResultState.DENIED
    assert (
        gate_decisions["call:noop"].reason_code == "workflow_control_batch_suppressed"
    )
    assert gate_decisions["call:noop"].reason_message == (
        "tool call suppressed because workflow control tool 'enter_plan' owns this tool batch"
    )


def test_hardline_terminal_reason_codes_are_stable_and_specific(tmp_path) -> None:
    transport = _ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:terminal",
                        "name": "terminal",
                        "arguments": json.dumps({"command": "rm -rf /"}),
                    },
                    {
                        "id": "call:terminal-process",
                        "name": "terminal_process",
                        "arguments": json.dumps(
                            {"action": "write", "data": "rm -rf /"}
                        ),
                    },
                ]
            },
            {"text": "done"},
        ]
    )
    agent = build_test_agent_runtime(
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=_llm_runtime(transport),
        capability_runtime=_runtime_for_descriptors(
            _descriptor("terminal", permission_category="terminal"),
            _descriptor("terminal_process", permission_category="terminal"),
        ),
    )
    registry = ToolRegistry()
    registry.register(
        DummyTool("terminal", is_read_only=False, is_concurrency_safe=True)
    )
    registry.register(
        DummyTool("terminal_process", is_read_only=False, is_concurrency_safe=True)
    )
    _install_test_registry(agent, registry)

    result = asyncio.run(run_agent_task(agent, "dangerous terminal calls"))

    assert result.status is LoopStatus.FINISHED
    gate_decisions = _gate_decisions_by_call(agent)
    assert gate_decisions["call:terminal"].decision == "deny"
    assert (
        gate_decisions["call:terminal"].reason_code
        == "hardline_terminal_command_blocked"
    )
    assert gate_decisions["call:terminal-process"].decision == "deny"
    assert (
        gate_decisions["call:terminal-process"].reason_code
        == "hardline_terminal_process_input_blocked"
    )


def test_degraded_descriptor_gate_event_projection(tmp_path) -> None:
    transport = _ScriptedTransport(
        [
            {
                "tool_calls": [
                    {"id": "call:degraded", "name": "degraded", "arguments": "{}"}
                ]
            },
            {"text": "done"},
        ]
    )
    agent = build_test_agent_runtime(
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=_llm_runtime(transport),
        capability_runtime=_runtime_for_descriptors(
            _descriptor("degraded", availability=CapabilityAvailability.DEGRADED)
        ),
    )
    registry = ToolRegistry()
    registry.register(
        DummyTool("degraded", is_read_only=True, is_concurrency_safe=True)
    )
    _install_test_registry(agent, registry)

    result = asyncio.run(run_agent_task(agent, "call degraded"))

    assert result.status is LoopStatus.FINISHED
    gate_decision = _gate_decisions(agent)[0]
    assert gate_decision.decision == "allow"
    assert gate_decision.availability == "degraded"


def test_artifact_policy_uses_descriptor_mode() -> None:
    archive = InMemoryArchiveStore()
    index = FakeToolResultArtifactIndex()
    service = ToolResultArtifactService(
        archive=archive,
        index=index,
        runtime_session_id="runtime:test",
        options=ToolResultArtifactOptions(
            archive_threshold_bytes=10,
            large_preview_chars=10,
        ),
    )
    context = EventContext(
        run_id="run:test", turn_id="turn:test", reply_id="reply:test"
    )
    artifact_read = _descriptor("artifact_read")
    artifact_read = replace(artifact_read, artifact_mode=ToolArtifactMode.NEVER)
    always = _descriptor("small_json")
    always = replace(always, artifact_mode=ToolArtifactMode.ALWAYS)

    read_call = ToolCall(id="call:read", name="artifact_read")
    read_result, read_refs = service.process_result(
        ToolExecutionResult(
            call_id="call:read",
            tool_name="artifact_read",
            status=ToolResultState.SUCCESS,
            output="small",
        ),
        event_context=context,
        tool_call=read_call,
        policy=artifact_processing_policy_for_descriptor(
            descriptor=artifact_read,
            tool_call=read_call,
            options=service.options,
        ),
    )
    small_call = ToolCall(id="call:small", name="small_json")
    small_result, small_refs = service.process_result(
        ToolExecutionResult(
            call_id="call:small",
            tool_name="small_json",
            status=ToolResultState.SUCCESS,
            output="small",
        ),
        event_context=context,
        tool_call=small_call,
        policy=artifact_processing_policy_for_descriptor(
            descriptor=always,
            tool_call=small_call,
            options=service.options,
        ),
    )

    assert read_result.output == "small"
    assert read_refs == ()
    assert small_result.output == "small"
    assert len(small_refs) == 1
    assert small_refs[0].artifact_id in archive.blobs


class _ScriptedTransport:
    api = "scripted"
    binding_id = "test.scripted"
    contract_version = "v1"

    def __init__(self, replies: list[dict]) -> None:
        self.replies = replies
        self.contexts: list[LLMContext] = []

    async def stream(
        self,
        *,
        call,
        context: LLMContext,
        event_context: EventContext,
    ) -> AsyncIterator[AgentEvent]:
        del call
        self.contexts.append(context)
        reply = self.replies.pop(0)
        if "text" in reply:
            yield RawProviderTextBlockStart(
                **event_context.event_fields(), block_id="text:1"
            )
            yield RawProviderTextDelta(
                **event_context.event_fields(), block_id="text:1", delta=reply["text"]
            )
            yield RawProviderTextBlockEnd(
                **event_context.event_fields(), block_id="text:1"
            )
        for call in reply.get("tool_calls", []):
            yield RawProviderToolCallStart(
                **event_context.event_fields(),
                tool_call_id=call["id"],
                tool_call_name=call["name"],
            )
            yield RawProviderToolCallDelta(
                **event_context.event_fields(),
                tool_call_id=call["id"],
                delta=call["arguments"],
            )
            yield RawProviderToolCallEnd(
                **event_context.event_fields(), tool_call_id=call["id"]
            )


def _llm_runtime(transport: _ScriptedTransport) -> LLMRuntime:
    registry = LLMTransportRegistry()
    registry.register(transport)
    return LLMRuntime(
        config=test_llm_config(
            api_key="sk-test",
            base_url="https://example.test/v1",
            pro_model="pro",
            flash_model="flash",
            api="scripted",
        ),
        registry=registry,
    )


def test_agent_runtime_records_capability_exposure_and_gate_diagnostics(
    tmp_path,
) -> None:
    transport = _ScriptedTransport(
        [
            {"tool_calls": [{"id": "call:noop", "name": "noop", "arguments": "{}"}]},
            {"text": "done"},
        ]
    )
    agent = build_test_agent_runtime(
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=_llm_runtime(transport),
        capability_runtime=_runtime_for_descriptors(_descriptor("noop")),
    )
    registry = ToolRegistry()
    registry.register(DummyTool("noop", is_read_only=True, is_concurrency_safe=True))
    _install_test_registry(agent, registry)

    result = asyncio.run(run_agent_task(agent, "use noop"))

    assert result.status is LoopStatus.FINISHED
    assert [
        [tool.name for tool in context.tools] for context in transport.contexts
    ] == [["noop"], ["noop"]]
    exposure_events = [
        event
        for event in runtime_session_for_test(agent).event_log.iter()
        if isinstance(event, CapabilityExposureResolvedEvent)
    ]
    assert len(exposure_events) == 1
    authorization = exposure_events[0].exposure.authorization_entries
    assert [
        entry.capability_name
        for entry in authorization
        if entry.disposition == "direct"
    ] == ["noop"]
    assert [entry.capability_name for entry in authorization if entry.callable] == [
        "noop"
    ]
    gate_decision = _gate_decisions(agent)[0]
    assert gate_decision.tool_call_id == "call:noop"
    assert gate_decision.descriptor_id == "builtin:noop"
    assert gate_decision.decision == "allow"
    assert gate_decision.reason_code is None
    assert gate_decision.permission_policy
    assert gate_decision.exposure_generation is not None
    assert gate_decision.availability == "available"
    assert gate_decision.permission_category == "general"
    assert gate_decision.effective_permission_category == "general"
    assert gate_decision.effective_read_only is True
    assert gate_decision.capability_context == {}


def test_terminal_gate_decision_records_active_skill_capability_context(
    tmp_path,
) -> None:
    transport = _ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:terminal",
                        "name": "terminal",
                        "arguments": json.dumps({"command": "echo denied"}),
                    }
                ]
            },
            {"text": "done"},
        ]
    )
    provider = StaticCapabilityProvider(
        descriptors=(_descriptor("terminal", permission_category="terminal"),),
        active_injections=(_firecrawl_active_injection(tmp_path),),
    )
    agent = build_test_agent_runtime(
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=_llm_runtime(transport),
        capability_runtime=_runtime_for_provider(provider),
    )
    registry = ToolRegistry()
    registry.register(
        DummyTool("terminal", is_read_only=True, is_concurrency_safe=True)
    )
    _install_test_registry(agent, registry)

    result = asyncio.run(run_agent_task(agent, "search with $firecrawl-search"))

    assert result.status is LoopStatus.FINISHED
    gate_decision = _gate_decisions(agent)[0]
    assert gate_decision.decision == "allow"
    assert gate_decision.capability_context == {
        "active_skill_names": ["firecrawl-search"],
        "context_kind": "active_skill_present",
        "skill_suggested_tools": ["terminal"],
        "cli_required_binaries": ["firecrawl"],
        "cli_optional_binaries": ["npx"],
        "cli_external_services": ["firecrawl"],
        "cli_usage_kinds": ["read"],
        "auth_required": "required",
        "network_required": True,
    }


def test_terminal_gate_decision_has_no_active_skill_context_without_active_skill(
    tmp_path,
) -> None:
    transport = _ScriptedTransport(
        [
            {
                "tool_calls": [
                    {"id": "call:terminal", "name": "terminal", "arguments": "{}"}
                ]
            },
            {"text": "done"},
        ]
    )
    agent = build_test_agent_runtime(
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=_llm_runtime(transport),
        capability_runtime=_runtime_for_descriptors(
            _descriptor("terminal", permission_category="terminal")
        ),
    )
    registry = ToolRegistry()
    registry.register(
        DummyTool("terminal", is_read_only=True, is_concurrency_safe=True)
    )
    _install_test_registry(agent, registry)

    result = asyncio.run(run_agent_task(agent, "run terminal"))

    assert result.status is LoopStatus.FINISHED
    gate_decision = _gate_decisions(agent)[0]
    assert gate_decision.capability_context == {}


def test_denied_terminal_gate_decision_keeps_active_skill_capability_context(
    tmp_path,
) -> None:
    transport = _ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:terminal",
                        "name": "terminal",
                        "arguments": json.dumps({"command": "echo denied"}),
                    }
                ]
            },
            {"text": "done"},
        ]
    )
    provider = StaticCapabilityProvider(
        descriptors=(_descriptor("terminal", permission_category="terminal"),),
        active_injections=(_firecrawl_active_injection(tmp_path),),
    )
    agent = build_test_agent_runtime(
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=_llm_runtime(transport),
        capability_runtime=_runtime_for_provider(provider),
        permission_policy=preset_to_policy(PermissionMode.READ_ONLY),
    )
    registry = ToolRegistry()
    registry.register(
        DummyTool("terminal", is_read_only=True, is_concurrency_safe=True)
    )
    _install_test_registry(agent, registry)

    result = asyncio.run(run_agent_task(agent, "search with $firecrawl-search"))

    assert result.status is LoopStatus.FINISHED
    gate_decision = _gate_decisions(agent)[0]
    assert gate_decision.decision == "deny"
    assert gate_decision.result_state is ToolResultState.DENIED
    assert gate_decision.capability_context["active_skill_names"] == [
        "firecrawl-search"
    ]
    assert gate_decision.capability_context["cli_required_binaries"] == ["firecrawl"]


def test_asking_terminal_gate_decision_keeps_active_skill_capability_context(
    tmp_path,
) -> None:
    transport = _ScriptedTransport(
        [
            {
                "tool_calls": [
                    {"id": "call:terminal", "name": "terminal", "arguments": "{}"}
                ]
            }
        ]
    )
    provider = StaticCapabilityProvider(
        descriptors=(_descriptor("terminal", permission_category="terminal"),),
        active_injections=(_firecrawl_active_injection(tmp_path),),
    )
    agent = build_test_agent_runtime(
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=_llm_runtime(transport),
        capability_runtime=_runtime_for_provider(provider),
        permission_policy=preset_to_policy(PermissionMode.ASK_PERMISSIONS),
    )
    registry = ToolRegistry()
    registry.register(
        DummyTool("terminal", is_read_only=True, is_concurrency_safe=True)
    )
    _install_test_registry(agent, registry)

    result = asyncio.run(run_agent_task(agent, "search with $firecrawl-search"))

    assert result.status is LoopStatus.WAITING_USER
    gate_decision = _gate_decisions(agent)[0]
    assert gate_decision.decision == "wait_for_user"
    assert gate_decision.reason_code == "permission_wait_for_user"
    assert gate_decision.capability_context["active_skill_names"] == [
        "firecrawl-search"
    ]


def test_huggingface_local_skill_terminal_context_dogfood(tmp_path) -> None:
    skill_dir = tmp_path / ".agents" / "skills" / "huggingface-local-models"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: huggingface-local-models
description: Use Hugging Face CLI for local model workflows.
suggested_tools: [terminal]
required_binaries: [hf]
optional_binaries: [git]
external_services: [huggingface]
auth_required: optional
cli_usage_kind: read
---
# Hugging Face Local Models

Use `hf` commands through the terminal when the user asks for Hugging Face local model workflows.
""",
        encoding="utf-8",
    )
    transport = _ScriptedTransport(
        [
            {
                "tool_calls": [
                    {"id": "call:terminal", "name": "terminal", "arguments": "{}"}
                ]
            },
            {"text": "done"},
        ]
    )
    provider = LocalSkillCapabilityProvider(
        provider=LocalSkillProvider(include_user_skills=False),
        skill_health_resolver=SkillHealthResolver(
            which=lambda binary: "/usr/bin/git" if binary == "git" else None
        ),
    )
    agent = build_test_agent_runtime(
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=_llm_runtime(transport),
        capability_runtime=CapabilityRuntime.with_default_providers(provider),
    )
    registry = ToolRegistry()
    registry.register(
        DummyTool("terminal", is_read_only=True, is_concurrency_safe=True)
    )
    _install_test_registry(agent, registry)

    result = asyncio.run(
        run_agent_task(agent, "$huggingface-local-models list local model caches")
    )

    assert result.status is LoopStatus.FINISHED
    system_prompt = transport.contexts[0].system_prompt or ""
    context_text = "\n".join(
        text for message in transport.contexts[0].messages for text in message.content
    )
    assert "Active Skill: huggingface-local-models" not in system_prompt
    assert "Active Skill: huggingface-local-models" in context_text
    assert "Required binaries: hf" in context_text
    assert "Skill CLI hints are guidance only" in context_text
    exposure_event = next(
        event.exposure
        for event in runtime_session_for_test(agent).event_log.iter()
        if isinstance(event, CapabilityExposureResolvedEvent)
    )
    assert "skill_required_binary_missing" in [
        diagnostic.code for diagnostic in exposure_event.diagnostics
    ]
    gate_decision = _gate_decisions(agent)[0]
    assert gate_decision.capability_context["active_skill_names"] == [
        "huggingface-local-models"
    ]
    assert gate_decision.capability_context["cli_required_binaries"] == ["hf"]
    assert gate_decision.capability_context["cli_external_services"] == ["huggingface"]
    assert gate_decision.capability_context["auth_required"] == "optional"


def test_agent_runtime_call_local_unknown_tool_does_not_block_valid_sibling(
    tmp_path,
) -> None:
    transport = _ScriptedTransport(
        [
            {
                "tool_calls": [
                    {"id": "call:missing", "name": "missing_tool", "arguments": "{}"},
                    {"id": "call:ok", "name": "ok", "arguments": "{}"},
                ]
            },
            {"text": "done"},
        ]
    )
    agent = build_test_agent_runtime(
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=_llm_runtime(transport),
        capability_runtime=_runtime_for_descriptors(_descriptor("ok")),
    )
    registry = ToolRegistry()
    ok_tool = DummyTool("ok", is_read_only=True, is_concurrency_safe=True)
    registry.register(ok_tool)
    _install_test_registry(agent, registry)

    result = asyncio.run(run_agent_task(agent, "call missing and ok"))

    assert result.status is LoopStatus.FINISHED
    assert ok_tool.calls == ["call:ok"]
    result_ends = {
        event.tool_call_id: event.state
        for event in runtime_session_for_test(agent).event_log.iter()
        if isinstance(event, ToolResultEndEvent)
    }
    assert result_ends["call:missing"] is ToolResultState.ERROR
    assert result_ends["call:ok"] is ToolResultState.SUCCESS
    gate_decisions = _gate_decisions_by_call(agent)
    assert gate_decisions["call:missing"].decision == "deny"
    assert gate_decisions["call:missing"].result_state is ToolResultState.ERROR
    assert gate_decisions["call:missing"].reason_code == "capability_descriptor_missing"
    assert gate_decisions["call:missing"].reason_message == (
        "Unknown tool: missing_tool (capability_descriptor_missing)"
    )
    assert gate_decisions["call:ok"].decision == "allow"
    assert gate_decisions["call:ok"].result_state is None


def test_approval_resume_uses_committed_descriptor_after_provider_removal(
    tmp_path,
) -> None:
    transport = _ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:write",
                        "name": "write_file",
                        "arguments": json.dumps(
                            {
                                "path": "review_tmp.txt",
                                "content": "committed authority wins",
                            }
                        ),
                    }
                ]
            },
            {"text": "done"},
        ]
    )
    agent = build_test_agent_runtime(
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=_llm_runtime(transport),
        capability_runtime=_runtime_for_descriptors(
            _descriptor("write_file", permission_category="filesystem_write")
        ),
        permission_policy=preset_to_policy(PermissionMode.ASK_PERMISSIONS),
    )
    tool = DummyTool(
        "write_file",
        is_read_only=False,
        is_concurrency_safe=True,
    )
    registry = ToolRegistry()
    registry.register(tool)
    _install_test_registry(agent, registry)

    first = asyncio.run(run_agent_task(agent, "write the requested file"))
    assert first.status is LoopStatus.WAITING_USER

    # The committed run revision owns the descriptor. Removing the current
    # provider catalog cannot retroactively change an approved continuation.
    agent.capability_runtime = CapabilityRuntime(providers=())
    result = asyncio.run(
        resume_test_agent_after_approval(
            agent,
            first.run_id,
            ApprovalResolution(
                approval_id="host-minted",
                decisions=(
                    ToolApprovalDecision(
                        tool_call_id="call:write",
                        confirmed=True,
                    ),
                ),
            ),
        )
    )

    assert result.status is LoopStatus.FINISHED
    assert tool.calls == ["call:write"]
    decisions = [
        event
        for event in runtime_session_for_test(agent).event_log.iter(run_id=first.run_id)
        if isinstance(event, CapabilityGateDecisionEvent)
        and event.tool_call_id == "call:write"
    ]
    assert [event.decision for event in decisions] == ["wait_for_user", "allow"]
    assert decisions[-1].descriptor_id is not None
    assert decisions[-1].reason_code is None


def test_agent_runtime_workflow_control_fails_closed_without_descriptor(
    tmp_path,
) -> None:
    transport = _ScriptedTransport(
        [
            {
                "tool_calls": [
                    {"id": "call:plan", "name": "enter_plan", "arguments": "{}"}
                ]
            },
            {"text": "done"},
        ]
    )
    agent = build_test_agent_runtime(
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=_llm_runtime(transport),
        capability_runtime=CapabilityRuntime(providers=()),
    )

    with pytest.raises(
        ValueError, match="execution bindings lack capability descriptors"
    ):
        asyncio.run(run_agent_task(agent, "enter plan"))

    assert not any(
        isinstance(event, PlanModeEnteredEvent)
        for event in runtime_session_for_test(agent).event_log.iter()
    )
    assert not _gate_decisions(agent)
