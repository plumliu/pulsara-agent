"""The production text rule covers JSON, Unicode and additive wire suffixes."""

import pytest

from pulsara_agent.llm.estimator import PulsaraHeuristicTokenEstimatorV3
from pulsara_agent.llm.input import LLMMessage, LLMTextPart, MessageRole
from pulsara_agent.primitives.context import canonical_json_bytes


@pytest.mark.parametrize(
    ("text", "tokens"),
    [
        ("", 0),
        ("abcd", 1),
        ("abcde", 2),
        ("Hello, how are you?", 5),
        ("你好，请帮我检查代码。", 11),
        ('def greet(name):\n    return "你好, " + name\n', 12),
        ("あいうえ", 4),
        ("アイウエ", 4),
        ("가나다라", 4),
        ("ᄀᄂᄃᄅ", 4),
        ("ㄅㄆㄇㄈ", 4),
        ("ﾊﾝｶｸ", 4),
        ("𠀀𠀁𠀂𠀃", 4),
        ("，。！？", 4),
        ("ａｂｃｄ", 3),
        ("éééé", 2),
        ("αβγδ", 2),
        ("अअअअ", 3),
        ("😀😀😀😀", 4),
    ],
)
def test_text_estimates_cjk_code_points_and_other_utf8_bytes(text, tokens):
    assert PulsaraHeuristicTokenEstimatorV3().estimate_text(text) == tokens


def test_json_uses_the_text_rule_and_keeps_serialized_syntax_and_escaping():
    estimator = PulsaraHeuristicTokenEstimatorV3()
    assert estimator.estimate_json({"query": "你好"}) == 5
    value = {"description": 'Read "quoted" text\n你好', "query": "é😀"}
    rendered = canonical_json_bytes(value).decode("utf-8")
    assert '\\n' in rendered and '\\"' in rendered
    assert "你好" in rendered
    assert estimator.estimate_json(value) == estimator.estimate_text(rendered)


def test_wire_suffix_additivity_keeps_fixed_tools_and_item_framing():
    estimator = PulsaraHeuristicTokenEstimatorV3()
    fixed = {"tools": [{"name": "search", "description": "Search files"}], "messages": []}
    prefix = {"role": "user", "content": "hello"}
    suffix = {"role": "assistant", "content": "你好"}
    first = estimator.estimate_final_wire_json_components(
        fixed_context=fixed, ordered_input_items=(prefix,), ordered_input_sources=(None,),
    )
    second = estimator.estimate_final_wire_json_components(
        fixed_context=fixed, ordered_input_items=(prefix, suffix),
        ordered_input_sources=(None, None),
    )
    assert first.total_input_tokens == 34
    assert second.total_input_tokens - first.total_input_tokens == 15
    assert second.total_input_tokens == 49


def test_semantic_message_framing_is_preserved_with_cjk_text():
    estimator = PulsaraHeuristicTokenEstimatorV3()
    message = LLMMessage(role=MessageRole.USER, content=(LLMTextPart("你好"),))
    assert estimator.estimate_message(message) == 6
    assert estimator.fact.estimator_version == "v3"
