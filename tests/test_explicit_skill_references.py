"""User body/comments select known skills; quoted/automatic material stays inert."""
import pytest

from pulsara_agent.capability.resolver import _explicit_skill_names, _active_injections
from pulsara_agent.llm.input import (
    FrozenPromptContent, LLMTextPart, PromptAnnotationPart,
    prompt_text_projection,
)
from pulsara_agent.llm.explicit_references import explicit_skill_reference_surfaces


@pytest.mark.parametrize("text,expected", [
    ("Use /alpha and /beta-skill.", ("alpha", "beta-skill")),
    ("```js\n/ignored\n```\n/alpha", ("alpha",)),
    ("~~~\n/ignored\n~~~~\n/alpha", ("alpha",)),
    ("`/ignored` /alpha", ("alpha",)),
    ("``/ignored ` still code`` /alpha", ("alpha",)),
    ("/alpha ` /ignored", ("alpha",)),
    ("> /ignored\n  > /also-ignored\n/alpha", ("alpha",)),
    ("https://example.com/x /alpha [link](https://x /ignored)", ("alpha",)),
    ("HTTPS://example.com?q=/alpha WWW.example.com?q=/beta", ()),
    ('【本地文件："/alpha"】 /beta', ("beta",)),
    ('【本地文件夹："/alpha"】 【本地文件（只读副本，8 bytes）："/beta"】', ()),
    ("/workspace/file.py ../hidden /alpha @/nope </tag>", ("alpha",)),
    ("```\n/ignored", ()),
    ("> /ignored\r\n/alpha", ("alpha",)),
    ("> /ignored\r/alpha", ("alpha",)),
    ("> /ignored\u2028/alpha", ()),
    ("> /ignored\u0085/alpha", ()),
])
def test_explicit_user_reference_boundaries(text, expected):
    assert _explicit_skill_names(text) == expected


def test_unknown_slash_reference_is_plain_text_not_unavailable_skill_selection():
    injections, diagnostics, unavailable = _active_injections(
        {}, user_input=("Use /unknown-skill",), active_skill_names=frozenset())
    assert injections == () and diagnostics == () and unavailable is None


def test_explicit_host_selection_still_reports_missing_skill():
    injections, diagnostics, unavailable = _active_injections(
        {}, user_input=(), active_skill_names=frozenset({"missing"}))
    assert not injections and diagnostics and unavailable is not None


def test_annotation_comment_is_active_but_quote_is_not_a_new_skill_request():
    prompt = FrozenPromptContent((LLMTextPart("Use /body"),
        PromptAnnotationPart("Quoted /not-active", None, "Explain with /comment and `/code`")))
    projected = prompt_text_projection(prompt)
    assert _explicit_skill_names(projected) == ("body", "comment")
    assert "not-active" not in projected


@pytest.mark.parametrize("parts,expected", [
    ((PromptAnnotationPart("/quote", None, "`"), PromptAnnotationPart("/quote", None, "` /alpha")), ()),
    ((PromptAnnotationPart("/quote", None, "```"), LLMTextPart("/alpha")), ("alpha",)),
    ((LLMTextPart("`"), PromptAnnotationPart("/quote", None, "/alpha")), ("alpha",)),
    ((LLMTextPart("```"), PromptAnnotationPart("/quote", None, "/comment"), LLMTextPart("/alpha")), ("comment",)),
])
def test_editing_surfaces_do_not_pair_markdown_delimiters(parts, expected):
    surfaces = explicit_skill_reference_surfaces(FrozenPromptContent(parts))
    assert _explicit_skill_names(surfaces) == expected
