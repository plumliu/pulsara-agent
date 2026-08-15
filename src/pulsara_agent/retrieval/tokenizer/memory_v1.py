"""Deterministic bilingual tokenizer for canonical memory search terms.

The Jieba instance is private to this object.  No caller can mutate the global
dictionary and index/query paths consume this exact facade.
"""

from __future__ import annotations

import re
import unicodedata

import jieba


MEMORY_RETRIEVAL_TOKENIZER_CONTRACT_ID = "pulsara.memory-retrieval-tokenizer"
MEMORY_RETRIEVAL_TOKENIZER_CONTRACT_VERSION = 2
MAXIMUM_MEMORY_SEARCH_TERMS = 256
MAXIMUM_MEMORY_SEARCH_TERM_BYTES = 128
MAXIMUM_MEMORY_SEARCH_TERMS_BYTES = 16 * 1024

_CODE_PATH_RE = re.compile(
    r"(?<![\w])(?:[A-Za-z0-9_.$@~+-]+(?:[/\\:#.-][A-Za-z0-9_.$@~+-]+)+|"
    r"[A-Za-z_][A-Za-z0-9_]{1,127})(?![\w])"
)
_ENGLISH_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_+-]{0,127}")
_ENGLISH_CONTRACTION_RULES = (
    (re.compile(r"\bwon['’]t\b", re.I), "will not"),
    (re.compile(r"\bcan['’]t\b", re.I), "can not"),
    (re.compile(r"\bshan['’]t\b", re.I), "shall not"),
    (re.compile(r"\b([A-Za-z]+)n['’]t\b", re.I), r"\1 not"),
    (re.compile(r"\b([A-Za-z]+)['’]re\b", re.I), r"\1 are"),
    (re.compile(r"\b([A-Za-z]+)['’]ve\b", re.I), r"\1 have"),
    (re.compile(r"\b([A-Za-z]+)['’]ll\b", re.I), r"\1 will"),
    (re.compile(r"\b([A-Za-z]+)['’]m\b", re.I), r"\1 am"),
    (re.compile(r"\b([A-Za-z]+)['’]d\b", re.I), r"\1 would"),
    (re.compile(r"\b([A-Za-z]+)['’]s\b", re.I), r"\1"),
)

# Sealed package data expressed as a frozen literal so packaging and hashing do
# not depend on user-home dictionaries or network resources.
_STOPWORDS = frozenset(
    """
    a an and are as at be been being but by did do does doing for from
    had has have having he her here hers herself him himself his how i if in into
    is it its itself me my myself of on or our ours ourselves she so some such than
    that the their theirs them themselves there these they this those to too was we
    were what when where which while who whom why with you your yours yourself
    yourselves
    的 了 和 是 在 我 人 到 说 去 你 着 看 这 那 这些 那些 与 及 而 被 把 对 于 从 为 以 中 里
    """.split()
)


class MemoryRetrievalTokenizerV1:
    """Private, bounded `cut_for_search(..., HMM=False)` tokenizer."""

    def __init__(self) -> None:
        self._jieba = jieba.Tokenizer()

    def tokenize(self, *parts: str | None) -> tuple[str, ...]:
        normalized = "\n".join(
            _normalize(part) for part in parts if part is not None and part.strip()
        )
        lexical = _expand_english_contractions(normalized)
        raw: list[str] = []
        raw.extend(match.group(0) for match in _CODE_PATH_RE.finditer(lexical))
        raw.extend(match.group(0) for match in _ENGLISH_WORD_RE.finditer(lexical))
        raw.extend(self._jieba.cut_for_search(lexical, HMM=False))

        result: list[str] = []
        seen: set[str] = set()
        aggregate = 0
        for item in raw:
            token = _normalize_token(item)
            if (
                not token
                or not any(character.isalnum() or character == "_" for character in token)
                or token in _STOPWORDS
                or token in seen
            ):
                continue
            encoded = token.encode("utf-8")
            if len(encoded) > MAXIMUM_MEMORY_SEARCH_TERM_BYTES:
                raise MemoryRetrievalTokenBoundError(
                    "memory retrieval term exceeds its byte bound"
                )
            if len(result) >= MAXIMUM_MEMORY_SEARCH_TERMS:
                raise MemoryRetrievalTokenBoundError(
                    "memory retrieval terms exceed their item bound"
                )
            if aggregate + len(encoded) > MAXIMUM_MEMORY_SEARCH_TERMS_BYTES:
                raise MemoryRetrievalTokenBoundError(
                    "memory retrieval terms exceed their aggregate byte bound"
                )
            result.append(token)
            seen.add(token)
            aggregate += len(encoded)
        return tuple(result)


class MemoryRetrievalTokenBoundError(ValueError):
    """The exact tokenizer output cannot be represented within closed bounds."""


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))


def _expand_english_contractions(value: str) -> str:
    expanded = value
    for pattern, replacement in _ENGLISH_CONTRACTION_RULES:
        expanded = pattern.sub(replacement, expanded)
    return expanded


def _normalize_token(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).casefold().split()).strip()


__all__ = [
    "MAXIMUM_MEMORY_SEARCH_TERMS",
    "MAXIMUM_MEMORY_SEARCH_TERM_BYTES",
    "MAXIMUM_MEMORY_SEARCH_TERMS_BYTES",
    "MEMORY_RETRIEVAL_TOKENIZER_CONTRACT_ID",
    "MEMORY_RETRIEVAL_TOKENIZER_CONTRACT_VERSION",
    "MemoryRetrievalTokenBoundError",
    "MemoryRetrievalTokenizerV1",
]
