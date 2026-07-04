"""Regex word-split tokenizer for Latin/code-heavy sparse retrieval."""

from __future__ import annotations

import re
from collections.abc import Sequence

_WORD_RE = re.compile(r"[A-Za-z0-9_./:#-]+|[\u4e00-\u9fff]+")


class RegexWordSplitTokenizer:
    """Simple regex tokenizer for non-jieba sparse channels."""

    def __init__(self, *, min_token_length: int = 1, lowercase: bool = True) -> None:
        self._min_token_length = min_token_length
        self._lowercase = lowercase

    def tokenize(self, text: str) -> list[str]:
        if not text:
            return []
        tokens: list[str] = []
        for raw in _WORD_RE.findall(text):
            token = raw.lower() if self._lowercase else raw
            if len(token) < self._min_token_length:
                continue
            tokens.append(token)
        return tokens

    def tokenize_batch(self, texts: Sequence[str]) -> list[list[str]]:
        return [self.tokenize(text) for text in texts]
