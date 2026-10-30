"""Dispatch preserves independent Markdown surfaces for explicit skill intent."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from pulsara_agent.conversation_kernel.provider_dispatch import (
    _activation_subject_for_anchor,
    _skill_reference_surfaces_for_anchor,
)
from pulsara_agent.llm.input import (
    LLMTextPart,
    PromptAnnotationPart,
    join_text_content,
    prompt_provider_parts,
)
from pulsara_agent.model_input.contracts import (
    CanonicalInputOriginKind,
    CapabilityActivationSubjectKind,
    StructuredModelInputCompileError,
)
from pulsara_agent.model_input.continuity import NewTriggerAnchor, NoNewTriggerAnchor
from tests.test_round3_structured_model_input_compiler import (
    _append_anchor,
    _snapshot,
    _user,
)


@pytest.mark.parametrize(
    "origin",
    [
        CanonicalInputOriginKind.HUMAN_MESSAGE,
        CanonicalInputOriginKind.HUMAN_STEER,
    ],
)
def test_human_anchor_retains_markdown_boundaries_and_memory_projection(origin):
    item = replace(
        _user("body", origin=origin),
        content=(
            LLMTextPart("`body"),
            PromptAnnotationPart("/inert_quote", None, "comment` /comment_skill"),
            PromptAnnotationPart("/also_inert", None, "`/inert_code"),
        ),
    )
    history = _user("/unselected_history", sequence=0)
    canonical = _snapshot(
        history,
        item,
        canonical_expanded_bytes=sum(
            len(join_text_content(prompt_provider_parts(value.content)).encode("utf-8"))
            for value in (history, item)
        ),
    )
    anchor = _append_anchor(SimpleNamespace(canonical_input=canonical))

    assert _skill_reference_surfaces_for_anchor(canonical, anchor) == (
        "`body",
        "comment` /comment_skill",
        "`/inert_code",
    )
    # Memory and Hook intent remains the original joined projection.
    assert _activation_subject_for_anchor(canonical, anchor) == (
        CapabilityActivationSubjectKind.ROOT_HUMAN_PROMPT,
        "`body\ncomment` /comment_skill\n`/inert_code",
    )


def test_nonhuman_anchor_and_continuation_cannot_reuse_prior_human_surfaces():
    human = _user("/human_skill")
    automatic = _user(
        "/automatic_text", sequence=2, origin=CanonicalInputOriginKind.AGENT_MESSAGE
    )
    canonical = _snapshot(human, automatic)
    anchor = _append_anchor(SimpleNamespace(canonical_input=canonical))
    assert _skill_reference_surfaces_for_anchor(canonical, anchor) == ()
    assert (
        _skill_reference_surfaces_for_anchor(
            canonical, NoNewTriggerAnchor(predecessor_frontier_fingerprint=None)
        )
        == ()
    )


def test_skill_surfaces_require_the_exact_canonical_anchor():
    canonical = _snapshot(_user("/human_skill"))
    selected = _append_anchor(SimpleNamespace(canonical_input=canonical))
    changed = NewTriggerAnchor(
        source_entry_id=selected.source_entry_id,
        provider_input_item_fingerprint="sha256:" + "0" * 64,
        provider_group_boundary_fingerprint=selected.provider_group_boundary_fingerprint,
    )
    with pytest.raises(StructuredModelInputCompileError):
        _skill_reference_surfaces_for_anchor(canonical, changed)
