"""Delegated Work final attempts share ordinary Hook continuation settlement."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from time import monotonic

import pytest

from pulsara_agent.conversation_kernel.cancellation import ActiveTurnCancellationIntent
from pulsara_agent.conversation_kernel.runner import _dispatch_session_stop_hooks
from pulsara_agent.hooks.config_parser import parse_hook_config
from pulsara_agent.hooks.contracts import (
    ContinuationDecision,
    HookDispatchEnvelope,
    HookDispatchScopeRef,
    HookEventType,
    HookScopeKind,
    HookScopePolicy,
    StopInput,
    StopRef,
    SubagentStopInput,
)
from pulsara_agent.hooks.dispatcher import KernelHookDispatcher
from pulsara_agent.hooks.output_parser import (
    InvalidHandlerOutput,
    ValidHandlerContribution,
    parse_handler_output,
)
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary
from tests.test_round9_2_hook_subsystem import (
    _definition,
    _execution,
    _provenance,
    _ScriptedExecutor,
    _view,
)


@pytest.mark.parametrize("kind", ["ROOT", "SUBAGENT"])
@pytest.mark.parametrize("already_used", [False, True])
@pytest.mark.parametrize(
    "common,child,expected",
    [
        (b"", b'{"decision":"block","reason":"child pass"}', "CONTINUE_ONCE"),
        (
            b'{"decision":"block","reason":"common pass"}',
            b'{"continue":false}',
            "TERMINALIZE",
        ),
        (
            b'{"continue":false}',
            b'{"decision":"block","reason":"child pass"}',
            "TERMINALIZE",
        ),
        (
            b'{"decision":"block","reason":"common pass"}',
            b'{"decision":"block","reason":"child pass"}',
            "CONTINUE_ONCE",
        ),
    ],
)
def test_work_final_composes_controls_and_preserves_veto(
    tmp_path, kind, already_used, common, child, expected
):
    async def scenario():
        parsed = parse_hook_config(
            json.dumps(
                {
                    "hooks": {
                        "Stop": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": "common",
                                        "scope_policy": "BOTH",
                                    }
                                ]
                            }
                        ],
                        "SubagentStop": [
                            {"hooks": [{"type": "command", "command": "child"}]}
                        ],
                    }
                }
            ).encode(),
            provenance=_provenance(tmp_path),
        )
        assert not parsed.diagnostics
        assert parsed.definitions[1].scope_policy is HookScopePolicy.SUBAGENT
        executor = _ScriptedExecutor(
            {"common": (0, common, b""), "child": (0, child, b"")}
        )
        dispatcher = KernelHookDispatcher(
            initial_view=_view(tmp_path, parsed.definitions),
            workspace_root=tmp_path,
            credential_boundary=ProcessCredentialBoundary(),
            executor=executor,
        )
        scope = HookDispatchScopeRef(
            object(), object(), HookScopeKind(kind), "session:test"
        )
        envelope = HookDispatchEnvelope(
            dispatcher.capture_view(),
            scope,
            StopInput(
                session_id=scope.session_id,
                session_kind=kind,
                input_origin=None,
                cwd=str(tmp_path),
                model="test",
                turn_id="turn:test",
                stop_hook_active=already_used,
                last_assistant_message="final candidate",
                permission_mode="default",
            ),
            StopRef("turn:test", "entry:test", object(), "revision:test"),
            monotonic() + 5,
        )
        try:
            result = await _dispatch_session_stop_hooks(
                dispatcher,
                envelope,
                ActiveTurnCancellationIntent("turn:test", ModelInputScopeKind(kind)),
            )
            assert [
                call.public_stdin["hook_event_name"] for call in executor.calls
            ] == (["Stop", "SubagentStop"] if kind == "SUBAGENT" else ["Stop"])
            if already_used:
                expected_decision = "TERMINALIZE"
            elif kind == "ROOT":
                expected_decision = (
                    "CONTINUE_ONCE" if b'"block"' in common else "TERMINALIZE"
                )
            else:
                expected_decision = expected
            assert result.decision.value == expected_decision
            assert result.explicit_terminalize is (
                b"false" in common or (kind == "SUBAGENT" and b"false" in child)
            )
            if result.decision is ContinuationDecision.CONTINUE_ONCE:
                assert result.continuation_source is not None
                assert result.reason == (
                    "common pass" if b'"block"' in common else "child pass"
                )
            for call in executor.calls:
                assert call.public_stdin["turn_id"] == "turn:test"
                assert call.public_stdin["session_id"] == scope.session_id
                assert call.public_stdin["last_assistant_message"] == "final candidate"
                assert call.public_stdin["stop_hook_active"] is already_used
        finally:
            await dispatcher.aclose(deadline_monotonic=monotonic() + 5)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "output,exit_code,stderr",
    [
        (b'{"decision":"block","reason":"extra pass"}', 0, b""),
        (b"", 2, b"extra pass"),
    ],
)
def test_subagent_stop_accepts_continuation_only_synchronously(
    tmp_path, output, exit_code, stderr
):
    definition = _definition(tmp_path, HookEventType.SUBAGENT_STOP_EVENT, "child")
    result = parse_handler_output(
        _execution(definition, stdout=output, exit_code=exit_code, stderr=stderr)
    )
    assert isinstance(result, ValidHandlerContribution)
    assert result.continuation_request and result.reason == "extra pass"
    result = parse_handler_output(
        _execution(
            replace(definition, asynchronous=True),
            stdout=output,
            exit_code=exit_code,
            stderr=stderr,
        )
    )
    assert isinstance(result, InvalidHandlerOutput)


def test_subagent_stop_rejects_plain_output_and_root_identity(tmp_path):
    definition = _definition(tmp_path, HookEventType.SUBAGENT_STOP_EVENT, "child")
    assert isinstance(
        parse_handler_output(_execution(definition, stdout=b"continue please")),
        InvalidHandlerOutput,
    )
    with pytest.raises(ValueError, match="delegated session"):
        SubagentStopInput(
            session_id="root",
            session_kind="ROOT",
            input_origin=None,
            cwd=str(tmp_path),
            model="test",
            turn_id="turn:test",
            stop_hook_active=False,
            last_assistant_message="done",
            permission_mode="default",
        )
