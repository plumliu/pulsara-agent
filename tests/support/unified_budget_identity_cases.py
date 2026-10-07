"""Deterministic frozen inputs for pre-hard-cut stable identity goldens."""

from dataclasses import replace
from pulsara_agent.model_input.contracts import model_input_compile_binding_fingerprint
from tests.test_round3_structured_model_input_compiler import (
    _prepared_request,
    _snapshot,
    _user,
    _sources,
    _candidate,
    ContextSourceKind,
)


def identity_request():
    request = _prepared_request(
        _snapshot(_user('中文 / "escaped" \\ input')),
        _sources(
            _candidate(ContextSourceKind.MEMORY_RECALL, ("记忆正文", "简版", "ref"))
        ),
        tool_names=("scheduled_tasks", "spawn_agent"),
    )
    binding = request.compile_binding
    call = binding.call_fact.model_copy(
        update={"resolved_model_call_id": "model_call:unified-budget-golden"}
    )
    return replace(
        request,
        compile_binding=replace(
            binding,
            call_fact=call,
            binding_fingerprint=model_input_compile_binding_fingerprint(
                call_fact=call,
                target_fact=binding.target_fact,
                estimator_fingerprint=binding.estimator_fingerprint,
                effective_input_budget_tokens=binding.effective_input_budget_tokens,
                effective_output_tokens=binding.effective_output_tokens,
                tool_surface=binding.tool_surface,
            ),
        ),
    )


def append_identity_digests():
    from uuid import UUID
    from unittest.mock import patch
    from pulsara_agent.model_input.compiler import StructuredModelInputCompiler
    from pulsara_agent.model_input.continuity import ProviderInputContinuityScope
    from tests.test_round3_structured_model_input_compiler import (
        new_test_provider_input_continuity_owner,
        _compile_and_install_append,
        _append_frontier,
        _append_anchor,
        ModelInputScopeKind,
    )

    with patch(
        "pulsara_agent.conversation_kernel.direct_model.uuid4", return_value=UUID(int=1)
    ):
        compiler = StructuredModelInputCompiler()
        owner = new_test_provider_input_continuity_owner()
        first = _user("first")
        request = _prepared_request(
            _snapshot(first),
            _sources(_candidate(ContextSourceKind.RUNTIME_CLOCK, ("clock=A", "A"))),
        )
        initial, _ = _compile_and_install_append(
            compiler=compiler, owner=owner, request=request
        )
        next_request = replace(
            _prepared_request(
                _snapshot(first, _user("next", sequence=2)),
                _sources(_candidate(ContextSourceKind.RUNTIME_CLOCK, ("clock=B", "B"))),
            ),
            context_id="context:next",
            model_call_index=2,
        )
        planning = owner.freeze_planning_input(
            scope=ProviderInputContinuityScope(
                "session:test", ModelInputScopeKind.ROOT, None
            ),
            canonical_frontier=_append_frontier(next_request),
            dispatch_anchor=_append_anchor(next_request),
        )
        appended = compiler.compile_installed_append(next_request, planning=planning)
        return (
            initial.compiled_input.compiled_semantic_fingerprint,
            appended.compiled_input.compiled_semantic_fingerprint,
        )
