from __future__ import annotations

from pulsara_agent.retrieval import JiebaSearchTokenizer, RegexWordSplitTokenizer


def test_jieba_search_tokenizer_handles_mixed_text() -> None:
    tokenizer = JiebaSearchTokenizer(min_token_length=2, lowercase=True)

    tokens = tokenizer.tokenize("Pulsara 做 memory recall with terminal logs")

    assert "pulsara" in tokens
    assert "memory" in tokens
    assert "recall" in tokens
    assert "terminal" in tokens
    assert "logs" in tokens
    assert "做" not in tokens


def test_jieba_search_tokenizer_drops_short_and_stopword_noise() -> None:
    tokenizer = JiebaSearchTokenizer(min_token_length=2, lowercase=True)

    tokens = tokenizer.tokenize("the 的 记忆 系统")

    assert "the" not in tokens
    assert "的" not in tokens
    assert "记忆" in tokens or "系统" in tokens


def test_regex_word_split_tokenizer_preserves_codeish_tokens() -> None:
    tokenizer = RegexWordSplitTokenizer(min_token_length=1, lowercase=True)

    tokens = tokenizer.tokenize("Use memory_search on src/pulsara_agent/host/core.py:42")

    assert "memory_search" in tokens
    assert "src/pulsara_agent/host/core.py:42" in tokens


def test_regex_word_split_tokenizer_tokenize_batch_preserves_order() -> None:
    tokenizer = RegexWordSplitTokenizer(min_token_length=1, lowercase=True)

    rows = tokenizer.tokenize_batch(["Alpha Beta", "Gamma Delta"])

    assert rows == [["alpha", "beta"], ["gamma", "delta"]]
