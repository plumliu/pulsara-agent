"""Only ordinary ROOT business preparation may take pending actions."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pulsara_agent.conversation_kernel.provider_dispatch import ProviderDispatchCoordinator
from pulsara_agent.model_input.contracts import ModelInputScopeKind


@pytest.mark.parametrize(
    ("scope", "semantic", "compaction", "prospective", "should_take"),
    [
        (ModelInputScopeKind.ROOT, False, False, False, True),
        (ModelInputScopeKind.SUBAGENT, False, False, False, False),
        (ModelInputScopeKind.ROOT, True, False, False, False),
        (ModelInputScopeKind.ROOT, False, True, False, False),
        (ModelInputScopeKind.ROOT, False, False, True, False),
    ],
)
def test_preparation_scope_controls_user_action_take(
    scope, semantic, compaction, prospective, should_take
):
    take = AsyncMock(return_value='{"actions":[]}')
    owner = SimpleNamespace(_take_user_actions=take)
    result = asyncio.run(ProviderDispatchCoordinator._take_user_actions_for_preparation(
        owner, scope=scope, semantic_only=semantic,
        compaction_candidate_family=compaction, noninstallable_preparation=prospective,
    ))
    assert result == ('{"actions":[]}' if should_take else None)
    assert take.await_count == int(should_take)


def test_reduced_wire_candidate_preserves_installed_actions_prefix():
    from dataclasses import dataclass, replace
    from pulsara_agent.llm.input import LLMTextPart, join_text_content
    from pulsara_agent.model_input.compiler import StructuredModelInputCompiler
    from pulsara_agent.model_input.contracts import (
        ContextSourceKind, FrozenProviderInputItem, FrozenProviderInputItemKind,
    )
    from tests.support.round3 import new_test_provider_input_continuity_owner
    from tests.test_round3_structured_model_input_compiler import (
        _candidate, _compile_and_install_append, _prepare_append_candidate,
        _prepared_request, _snapshot, _sources, _user,
    )

    compiler = StructuredModelInputCompiler()
    owner = new_test_provider_input_continuity_owner()
    first = _user("first", sequence=1)
    old_body = '{"actions":[{"kind":"old"}]}'
    _, view = _compile_and_install_append(
        compiler=compiler, owner=owner,
        request=_prepared_request(
            _snapshot(first), _sources(_candidate(ContextSourceKind.USER_ACTIONS, (old_body,)))
        ),
    )
    assistant = FrozenProviderInputItem(
        FrozenProviderInputItemKind.ASSISTANT,
        "entry:2xt3tmyw6w7nkoixnaassnppze", 2,
        "turn:klmmazz67vfgsfsvg6ceqgdsfu", (LLMTextPart("done"),),
    )
    second = _user("next", sequence=3)
    new_body = '{"actions":[{"kind":"new"}]}'
    request = replace(
        _prepared_request(
            _snapshot(first, assistant, second),
            _sources(_candidate(ContextSourceKind.USER_ACTIONS, (new_body,))),
        ),
        context_id="context:6igz6lmmsfk2xstdjjz6lfuzfy", model_call_index=2,
    )
    result, input_candidate = _prepare_append_candidate(
        compiler=compiler, owner=owner, request=request,
    )

    @dataclass(frozen=True)
    class Candidate:
        compile_request: object
        planning: object
        append_result: object
        semantic_input: object
        cold_semantic: object = None

    candidate = Candidate(request, input_candidate.planning, result, result.compiled_input)

    async def io_run(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def measure(candidate, *, deadline):
        return candidate

    coordinator = SimpleNamespace(
        wire_candidate_for_dispatch=lambda dispatch: candidate,
        _compiler=compiler,
        _io=SimpleNamespace(run=io_run),
        measure_prepared_wire_candidate=measure,
    )
    reduced = asyncio.run(ProviderDispatchCoordinator.measure_without_user_actions(
        coordinator, object(), deadline=10**12,
    ))
    assert reduced.semantic_input.messages[:len(view.messages)] == view.messages
    text = " ".join(join_text_content(message.content) for message in reduced.semantic_input.messages)
    assert 'old' in text
    assert 'new' not in text
