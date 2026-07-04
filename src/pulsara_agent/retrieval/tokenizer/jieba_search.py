"""Jieba search-mode tokenizer for CJK-heavy sparse retrieval."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import jieba

_DEFAULT_STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "but",
        "if",
        "of",
        "to",
        "in",
        "on",
        "at",
        "by",
        "for",
        "with",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "do",
        "does",
        "did",
        "has",
        "have",
        "had",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "的",
        "了",
        "和",
        "是",
        "在",
        "我",
        "你",
        "他",
        "她",
        "它",
        "也",
        "都",
        "就",
        "还",
        "或",
        "及",
        "与",
        "对",
        "把",
        "被",
        "有",
        "没",
        "不",
        "啊",
        "吗",
        "呢",
        "吧",
        "哦",
    }
)


class JiebaSearchTokenizer:
    """Tokenizer using ``jieba.cut_for_search`` plus light filtering."""

    def __init__(
        self,
        *,
        min_token_length: int = 2,
        lowercase: bool = True,
        extra_stopwords: frozenset[str] | None = None,
    ) -> None:
        self._min_token_length = min_token_length
        self._lowercase = lowercase
        self._stopwords = (
            _DEFAULT_STOPWORDS | extra_stopwords if extra_stopwords else _DEFAULT_STOPWORDS
        )

    def tokenize(self, text: str) -> list[str]:
        if not text:
            return []
        tokens: list[str] = []
        for raw in jieba.cut_for_search(text):
            token = raw.strip()
            if self._lowercase:
                token = token.lower()
            if not token or token.isspace():
                continue
            if len(token) < self._min_token_length:
                continue
            if token in self._stopwords:
                continue
            tokens.append(token)
        return tokens

    def tokenize_batch(self, texts: Sequence[str]) -> list[list[str]]:
        return [self.tokenize(text) for text in texts]
