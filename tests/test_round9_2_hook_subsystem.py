"""Round 9.2 independent Hook subsystem product and architecture gates."""

from __future__ import annotations

import asyncio
from dataclasses import fields, replace
import json
import os
from pathlib import Path
import shlex
import sys
import threading
from time import monotonic
from types import SimpleNamespace
from typing import get_args

import pytest

from pulsara_agent.cli import _hook_snapshot_public
from pulsara_agent.conversation_kernel.limits import STAGE2_LIMITS
from pulsara_agent.conversation_kernel.tool_contracts import (
    AcceptedCanonicalToolResultSettlement,
    PreparedPermissionRequest,
)
from pulsara_agent.conversation_kernel.vocabulary import (
    APPEND_GUARDS,
    COMMITTED_EVENT_DESCRIPTORS,
    LIVE_EVENT_TYPES,
    SUBJECT_SLOTS,
)
from pulsara_agent.hooks.config_parser import (
    HookConfigParseError,
    parse_hook_config,
)
from pulsara_agent.hooks.context import HookContextOwner
from pulsara_agent.hooks.contracts import (
    ContextOutcome,
    ContinuationDecision,
    ContinuationOutcome,
    DirectPromptRef,
    EVENT_OUTCOME_FAMILY,
    FrozenHookDefinition,
    FrozenHookDefinitionView,
    FrozenHookMatcherFact,
    FrozenHookSourceProvenance,
    FrozenHookSourceSnapshot,
    GateDecision,
    GateOutcome,
    HookDispatchCausalRef,
    HookDispatchEnvelope,
    HookDispatchScopeRef,
    HookEventType,
    HookScopeKind,
    HookSourceKind,
    HookSourceSnapshotDisposition,
    HookSourceTrustAssessment,
    HookTrustDisposition,
    HookVisibilityScope,
    LocalFileHookSourceIdentity,
    LocalFileHookTrustSubject,
    ObserveOutcome,
    PermissionDecision,
    PermissionOutcome,
    PermissionRef,
    PermissionRequestInput,
    PostCompactInput,
    PostCompactRef,
    PostToolRef,
    PostToolUseInput,
    PreCompactInput,
    PreCompactRef,
    PreToolRef,
    PreToolUseInput,
    QueuedPromptRef,
    SessionEndInput,
    SessionEndRef,
    SessionStartInput,
    SessionStartRef,
    StopInput,
    StopRef,
    SubagentStartInput,
    SubagentStartRef,
    SubagentStopInput,
    SubagentStopRef,
    UserPromptSubmitInput,
    external_permission_mode,
)
from pulsara_agent.hooks.dispatcher import KernelHookDispatcher, _aggregate
from pulsara_agent.hooks.executor import (
    API_KEY_REPLACEMENT,
    HookCommandExecution,
    HookCommandExecutor,
    HookExecutionRequest,
    HookSecretScrubSet,
)
from pulsara_agent.hooks.matcher import (
    event_matcher_subject,
    tool_matcher_subject,
)
from pulsara_agent.hooks.output_parser import (
    HandlerFailure,
    InvalidHandlerOutput,
    ValidHandlerContribution,
    parse_handler_output,
)
from pulsara_agent.process_api_key_boundary import ProcessApiKeyBoundary
from pulsara_agent.hooks.source import LocalHookSourceProvider
from pulsara_agent.hooks.trust import normalized_definition_digest
from pulsara_agent.storage.migrations.manifest import CONVERSATION_KERNEL_RELATIONS


ROOT = Path(__file__).resolve().parents[1]


class _Diagnostics:
    def __init__(self) -> None:
        self.values = []

    def offer(self, diagnostics) -> None:
        self.values.extend(diagnostics)


class _Estimator:
    def estimate_text(self, text: str) -> int:
        return len(text.split())


class _ScriptedExecutor:
    def __init__(self, outputs: dict[str, tuple[int, bytes, bytes]]) -> None:
        self.outputs = outputs
        self.calls: list[HookExecutionRequest] = []
        self.release: asyncio.Event | None = None

    async def execute(self, request: HookExecutionRequest) -> HookCommandExecution:
        self.calls.append(request)
        if self.release is not None:
            await self.release.wait()
        exit_code, stdout, stderr = self.outputs[request.definition.command]
        return HookCommandExecution(
            request.definition,
            request.event_dispatch_ordinal,
            request.source_ordinal,
            exit_code,
            stdout,
            stderr,
            scrub_set=HookSecretScrubSet.capture(),
        )

    async def aclose(self, *, deadline_monotonic: float | None = None) -> None:
        del deadline_monotonic


def _provenance(
    root: Path,
    *,
    kind: HookSourceKind = HookSourceKind.USER_FILE,
    label: str = "USER",
) -> FrozenHookSourceProvenance:
    workspace_key = "workspace:test" if kind is HookSourceKind.WORKSPACE_FILE else None
    return FrozenHookSourceProvenance(
        LocalFileHookSourceIdentity(
            kind,
            (
                root / ("workspace-hooks.json" if workspace_key else "hooks.json")
            ).resolve(),
            HookVisibilityScope.WORKSPACE
            if workspace_key
            else HookVisibilityScope.USER,
            workspace_key,
        ),
        LocalFileHookTrustSubject(kind, workspace_key),
        None,
        label,
    )


def _definition(
    root: Path,
    event: HookEventType,
    command: str,
    *,
    ordinal: int = 0,
    asynchronous: bool = False,
    matcher: str = "",
    context_limit: int = 0,
    status: str | None = None,
) -> FrozenHookDefinition:
    return FrozenHookDefinition(
        _provenance(root),
        ordinal,
        list(HookEventType).index(event),
        0,
        ordinal,
        event,
        FrozenHookMatcherFact(matcher, matcher in {"", "*"}),
        command,
        None,
        1 if event is HookEventType.SESSION_END_EVENT else 30,
        asynchronous,
        status,
        context_limit,
    )


def _view(
    root: Path, definitions: tuple[FrozenHookDefinition, ...]
) -> FrozenHookDefinitionView:
    provenance = definitions[0].provenance if definitions else _provenance(root)
    snapshot = FrozenHookSourceSnapshot(
        provenance,
        HookSourceSnapshotDisposition.COMPLETE,
        definitions,
        (),
        HookSourceTrustAssessment(
            HookTrustDisposition.TRUSTED,
            "0" * 64,
            "0" * 64,
            True,
            "2026-08-24T00:00:00+00:00",
        ),
    )
    return FrozenHookDefinitionView((snapshot,))


def _scope(*, child_task_id: str | None = None) -> HookDispatchScopeRef:
    return HookDispatchScopeRef(
        object(),
        object(),
        HookScopeKind.CHILD if child_task_id is not None else HookScopeKind.ROOT,
        child_task_id,
    )


def _inputs() -> tuple[object, ...]:
    common = {"session_id": "session:1", "cwd": "/tmp", "model": "model:1"}
    return (
        SessionStartInput(**common, source="startup", permission_mode="default"),
        SessionEndInput(**common),
        UserPromptSubmitInput(
            **common,
            turn_id="turn:1",
            prompt="hello",
            permission_mode="acceptEdits",
        ),
        PreToolUseInput(
            **common,
            turn_id="turn:1",
            tool_name="Bash",
            tool_use_id="call:1",
            tool_input={"command": "pwd"},
            permission_mode="bypassPermissions",
            pulsara_tool_name="terminal",
        ),
        PermissionRequestInput(
            **common,
            turn_id="turn:1",
            tool_name="remote.tool",
            tool_input={"value": 1},
            permission_mode="default",
            pulsara_tool_name="use_new_mcp_tool",
        ),
        PostToolUseInput(
            **common,
            turn_id="turn:1",
            tool_name="remote.tool",
            tool_use_id="call:1",
            tool_input={"value": 1},
            tool_response={"status": "ok"},
            permission_mode="default",
            pulsara_tool_name="use_new_mcp_tool",
        ),
        PreCompactInput(**common, turn_id="turn:1", trigger="auto"),
        PostCompactInput(**common, turn_id="turn:1", trigger="auto"),
        SubagentStartInput(
            **common,
            turn_id="turn:child",
            agent_id="task:1",
            agent_type="research_worker",
            permission_mode="dontAsk",
        ),
        SubagentStopInput(
            **common,
            turn_id="turn:child",
            agent_id="task:1",
            agent_type="research_worker",
            stop_hook_active=False,
            last_assistant_message="done",
            permission_mode="dontAsk",
        ),
        StopInput(
            **common,
            turn_id="turn:1",
            stop_hook_active=False,
            last_assistant_message="done",
            permission_mode="default",
        ),
    )


def _causal_refs() -> tuple[object, ...]:
    return (
        SessionStartRef(object(), "startup"),
        SessionEndRef(object()),
        DirectPromptRef("command:1", "turn:1", "entry:1", "revision:1"),
        QueuedPromptRef("command:1", "queue:1", "turn:2", "entry:2", "revision:2"),
        PreToolRef("turn:1", "entry:assistant", "call:1", "terminal"),
        PermissionRef("turn:1", "call:1", object()),
        PostToolRef(
            "turn:1", "call:1", "result:1", "entry:result", "SUCCESS", object()
        ),
        PreCompactRef(object(), "ROOT", "turn:1", "auto"),
        PostCompactRef(object(), "snapshot:1", "revision:compact"),
        SubagentStartRef("event:start", "task:1", "turn:child"),
        SubagentStopRef("task:1", "INFERRED", "entry:assistant"),
        StopRef("turn:1", "entry:assistant", object(), "revision:1"),
    )


def _execution(
    definition: FrozenHookDefinition,
    *,
    stdout: bytes = b"",
    stderr: bytes = b"",
    exit_code: int = 0,
) -> HookCommandExecution:
    return HookCommandExecution(
        definition,
        0,
        0,
        exit_code,
        stdout,
        stderr,
        scrub_set=HookSecretScrubSet.capture(),
    )


def test_round9_2_closed_vocabulary_inputs_outcomes_causal_arms_and_oracle() -> None:
    inputs = _inputs()
    causal_refs = _causal_refs()
    alias = HookDispatchCausalRef.__value__

    assert len(HookEventType) == 11
    assert len(inputs) == 11
    assert len(EVENT_OUTCOME_FAMILY) == 11
    assert {value.__name__ for value in EVENT_OUTCOME_FAMILY.values()} == {
        "ObserveOutcome",
        "ContextOutcome",
        "GateOutcome",
        "PermissionOutcome",
        "ContinuationOutcome",
    }
    assert len(get_args(alias)) == 12
    assert {type(item) for item in causal_refs} == set(get_args(alias))

    internal_fields = {
        "scope",
        "causal_ref",
        "deadline_monotonic",
        "cancellation_signal",
        "request_nonce",
        "epoch_nonce",
    }
    for public_input in inputs:
        wire = public_input.to_wire()
        assert wire["hook_event_name"] == public_input.event_type.external_name
        assert wire["transcript_path"] is None
        assert wire.keys().isdisjoint(internal_fields)
        assert wire["session_id"] == "session:1"
        assert wire["cwd"] == "/tmp"
        assert wire["model"] == "model:1"
    assert "permission_mode" not in inputs[1].to_wire()
    assert "permission_mode" not in inputs[6].to_wire()
    assert "permission_mode" not in inputs[7].to_wire()

    assert external_permission_mode("ask-permissions") == "default"
    assert external_permission_mode("accept-edits") == "acceptEdits"
    assert external_permission_mode("read-only") == "dontAsk"
    assert external_permission_mode("read-only", active_plan_workflow=True) == "plan"
    assert external_permission_mode("bypass-permissions") == "bypassPermissions"

    assert len(COMMITTED_EVENT_DESCRIPTORS) == 29
    assert len(LIVE_EVENT_TYPES) == 24
    assert len(SUBJECT_SLOTS) == 11
    assert len(APPEND_GUARDS) == 1
    assert len(CONVERSATION_KERNEL_RELATIONS) == 25
    assert not hasattr(STAGE2_LIMITS, "model_calls_per_turn_hard")


def test_round9_2_parser_recovery_trust_and_exact_sources(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    (workspace / ".pulsara").mkdir(parents=True)
    provider = LocalHookSourceProvider(
        workspace_root=workspace,
        workspace_kind="project",
        workspace_state_key="workspace-safe-key",
        pulsara_home=home,
    )

    missing = provider.discover(deadline_monotonic=monotonic() + 10)
    assert len(missing.source_snapshots) == 2
    assert all(
        item.disposition is HookSourceSnapshotDisposition.COMPLETE
        and not item.definitions
        for item in missing.source_snapshots
    )
    expired = provider.discover(deadline_monotonic=monotonic() - 1)
    assert all(
        item.disposition is HookSourceSnapshotDisposition.UNAVAILABLE
        and "HOOK_SOURCE_UNAVAILABLE"
        in {diagnostic.code for diagnostic in item.diagnostics}
        for item in expired.source_snapshots
    )

    user_config = {
        "description": "reviewed user hooks",
        "ignoredRoot": True,
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash|apply_patch",
                    "hooks": [
                        {"type": "command", "command": "printf user"},
                        {"type": "prompt", "command": "ignored"},
                        {"type": "command", "command": "typo", "timout": 1},
                    ],
                },
                {"matcher": "[", "hooks": [{"type": "command", "command": "bad"}]},
            ],
            "SessionEnd": [
                {
                    "matcher": "other",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "printf end",
                            "async": True,
                        }
                    ],
                }
            ],
            "FutureEvent": [],
        },
    }
    workspace_config = {
        "description": "workspace hooks",
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash|apply_patch",
                    "hooks": [{"type": "command", "command": "printf user"}],
                }
            ]
        },
    }
    (home / "hooks.json").write_text(json.dumps(user_config), encoding="utf-8")
    (workspace / ".pulsara" / "hooks.json").write_text(
        json.dumps(workspace_config), encoding="utf-8"
    )
    observed = provider.discover(deadline_monotonic=monotonic() + 10)
    user, project = observed.source_snapshots
    assert user.provenance.description == "reviewed user hooks"
    assert [item.command for item in user.definitions] == ["printf user", "printf end"]
    assert user.definitions[1].asynchronous is False
    assert {item.code for item in user.diagnostics} >= {
        "HOOK_CONFIG_UNKNOWN_ROOT_FIELD",
        "HOOK_CONFIG_KNOWN_UNSUPPORTED_HANDLER",
        "HOOK_CONFIG_UNSUPPORTED_HANDLER",
        "HOOK_CONFIG_INVALID_MATCHER",
        "HOOK_CONFIG_SESSION_END_ASYNC_IGNORED",
        "HOOK_CONFIG_UNKNOWN_EVENT",
    }
    assert project.definitions[0].command == user.definitions[0].command
    assert user.trust.disposition is HookTrustDisposition.UNTRUSTED
    assert project.trust.disposition is HookTrustDisposition.UNTRUSTED

    for snapshot in observed.source_snapshots:
        digest = snapshot.trust.current_definition_digest
        assert digest is not None
        provider.trust_store.trust(
            snapshot.provenance.trust_subject, expected_digest=digest
        )
    trusted = provider.discover(deadline_monotonic=monotonic() + 10)
    assert all(item.runnable for item in trusted.source_snapshots)
    assert [
        (source, definition.command)
        for source, definition in trusted.selected_definitions(
            HookEventType.PRE_TOOL_USE_EVENT
        )
    ] == [(0, "printf user"), (1, "printf user")]
    public = _hook_snapshot_public(trusted.source_snapshots[0], inspect=True)
    assert public["description"] == "reviewed user hooks"
    assert public["trusted_at"]
    assert public["definitions"][0]["command"] == "printf user"

    # Whitespace and object-field ordering do not change normalized trust.
    equivalent = {
        "hooks": user_config["hooks"],
        "ignoredRoot": True,
        "description": "reviewed user hooks",
    }
    (home / "hooks.json").write_text(
        json.dumps(equivalent, indent=4, ensure_ascii=False), encoding="utf-8"
    )
    same = provider.discover(deadline_monotonic=monotonic() + 10)
    assert same.source_snapshots[0].trust.disposition is HookTrustDisposition.TRUSTED

    script = home / "unhashed-script.py"
    script.write_text("print('one')", encoding="utf-8")
    digest_before = same.source_snapshots[0].trust.current_definition_digest
    script.write_text("print('two')", encoding="utf-8")
    assert (
        provider.discover(deadline_monotonic=monotonic() + 10)
        .source_snapshots[0]
        .trust.current_definition_digest
        == digest_before
    )

    changed = json.loads((home / "hooks.json").read_text(encoding="utf-8"))
    changed["hooks"]["PreToolUse"][0]["hooks"][0]["command"] = "printf changed"
    (home / "hooks.json").write_text(json.dumps(changed), encoding="utf-8")
    modified = provider.discover(deadline_monotonic=monotonic() + 10)
    assert (
        modified.source_snapshots[0].trust.disposition is HookTrustDisposition.MODIFIED
    )
    assert not modified.source_snapshots[0].runnable

    trust_path = home / "hooks" / "trust" / "user.json"
    trust_path.write_text("not-json", encoding="utf-8")
    invalid_trust = provider.discover(deadline_monotonic=monotonic() + 10)
    assert (
        invalid_trust.source_snapshots[0].trust.disposition
        is HookTrustDisposition.UNTRUSTED
    )
    assert "HOOK_TRUST_STATE_INVALID" in {
        item.code for item in invalid_trust.source_snapshots[0].diagnostics
    }
    repaired_digest = invalid_trust.source_snapshots[0].trust.current_definition_digest
    assert repaired_digest is not None
    provider.trust_store.trust_after_revalidation(
        invalid_trust.source_snapshots[0].provenance.trust_subject,
        expected_digest=repaired_digest,
        current_digest_reader=lambda: repaired_digest,
    )
    assert provider.discover().source_snapshots[0].runnable

    target = tmp_path / "outside.json"
    target.write_text('{"hooks":{}}', encoding="utf-8")
    workspace_path = workspace / ".pulsara" / "hooks.json"
    workspace_path.unlink()
    workspace_path.symlink_to(target)
    unavailable = provider.discover(deadline_monotonic=monotonic() + 10)
    assert (
        unavailable.source_snapshots[1].disposition
        is HookSourceSnapshotDisposition.UNAVAILABLE
    )


def test_round9_2_nonregular_source_cannot_outlive_discovery_deadline(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    fifo = home / "hooks.json"
    os.mkfifo(fifo)
    provider = LocalHookSourceProvider(
        workspace_root=workspace,
        workspace_kind="transient",
        workspace_state_key="workspace-safe-key",
        pulsara_home=home,
    )

    release = threading.Event()

    def unblock_a_regressed_reader() -> None:
        if release.wait(0.3):
            return
        descriptor = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        os.close(descriptor)

    writer = threading.Thread(target=unblock_a_regressed_reader, daemon=True)
    writer.start()
    started = monotonic()
    observed = provider.discover(deadline_monotonic=started + 0.05)
    elapsed = monotonic() - started
    release.set()
    writer.join(timeout=1)

    assert not writer.is_alive()
    assert elapsed < 0.2
    assert (
        observed.source_snapshots[0].disposition
        is HookSourceSnapshotDisposition.UNAVAILABLE
    )


def test_round9_2_user_source_binds_root_alias_before_no_follow(
    tmp_path: Path,
) -> None:
    physical_parent = tmp_path / "private"
    physical_home = physical_parent / "home"
    physical_home.mkdir(parents=True)
    configured_parent = tmp_path / "var"
    configured_parent.symlink_to(physical_parent, target_is_directory=True)
    configured_home = configured_parent / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = physical_home / "hooks.json"
    source.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "read_file",
                            "hooks": [{"type": "command", "command": "printf alias"}],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    provider = LocalHookSourceProvider(
        workspace_root=workspace,
        workspace_kind="transient",
        workspace_state_key="transient:root-alias",
        pulsara_home=configured_home,
    )

    observed = provider.discover(deadline_monotonic=monotonic() + 10)
    snapshot = observed.source_snapshots[0]
    assert snapshot.disposition is HookSourceSnapshotDisposition.COMPLETE
    assert [definition.command for definition in snapshot.definitions] == [
        "printf alias"
    ]
    assert snapshot.trust.current_definition_digest is not None

    outside = tmp_path / "outside-hooks.json"
    outside.write_text('{"hooks":{}}', encoding="utf-8")
    source.unlink()
    source.symlink_to(outside)
    unavailable = provider.discover(deadline_monotonic=monotonic() + 10)
    assert (
        unavailable.source_snapshots[0].disposition
        is HookSourceSnapshotDisposition.UNAVAILABLE
    )


def test_round9_2_parser_rejects_duplicate_keys_nonfinite_and_source_bounds(
    tmp_path: Path,
) -> None:
    provenance = _provenance(tmp_path)
    for raw in (
        b'{"hooks":{},"hooks":{}}',
        b'{"hooks":{},"value":NaN}',
        b"[]",
        b"\xff",
    ):
        with pytest.raises(HookConfigParseError):
            parse_hook_config(raw, provenance=provenance)

    nested = '{"hooks":' + "[" * 70 + "0" + "]" * 70 + "}"
    with pytest.raises(HookConfigParseError):
        parse_hook_config(nested.encode(), provenance=provenance)


def test_round9_2_matcher_aliases_and_no_exact_alternative_bypass(
    tmp_path: Path,
) -> None:
    terminal = tool_matcher_subject("terminal")
    terminal_process = tool_matcher_subject("terminal_process")
    edit = tool_matcher_subject("edit_file")
    write = tool_matcher_subject("write_file")
    agent = tool_matcher_subject("spawn_agent")
    task_batch = tool_matcher_subject("create_agent_tasks")
    remote = tool_matcher_subject(
        "use_new_mcp_tool", resolved_remote_identity="server.remote_tool"
    )

    assert terminal.external_primary == "Bash" and "Bash" in terminal.candidates
    assert terminal_process.external_primary == "terminal_process"
    assert "Bash" not in terminal_process.candidates
    assert edit.external_primary == "apply_patch" and "Edit" in edit.candidates
    assert write.external_primary == "apply_patch" and "Write" in write.candidates
    assert "Agent" in agent.candidates and "Agent" not in task_batch.candidates
    assert remote.candidates == ("server.remote_tool",)
    assert remote.pulsara_tool_name == "use_new_mcp_tool"

    definition = _definition(
        tmp_path, HookEventType.PRE_TOOL_USE_EVENT, "noop", matcher="Bash|apply_patch"
    )
    from pulsara_agent.hooks.matcher import definition_matches

    assert definition_matches(definition, terminal)
    assert definition_matches(definition, edit)
    assert not definition_matches(definition, terminal_process)
    invalid = parse_hook_config(
        b'{"hooks":{"PreToolUse":[{"matcher":"(?=x)","hooks":[]}]}}',
        provenance=_provenance(tmp_path),
    )
    assert not invalid.definitions
    assert {item.code for item in invalid.diagnostics} == {
        "HOOK_CONFIG_INVALID_MATCHER"
    }


def test_round9_2_single_parser_control_matrix_and_invalid_output(
    tmp_path: Path,
) -> None:
    def parsed(
        event: HookEventType,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        exit_code: int = 0,
        asynchronous: bool = False,
    ):
        definition = _definition(
            tmp_path, event, event.external_name, asynchronous=asynchronous
        )
        return parse_handler_output(
            _execution(
                definition,
                stdout=stdout,
                stderr=stderr,
                exit_code=exit_code,
            )
        )

    assert isinstance(
        parsed(HookEventType.SESSION_START_EVENT, stdout=b"context"),
        ValidHandlerContribution,
    )
    assert isinstance(parsed(HookEventType.SESSION_END_EVENT), ValidHandlerContribution)
    prompt = parsed(
        HookEventType.USER_PROMPT_SUBMIT_EVENT,
        exit_code=2,
        stderr=b"blocked prompt",
    )
    assert isinstance(prompt, ValidHandlerContribution) and prompt.gate_block
    pre = parsed(
        HookEventType.PRE_TOOL_USE_EVENT,
        stdout=json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": "policy",
                    "additionalContext": "pre context",
                }
            }
        ).encode(),
    )
    assert isinstance(pre, ValidHandlerContribution) and pre.gate_block
    permission = parsed(
        HookEventType.PERMISSION_REQUEST_EVENT,
        stdout=b'{"hookSpecificOutput":{"hookEventName":"PermissionRequest","decision":{"behavior":"allow","message":"reviewed"}}}',
    )
    assert (
        isinstance(permission, ValidHandlerContribution)
        and permission.permission_decision is PermissionDecision.ALLOW
    )
    post = parsed(
        HookEventType.POST_TOOL_USE_EVENT,
        stdout=b'{"continue":true,"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"feedback"}}',
    )
    assert (
        isinstance(post, ValidHandlerContribution) and post.context_text == "feedback"
    )
    precompact = parsed(
        HookEventType.PRE_COMPACT_EVENT,
        stdout=b'{"continue":false,"stopReason":"wait"}',
    )
    assert isinstance(precompact, ValidHandlerContribution) and precompact.gate_block
    postcompact = parsed(HookEventType.POST_COMPACT_EVENT, stdout=b'{"continue":true}')
    assert (
        isinstance(postcompact, ValidHandlerContribution) and not postcompact.gate_block
    )
    substart = parsed(HookEventType.SUBAGENT_START_EVENT, stdout=b"worker context")
    assert isinstance(substart, ValidHandlerContribution) and substart.context_text
    substop = parsed(HookEventType.SUBAGENT_STOP_EVENT, exit_code=2, stderr=b"more")
    assert (
        isinstance(substop, ValidHandlerContribution) and substop.continuation_request
    )
    stop = parsed(
        HookEventType.STOP_EVENT, stdout=b'{"continue":false,"stopReason":"done"}'
    )
    assert isinstance(stop, ValidHandlerContribution) and stop.explicit_terminalize

    invalid_cases = (
        (
            HookEventType.PRE_TOOL_USE_EVENT,
            b'{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow"}}',
        ),
        (HookEventType.POST_TOOL_USE_EVENT, b'{"continue":false}'),
        (HookEventType.POST_TOOL_USE_EVENT, b'{"updatedMCPToolOutput":{}}'),
        (HookEventType.STOP_EVENT, b"plain continuation"),
        (HookEventType.SESSION_END_EVENT, b'{"continue":false}'),
        (
            HookEventType.USER_PROMPT_SUBMIT_EVENT,
            b'{"hookSpecificOutput":{"hookEventName":"Stop","additionalContext":"wrong"}}',
        ),
        (
            HookEventType.USER_PROMPT_SUBMIT_EVENT,
            b'{"decision":"block","reason":"x","unknown":1}',
        ),
    )
    for event, stdout in invalid_cases:
        assert isinstance(parsed(event, stdout=stdout), InvalidHandlerOutput)
    assert isinstance(
        parsed(HookEventType.POST_TOOL_USE_EVENT, exit_code=2), HandlerFailure
    )
    assert isinstance(
        parsed(
            HookEventType.USER_PROMPT_SUBMIT_EVENT,
            stdout=b'{"continue":true,"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"x"}}',
            asynchronous=True,
        ),
        InvalidHandlerOutput,
    )


def test_round9_2_ordered_aggregation_precedence_and_once_guard(tmp_path: Path) -> None:
    scrub = HookSecretScrubSet.capture()
    gate_definitions = tuple(
        _definition(
            tmp_path,
            HookEventType.PRE_TOOL_USE_EVENT,
            f"gate:{ordinal}",
            ordinal=ordinal,
        )
        for ordinal in range(3)
    )
    gate_values = (
        ValidHandlerContribution(
            gate_definitions[0], 1, 0, context_text="one", scrub_set=scrub
        ),
        ValidHandlerContribution(
            gate_definitions[1],
            1,
            0,
            gate_block=True,
            reason="first block",
            scrub_set=scrub,
        ),
        ValidHandlerContribution(
            gate_definitions[2],
            1,
            0,
            gate_block=True,
            reason="later block",
            scrub_set=scrub,
        ),
    )
    gate = _aggregate(
        HookEventType.PRE_TOOL_USE_EVENT,
        gate_values,
        continuation_already_used=False,
    )
    assert isinstance(gate, GateOutcome)
    assert gate.decision is GateDecision.BLOCK and gate.reason == "first block"
    assert [item.text for item in gate.context_entries] == ["one"]

    permission_definitions = tuple(
        _definition(
            tmp_path,
            HookEventType.PERMISSION_REQUEST_EVENT,
            f"permission:{ordinal}",
            ordinal=ordinal,
        )
        for ordinal in range(3)
    )
    permission = _aggregate(
        HookEventType.PERMISSION_REQUEST_EVENT,
        (
            ValidHandlerContribution(
                permission_definitions[0],
                2,
                0,
                permission_decision=PermissionDecision.ALLOW,
                reason="allow reason",
                scrub_set=scrub,
            ),
            ValidHandlerContribution(
                permission_definitions[1],
                2,
                0,
                permission_decision=PermissionDecision.DENY,
                scrub_set=scrub,
            ),
            ValidHandlerContribution(
                permission_definitions[2],
                2,
                0,
                permission_decision=PermissionDecision.DENY,
                reason="deny reason",
                scrub_set=scrub,
            ),
        ),
        continuation_already_used=False,
    )
    assert isinstance(permission, PermissionOutcome)
    assert permission.decision is PermissionDecision.DENY
    assert permission.reason == "deny reason"

    stop_definitions = tuple(
        _definition(
            tmp_path,
            HookEventType.STOP_EVENT,
            f"stop:{ordinal}",
            ordinal=ordinal,
        )
        for ordinal in range(2)
    )
    request = ValidHandlerContribution(
        stop_definitions[0],
        3,
        0,
        continuation_request=True,
        reason="continue",
        scrub_set=scrub,
    )
    veto = ValidHandlerContribution(
        stop_definitions[1],
        3,
        0,
        explicit_terminalize=True,
        reason="terminal",
        scrub_set=scrub,
    )
    terminal = _aggregate(
        HookEventType.STOP_EVENT,
        (request, veto),
        continuation_already_used=False,
    )
    assert isinstance(terminal, ContinuationOutcome)
    assert terminal.decision is ContinuationDecision.TERMINALIZE
    assert terminal.reason == "terminal"
    continued = _aggregate(
        HookEventType.STOP_EVENT,
        (request,),
        continuation_already_used=False,
    )
    assert continued.decision is ContinuationDecision.CONTINUE_ONCE
    assert continued.continuation_source is not None
    later_reason = replace(
        request,
        definition=replace(request.definition, source_local_definition_ordinal=9),
        reason="later exact reason",
    )
    selected_reason = _aggregate(
        HookEventType.STOP_EVENT,
        (replace(request, reason=None), later_reason),
        continuation_already_used=False,
    )
    assert selected_reason.reason == "later exact reason"
    assert selected_reason.continuation_source is not None
    assert selected_reason.continuation_source.definition is later_reason.definition
    second = _aggregate(
        HookEventType.STOP_EVENT,
        (request,),
        continuation_already_used=True,
    )
    assert second.decision is ContinuationDecision.TERMINALIZE
    assert "HOOK_CONTINUATION_ALREADY_USED" in {
        item.code for item in second.diagnostics
    }


def test_round9_2_dispatcher_all_events_status_and_terminal_lane(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        outputs = {event.external_name: (0, b"", b"") for event in HookEventType}
        outputs["SessionStart"] = (0, b"start context", b"")
        outputs["UserPromptSubmit"] = (2, b"", b"prompt block")
        outputs["PreToolUse"] = (
            0,
            b'{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny"}}',
            b"",
        )
        outputs["PermissionRequest"] = (
            0,
            b'{"hookSpecificOutput":{"hookEventName":"PermissionRequest","decision":{"behavior":"allow"}}}',
            b"",
        )
        outputs["PostToolUse"] = (
            0,
            b'{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"post context"}}',
            b"",
        )
        outputs["PreCompact"] = (0, b'{"continue":false}', b"")
        outputs["PostCompact"] = (0, b'{"continue":true}', b"")
        outputs["SubagentStart"] = (0, b"child context", b"")
        outputs["SubagentStop"] = (2, b"", b"continue child")
        outputs["Stop"] = (0, b'{"continue":false}', b"")
        definitions = tuple(
            _definition(
                tmp_path,
                event,
                event.external_name,
                ordinal=ordinal,
                status=f"status:{event.external_name}",
            )
            for ordinal, event in enumerate(HookEventType)
        )
        executor = _ScriptedExecutor(outputs)
        diagnostics = _Diagnostics()
        dispatcher = KernelHookDispatcher(
            initial_view=_view(tmp_path, definitions),
            workspace_root=tmp_path,
            api_key_boundary=ProcessApiKeyBoundary(),
            executor=executor,  # type: ignore[arg-type]
            diagnostic_adapter=diagnostics,
        )
        scope = _scope()
        causal_by_event = dict(
            zip(
                HookEventType,
                (
                    _causal_refs()[0],
                    _causal_refs()[1],
                    _causal_refs()[2],
                    *_causal_refs()[4:],
                ),
                strict=True,
            )
        )
        outcomes = {}
        inputs = _inputs()
        for public_input in inputs:
            if public_input.event_type is HookEventType.SESSION_END_EVENT:
                continue
            outcome = await dispatcher.dispatch(
                HookDispatchEnvelope(
                    dispatcher.capture_view(),
                    scope,
                    public_input,
                    causal_by_event[public_input.event_type],
                    monotonic() + 10,
                ),
                matcher_subject=_matcher_for_input(public_input),
            )
            outcomes[public_input.event_type] = outcome
        terminal_view = await dispatcher.fence_ordinary()
        terminal_input = inputs[1]
        terminal = await dispatcher.dispatch_terminal(
            HookDispatchEnvelope(
                terminal_view,
                scope,
                terminal_input,
                causal_by_event[HookEventType.SESSION_END_EVENT],
                monotonic() + 10,
            ),
            matcher_subject=_matcher_for_input(terminal_input),
        )
        await dispatcher.aclose(deadline_monotonic=monotonic() + 10)

        assert isinstance(outcomes[HookEventType.SESSION_START_EVENT], GateOutcome)
        assert outcomes[HookEventType.SESSION_START_EVENT].context_entries
        assert (
            outcomes[HookEventType.USER_PROMPT_SUBMIT_EVENT].decision
            is GateDecision.BLOCK
        )
        assert outcomes[HookEventType.PRE_TOOL_USE_EVENT].decision is GateDecision.BLOCK
        assert (
            outcomes[HookEventType.PERMISSION_REQUEST_EVENT].decision
            is PermissionDecision.ALLOW
        )
        assert isinstance(outcomes[HookEventType.POST_TOOL_USE_EVENT], ContextOutcome)
        assert outcomes[HookEventType.PRE_COMPACT_EVENT].decision is GateDecision.BLOCK
        assert (
            outcomes[HookEventType.POST_COMPACT_EVENT].decision is GateDecision.PROCEED
        )
        assert isinstance(outcomes[HookEventType.SUBAGENT_START_EVENT], ContextOutcome)
        assert (
            outcomes[HookEventType.SUBAGENT_STOP_EVENT].decision
            is ContinuationDecision.CONTINUE_ONCE
        )
        assert (
            outcomes[HookEventType.STOP_EVENT].decision
            is ContinuationDecision.TERMINALIZE
        )
        assert isinstance(terminal, ObserveOutcome)
        assert len(executor.calls) == 11
        assert sum(item.code == "HOOK_STATUS" for item in diagnostics.values) == 11

    asyncio.run(exercise())


def _matcher_for_input(public_input):
    event = public_input.event_type
    if event is HookEventType.SESSION_START_EVENT:
        return event_matcher_subject(event, source=public_input.source)
    if event is HookEventType.SESSION_END_EVENT:
        return event_matcher_subject(event, reason=public_input.reason)
    if event in {HookEventType.PRE_COMPACT_EVENT, HookEventType.POST_COMPACT_EVENT}:
        return event_matcher_subject(event, trigger=public_input.trigger)
    if event in {HookEventType.SUBAGENT_START_EVENT, HookEventType.SUBAGENT_STOP_EVENT}:
        return event_matcher_subject(event, agent_type=public_input.agent_type)
    if event in {
        HookEventType.PRE_TOOL_USE_EVENT,
        HookEventType.PERMISSION_REQUEST_EVENT,
        HookEventType.POST_TOOL_USE_EVENT,
    }:
        return event_matcher_subject(event, tool=tool_matcher_subject("terminal"))
    return event_matcher_subject(event)


def test_round9_2_real_command_executor_secret_environment_stdin_output_and_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        secret = "round92-secret-value"
        future_secret = "round92-future-secret"
        monkeypatch.setenv("PULSARA_API_KEY", secret)
        monkeypatch.setenv("HOOK_SECRET_COPY", secret)
        secret_file = tmp_path / "secret.txt"
        secret_file.write_text(secret, encoding="utf-8")
        script = tmp_path / "handler.py"
        script.write_text(
            """
import json, os, pathlib, sys
value = json.load(sys.stdin)
secret = pathlib.Path(sys.argv[1]).read_text(encoding='utf-8')
print(json.dumps({'hookSpecificOutput': {
    'hookEventName': 'UserPromptSubmit',
    'additionalContext': 'prompt=' + value['prompt']
        + '|ambient=' + os.environ.get('HOOK_SECRET_COPY', '<missing>')
        + '|api=' + str('PULSARA_API_KEY' in os.environ)
        + '|file=' + secret
        + '|future=round92-future-secret'
}}))
print('visible stderr ' + secret, file=sys.stderr)
""".strip(),
            encoding="utf-8",
        )
        command = " ".join(
            (
                shlex.quote(sys.executable),
                shlex.quote(str(script)),
                shlex.quote(str(secret_file)),
            )
        )
        definition = _definition(
            tmp_path,
            HookEventType.USER_PROMPT_SUBMIT_EVENT,
            command,
            context_limit=0,
        )
        scope = _scope()
        request = HookExecutionRequest(
            definition,
            UserPromptSubmitInput(
                "session:1",
                str(tmp_path),
                "model:1",
                "turn:1",
                "prompt contains " + secret,
                "default",
            ).to_wire(),
            scope,
            1,
            0,
            tmp_path,
            tmp_path,
            monotonic() + 10,
        )
        executor = HookCommandExecutor(api_key_boundary=ProcessApiKeyBoundary())
        execution = await executor.execute(request)
        assert execution.failure_code is None and execution.exit_code == 0
        parsed = parse_handler_output(execution)
        assert isinstance(parsed, ValidHandlerContribution)
        assert parsed.context_text is not None
        assert secret not in parsed.context_text
        assert API_KEY_REPLACEMENT.decode() in parsed.context_text
        assert "api=False" in parsed.context_text
        assert any(item.code == "HOOK_STDERR" for item in parsed.diagnostics)
        assert all(secret not in item.message for item in parsed.diagnostics)

        # A later key rotation is scrubbed at the final context/provider source,
        # using the same attempt-owned scrub set retained by the contribution.
        parsed = replace(parsed, scrub_set=execution.scrub_set)
        from pulsara_agent.hooks.dispatcher import _aggregate as aggregate

        outcome = aggregate(
            HookEventType.USER_PROMPT_SUBMIT_EVENT,
            (parsed,),
            continuation_already_used=False,
        )
        owner = HookContextOwner()
        owner.register_scope(scope)
        owner.accept_sync(
            scope=scope,
            causal_ref=DirectPromptRef("command:1", "turn:1", "entry:1", "revision:1"),
            entries=outcome.context_entries,
        )
        monkeypatch.setenv("PULSARA_API_KEY", future_secret)
        prepared = owner.freeze_for_target(
            scope_kind="ROOT", child_task_id=None, estimator=_Estimator()
        )
        assert prepared is not None
        assert secret not in prepared.full_text
        assert future_secret not in prepared.full_text
        assert "HOOK_CONTEXT" in prepared.full_text
        prepared.reservation.retire()
        assert (
            owner.freeze_for_target(
                scope_kind="ROOT", child_task_id=None, estimator=_Estimator()
            )
            is None
        )

        marker = tmp_path / "must-not-spawn"
        rejected = replace(
            definition,
            command=f"touch {shlex.quote(str(marker))} {future_secret}",
        )
        denied = await executor.execute(replace(request, definition=rejected))
        assert denied.failure_code == "API_KEY_VALUE_PRESENT"
        assert not marker.exists()

        # A command that never reads a maximum-sized stdin remains governed by
        # the original dispatch deadline; pipe backpressure cannot hang it.
        sleeper = tmp_path / "sleeper.py"
        sleeper.write_text("import time; time.sleep(10)", encoding="utf-8")
        timeout_definition = replace(
            definition,
            command=f"{shlex.quote(sys.executable)} {shlex.quote(str(sleeper))}",
            timeout_seconds=1,
        )
        large = dict(request.public_stdin)
        large["prompt"] = "x" * (512 * 1024)
        started = monotonic()
        timeout = await executor.execute(
            replace(
                request,
                definition=timeout_definition,
                public_stdin=large,
                inherited_deadline_monotonic=monotonic() + 1.2,
            )
        )
        assert timeout.failure_code == "HOOK_TIMEOUT"
        assert monotonic() - started < 3

        # Host close dynamically bounds/cancels an already-running ordinary
        # attempt, while the subsequent terminal SessionEnd lane remains able
        # to run within the same absolute deadline.
        close_started = tmp_path / "close-started"
        close_script = tmp_path / "close-handler.py"
        close_script.write_text(
            "import pathlib, sys, time\n"
            "pathlib.Path(sys.argv[1]).write_text('started', encoding='utf-8')\n"
            "time.sleep(10)\n",
            encoding="utf-8",
        )
        closing_definition = replace(
            definition,
            command=(
                f"{shlex.quote(sys.executable)} {shlex.quote(str(close_script))} "
                f"{shlex.quote(str(close_started))}"
            ),
            timeout_seconds=30,
        )
        closing_attempt = asyncio.create_task(
            executor.execute(replace(request, definition=closing_definition))
        )
        while not close_started.exists():
            await asyncio.sleep(0.01)
        close_deadline = monotonic() + 2
        executor.begin_host_close(deadline_monotonic=close_deadline)
        cancelled = await closing_attempt
        assert cancelled.failure_code == "HOOK_CANCELLED"
        terminal_definition = replace(
            definition,
            event_type=HookEventType.SESSION_END_EVENT,
            command=f"{shlex.quote(sys.executable)} -c {shlex.quote('pass')}",
            timeout_seconds=1,
        )
        terminal_execution = await executor.execute(
            replace(
                request,
                definition=terminal_definition,
                inherited_deadline_monotonic=close_deadline,
            )
        )
        assert terminal_execution.failure_code is None
        assert terminal_execution.exit_code == 0
        await executor.aclose(deadline_monotonic=monotonic() + 5)

    asyncio.run(exercise())


def test_round9_2_context_scope_threshold_occurrence_and_terminal_drop(
    tmp_path: Path,
) -> None:
    scrub = HookSecretScrubSet.capture()
    root = _scope()
    child = _scope(child_task_id="task:1")
    owner = HookContextOwner()
    owner.register_scope(root)
    owner.register_scope(child)
    definition = _definition(
        tmp_path,
        HookEventType.POST_TOOL_USE_EVENT,
        "noop",
        context_limit=2,
    )
    from pulsara_agent.hooks.contracts import HookContextEntry

    entry = HookContextEntry(definition, 0, 1, "three token value", scrub)
    owner.accept_sync(
        scope=root,
        causal_ref=PostToolRef(
            "turn:1", "call:1", "result:1", "entry:1", "SUCCESS", object()
        ),
        entries=(entry,),
    )
    assert (
        owner.freeze_for_target(
            scope_kind="ROOT", child_task_id=None, estimator=_Estimator()
        )
        is None
    )

    accepted = replace(definition, additional_context_limit=0)
    first = HookContextEntry(accepted, 0, 2, "identical", scrub)
    second = HookContextEntry(accepted, 0, 3, "identical", scrub)
    owner.accept_sync(
        scope=root,
        causal_ref=PostToolRef(
            "turn:1", "call:1", "result:2", "entry:2", "SUCCESS", object()
        ),
        entries=(first,),
    )
    owner.accept_sync(
        scope=root,
        causal_ref=PostToolRef(
            "turn:1", "call:2", "result:3", "entry:3", "SUCCESS", object()
        ),
        entries=(second,),
    )
    prepared = owner.freeze_for_target(
        scope_kind="ROOT", child_task_id=None, estimator=_Estimator()
    )
    assert prepared is not None
    assert prepared.full_text.count("identical") == 2
    assert len(set(prepared.domain_identity)) == 2
    assert (
        owner.freeze_for_target(
            scope_kind="CHILD", child_task_id="task:1", estimator=_Estimator()
        )
        is None
    )
    prepared.reservation.retire()

    owner.accept_sync(
        scope=child,
        causal_ref=SubagentStartRef("event:1", "task:1", "turn:child"),
        entries=(HookContextEntry(accepted, 0, 4, "child only", scrub),),
    )
    owner.retire_scope(child)
    assert (
        owner.freeze_for_target(
            scope_kind="CHILD", child_task_id="task:1", estimator=_Estimator()
        )
        is None
    )


def test_round9_2_reload_publishes_future_view_and_old_attempt_keeps_reference(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        old = _definition(tmp_path, HookEventType.POST_TOOL_USE_EVENT, "old")
        new = _definition(tmp_path, HookEventType.POST_TOOL_USE_EVENT, "new")
        old_view = _view(tmp_path, (old,))
        new_view = _view(tmp_path, (new,))

        class _Provider:
            def discover(self, *, deadline_monotonic=None):
                del deadline_monotonic
                return new_view

        executor = _ScriptedExecutor({"old": (0, b"", b""), "new": (0, b"", b"")})
        executor.release = asyncio.Event()
        dispatcher = KernelHookDispatcher(
            initial_view=old_view,
            workspace_root=tmp_path,
            source_provider=_Provider(),  # type: ignore[arg-type]
            api_key_boundary=ProcessApiKeyBoundary(),
            executor=executor,  # type: ignore[arg-type]
        )
        scope = _scope()
        public_input = _inputs()[5]
        causal = _causal_refs()[6]
        old_attempt = asyncio.create_task(
            dispatcher.dispatch(
                HookDispatchEnvelope(
                    old_view,
                    scope,
                    public_input,
                    causal,
                    monotonic() + 10,
                ),
                matcher_subject=tool_matcher_subject("terminal"),
            )
        )
        while not executor.calls:
            await asyncio.sleep(0)

        async def publish(predecessor, replacement):
            return dispatcher.publish_scanned_view(predecessor, replacement)

        replacement = await dispatcher.reload(
            deadline_monotonic=monotonic() + 10,
            publish_scanned_view=publish,
        )
        assert replacement is new_view
        assert dispatcher.current_view is new_view
        executor.release.set()
        await old_attempt
        await dispatcher.dispatch(
            HookDispatchEnvelope(
                dispatcher.capture_view(),
                scope,
                public_input,
                causal,
                monotonic() + 10,
            ),
            matcher_subject=tool_matcher_subject("terminal"),
        )
        assert [item.definition.command for item in executor.calls] == ["old", "new"]
        await dispatcher.fence_ordinary()
        await dispatcher.aclose(deadline_monotonic=monotonic() + 10)

    asyncio.run(exercise())


def test_round9_2_architecture_has_one_independent_engine_and_no_new_durability() -> (
    None
):
    hook_files = tuple((ROOT / "src/pulsara_agent/hooks").glob("*.py"))
    source = "\n".join(path.read_text(encoding="utf-8") for path in hook_files)
    assert "pulsara_agent.plugins" not in source
    assert "OperationalHookType" not in source
    assert "KernelExtensionHost" not in source
    assert "ConversationKernelRepository" not in source
    assert "PluginHookDispatcher" not in source
    assert "PLUGIN_HOOK_CONTEXT" not in source
    assert "receipt" not in source.lower()
    assert "checkpoint" not in source.lower()
    assert "retry_queue" not in source.lower()
    assert "digest_to_object" not in source.lower()

    for value in (
        PreparedPermissionRequest,
        AcceptedCanonicalToolResultSettlement,
    ):
        assert all("fingerprint" not in item.name for item in fields(value))
    for path in hook_files:
        module_source = path.read_text(encoding="utf-8")
        assert "class PluginHookDispatcher" not in module_source

    production = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "src/pulsara_agent/conversation_kernel").rglob("*.py")
    )
    assert "def run_subagent_turn(" not in production
    assert "_spawning" not in production
    assert "maximum_hook_handlers" not in source
    assert "maximum_hooks_per_session" not in source
    assert "def restore(" not in source
    assert "_restore_reservation" not in source

    provider_dispatch = (
        ROOT / "src/pulsara_agent/conversation_kernel/provider_dispatch.py"
    ).read_text(encoding="utf-8")
    assert "hook_context_reservation.restore" not in provider_dispatch
    for owner_path in (
        ROOT / "src/pulsara_agent/conversation_kernel/runner.py",
        ROOT / "src/pulsara_agent/conversation_kernel/compaction/coordinator.py",
    ):
        owner_source = owner_path.read_text(encoding="utf-8")
        assert "install_provider_open(" in owner_source
        assert "hook_context_reservation.retire()" in owner_source

    host_source = (ROOT / "src/pulsara_agent/conversation_kernel/host.py").read_text(
        encoding="utf-8"
    )
    close_start = host_source.index(
        "    async def aclose(\n        self,\n        *,\n        close_conversation"
    )
    close_end = host_source.index("    async def _renew_writer", close_start)
    close_source = host_source[close_start:close_end]
    ordered_close_markers = (
        "session_end_cwd = self._tools.snapshot_terminal_cwd()",
        "self._hooks.begin_host_close",
        "self._delivery_task.cancel()",
        "await self._settle_active_root_task(task)",
        "await self._subagents.aclose",
        "await self._hooks.fence_ordinary()",
        "await self._hooks.dispatch_terminal",
        "await self._hooks.aclose",
        "await self._io.aclose",
    )
    assert tuple(close_source.index(item) for item in ordered_close_markers) == tuple(
        sorted(close_source.index(item) for item in ordered_close_markers)
    )
    terminal_lane = close_source[
        close_source.index("await self._hooks.fence_ordinary()") :
    ]
    assert "cwd=str(session_end_cwd)" in terminal_lane
    assert "snapshot_terminal_cwd()" not in terminal_lane

    tool_runtime_source = (
        ROOT / "src/pulsara_agent/conversation_kernel/tool_runtime.py"
    ).read_text(encoding="utf-8")
    permission_start = tool_runtime_source.index("    def prepare_permission_request(")
    permission_end = tool_runtime_source.index(
        "    async def request_confirmation(", permission_start
    )
    permission_source = tool_runtime_source[permission_start:permission_end]
    assert "state_key = (access.surface_generation, tool_call_id)" in permission_source
    assert "_mcp_confirmation_admissions.get(state_key)" in permission_source
    assert "_mcp_dispatch_permits.get(state_key)" in permission_source
    assert ".items()" not in permission_source


def test_round9_2_normalized_digest_has_no_recursive_script_or_description_input(
    tmp_path: Path,
) -> None:
    provenance = _provenance(tmp_path)
    definition = _definition(
        tmp_path, HookEventType.PRE_TOOL_USE_EVENT, "python scripts/check.py"
    )
    first = normalized_definition_digest(provenance, (definition,))
    described = replace(provenance, description="display only")
    second = normalized_definition_digest(
        described, (replace(definition, provenance=described),)
    )
    assert first == second
    assert len(first) == 64


def test_round9_2_api_key_replacement_collision_drops_exact_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = API_KEY_REPLACEMENT.decode()
    monkeypatch.setenv("PULSARA_API_KEY", marker)
    scrub = HookSecretScrubSet.capture()
    assert marker not in scrub.scrub_text("before " + marker + " after")


def test_round9_2_queued_prompt_context_stays_candidate_bound_until_full(
    tmp_path: Path,
) -> None:
    from pulsara_agent.hooks.contracts import HookContextEntry

    owner = HookContextOwner()
    scope = _scope()
    owner.register_scope(scope)
    definition = _definition(
        tmp_path,
        HookEventType.USER_PROMPT_SUBMIT_EVENT,
        "noop",
        context_limit=0,
    )
    scrub = HookSecretScrubSet.capture()
    active = owner.prepare_sync(
        scope=scope,
        causal_ref=DirectPromptRef("command:A", "turn:A", "entry:A", "revision:A"),
        entries=(
            HookContextEntry(definition, 0, 1, "context-for-active-turn-A", scrub),
        ),
    )
    queued = owner.prepare_sync(
        scope=scope,
        causal_ref=QueuedPromptRef(
            "command:B",
            "queue:B",
            "turn:B",
            "entry:B",
            "revision:B",
        ),
        entries=(
            HookContextEntry(definition, 0, 2, "context-for-queued-turn-B", scrub),
        ),
    )
    assert active is not None and queued is not None
    active.commit()
    queued.commit_prompt_bound("queue:B")

    selected_for_a = owner.freeze_for_target(
        scope_kind="ROOT", child_task_id=None, estimator=_Estimator()
    )
    assert selected_for_a is not None
    assert "context-for-active-turn-A" in selected_for_a.full_text
    assert "context-for-queued-turn-B" not in selected_for_a.full_text
    selected_for_a.reservation.retire()

    owner.activate_prompt_candidate(scope=scope, prompt_candidate_id="queue:B")
    selected_for_b = owner.freeze_for_target(
        scope_kind="ROOT", child_task_id=None, estimator=_Estimator()
    )
    assert selected_for_b is not None
    assert "context-for-active-turn-A" not in selected_for_b.full_text
    assert "context-for-queued-turn-B" in selected_for_b.full_text
    selected_for_b.reservation.retire()


def test_round9_2_api_key_rotation_is_rejected_at_exact_spawn_sink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pulsara_agent.hooks.executor as executor_module

    rotated_secret = "rotated-key-at-final-sink"
    marker = tmp_path / "spawned.txt"
    program = (
        f"from pathlib import Path;Path({str(marker)!r}).write_text({rotated_secret!r})"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(program)}"
    definition = _definition(
        tmp_path,
        HookEventType.SESSION_START_EVENT,
        command,
    )
    request = HookExecutionRequest(
        definition,
        {"hook_event_name": "SessionStart", "value": "public"},
        _scope(),
        1,
        0,
        tmp_path,
        None,
        monotonic() + 5,
    )
    original = executor_module._spawn_environment
    monkeypatch.setenv("PULSARA_API_KEY", "initial-key-before-environment")

    def rotate_after_environment(scrub, overlay):
        environment = original(scrub, overlay)
        monkeypatch.setenv("PULSARA_API_KEY", rotated_secret)
        return environment

    monkeypatch.setattr(executor_module, "_spawn_environment", rotate_after_environment)

    async def exercise() -> None:
        executor = HookCommandExecutor(api_key_boundary=ProcessApiKeyBoundary())
        outcome = await executor.execute(request)
        assert outcome.failure_code == "API_KEY_VALUE_PRESENT"
        assert outcome.exit_code is None
        assert not marker.exists()
        await executor.aclose(deadline_monotonic=monotonic() + 2)

    asyncio.run(exercise())


def test_round9_2_source_rejects_same_inode_same_size_in_place_rewrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pulsara_agent.hooks.source as source_module

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    source_path = home / "hooks.json"
    size = 96 * 1024
    first = b'{"hooks":{}}' + b" " * (size - len(b'{"hooks":{}}'))
    second = b'{"hooks":{}}' + b"\n" * (size - len(b'{"hooks":{}}'))
    source_path.write_bytes(first)
    original_read = source_module.os.read
    before_inode = source_path.stat().st_ino
    rewritten = False

    def racing_read(file_descriptor: int, count: int) -> bytes:
        nonlocal rewritten
        chunk = original_read(file_descriptor, count)
        if not rewritten and len(chunk) == 64 * 1024:
            rewritten = True
            prior = source_path.stat()
            source_path.write_bytes(second)
            source_module.os.utime(
                source_path,
                ns=(prior.st_atime_ns, prior.st_mtime_ns + 1_000_000),
            )
        return chunk

    monkeypatch.setattr(source_module.os, "read", racing_read)
    provider = LocalHookSourceProvider(
        workspace_root=workspace,
        workspace_kind="transient",
        workspace_state_key="transient:test",
        pulsara_home=home,
    )
    snapshot = provider.discover().source_snapshots[0]
    assert rewritten
    assert source_path.stat().st_ino == before_inode
    assert snapshot.disposition is HookSourceSnapshotDisposition.UNAVAILABLE
    assert [diagnostic.code for diagnostic in snapshot.diagnostics] == [
        "HOOK_SOURCE_DISCOVERY_RACED"
    ]


def test_round9_2_session_start_boundary_supersedes_resume_and_inherits_deadlines(
    tmp_path: Path,
) -> None:
    from pulsara_agent.conversation_kernel.cancellation import (
        ActiveTurnCancellationIntent,
    )
    from pulsara_agent.conversation_kernel.compaction.contracts import (
        CompactionScope,
        CompactionTrigger,
    )
    from pulsara_agent.conversation_kernel.compaction.coordinator import (
        CompactionAttemptToken,
        PreparedCompactSessionStartFacts,
    )
    from pulsara_agent.conversation_kernel.runner import (
        ConversationKernelRunner,
        _RootSessionStartCompactPort,
        _SessionStartColdBoundaryOwner,
    )
    from pulsara_agent.model_input.contracts import ModelInputScopeKind
    from pulsara_agent.primitives.permission import PermissionMode

    class _Dispatcher:
        def __init__(self) -> None:
            self.envelopes = []

        def capture_view(self):
            return _view(tmp_path, ())

        async def dispatch(self, envelope, *, matcher_subject):
            del matcher_subject
            self.envelopes.append(envelope)
            return GateOutcome()

    async def exercise() -> None:
        dispatcher = _Dispatcher()
        context_owner = HookContextOwner()
        scope = _scope()
        context_owner.register_scope(scope)
        permission = SimpleNamespace(
            snapshot_id="permission:1",
            effective_mode=PermissionMode.ACCEPT_EDITS,
            plan_workflow_id=None,
        )

        initial_boundary = _SessionStartColdBoundaryOwner("resume")
        runner = object.__new__(ConversationKernelRunner)
        runner._session_start_boundary = initial_boundary
        runner._hook_dispatcher = dispatcher
        runner._hook_context_owner = context_owner
        runner._hook_scope = scope
        runner._writer_lease = SimpleNamespace(
            guard=SimpleNamespace(session_id="session:1")
        )
        runner._tools = SimpleNamespace(snapshot_terminal_cwd=lambda: tmp_path)
        initial_deadline = monotonic() + 11
        await runner._dispatch_initial_session_start(
            ActiveTurnCancellationIntent(
                "turn:initial", ModelInputScopeKind.ROOT, None
            ),
            canonical_facts=SimpleNamespace(run_permission_snapshot=permission),
            model_id="model:1",
            deadline_monotonic=initial_deadline,
        )

        compact_boundary = _SessionStartColdBoundaryOwner("resume")
        attempt = CompactionAttemptToken(
            "session:1",
            "turn:compact",
            ModelInputScopeKind.ROOT,
            None,
            CompactionTrigger.MANUAL,
        )
        await compact_boundary.arm_compact_boundary(
            attempt_token=attempt,
            adopted_snapshot_id="snapshot:1",
            adopted_binding_revision_id="revision:1",
        )
        compact_deadline = monotonic() + 7
        facts = PreparedCompactSessionStartFacts(
            attempt_token=attempt,
            adopted_snapshot_id="snapshot:1",
            adopted_binding_revision_id="revision:1",
            scope=CompactionScope(
                "session:1",
                "workspace:1",
                "turn:compact",
                ModelInputScopeKind.ROOT,
                None,
            ),
            turn_id="turn:compact",
            model_id="model:1",
            permission_snapshot=permission,
            deadline_monotonic=compact_deadline,
        )
        port = _RootSessionStartCompactPort(
            dispatcher,
            context_owner,
            scope,
            compact_boundary,
            ActiveTurnCancellationIntent(
                "turn:compact", ModelInputScopeKind.ROOT, None
            ),
            "session:1",
            str(tmp_path),
        )
        assert (await port(facts)).proceed
        assert await compact_boundary.consume_any() is None

        assert [item.public_input.source for item in dispatcher.envelopes] == [
            "resume",
            "compact",
        ]
        assert [item.deadline_monotonic for item in dispatcher.envelopes] == [
            initial_deadline,
            compact_deadline,
        ]

    asyncio.run(exercise())


def test_round9_2_post_adoption_status_revalidation_is_bounded_and_control_linearized() -> (
    None
):
    from pulsara_agent.conversation_kernel.compaction.contracts import (
        CompactionConfirmationKind,
        CompactionDisposition,
        CompactionScope,
        CompactionTargetBranch,
        CompactionTrigger,
    )
    from pulsara_agent.conversation_kernel.compaction.coordinator import (
        CompactionAttemptToken,
        CompactionCoordinator,
        _PostAdoptionCompactionFailure,
    )
    from pulsara_agent.conversation_kernel.contracts import TurnStatus
    from pulsara_agent.conversation_kernel.memory.contracts import MemoryUsePolicy
    from pulsara_agent.model_input.contracts import ModelInputScopeKind

    class _Dry:
        def __init__(self) -> None:
            self.handle = SimpleNamespace(close=self._close)
            self.closed = False

        def _close(self) -> None:
            self.closed = True

        def close_surface_borrow(self) -> None:
            pass

    class _Owner:
        def __init__(self) -> None:
            self.reset = False

        def reset_automatic_failures(self, **kwargs) -> None:
            del kwargs
            self.reset = True

    class _Continuity:
        def __init__(self) -> None:
            self.discarded = False

        def discard_scope(self, scope) -> None:
            del scope
            self.discarded = True

    async def invoke(status_or_error):
        order = []
        coordinator = object.__new__(CompactionCoordinator)
        owner = _Owner()
        continuity = _Continuity()
        coordinator._compaction_owner = owner
        coordinator._continuity = continuity
        coordinator._repository = SimpleNamespace(read_turn_status=object())

        async def settle(**kwargs):
            del kwargs
            order.append("adoption")
            return SimpleNamespace(
                kind=CompactionConfirmationKind.FULL,
                revision_ordinal=3,
            )

        async def post(**kwargs):
            del kwargs
            order.append("post")
            return False, "BLOCK_FROM_POST"

        class _IO:
            async def run(self, operation, *args, **kwargs):
                del operation, args, kwargs
                order.append("status")
                if isinstance(status_or_error, BaseException):
                    raise status_or_error
                return status_or_error

        coordinator._settle_compaction_adoption = settle
        coordinator._dispatch_post_compact = post
        coordinator._io = _IO()
        dry = _Dry()
        scope = CompactionScope(
            "session:1",
            "workspace:1",
            "turn:1",
            ModelInputScopeKind.ROOT,
            None,
        )
        attempt = CompactionAttemptToken(
            "session:1",
            "turn:1",
            ModelInputScopeKind.ROOT,
            None,
            CompactionTrigger.MANUAL,
        )
        candidate = SimpleNamespace(
            snapshot=SimpleNamespace(snapshot_id="snapshot:1"),
            binding=SimpleNamespace(binding_revision_id="revision:1"),
        )
        result = await coordinator._complete_compaction_settlement(
            turn_id="turn:1",
            model_call_index=1,
            inherited_memory_use_policy=None,
            force=True,
            expected_scope=scope,
            target_branch=CompactionTargetBranch.ACTIVE_INSTALLATION,
            successor_deadline=monotonic() + 3,
            candidate=candidate,
            preconditions=None,
            dry_dispatch=dry,
            source_tokens=1,
            protected_tail_selection_fingerprint="tail:1",
            compaction_read=None,
            trigger=CompactionTrigger.MANUAL,
            attempt_token=attempt,
            hook_scope=None,
            hook_model_id="model:1",
            hook_cwd=str(ROOT),
            session_start_compact_port=None,
            session_start_boundary_port=None,
        )
        return result, order, dry, owner, continuity

    async def exercise() -> None:
        historical, order, dry, owner, continuity = await invoke(TurnStatus.COMPLETED)
        assert order == ["adoption", "post", "status"]
        assert historical.outcome.public_code == "HISTORICAL_COMPACTION_WINNER"
        assert historical.active_continuation_blocked_reason is None
        assert dry.closed and owner.reset and continuity.discarded

        for failure in (RuntimeError("database unavailable"), asyncio.CancelledError()):
            with pytest.raises(_PostAdoptionCompactionFailure) as raised:
                await invoke(failure)
            carrier = raised.value
            assert carrier.error is failure
            assert carrier.outcome.disposition is CompactionDisposition.COMPACTED
            assert carrier.outcome.snapshot_id == "snapshot:1"
            assert carrier.outcome.revision_ordinal == 3
            assert carrier.outcome.public_code == "COMPACTED_CONTINUATION_UNAVAILABLE"

            class _OuterOwner:
                def __init__(self) -> None:
                    self.settled = None

                async def run_fenced(self, *, scope, trigger, operation):
                    del scope, trigger
                    return await operation()

                async def settle_manual(self, request, outcome) -> None:
                    self.settled = (request, outcome)

                def record_automatic_failure(self, **kwargs) -> None:
                    del kwargs

            outer = object.__new__(CompactionCoordinator)
            outer_owner = _OuterOwner()
            outer._compaction_owner = outer_owner
            outer._writer_lease = SimpleNamespace(
                guard=SimpleNamespace(session_id="session:1")
            )
            outer._hook_root_scope = None

            async def workspace_id():
                return "workspace:1"

            async def fail_after_adoption(**kwargs):
                del kwargs
                raise carrier

            outer._resolved_workspace_id = workspace_id
            outer._execute_compaction_fenced = fail_after_adoption
            manual = SimpleNamespace(
                command_id="command:1",
                scope_kind=ModelInputScopeKind.ROOT,
                scope_subagent_task_id=None,
            )
            with pytest.raises(type(failure)) as propagated:
                await outer._execute_active(
                    turn_id="turn:1",
                    model_call_index=1,
                    inherited_memory_use_policy=MemoryUsePolicy.ENABLED,
                    trigger=CompactionTrigger.MANUAL,
                    force=False,
                    manual_request=manual,
                    scope_kind=ModelInputScopeKind.ROOT,
                    scope_subagent_task_id=None,
                    prepared_source=None,
                    hook_scope=None,
                    session_start_compact_port=None,
                    session_start_boundary_port=None,
                )
            assert propagated.value is failure
            assert outer_owner.settled == (manual, carrier.outcome)

    asyncio.run(exercise())
