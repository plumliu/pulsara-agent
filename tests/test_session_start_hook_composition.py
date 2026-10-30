"""Session start events compose at one existing session boundary."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from time import monotonic

import pytest

from pulsara_agent.conversation_kernel.cancellation import (
    ActiveTurnCancellationIntent,
    ForegroundCancellationCause,
)
from pulsara_agent.conversation_kernel.runner import _dispatch_session_start_hooks
from pulsara_agent.hooks.config_parser import parse_hook_config
from pulsara_agent.hooks.contracts import (
    GateDecision,
    HookDispatchEnvelope,
    HookDispatchScopeRef,
    HookEventType,
    HookScopeKind,
    HookScopePolicy,
    SessionStartInput,
    SessionStartRef,
    SubagentStartInput,
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


def _parse(tmp_path, events, **handler_options):
    return parse_hook_config(
        json.dumps(
            {
                "hooks": {
                    event: [
                        {
                            "matcher": "^(startup|resume|compact)$",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "same-command",
                                    **handler_options,
                                }
                            ],
                        }
                    ]
                    for event in events
                }
            }
        ).encode(),
        provenance=_provenance(tmp_path),
    )


@pytest.mark.parametrize("kind", ["ROOT", "SUBAGENT"])
@pytest.mark.parametrize("source", ["startup", "resume", "compact"])
@pytest.mark.parametrize(
    "events",
    [
        ("SessionStart",),
        ("SubagentStart",),
        ("SessionStart", "SubagentStart"),
    ],
)
def test_start_handlers_compose_without_deduplicating_commands(
    tmp_path, kind, source, events
):
    async def scenario():
        parsed = _parse(tmp_path, events)
        assert not parsed.diagnostics
        assert [d.scope_policy for d in parsed.definitions] == [
            HookScopePolicy.BOTH
            if event == "SessionStart"
            else HookScopePolicy.SUBAGENT
            for event in events
        ]
        executor = _ScriptedExecutor({"same-command": (0, b"same context", b"")})
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
            SessionStartInput(
                session_id=scope.session_id,
                session_kind=kind,
                input_origin=None,
                cwd=str(tmp_path),
                model="test",
                source=source,
                permission_mode="default",
            ),
            SessionStartRef(object(), source),
            monotonic() + 5,
        )
        try:
            result = await _dispatch_session_start_hooks(
                dispatcher,
                envelope,
                ActiveTurnCancellationIntent("turn:test", ModelInputScopeKind(kind)),
            )
            expected = [e for e in events if kind == "SUBAGENT" or e == "SessionStart"]
            assert [
                call.public_stdin["hook_event_name"] for call in executor.calls
            ] == expected
            assert [
                entry.definition.event_type.external_name
                for entry in result.context_entries
            ] == expected
            assert [entry.text for entry in result.context_entries] == [
                "same context"
            ] * len(expected)
            assert all(
                call.public_stdin["source"] == source
                and call.public_stdin["session_kind"] == kind
                and call.public_stdin["session_id"] == scope.session_id
                for call in executor.calls
            )
        finally:
            await dispatcher.aclose(deadline_monotonic=monotonic() + 5)

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["block", "cancel", "scope", "matcher"])
def test_start_composition_respects_gate_cancellation_and_selection(tmp_path, mode):
    async def scenario():
        parsed = _parse(tmp_path, ("SessionStart", "SubagentStart"))
        definitions = parsed.definitions
        intent = ActiveTurnCancellationIntent("turn:test", ModelInputScopeKind.SUBAGENT)
        if mode == "scope":
            definitions = tuple(
                replace(d, scope_policy=HookScopePolicy.ROOT) for d in definitions
            )
        if mode == "matcher":
            parsed = parse_hook_config(
                json.dumps(
                    {
                        "hooks": {
                            "SubagentStart": [
                                {
                                    "matcher": "^resume$",
                                    "hooks": [
                                        {"type": "command", "command": "same-command"}
                                    ],
                                }
                            ]
                        }
                    }
                ).encode(),
                provenance=_provenance(tmp_path),
            )
            definitions = parsed.definitions

        class Executor(_ScriptedExecutor):
            async def execute(self, request):
                result = await super().execute(request)
                if mode == "cancel":
                    intent.install_cause(ForegroundCancellationCause.USER_REQUEST)
                return result

        executor = Executor(
            {
                "same-command": (
                    0,
                    b'{"continue":false}' if mode == "block" else b"",
                    b"",
                )
            }
        )
        dispatcher = KernelHookDispatcher(
            initial_view=_view(tmp_path, definitions),
            workspace_root=tmp_path,
            credential_boundary=ProcessCredentialBoundary(),
            executor=executor,
        )
        scope = HookDispatchScopeRef(
            object(), object(), HookScopeKind.SUBAGENT, "session:test"
        )
        envelope = HookDispatchEnvelope(
            dispatcher.capture_view(),
            scope,
            SessionStartInput(
                session_id=scope.session_id,
                session_kind="SUBAGENT",
                input_origin=None,
                cwd=str(tmp_path),
                model="test",
                source="startup",
                permission_mode="default",
            ),
            SessionStartRef(object(), "startup"),
            monotonic() + 5,
        )
        try:
            if mode == "cancel":
                with pytest.raises(asyncio.CancelledError):
                    await _dispatch_session_start_hooks(dispatcher, envelope, intent)
            else:
                outcome = await _dispatch_session_start_hooks(
                    dispatcher, envelope, intent
                )
                assert outcome.decision is (
                    GateDecision.BLOCK if mode == "block" else GateDecision.PROCEED
                )
            assert [
                call.public_stdin["hook_event_name"] for call in executor.calls
            ] == (["SessionStart"] if mode in {"block", "cancel"} else [])
        finally:
            await dispatcher.aclose(deadline_monotonic=monotonic() + 5)

    asyncio.run(scenario())


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("structured", [False, True])
def test_subagent_start_accepts_context_output(tmp_path, asynchronous, structured):
    definition = replace(
        _definition(tmp_path, HookEventType.SUBAGENT_START_EVENT, "test"),
        asynchronous=asynchronous,
    )
    stdout = (
        b'{"hookSpecificOutput":{"hookEventName":"SubagentStart","additionalContext":"child context"}}'
        if structured
        else b"child context"
    )
    result = parse_handler_output(_execution(definition, stdout=stdout))
    assert isinstance(result, ValidHandlerContribution)
    assert result.context_text == "child context"
    assert not result.gate_block


def test_subagent_start_is_context_only_and_requires_child_identity(tmp_path):
    definition = _definition(tmp_path, HookEventType.SUBAGENT_START_EVENT, "test")
    output = b'{"continue":false,"hookSpecificOutput":{"hookEventName":"SubagentStart","additionalContext":"child context"}}'
    result = parse_handler_output(_execution(definition, stdout=output))
    assert isinstance(result, ValidHandlerContribution)
    assert result.context_text == "child context" and not result.gate_block
    assert "HOOK_CONTROL_IGNORED" in {d.code for d in result.diagnostics}
    assert isinstance(
        parse_handler_output(
            _execution(replace(definition, asynchronous=True), stdout=output)
        ),
        InvalidHandlerOutput,
    )
    with pytest.raises(ValueError, match="delegated session"):
        SubagentStartInput(
            session_id="root",
            session_kind="ROOT",
            input_origin=None,
            cwd=str(tmp_path),
            model="test",
            source="startup",
            permission_mode="default",
        )


def test_async_start_contexts_use_existing_child_scope_owner(tmp_path):
    from pulsara_agent.hooks.context import HookContextOwner
    from tests.test_round9_2_hook_subsystem import _Estimator

    async def scenario():
        parsed = _parse(tmp_path, ("SessionStart", "SubagentStart"), **{"async": True})
        scope = HookDispatchScopeRef(
            object(), object(), HookScopeKind.SUBAGENT, "session:child"
        )
        owner = HookContextOwner()
        owner.register_scope(scope)
        settled = asyncio.Event()

        class Background:
            count = 0

            async def accept_background(self, **kwargs):
                await owner.accept_background(**kwargs)
                self.count += 1
                if self.count == 2:
                    settled.set()

        executor = _ScriptedExecutor({"same-command": (0, b"async child context", b"")})
        executor.release = asyncio.Event()
        dispatcher = KernelHookDispatcher(
            initial_view=_view(tmp_path, parsed.definitions),
            workspace_root=tmp_path,
            credential_boundary=ProcessCredentialBoundary(),
            executor=executor,
            background_context=Background(),
        )
        envelope = HookDispatchEnvelope(
            dispatcher.capture_view(),
            scope,
            SessionStartInput(
                session_id=scope.session_id,
                session_kind="SUBAGENT",
                input_origin=None,
                cwd=str(tmp_path),
                model="test",
                source="startup",
                permission_mode="default",
            ),
            SessionStartRef(object(), "startup"),
            monotonic() + 5,
        )
        try:
            outcome = await _dispatch_session_start_hooks(
                dispatcher,
                envelope,
                ActiveTurnCancellationIntent("turn:test", ModelInputScopeKind.SUBAGENT),
            )
            assert not outcome.context_entries
            executor.release.set()
            await asyncio.wait_for(settled.wait(), 5)
            assert (
                owner.freeze_for_target(
                    scope_kind="ROOT", session_id="session:root", estimator=_Estimator()
                )
                is None
            )
            prepared = owner.freeze_for_target(
                scope_kind="SUBAGENT",
                session_id=scope.session_id,
                estimator=_Estimator(),
            )
            assert prepared is not None
            assert [
                item.definition.event_type.external_name
                for item in prepared.reservation.batch.contributions
            ] == ["SessionStart", "SubagentStart"]
            assert prepared.full_text.count("async child context") == 2
            prepared.reservation.retire()
            assert (
                owner.freeze_for_target(
                    scope_kind="SUBAGENT",
                    session_id=scope.session_id,
                    estimator=_Estimator(),
                )
                is None
            )
        finally:
            executor.release.set()
            await dispatcher.aclose(deadline_monotonic=monotonic() + 5)

    asyncio.run(scenario())
