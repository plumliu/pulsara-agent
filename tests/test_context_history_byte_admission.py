"""History size alone cannot force summaries or replace the provider token budget."""

from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace

from PIL import Image
import pytest

from pulsara_agent.conversation_kernel.compaction.contracts import (
    ResolvedCompactionPolicy,
)
from pulsara_agent.conversation_kernel.compaction.planner import (
    should_trigger_compaction,
)
from pulsara_agent.conversation_kernel.prompt_content import (
    MAXIMUM_PROMPT_MULTIPART_BYTES,
    freeze_canonical_prompt,
)
from pulsara_agent.conversation_kernel.runner import (
    ConversationKernelRunner,
    FrozenPostResponseResourceQuote,
)
from pulsara_agent.identity import derive_id
from pulsara_agent.llm.input import FrozenPromptContent, LLMTextPart
from pulsara_agent.model_input.compiler import StructuredModelInputCompiler
from pulsara_agent.model_input.contracts import (
    CanonicalInputOriginKind,
    FrozenProviderInputItem,
    FrozenProviderInputItemKind,
)
from tests.support.images import bound_test_image
from tests.test_round3_structured_model_input_compiler import (
    _prepared_request,
    _snapshot,
    _sources,
    _compile_and_install_append,
    _append_frontier,
    _append_anchor,
)
from tests.support.round3 import new_test_provider_input_continuity_owner
from pulsara_agent.model_input.continuity import ProviderInputContinuityScope


def test_compiler_keeps_image_history_above_old_canonical_and_working_set_limits():
    output = BytesIO()
    Image.new("RGB", (512, 512), "white").save(output, "PNG", compress_level=0)
    image = bound_test_image("image/png", output.getvalue(), 512, 512)
    # Each original message is small and valid. Repeated occurrences are distinct
    # timeline evidence even though they share immutable image bytes in memory.
    one = FrozenPromptContent((LLMTextPart("Recorded screen evidence."), image))
    assert (
        freeze_canonical_prompt(one).resource_quote.canonical_expanded_bytes
        < MAXIMUM_PROMPT_MULTIPART_BYTES
    )
    items = tuple(
        FrozenProviderInputItem(
            item_kind=FrozenProviderInputItemKind.USER,
            source_entry_id=derive_id("entry", "history-byte-admission", str(i)),
            source_entry_sequence=i + 1,
            source_turn_id="turn:klmmazz67vfgsfsvg6ceqgdsfu",
            content=one.parts,
            input_origin=CanonicalInputOriginKind.HUMAN_MESSAGE,
        )
        for i in range(90)
    )
    expanded = 90 * freeze_canonical_prompt(one).resource_quote.canonical_expanded_bytes
    assert expanded > 64 << 20
    request = _prepared_request(
        _snapshot(*items, canonical_expanded_bytes=expanded), _sources(), budget=100_000
    )
    compiled = StructuredModelInputCompiler().compile(request)
    parts = [part for message in compiled.messages for part in message.content]
    assert sum(part is image for part in parts) == 90
    from pulsara_agent.model_input._stable_input_identity import _input_code

    estimated = _input_code(compiled.system_prompt, compiled.messages, compiled.tools)
    assert (
        estimated["total_input_tokens"]
        < request.compile_binding.effective_input_budget_tokens
    )

    # The actual cold-epoch and append compiler paths must also accept this
    # history while preserving the previously installed input byte-for-byte.
    compiler = StructuredModelInputCompiler()
    owner = new_test_provider_input_continuity_owner()
    try:
        initial = _prepared_request(
            _snapshot(*items[:20], canonical_expanded_bytes=expanded // 90 * 20),
            _sources(),
            budget=100_000,
        )
        _, installed = _compile_and_install_append(
            compiler=compiler, owner=owner, request=initial
        )
        successor = replace(
            request,
            context_id=derive_id("context", "large-history-append"),
            model_call_index=2,
        )
        scope = ProviderInputContinuityScope(
            session_id=request.canonical_input.identity.session_id,
            scope_kind=request.canonical_input.identity.conversation_scope_kind,
        )
        planning = owner.freeze_planning_input(
            scope=scope,
            canonical_frontier=_append_frontier(successor),
            dispatch_anchor=_append_anchor(successor),
        )
        appended = compiler.compile_installed_append(
            successor, planning=planning
        ).compiled_input
        assert appended.system_prompt == installed.system_prompt
        assert appended.tools == installed.tools
        assert appended.messages[: len(installed.messages)] == installed.messages
        assert (
            sum(
                isinstance(part, type(image))
                for message in appended.messages
                for part in message.content
            )
            == 90
        )
        assert owner.current_view(scope) is installed
    finally:
        owner.close()

    cold_owner = new_test_provider_input_continuity_owner()
    try:
        planning = cold_owner.freeze_planning_input(
            scope=scope,
            canonical_frontier=_append_frontier(request),
            dispatch_anchor=_append_anchor(request),
        )
        cold = compiler.compile_new_epoch(request, planning=planning).compiled_input
        assert (
            sum(
                isinstance(part, type(image))
                for message in cold.messages
                for part in message.content
            )
            == 90
        )
    finally:
        cold_owner.close()


def test_history_bytes_do_not_trigger_compaction_but_token_threshold_does():
    source = SimpleNamespace(
        normal_compile_binding=SimpleNamespace(effective_input_budget_tokens=1_000_000),
        provider_wire_quote=SimpleNamespace(budget_input_tokens=86_762),
        physical_working_set=SimpleNamespace(
            selected_item_count=10_000,
            selected_canonical_expanded_bytes=128 << 20,
            continuity_epoch_logical_bytes=256 << 20,
        ),
    )
    policy = ResolvedCompactionPolicy()
    assert not should_trigger_compaction(source_view=source, policy=policy, force=False)
    source.provider_wire_quote.budget_input_tokens = 850_000
    assert should_trigger_compaction(source_view=source, policy=policy, force=False)
    source.provider_wire_quote.budget_input_tokens = 86_762
    assert should_trigger_compaction(source_view=source, policy=policy, force=True)


def test_large_history_does_not_reduce_post_response_admission():
    quote = FrozenPostResponseResourceQuote(
        current_canonical_expanded_bytes=128 << 20,
        actual_assistant_canonical_bytes=1,
        bounded_followup_canonical_bytes=100,
        canonical_upper_after=(128 << 20) + 101,
        current_epoch_logical_bytes=256 << 20,
        actual_assistant_logical_bytes=1,
        bounded_followup_logical_bytes=100,
        logical_upper_after=(256 << 20) + 101,
        current_canonical_items=10_000,
        bounded_followup_items=1,
        item_upper_after=10_002,
    )
    ConversationKernelRunner._require_post_response_resources(
        quote, effective_input_budget_tokens=100_000
    )


def test_individual_prompt_message_allocation_is_unchanged():
    # This is a single-message boundary, not cumulative history admission.
    output = BytesIO()
    Image.new("RGB", (512, 512), "white").save(output, "PNG", compress_level=0)
    image = bound_test_image("image/png", output.getvalue(), 512, 512)
    count = MAXIMUM_PROMPT_MULTIPART_BYTES // len(image.immutable_bytes) + 1
    with pytest.raises(ValueError):
        freeze_canonical_prompt(
            FrozenPromptContent((LLMTextPart("one message"), *((image,) * count)))
        )


def test_steer_into_large_history_keeps_its_new_message_allocation():
    from pulsara_agent.conversation_kernel.steer import SteerSuffixAdmissionQuote

    quote = SteerSuffixAdmissionQuote(
        selected_item_count=1,
        selected_canonical_expanded_bytes=256,
        prospective_snapshot_hydrated_bytes=128 << 20,
        resulting_epoch_logical_bytes=256 << 20,
        effective_target_budget=1_000_000,
        estimator_fingerprint="sha256:" + "a" * 64,
        predecessor_prefix_fingerprint=None,
        memory_recall_reservation=None,
        memory_response_preference_reservation=None,
    )
    assert quote.selected_canonical_expanded_bytes == 256
    assert quote.prospective_snapshot_hydrated_bytes == 128 << 20
    assert quote.resulting_epoch_logical_bytes == 256 << 20


def test_history_items_beyond_old_limit_append_without_rewriting_installed_prefix():
    from tests.test_round3_structured_model_input_compiler import _user

    items = tuple(_user(f"fact-{index}", sequence=index + 1) for index in range(5_000))
    compiler = StructuredModelInputCompiler()
    owner = new_test_provider_input_continuity_owner()
    try:
        initial = _prepared_request(_snapshot(*items[:100]), _sources(), budget=100_000)
        _, installed = _compile_and_install_append(
            compiler=compiler, owner=owner, request=initial
        )
        successor = replace(
            _prepared_request(_snapshot(*items), _sources(), budget=100_000),
            context_id=derive_id("context", "history-items-append"),
            model_call_index=2,
        )
        scope = ProviderInputContinuityScope(
            session_id=successor.canonical_input.identity.session_id,
            scope_kind=successor.canonical_input.identity.conversation_scope_kind,
        )
        planning = owner.freeze_planning_input(
            scope=scope,
            canonical_frontier=_append_frontier(successor),
            dispatch_anchor=_append_anchor(successor),
        )
        appended = compiler.compile_installed_append(
            successor, planning=planning
        ).compiled_input
        assert appended.system_prompt == installed.system_prompt
        assert appended.tools == installed.tools
        assert appended.messages[: len(installed.messages)] == installed.messages
        facts = [part.text for message in appended.messages for part in message.content
                 if isinstance(part, LLMTextPart) and part.text.startswith("fact-")]
        assert facts == [f"fact-{index}" for index in range(5_000)]
        assert owner.current_view(scope) is installed
    finally:
        owner.close()
