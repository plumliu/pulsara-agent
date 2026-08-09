from __future__ import annotations

from dataclasses import dataclass

import pytest

from pulsara_agent.capability.builtin_catalog import builtin_tool_catalog
from pulsara_agent.event import EventContext, ToolResultStartEvent
from pulsara_agent.message import ToolResultState
from pulsara_agent.ports.artifact import (
    ToolResultArtifactOptions,
    resolve_tool_result_artifact_policy_for_call,
)
from pulsara_agent.ports.tool_execution import ToolCall, ToolExecutionResult
from pulsara_agent.runtime.tool_composition import (
    build_runtime_tool_binding_installation,
)
from pulsara_agent.runtime.tool_executor import ToolExecutor
from pulsara_agent.tools.registry import ToolRegistry
from tests.support.capability import descriptor_attribution_for_test
from tests.support.events import settled_test_event


CTX = EventContext(
    run_id="run:artifact-policy",
    turn_id="turn:artifact-policy",
    reply_id="reply:artifact-policy",
)


@dataclass(slots=True)
class _EchoTool:
    name: str

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        return ToolExecutionResult(
            call_id=call.id,
            tool_name=call.name,
            status=ToolResultState.SUCCESS,
            output="artifact policy probe",
        )


@dataclass(slots=True)
class _RecordingArtifactPort:
    seen_policy: object | None = None

    def process_result(self, result, *, event_context, tool_call, policy):
        del event_context, tool_call
        self.seen_policy = policy
        return result, ()


@dataclass(slots=True)
class _FailingArtifactPort:
    def process_result(self, result, *, event_context, tool_call, policy):
        del result, event_context, tool_call, policy
        raise RuntimeError("artifact unavailable")


def _catalog_entry(name: str):
    return next(item for item in builtin_tool_catalog() if item.name == name)


def test_descriptor_lowering_freezes_the_only_artifact_policy() -> None:
    entry = _catalog_entry("read_file")
    options = ToolResultArtifactOptions(
        archive_threshold_bytes=1_111,
        complete_preview_body_chars=2_222,
        large_preview_chars=333,
        huge_output_chars=4_444,
        huge_preview_chars=222,
        streaming_live_head_cap_chars=111,
    )
    installation = build_runtime_tool_binding_installation(
        tool=_EchoTool(entry.name),
        descriptor=entry.descriptor,
        binding_contract=entry.binding_contract,
        artifact_options=options,
    )

    policy = installation.artifact_processing_policy
    assert policy.descriptor_id == entry.descriptor.id
    assert policy.descriptor_fingerprint == entry.descriptor.fingerprint()
    assert policy.artifact_mode is entry.descriptor.artifact_mode
    assert policy.archive_threshold_bytes == 1_111
    assert policy.complete_preview_body_chars == 2_222
    assert policy.max_inline_chars == entry.descriptor.max_inline_chars


def test_executor_rejects_missing_or_drifted_artifact_policy() -> None:
    entry = _catalog_entry("read_file")
    registry = ToolRegistry()
    registry.register(_EchoTool(entry.name), binding_contract=entry.binding_contract)
    artifact_port = _RecordingArtifactPort()
    executor = ToolExecutor(
        registry=registry,
        artifact_service=artifact_port,
        runtime_session_id="runtime:artifact-policy",
    )
    call = ToolCall(id="call:missing-policy", name=entry.name, arguments={})

    with pytest.raises(ValueError, match="lacks a frozen artifact processing policy"):
        executor.execute(
            call,
            event_context=CTX,
            descriptor=entry.descriptor,
            descriptor_attribution=descriptor_attribution_for_test(
                entry.descriptor,
                runtime_session_id="runtime:artifact-policy",
            ),
        )


def test_executor_passes_exact_frozen_policy_and_terminal_cap_is_call_local() -> None:
    entry = _catalog_entry("read_file")
    installation = build_runtime_tool_binding_installation(
        tool=_EchoTool(entry.name),
        descriptor=entry.descriptor,
        binding_contract=entry.binding_contract,
        artifact_options=ToolResultArtifactOptions(),
    )
    registry = ToolRegistry()
    registry.register(installation.tool, binding_contract=entry.binding_contract)
    artifact_port = _RecordingArtifactPort()
    executor = ToolExecutor(
        registry=registry,
        artifact_service=artifact_port,
        artifact_policies={entry.name: installation.artifact_processing_policy},
        runtime_session_id="runtime:artifact-policy",
    )
    executor.execute(
        ToolCall(id="call:exact-policy", name=entry.name, arguments={}),
        event_context=CTX,
        descriptor=entry.descriptor,
        descriptor_attribution=descriptor_attribution_for_test(
            entry.descriptor,
            runtime_session_id="runtime:artifact-policy",
        ),
    )
    assert artifact_port.seen_policy is installation.artifact_processing_policy

    terminal_entry = _catalog_entry("terminal_process")
    terminal_installation = build_runtime_tool_binding_installation(
        tool=_EchoTool(terminal_entry.name),
        descriptor=terminal_entry.descriptor,
        binding_contract=terminal_entry.binding_contract,
        artifact_options=ToolResultArtifactOptions(),
    )
    base = terminal_installation.artifact_processing_policy
    resolved = resolve_tool_result_artifact_policy_for_call(
        base_policy=base,
        tool_call=ToolCall(
            id="call:terminal-cap",
            name="terminal_process",
            arguments={
                "action": "poll",
                "process_id": "process:x",
                "max_output_chars": 512,
            },
        ),
    )
    assert resolved.complete_preview_body_chars == 512
    assert resolved.large_preview_chars == 512
    assert resolved.descriptor_fingerprint == base.descriptor_fingerprint
    assert base.complete_preview_body_chars > resolved.complete_preview_body_chars


def test_artifact_processing_failure_closes_the_physical_tool_result() -> None:
    entry = _catalog_entry("read_file")
    installation = build_runtime_tool_binding_installation(
        tool=_EchoTool(entry.name),
        descriptor=entry.descriptor,
        binding_contract=entry.binding_contract,
        artifact_options=ToolResultArtifactOptions(),
    )
    registry = ToolRegistry()
    registry.register(installation.tool, binding_contract=entry.binding_contract)
    events = []

    def record_event(event):
        receipt = settled_test_event(event, sequence=len(events) + 1)
        events.append(receipt.committed_event)
        return receipt

    executor = ToolExecutor(
        registry=registry,
        record_event=record_event,
        artifact_service=_FailingArtifactPort(),
        artifact_policies={entry.name: installation.artifact_processing_policy},
        runtime_session_id="runtime:artifact-policy",
    )

    result = executor.execute(
        ToolCall(id="call:artifact-failure", name=entry.name, arguments={}),
        event_context=CTX,
        descriptor=entry.descriptor,
        descriptor_attribution=descriptor_attribution_for_test(
            entry.descriptor,
            runtime_session_id="runtime:artifact-policy",
        ),
    )

    assert result.status is ToolResultState.ERROR
    assert result.prepared_terminal_result is not None
    assert result.prepared_terminal_result.state is ToolResultState.ERROR
    assert result.metadata["artifact_processing_failure_code"] == (
        "artifact_processing_failed"
    )
    assert isinstance(events[0], ToolResultStartEvent)
