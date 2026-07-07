import asyncio
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import AsyncIterator

import pytest

from tests.support.runtime_session import in_memory_runtime_session

from pulsara_agent.capability import LocalSkillCapabilityProvider, LocalSkillProvider, SkillHealthResolver
from pulsara_agent.capability.descriptor import (
    CapabilityAdvertisePolicy,
    CapabilityArtifactMode,
    CapabilityAvailability,
    CapabilityDescriptor,
    CapabilityProviderKind,
)
from pulsara_agent.capability.builtin_provider import BuiltinToolCapabilityProvider
from pulsara_agent.capability.exposure import build_exposure_plan
from pulsara_agent.capability.provider import CapabilityProviderOutput
from pulsara_agent.capability.registry import CapabilityRegistry
from pulsara_agent.capability.runtime import CapabilityRuntime
from pulsara_agent.capability.types import ActiveSkillInjection, CapabilityResolveContext
from pulsara_agent.event import (
    AgentEvent,
    CapabilityGateDecisionEvent,
    CustomEvent,
    EventContext,
    ModelCallEndEvent,
    ModelCallStartEvent,
    PlanModeEnteredEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
)
from pulsara_agent.llm import LLMConfig, LLMRuntime, ModelProfile
from pulsara_agent.llm.registry import LLMTransportRegistry
from pulsara_agent.llm.request import LLMContext, LLMOptions
from pulsara_agent.memory.artifacts.archive import InMemoryArchiveStore
from pulsara_agent.message import ToolCallBlock, ToolCallState, ToolResultState
from pulsara_agent.runtime import AgentRuntime, ApprovalResolution, LoopState, LoopStatus, ToolApprovalDecision
from pulsara_agent.runtime.permission import (
    AllowAllPermissionGate,
    ApprovalPolicy,
    EffectivePermissionPolicy,
    PermissionDecision,
    PermissionDecisionKind,
    PermissionProfile,
    PolicyPermissionGate,
    TerminalAccess,
)
from pulsara_agent.runtime.tool_artifacts import (
    InMemoryToolResultArtifactIndex,
    ToolResultArtifactOptions,
    ToolResultArtifactService,
)
from pulsara_agent.tools.base import ToolCall, ToolExecutionResult
from pulsara_agent.tools.registry import ToolRegistry


@dataclass(slots=True)
class DummyTool:
    name: str
    is_read_only: bool
    is_concurrency_safe: bool
    description: str = "dummy"
    parameters: dict = field(default_factory=lambda: {"type": "object", "properties": {}})
    calls: list[str] = field(default_factory=list)

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        self.calls.append(call.id)
        return ToolExecutionResult(
            call_id=call.id,
            tool_name=call.name,
            status=ToolResultState.SUCCESS,
            output=f"ran:{call.id}",
        )


@dataclass(frozen=True, slots=True)
class StaticCapabilityProvider:
    descriptors: tuple[CapabilityDescriptor, ...]
    active_injections: tuple[ActiveSkillInjection, ...] = ()
    provider_id: str = "static-test"

    def resolve(
        self,
        context: CapabilityResolveContext,
        *,
        bound_tool_names: frozenset[str],
    ) -> CapabilityProviderOutput:
        del context, bound_tool_names
        return CapabilityProviderOutput(
            descriptors=self.descriptors,
            active_injections=self.active_injections,
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
        for event in agent.runtime_session.event_log.iter()
        if isinstance(event, CapabilityGateDecisionEvent)
    ]


def _gate_decisions_by_call(agent: AgentRuntime) -> dict[str, CapabilityGateDecisionEvent]:
    return {event.tool_call_id: event for event in _gate_decisions(agent)}


def _descriptor(
    name: str,
    *,
    advertise_policy: CapabilityAdvertisePolicy = CapabilityAdvertisePolicy.DIRECT,
    availability: CapabilityAvailability = CapabilityAvailability.AVAILABLE,
    permission_category: str = "general",
) -> CapabilityDescriptor:
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
        permission_category=permission_category,
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
        provider_output=CapabilityProviderOutput(),
        bound_tool_names=bound_tool_names,
    )


def _runtime_for_descriptors(*descriptors: CapabilityDescriptor) -> CapabilityRuntime:
    return CapabilityRuntime(providers=(StaticCapabilityProvider(tuple(descriptors)),))


def _runtime_for_provider(provider: StaticCapabilityProvider) -> CapabilityRuntime:
    return CapabilityRuntime(providers=(provider,))


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


def test_builtin_provider_uses_explicit_descriptor_truth_for_bound_core_tools() -> None:
    output = BuiltinToolCapabilityProvider().resolve(
        CapabilityResolveContext(
            workspace_root=Path("."),
            workspace_kind="transient",
            memory_domain=None,
            available_tool_names=frozenset(),
            user_input="",
        ),
        bound_tool_names=frozenset({"artifact_read", "terminal_process", "exit_plan"}),
    )
    descriptors = {descriptor.name: descriptor for descriptor in output.descriptors}

    assert descriptors["artifact_read"].artifact_mode is CapabilityArtifactMode.NEVER
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
        AgentRuntime(  # type: ignore[arg-type]
            runtime_session=in_memory_runtime_session(tmp_path),
            llm_runtime=_llm_runtime(_ScriptedTransport([{"text": "done"}])),
            capability_runtime=None,
        )


def test_exposure_plan_separates_direct_deferred_hidden_unavailable_and_callable() -> None:
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
        provider_output=CapabilityProviderOutput(),
        bound_tool_names=frozenset({"real_tool"}),
    )

    assert exposure.direct_tool_specs == ()
    assert exposure.direct_names == frozenset()
    assert exposure.callable_names == frozenset()
    assert exposure.hidden_names == frozenset({"ghost"})
    assert [diagnostic.code for diagnostic in exposure.diagnostics] == [
        "capability_missing_execution_binding"
    ]


def test_exposure_plan_diagnoses_non_direct_descriptor_without_execution_binding() -> None:
    exposure = _exposure_for_descriptors(
        _descriptor("ghost_deferred", advertise_policy=CapabilityAdvertisePolicy.DEFERRED),
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
    deferred = _descriptor("deferred", advertise_policy=CapabilityAdvertisePolicy.DEFERRED)
    exposure = _exposure_for_descriptors(hidden, unavailable, deferred)
    gate = PolicyPermissionGate(
        EffectivePermissionPolicy(
            profile=PermissionProfile.TRUSTED_HOST,
            approval=ApprovalPolicy.NEVER,
            terminal=TerminalAccess.ALLOW,
        ),
        inner=AllowAllPermissionGate(),
    )

    hidden_decision = asyncio.run(gate.evaluate([ToolCall(id="call:hidden", name="hidden")], exposure=exposure))
    unavailable_decision = asyncio.run(gate.evaluate([ToolCall(id="call:down", name="down")], exposure=exposure))
    deferred_decision = asyncio.run(
        gate.evaluate([ToolCall(id="call:deferred", name="deferred")], exposure=exposure)
    )

    assert hidden_decision.kind is PermissionDecisionKind.DENY
    assert hidden_decision.reason == "capability_hidden_in_current_exposure: hidden"
    assert unavailable_decision.kind is PermissionDecisionKind.DENY
    assert unavailable_decision.reason == "capability_unavailable: down"
    assert deferred_decision.kind is PermissionDecisionKind.DENY
    assert deferred_decision.reason == "capability_not_callable_in_current_exposure: deferred"


def test_capability_gate_preserves_terminal_process_observe_contract_and_terminal_off() -> None:
    terminal_process = CapabilityDescriptor(
        id="builtin:terminal_process",
        name="terminal_process",
        description="process",
        input_schema={"type": "object", "properties": {}},
        namespace=None,
        provider_kind=CapabilityProviderKind.BUILTIN,
        provider_id="builtin",
        is_model_callable=True,
        is_read_only=False,
        is_concurrency_safe=False,
        is_open_world=True,
        permission_category="terminal",
    )
    exposure = _exposure_for_descriptors(terminal_process)
    observe = ToolCall(id="call:observe", name="terminal_process", arguments={"action": "list"})

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

    assert asyncio.run(ask_gate.evaluate([observe], exposure=exposure)).kind is PermissionDecisionKind.ALLOW
    off_decision = asyncio.run(off_gate.evaluate([observe], exposure=exposure))
    assert off_decision.kind is PermissionDecisionKind.DENY
    assert off_decision.reason == "tool 'terminal_process' is not allowed by permission policy"


def test_terminal_process_observe_gate_event_records_effective_category(tmp_path) -> None:
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
    agent = AgentRuntime(
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=_llm_runtime(transport),
        capability_runtime=_runtime_for_descriptors(_descriptor("terminal_process", permission_category="terminal")),
        permission_policy=EffectivePermissionPolicy(
            profile=PermissionProfile.TRUSTED_HOST,
            approval=ApprovalPolicy.ON_REQUEST,
            terminal=TerminalAccess.ASK,
        ),
    )
    registry = ToolRegistry()
    registry.register(DummyTool("terminal_process", is_read_only=False, is_concurrency_safe=True))
    agent.tool_executor.registry = registry

    result = asyncio.run(agent.run_task("list processes"))

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
    agent = AgentRuntime(
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=_llm_runtime(transport),
        capability_runtime=_runtime_for_descriptors(
            _descriptor("read_file", permission_category="filesystem_read"),
            _descriptor("terminal", permission_category="terminal"),
        ),
        permission_policy=EffectivePermissionPolicy(
            profile=PermissionProfile.TRUSTED_HOST,
            approval=ApprovalPolicy.ON_REQUEST,
            terminal=TerminalAccess.ASK,
        ),
    )
    registry = ToolRegistry()
    registry.register(DummyTool("read_file", is_read_only=True, is_concurrency_safe=True))
    registry.register(DummyTool("terminal", is_read_only=False, is_concurrency_safe=True))
    agent.tool_executor.registry = registry

    result = asyncio.run(agent.run_task("read and terminal"))

    assert result.status is LoopStatus.WAITING_USER
    gate_decisions = _gate_decisions_by_call(agent)
    assert gate_decisions["call:terminal"].decision == "wait_for_user"
    assert gate_decisions["call:terminal"].reason_code == "permission_wait_for_user"
    assert gate_decisions["call:read"].decision == "wait_for_user"
    assert gate_decisions["call:read"].reason_code == "permission_wait_for_user_batch_suspension"
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
    agent = AgentRuntime(
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=_llm_runtime(transport),
        capability_runtime=_runtime_for_descriptors(
            _descriptor("write_file", permission_category="filesystem_write"),
            _descriptor("terminal", permission_category="terminal"),
        ),
        permission_policy=EffectivePermissionPolicy(
            profile=PermissionProfile.TRUSTED_HOST,
            approval=ApprovalPolicy.ON_REQUEST,
            terminal=TerminalAccess.ASK,
        ),
    )
    registry = ToolRegistry()
    registry.register(DummyTool("write_file", is_read_only=False, is_concurrency_safe=True))
    registry.register(DummyTool("terminal", is_read_only=False, is_concurrency_safe=True))
    agent.tool_executor.registry = registry

    result = asyncio.run(agent.run_task("write and terminal"))

    assert result.status is LoopStatus.WAITING_USER
    gate_decisions = _gate_decisions_by_call(agent)
    assert gate_decisions["call:write"].reason_code == "permission_wait_for_user"
    assert gate_decisions["call:write"].reason_message == "file write tool requires user confirmation by approval policy"
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
    agent = AgentRuntime(
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=_llm_runtime(transport),
        capability_runtime=_runtime_for_descriptors(_descriptor("a"), _descriptor("b")),
        permission_gate=inner_gate,
    )
    registry = ToolRegistry()
    registry.register(DummyTool("a", is_read_only=True, is_concurrency_safe=True))
    registry.register(DummyTool("b", is_read_only=True, is_concurrency_safe=True))
    agent.tool_executor.registry = registry

    result = asyncio.run(agent.run_task("call two tools"))

    assert result.status is LoopStatus.FINISHED
    assert inner_gate.call_batches == [["call:a", "call:b"]]


def test_workflow_control_emits_gate_decision_before_execution_and_suppresses_siblings(tmp_path) -> None:
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
    agent = AgentRuntime(
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=_llm_runtime(transport),
        capability_runtime=_runtime_for_descriptors(
            _descriptor("enter_plan", permission_category="plan_workflow"),
            _descriptor("noop"),
        ),
    )
    registry = ToolRegistry()
    registry.register(DummyTool("enter_plan", is_read_only=False, is_concurrency_safe=True))
    registry.register(DummyTool("noop", is_read_only=True, is_concurrency_safe=True))
    agent.tool_executor.registry = registry

    result = asyncio.run(agent.run_task("enter plan and noop"))

    assert result.status is LoopStatus.FINISHED
    gate_decisions = _gate_decisions_by_call(agent)
    assert gate_decisions["call:plan"].decision == "allow"
    assert gate_decisions["call:plan"].permission_category == "plan_workflow"
    assert gate_decisions["call:noop"].decision == "deny"
    assert gate_decisions["call:noop"].result_state is ToolResultState.DENIED
    assert gate_decisions["call:noop"].reason_code == "workflow_control_batch_suppressed"
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
                        "arguments": json.dumps({"action": "write", "data": "rm -rf /"}),
                    },
                ]
            },
            {"text": "done"},
        ]
    )
    agent = AgentRuntime(
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=_llm_runtime(transport),
        capability_runtime=_runtime_for_descriptors(
            _descriptor("terminal", permission_category="terminal"),
            _descriptor("terminal_process", permission_category="terminal"),
        ),
    )
    registry = ToolRegistry()
    registry.register(DummyTool("terminal", is_read_only=False, is_concurrency_safe=True))
    registry.register(DummyTool("terminal_process", is_read_only=False, is_concurrency_safe=True))
    agent.tool_executor.registry = registry

    result = asyncio.run(agent.run_task("dangerous terminal calls"))

    assert result.status is LoopStatus.FINISHED
    gate_decisions = _gate_decisions_by_call(agent)
    assert gate_decisions["call:terminal"].decision == "deny"
    assert gate_decisions["call:terminal"].reason_code == "hardline_terminal_command_blocked"
    assert gate_decisions["call:terminal-process"].decision == "deny"
    assert gate_decisions["call:terminal-process"].reason_code == "hardline_terminal_process_input_blocked"


def test_degraded_descriptor_gate_event_projection(tmp_path) -> None:
    transport = _ScriptedTransport(
        [
            {"tool_calls": [{"id": "call:degraded", "name": "degraded", "arguments": "{}"}]},
            {"text": "done"},
        ]
    )
    agent = AgentRuntime(
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=_llm_runtime(transport),
        capability_runtime=_runtime_for_descriptors(
            _descriptor("degraded", availability=CapabilityAvailability.DEGRADED)
        ),
    )
    registry = ToolRegistry()
    registry.register(DummyTool("degraded", is_read_only=True, is_concurrency_safe=True))
    agent.tool_executor.registry = registry

    result = asyncio.run(agent.run_task("call degraded"))

    assert result.status is LoopStatus.FINISHED
    gate_decision = _gate_decisions(agent)[0]
    assert gate_decision.decision == "allow"
    assert gate_decision.availability == "degraded"


def test_artifact_policy_uses_descriptor_mode() -> None:
    archive = InMemoryArchiveStore()
    index = InMemoryToolResultArtifactIndex()
    service = ToolResultArtifactService(
        archive=archive,
        index=index,
        runtime_session_id="runtime:test",
        options=ToolResultArtifactOptions(
            archive_threshold_bytes=10,
            large_preview_chars=10,
            tool_result_message_context_chars=20,
        ),
    )
    context = EventContext(run_id="run:test", turn_id="turn:test", reply_id="reply:test")
    artifact_read = _descriptor("artifact_read")
    artifact_read = replace(artifact_read, artifact_mode=CapabilityArtifactMode.NEVER)
    always = _descriptor("small_json")
    always = replace(always, artifact_mode=CapabilityArtifactMode.ALWAYS)

    read_result, read_refs = service.process_result(
        ToolExecutionResult(
            call_id="call:read",
            tool_name="artifact_read",
            status=ToolResultState.SUCCESS,
            output="small",
        ),
        event_context=context,
        tool_call=ToolCall(id="call:read", name="artifact_read"),
        descriptor=artifact_read,
    )
    small_result, small_refs = service.process_result(
        ToolExecutionResult(
            call_id="call:small",
            tool_name="small_json",
            status=ToolResultState.SUCCESS,
            output="small",
        ),
        event_context=context,
        tool_call=ToolCall(id="call:small", name="small_json"),
        descriptor=always,
    )

    assert read_result.output == "small"
    assert read_refs == ()
    assert small_result.output == "small"
    assert len(small_refs) == 1
    assert small_refs[0].artifact_id in archive.blobs


class _ScriptedTransport:
    api = "scripted"

    def __init__(self, replies: list[dict]) -> None:
        self.replies = replies
        self.contexts: list[LLMContext] = []

    async def stream(
        self,
        *,
        model: ModelProfile,
        context: LLMContext,
        event_context: EventContext,
        options: LLMOptions | None = None,
    ) -> AsyncIterator[AgentEvent]:
        del options
        self.contexts.append(context)
        reply = self.replies.pop(0)
        yield ModelCallStartEvent(
            **event_context.event_fields(),
            model_name=model.id,
            model_role=model.role.value,
            provider=model.provider,
        )
        if "text" in reply:
            yield TextBlockStartEvent(**event_context.event_fields(), block_id="text:1")
            yield TextBlockDeltaEvent(**event_context.event_fields(), block_id="text:1", delta=reply["text"])
            yield TextBlockEndEvent(**event_context.event_fields(), block_id="text:1")
        for call in reply.get("tool_calls", []):
            yield ToolCallStartEvent(
                **event_context.event_fields(),
                tool_call_id=call["id"],
                tool_call_name=call["name"],
            )
            yield ToolCallDeltaEvent(
                **event_context.event_fields(),
                tool_call_id=call["id"],
                delta=call["arguments"],
            )
            yield ToolCallEndEvent(**event_context.event_fields(), tool_call_id=call["id"])
        yield ModelCallEndEvent(**event_context.event_fields())


def _llm_runtime(transport: _ScriptedTransport) -> LLMRuntime:
    registry = LLMTransportRegistry()
    registry.register(transport)
    return LLMRuntime(
        config=LLMConfig(
            api_key="sk-test",
            base_url="https://example.test/v1",
            pro_model="pro",
            flash_model="flash",
            api="scripted",
        ),
        registry=registry,
    )


def test_agent_runtime_records_capability_exposure_and_gate_diagnostics(tmp_path) -> None:
    transport = _ScriptedTransport(
        [
            {"tool_calls": [{"id": "call:noop", "name": "noop", "arguments": "{}"}]},
            {"text": "done"},
        ]
    )
    agent = AgentRuntime(
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=_llm_runtime(transport),
        capability_runtime=_runtime_for_descriptors(_descriptor("noop")),
    )
    registry = ToolRegistry()
    registry.register(DummyTool("noop", is_read_only=True, is_concurrency_safe=True))
    agent.tool_executor.registry = registry

    result = asyncio.run(agent.run_task("use noop"))

    assert result.status is LoopStatus.FINISHED
    assert [[tool.name for tool in context.tools] for context in transport.contexts] == [["noop"], ["noop"]]
    custom_events = [
        event for event in agent.runtime_session.event_log.iter() if isinstance(event, CustomEvent)
    ]
    assert [event.name for event in custom_events] == ["capability_exposure_resolved"]
    assert custom_events[0].value["direct_names"] == ["noop"]
    assert custom_events[0].value["callable_names"] == ["noop"]
    gate_decision = _gate_decisions(agent)[0]
    assert gate_decision.tool_call_id == "call:noop"
    assert gate_decision.descriptor_id == "builtin:noop"
    assert gate_decision.decision == "allow"
    assert gate_decision.reason_code is None
    assert gate_decision.permission_policy
    assert gate_decision.exposure_generation == custom_events[0].value["registry_generation"]
    assert gate_decision.availability == "available"
    assert gate_decision.permission_category == "general"
    assert gate_decision.effective_permission_category == "general"
    assert gate_decision.effective_read_only is True
    assert gate_decision.capability_context == {}


def test_terminal_gate_decision_records_active_skill_capability_context(tmp_path) -> None:
    transport = _ScriptedTransport(
        [
            {"tool_calls": [{"id": "call:terminal", "name": "terminal", "arguments": "{}"}]},
            {"text": "done"},
        ]
    )
    provider = StaticCapabilityProvider(
        descriptors=(_descriptor("terminal", permission_category="terminal"),),
        active_injections=(_firecrawl_active_injection(tmp_path),),
    )
    agent = AgentRuntime(
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=_llm_runtime(transport),
        capability_runtime=_runtime_for_provider(provider),
    )
    registry = ToolRegistry()
    registry.register(DummyTool("terminal", is_read_only=True, is_concurrency_safe=True))
    agent.tool_executor.registry = registry

    result = asyncio.run(agent.run_task("search with $firecrawl-search"))

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


def test_terminal_gate_decision_has_no_active_skill_context_without_active_skill(tmp_path) -> None:
    transport = _ScriptedTransport(
        [
            {"tool_calls": [{"id": "call:terminal", "name": "terminal", "arguments": "{}"}]},
            {"text": "done"},
        ]
    )
    agent = AgentRuntime(
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=_llm_runtime(transport),
        capability_runtime=_runtime_for_descriptors(_descriptor("terminal", permission_category="terminal")),
    )
    registry = ToolRegistry()
    registry.register(DummyTool("terminal", is_read_only=True, is_concurrency_safe=True))
    agent.tool_executor.registry = registry

    result = asyncio.run(agent.run_task("run terminal"))

    assert result.status is LoopStatus.FINISHED
    gate_decision = _gate_decisions(agent)[0]
    assert gate_decision.capability_context == {}


def test_denied_terminal_gate_decision_keeps_active_skill_capability_context(tmp_path) -> None:
    transport = _ScriptedTransport(
        [
            {"tool_calls": [{"id": "call:terminal", "name": "terminal", "arguments": "{}"}]},
            {"text": "done"},
        ]
    )
    provider = StaticCapabilityProvider(
        descriptors=(_descriptor("terminal", permission_category="terminal"),),
        active_injections=(_firecrawl_active_injection(tmp_path),),
    )
    agent = AgentRuntime(
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=_llm_runtime(transport),
        capability_runtime=_runtime_for_provider(provider),
        permission_policy=EffectivePermissionPolicy(
            profile=PermissionProfile.TRUSTED_HOST,
            approval=ApprovalPolicy.NEVER,
            terminal=TerminalAccess.OFF,
        ),
    )
    registry = ToolRegistry()
    registry.register(DummyTool("terminal", is_read_only=True, is_concurrency_safe=True))
    agent.tool_executor.registry = registry

    result = asyncio.run(agent.run_task("search with $firecrawl-search"))

    assert result.status is LoopStatus.FINISHED
    gate_decision = _gate_decisions(agent)[0]
    assert gate_decision.decision == "deny"
    assert gate_decision.result_state is ToolResultState.DENIED
    assert gate_decision.capability_context["active_skill_names"] == ["firecrawl-search"]
    assert gate_decision.capability_context["cli_required_binaries"] == ["firecrawl"]


def test_asking_terminal_gate_decision_keeps_active_skill_capability_context(tmp_path) -> None:
    transport = _ScriptedTransport(
        [{"tool_calls": [{"id": "call:terminal", "name": "terminal", "arguments": "{}"}]}]
    )
    provider = StaticCapabilityProvider(
        descriptors=(_descriptor("terminal", permission_category="terminal"),),
        active_injections=(_firecrawl_active_injection(tmp_path),),
    )
    agent = AgentRuntime(
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=_llm_runtime(transport),
        capability_runtime=_runtime_for_provider(provider),
        permission_policy=EffectivePermissionPolicy(
            profile=PermissionProfile.TRUSTED_HOST,
            approval=ApprovalPolicy.ON_REQUEST,
            terminal=TerminalAccess.ASK,
        ),
    )
    registry = ToolRegistry()
    registry.register(DummyTool("terminal", is_read_only=True, is_concurrency_safe=True))
    agent.tool_executor.registry = registry

    result = asyncio.run(agent.run_task("search with $firecrawl-search"))

    assert result.status is LoopStatus.WAITING_USER
    gate_decision = _gate_decisions(agent)[0]
    assert gate_decision.decision == "wait_for_user"
    assert gate_decision.reason_code == "permission_wait_for_user"
    assert gate_decision.capability_context["active_skill_names"] == ["firecrawl-search"]


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
            {"tool_calls": [{"id": "call:terminal", "name": "terminal", "arguments": "{}"}]},
            {"text": "done"},
        ]
    )
    provider = LocalSkillCapabilityProvider(
        provider=LocalSkillProvider(include_user_skills=False),
        skill_health_resolver=SkillHealthResolver(which=lambda binary: "/usr/bin/git" if binary == "git" else None),
    )
    agent = AgentRuntime(
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=_llm_runtime(transport),
        capability_runtime=CapabilityRuntime.with_default_providers(provider),
    )
    registry = ToolRegistry()
    registry.register(DummyTool("terminal", is_read_only=True, is_concurrency_safe=True))
    agent.tool_executor.registry = registry

    result = asyncio.run(agent.run_task("$huggingface-local-models list local model caches"))

    assert result.status is LoopStatus.FINISHED
    system_prompt = transport.contexts[0].system_prompt or ""
    assert "Active Skill: huggingface-local-models" in system_prompt
    assert "Required binaries: hf" in system_prompt
    assert "Skill CLI hints are guidance only" in system_prompt
    exposure_event = next(
        event.value
        for event in agent.runtime_session.event_log.iter()
        if isinstance(event, CustomEvent) and event.name == "capability_exposure_resolved"
    )
    assert "skill_required_binary_missing" in [
        diagnostic["code"] for diagnostic in exposure_event["diagnostics"]
    ]
    gate_decision = _gate_decisions(agent)[0]
    assert gate_decision.capability_context["active_skill_names"] == ["huggingface-local-models"]
    assert gate_decision.capability_context["cli_required_binaries"] == ["hf"]
    assert gate_decision.capability_context["cli_external_services"] == ["huggingface"]
    assert gate_decision.capability_context["auth_required"] == "optional"


def test_agent_runtime_call_local_unknown_tool_does_not_block_valid_sibling(tmp_path) -> None:
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
    agent = AgentRuntime(
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=_llm_runtime(transport),
        capability_runtime=_runtime_for_descriptors(_descriptor("ok")),
    )
    registry = ToolRegistry()
    ok_tool = DummyTool("ok", is_read_only=True, is_concurrency_safe=True)
    registry.register(ok_tool)
    agent.tool_executor.registry = registry

    result = asyncio.run(agent.run_task("call missing and ok"))

    assert result.status is LoopStatus.FINISHED
    assert ok_tool.calls == ["call:ok"]
    result_ends = {
        event.tool_call_id: event.state
        for event in agent.runtime_session.event_log.iter()
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


def test_agent_runtime_approval_resume_fails_closed_without_descriptor(tmp_path) -> None:
    transport = _ScriptedTransport([{"text": "done"}])
    agent = AgentRuntime(
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=_llm_runtime(transport),
        capability_runtime=CapabilityRuntime(providers=()),
    )
    state = LoopState(session_id=agent.runtime_session.runtime_session_id)
    state.status = LoopStatus.WAITING_USER
    state.stop_reason = "waiting_user"
    state.pending_tool_calls = [
        ToolCallBlock(
            id="call:write",
            name="write_file",
            input=json.dumps({"path": "review_tmp.txt", "content": "should not be written"}),
            state=ToolCallState.ASKING,
        )
    ]

    result = asyncio.run(
        agent.resume_after_approval(
            state,
            ApprovalResolution(
                approval_id="approval:test",
                decisions=(ToolApprovalDecision(tool_call_id="call:write", confirmed=True),),
            ),
        )
    )

    assert result.status is LoopStatus.FINISHED
    assert not (tmp_path / "review_tmp.txt").exists()
    gate_decision = _gate_decisions(agent)[0]
    assert gate_decision.tool_call_id == "call:write"
    assert gate_decision.tool_name == "write_file"
    assert gate_decision.descriptor_id is None
    assert gate_decision.decision == "deny"
    assert gate_decision.reason_code == "capability_descriptor_missing"
    assert gate_decision.reason_message == "Unknown tool: write_file (capability_descriptor_missing)"
    assert gate_decision.policy_mode == "bypass-permissions"
    assert gate_decision.permission_policy
    assert gate_decision.exposure_generation == 0
    assert gate_decision.result_state is ToolResultState.ERROR


def test_agent_runtime_workflow_control_fails_closed_without_descriptor(tmp_path) -> None:
    transport = _ScriptedTransport(
        [
            {"tool_calls": [{"id": "call:plan", "name": "enter_plan", "arguments": "{}"}]},
            {"text": "done"},
        ]
    )
    agent = AgentRuntime(
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=_llm_runtime(transport),
        capability_runtime=CapabilityRuntime(providers=()),
    )

    result = asyncio.run(agent.run_task("enter plan"))

    assert result.status is LoopStatus.FINISHED
    assert not any(isinstance(event, PlanModeEnteredEvent) for event in agent.runtime_session.event_log.iter())
    assert not agent._plan_state(result.state).active
    gate_decision = _gate_decisions(agent)[0]
    assert gate_decision.tool_call_id == "call:plan"
    assert gate_decision.decision == "deny"
    assert gate_decision.reason_code == "capability_descriptor_missing"
    assert gate_decision.reason_message == "Unknown tool: enter_plan (capability_descriptor_missing)"
    assert gate_decision.result_state is ToolResultState.ERROR
