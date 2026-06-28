"""Tokenizer protocol for sparse retrieval.

Dense retrieval and rerank consume raw text directly. Sparse retrieval needs a
separate tokenization layer so we can own Chinese segmentation and token
normalization at the application layer rather than inheriting whatever the
storage engine happens to do.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable


@runtime_checkable
class Tokenizer(Protocol):
    """Sync tokenizer contract for sparse retrieval fields and queries."""

    def tokenize(self, text: str) -> list[str]:
        """Return ordered non-empty tokens for ``text``."""

    def tokenize_batch(self, texts: Sequence[str]) -> list[list[str]]:
        """Tokenize many strings preserving input order."""
