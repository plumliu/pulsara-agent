"""Factories for sparse-retrieval tokenizers."""

from __future__ import annotations

from pulsara_agent.retrieval.config import TokenizerBackendConfig

from .jieba_search import JiebaSearchTokenizer
from .protocol import Tokenizer
from .regex_word_split import RegexWordSplitTokenizer


def build_tokenizer(config: TokenizerBackendConfig) -> Tokenizer:
    if config.min_token_length < 1:
        raise ValueError("Tokenizer min_token_length must be >= 1.")
    if config.provider == "jieba_search":
        return JiebaSearchTokenizer(
            min_token_length=config.min_token_length,
            lowercase=config.lowercase,
        )
    if config.provider == "regex_word_split":
        return RegexWordSplitTokenizer(
            min_token_length=config.min_token_length,
            lowercase=config.lowercase,
        )
    raise ValueError(f"Unknown tokenizer provider: {config.provider!r}")
