"""Embedding providers."""

from .factory import build_embedding_provider
from .openai_compatible import OpenAICompatibleEmbeddingProvider
from .protocol import EmbeddingProvider

__all__ = [
    "EmbeddingProvider",
    "OpenAICompatibleEmbeddingProvider",
    "build_embedding_provider",
]
